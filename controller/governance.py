"""Governance client: the only code that changes a workflow's config.

Modules never call the simulator to change anything. They queue change items and watch their status.
This gives one owner of config writes, so proposals cannot conflict, and it makes batching natural: all
items queued for a workflow when a request goes out travel in that one request.

Proposal state machine (statuses are also the audit trail in proposal_transition):

    draft -> pending -> approved -> applying -> applied
       |        |           |           |
       |        |           +-> lapsed  +-> stale_base | lapsed
       |        +-> rejected | expired
       +-> applied (auto path) | refused (auto path) | failed

Every terminal status except 'applied' drops the items with a reason. The owner regenerates its change
from fresh data, so an expired proposal is never resubmitted as it was.

Crash safety: each outside call writes an intent row first and closes it afterwards. reconcile() runs at
startup and asks the simulator what really happened for every intent still open.
"""
from __future__ import annotations

import copy
import json
import logging
import math

from .client import ApiError, SimClient
from .cohorts import DEFAULT, effective_dist
from .faults import crash_point
from .settings import Settings
from .stats import total_variation
from .store import Store, dumps

log = logging.getLogger("controller.governance")

ALLOWED = {
    None: {"draft"},
    "draft": {"pending", "applied", "refused", "failed"},
    "pending": {"approved", "rejected", "expired"},
    "approved": {"applying", "lapsed"},
    "applying": {"applied", "stale_base", "lapsed", "approved"},
}
ACTIVE = ("draft", "pending", "approved", "applying")
ALLOC_DIMS = ("welcome_message", "voice", "caller_id")
OPERATIONAL_DIMS = {"schedule", "caller_id"}  # what a customer does not hear; the only auto-apply candidates


class IllegalTransition(Exception):
    pass


# ---- pure config helpers
def apply_ops(config: dict, ops: list[dict]) -> dict:
    c = copy.deepcopy(config)
    for op in ops:
        kind = op["op"]
        if kind == "alloc":
            c["allocations"].setdefault(op["cohort"], {})[op["dim"]] = op["dist"]
        elif kind == "prompt":
            c["prompt_version"] = op["value"]
        elif kind == "cohorts":
            have = {x["id"] for x in c["cohorts"]}
            c["cohorts"] = c["cohorts"] + [x for x in op["add"] if x["id"] not in have]
        elif kind == "hours":
            c["schedule"]["hour_weights"] = {**c["schedule"].get("hour_weights", {}), **op["tables"]}
        elif kind == "hours_restore":
            c["schedule"]["hour_weights"] = op["tables"]
        else:
            raise ValueError(f"unknown op {kind}")
    return c


def read_paths(config: dict, ops: list[dict]) -> list:
    """Current values at the paths these ops write; used to detect edits made after an item was created."""
    out = []
    for op in ops:
        kind = op["op"]
        if kind == "alloc":
            out.append(config["allocations"].get(op["cohort"], {}).get(op["dim"]))
        elif kind == "prompt":
            out.append(config["prompt_version"])
        elif kind == "cohorts":
            out.append(None)  # additive: adding after a supervisor's cohorts cannot overwrite them
        elif kind in ("hours", "hours_restore"):
            out.append(config["schedule"].get("hour_weights", {}))
    return out


def changed_dims(old: dict, new: dict) -> set[str]:
    dims: set[str] = set()
    if old["cohorts"] != new["cohorts"]:
        dims.add("cohorts")
    if old["prompt_version"] != new["prompt_version"]:
        dims.add("prompt_version")
    if old["schedule"] != new["schedule"]:
        dims.add("schedule")
    ids = {DEFAULT} | set(old["allocations"]) | set(new["allocations"])
    for cid in ids:
        for d in ALLOC_DIMS:
            if old["allocations"].get(cid, {}).get(d) != new["allocations"].get(cid, {}).get(d):
                dims.add(d)
    return dims


def max_shift(old: dict, new: dict) -> float:
    """Largest total-variation move of any cohort's effective distribution, as the policy measures it."""
    worst = 0.0
    ids = {DEFAULT} | {c["id"] for c in old["cohorts"]} | {c["id"] for c in new["cohorts"]}
    for cid in ids:
        for d in ALLOC_DIMS:
            worst = max(worst, total_variation(effective_dist(old, cid, d), effective_dist(new, cid, d)))
    return worst


def diff_for(old: dict, ops: list[dict]) -> dict:
    out = {}
    for op, before in zip(ops, read_paths(old, ops)):
        if op["op"] == "alloc":
            out[f"allocations.{op['cohort']}.{op['dim']}"] = {"from": before, "to": op["dist"]}
        elif op["op"] == "prompt":
            out["prompt_version"] = {"from": before, "to": op["value"]}
        elif op["op"] == "cohorts":
            out["cohorts"] = {"add": op["add"]}
        else:
            out["schedule.hour_weights"] = {"from": before, "to": op["tables"]}
    return out


class Governance:
    def __init__(self, store: Store, client: SimClient, settings: Settings):
        self.store, self.client, self.settings = store, client, settings

    # ---- owners call these
    def queue(self, workflow_id: str, source: str, ops: list[dict], summary: str, rationale: str, evidence: dict,
              dry_run: dict, minute: int, emergency: bool = False, expected_daily_gain: float = 0.0,
              affected_leads: int = 0) -> int:
        _, cfg = self.store.latest_config(workflow_id)
        item_id = self.store.db.execute(
            "INSERT INTO change_item(workflow_id,source,emergency,ops,expected_old,summary,rationale,evidence,dry_run,"
            "expected_daily_gain,affected_leads,status,created_minute) VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued',?)",
            (workflow_id, source, int(emergency), dumps(ops), dumps(read_paths(cfg, ops)), summary, rationale,
             dumps(evidence), dumps(dry_run), expected_daily_gain, int(affected_leads), minute)).lastrowid
        self.store.decide(minute, workflow_id, "change_queued", summary,
                          {"ops": ops, "emergency": emergency, "evidence": evidence}, item_id=item_id)
        return item_id

    def item(self, item_id: int):
        return self.store.db.execute("SELECT * FROM change_item WHERE item_id=?", (item_id,)).fetchone()

    def has_active(self, workflow_id: str) -> bool:
        q = f"SELECT 1 FROM proposal WHERE workflow_id=? AND status IN ({','.join('?' * len(ACTIVE))}) LIMIT 1"
        return self.store.db.execute(q, (workflow_id, *ACTIVE)).fetchone() is not None

    # ---- state machine
    def _transition(self, pid: str, to: str, minute: int, note: str = "", **cols) -> None:
        row = self.store.db.execute("SELECT status FROM proposal WHERE proposal_id=?", (pid,)).fetchone()
        cur = row["status"] if row else None
        if to not in ALLOWED.get(cur, set()):
            raise IllegalTransition(f"{pid}: {cur} -> {to}")
        sets = ",".join(f"{k}=?" for k in ("status", *cols))
        self.store.db.execute(f"UPDATE proposal SET {sets} WHERE proposal_id=?", (to, *cols.values(), pid))
        self.store.db.execute("INSERT INTO proposal_transition(proposal_id,from_status,to_status,minute,note) "
                              "VALUES(?,?,?,?,?)", (pid, cur, to, minute, note))

    def _close_items(self, pid: str, status: str, reason: str, minute: int, version: int | None = None) -> None:
        self.store.db.execute(
            "UPDATE change_item SET status=?,status_reason=?,applied_version=?,closed_minute=? WHERE proposal_id=?",
            (status, reason, version, minute, pid))

    # ---- config sync
    def sync_config(self, workflow_id: str, minute: int) -> int:
        """Stores the current config and any versions we have not seen, whoever made them."""
        cur = self.client.config(workflow_id)
        known = {r[0] for r in self.store.db.execute(
            "SELECT version FROM config_version WHERE workflow_id=? AND config IS NOT NULL", (workflow_id,))}
        with self.store.tx():
            for h in self.client.config_history(workflow_id):
                if h["version"] in known:
                    continue
                is_cur = h["version"] == cur["version"]
                self.store.db.execute(
                    "INSERT INTO config_version VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                    "config=excluded.config, actor=excluded.actor, changed_dims=excluded.changed_dims",
                    (workflow_id, h["version"], h["actor"], dumps(h.get("changed_dims", [])), h.get("proposal_id"),
                     h.get("approval_id"), dumps(cur["config"]) if is_cur else None, minute))
                if h["actor"] == "supervisor":
                    self.store.decide(minute, workflow_id, "supervisor_edit",
                                      f"Supervisor created v{h['version']}, changing {h.get('changed_dims')}. "
                                      "Not reverted; anything queued against the old values is re-checked.",
                                      {"version": h["version"], "changed_dims": h.get("changed_dims")})
        return cur["version"]

    def consume_inbox(self, minute: int) -> None:
        rows = self.store.db.execute(
            "SELECT event_id,type,payload FROM inbox WHERE processed=0 ORDER BY event_id").fetchall()
        wf_changed = set()
        for r in rows:
            ev = json.loads(r["payload"])
            with self.store.tx():
                if r["type"] == "config_changed":
                    wf_changed.add(ev["workflow_id"])
                elif r["type"] == "approval_decided":
                    self._on_decision(ev["approval_id"], ev["status"], ev.get("reasons", []), minute)
                self.store.db.execute("UPDATE inbox SET processed=1 WHERE event_id=?", (r["event_id"],))
        for wid in sorted(wf_changed):
            self.sync_config(wid, minute)

    def _on_decision(self, approval_id: str, status: str, reasons: list, minute: int, apply_by: int | None = None) -> None:
        row = self.store.db.execute("SELECT * FROM proposal WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None or row["status"] != "pending":
            return  # already handled via polling, or not ours
        self._transition(row["proposal_id"], status, minute, dumps(reasons), reasons=dumps(reasons), decided_minute=minute,
                         apply_by_minute=apply_by)
        if status == "approved":
            self.store.decide(minute, row["workflow_id"], "proposal_approved", f"Approval {approval_id} granted.",
                              {"approval_id": approval_id}, proposal_id=row["proposal_id"])
        else:
            self._close_items(row["proposal_id"], "dropped", f"{status}: {reasons}", minute)
            self.store.decide(minute, row["workflow_id"], f"proposal_{status}",
                              f"Approval {approval_id} {status}; the owner regenerates from fresh data.",
                              {"approval_id": approval_id, "reasons": reasons}, proposal_id=row["proposal_id"])

    # ---- each tick
    def poll_and_apply(self, minute: int) -> None:
        for r in self.store.db.execute("SELECT * FROM proposal WHERE status='pending'").fetchall():
            a = self.client.approval(r["approval_id"])
            if a["status"] != "pending":
                with self.store.tx():
                    self._on_decision(r["approval_id"], a["status"], a.get("reasons", []), minute)
        for r in self.store.db.execute("SELECT * FROM proposal WHERE status='approved' ORDER BY emergency DESC").fetchall():
            if not r["emergency"] and self.store.db.execute(
                    "SELECT 1 FROM proposal WHERE workflow_id=? AND emergency=1 AND status IN ('pending','approved')",
                    (r["workflow_id"],)).fetchone():
                continue  # applying this would make the waiting emergency fix stale
            self._apply(r, minute)

    def _apply(self, r, minute: int) -> None:
        pid, wid, aid = r["proposal_id"], r["workflow_id"], r["approval_id"]
        with self.store.tx():
            self._transition(pid, "applying", minute)
            self._intent_start("apply", aid, wid, minute)
        crash_point("before_apply_http")
        try:
            res = self.client.apply(wid, aid)
        except ApiError as e:
            with self.store.tx():
                self._on_apply_error(pid, wid, aid, e, minute)
            if e.code == "stale_base_version":
                self.sync_config(wid, minute)
            return
        crash_point("after_apply_http")
        self._finish_applied(pid, wid, aid, res["version"], minute)

    def _on_apply_error(self, pid: str, wid: str, aid: str, e: ApiError, minute: int) -> None:
        if e.status == 409 and isinstance(e.detail, dict) and e.detail.get("status") == "applied":
            return  # an earlier attempt landed; reconcile() records the version
        status = "lapsed" if e.status == 410 else "stale_base"
        self._transition(pid, status, minute, str(e))
        self._close_items(pid, "dropped", f"{status}: {e.code}", minute)
        self._intent_end("apply", aid, "failed", str(e))
        self.store.decide(minute, wid, f"proposal_{status}",
                          "Approved change was not applied. Owner regenerates against the current config.",
                          {"error": str(e)}, proposal_id=pid)

    def _finish_applied(self, pid: str, wid: str, aid: str, version: int, minute: int) -> None:
        with self.store.tx():
            self._transition(pid, "applied", minute, f"v{version}", applied_version=version)
            self._close_items(pid, "applied", "applied", minute, version)
            self._intent_end("apply", aid, "done", str(version))
            self.store.decide(minute, wid, "config_applied", f"Applied approval {aid} as v{version}.",
                              {"approval_id": aid, "version": version}, proposal_id=pid)
        self.sync_config(wid, minute)

    def flush(self, minute: int, specs: dict[str, dict]) -> None:
        """Turns queued items into at most one auto-apply and one approval request per workflow."""
        for wid, spec in specs.items():
            items = self.store.db.execute(
                "SELECT * FROM change_item WHERE workflow_id=? AND status='queued' ORDER BY emergency DESC,item_id",
                (wid,)).fetchall()
            if not items:
                continue
            version, cfg = self.store.latest_config(wid)
            items = self._drop_outdated(wid, items, cfg, minute)
            if not items:
                continue
            auto = [i for i in items if self._auto_eligible(i, cfg, spec)]
            if auto and not self.has_active(wid):
                new = apply_ops(cfg, [op for i in auto for op in json.loads(i["ops"])])
                if self._within_auto_policy(cfg, new, spec):
                    self._auto_apply(wid, spec, version, cfg, auto, minute)
                    version, cfg = self.store.latest_config(wid)
                    ids = {i["item_id"] for i in auto}
                    items = [i for i in items if i["item_id"] not in ids]
            if items:
                self._submit_approval(wid, spec, version, cfg, items, minute)

    def _drop_outdated(self, wid: str, items: list, cfg: dict, minute: int) -> list:
        keep = []
        for i in items:
            ops = json.loads(i["ops"])
            if not i["emergency"] and dumps(read_paths(cfg, ops)) != i["expected_old"]:
                with self.store.tx():
                    self.store.db.execute(
                        "UPDATE change_item SET status='dropped',status_reason='config moved since this was queued',"
                        "closed_minute=? WHERE item_id=?", (minute, i["item_id"]))
                    self.store.decide(minute, wid, "change_dropped",
                                      f"Dropped '{i['summary']}': the values it would replace changed. "
                                      "A supervisor edit takes precedence over a queued change.", {}, item_id=i["item_id"])
            else:
                keep.append(i)
        return keep

    def _auto_eligible(self, item, cfg: dict, spec: dict) -> bool:
        """Customer-facing changes always take the approval path, even where policy would allow auto-apply."""
        auto = spec["governance"].get("auto_apply", {})
        if not auto.get("enabled") or json.loads(item["evidence"]).get("force_approval"):
            return False
        if self.settings.tenant_prefs.get(spec.get("tenant_id"), {}).get("no_auto_apply"):
            return False  # a client asked to approve every change, even where policy would let us skip it
        ops = json.loads(item["ops"])
        new = apply_ops(cfg, ops)
        dims = changed_dims(cfg, new)
        if not dims or not dims <= OPERATIONAL_DIMS or not dims <= set(auto.get("allowed_dims", [])):
            return False
        have = {c["id"] for c in cfg["cohorts"]} | {DEFAULT}
        for op in ops:
            if op["op"] == "hours" and not set(op["tables"]) <= have:
                return False  # needs cohorts that are not live yet: travels with them
        return True

    def _within_auto_policy(self, old: dict, new: dict, spec: dict) -> bool:
        return max_shift(old, new) <= spec["governance"]["auto_apply"].get("max_shift", 0) + 1e-9

    def _next_proposal_id(self, wid: str, minute: int) -> str:
        n = self.store.db.execute("SELECT COUNT(*) FROM proposal WHERE workflow_id=?", (wid,)).fetchone()[0] + 1
        return f"{wid}-p{n:04d}"

    def _auto_apply(self, wid: str, spec: dict, version: int, cfg: dict, items: list, minute: int) -> None:
        ops = [op for i in items for op in json.loads(i["ops"])]
        new = apply_ops(cfg, ops)
        pid = self._next_proposal_id(wid, minute)
        body = {"proposal_id": pid, "base_version": version, "proposed_config": new,
                "rationale": " | ".join(i["rationale"] for i in items),
                "evidence": {"items": [i["item_id"] for i in items]}}
        with self.store.tx():
            self._create_proposal(pid, wid, "auto", version, items, body, minute)
            self._intent_start("auto_apply", pid, wid, minute)
        crash_point("before_auto_http")
        try:
            res = self.client.auto_apply(wid, body)
        except ApiError as e:
            with self.store.tx():
                self._transition(pid, "refused", minute, str(e), reasons=dumps(e.detail))
                self._close_items(pid, "dropped", f"auto refused: {e.code}", minute)
                self._intent_end("auto_apply", pid, "failed", str(e))
                self.store.decide(minute, wid, "auto_apply_refused", str(e), {"detail": e.detail}, proposal_id=pid)
            if e.code == "stale_base_version":
                self.sync_config(wid, minute)
            return
        crash_point("after_auto_http")
        with self.store.tx():
            self._transition(pid, "applied", minute, f"v{res['version']}", applied_version=res["version"])
            self._close_items(pid, "applied", "auto-applied within policy", minute, res["version"])
            self._intent_end("auto_apply", pid, "done", str(res["version"]))
            self.store.decide(minute, wid, "config_auto_applied",
                              f"Auto-applied v{res['version']}: " + "; ".join(i["summary"] for i in items),
                              {"changed_dims": res.get("changed_dims"), "max_shift": max_shift(cfg, new)}, proposal_id=pid)
        self.sync_config(wid, minute)

    # ---- request budget and timing
    def request_limit(self, spec: dict) -> int:
        limit = spec["governance"]["max_requests_per_24h"]
        pref = self.settings.tenant_prefs.get(spec.get("tenant_id"), {}).get("max_requests_per_day")
        return min(limit, pref) if pref else limit

    def can_submit(self, wid: str, spec: dict, minute: int, emergency: bool) -> tuple[bool, str]:
        """One slot is reserved per workflow so an incident or rollback is never blocked by routine work."""
        gov = spec["governance"]
        start, end = gov["approver_hours"]
        hour, mod = (minute // 60) % 24, minute % 1440
        if not start <= hour < end:
            return False, "approver is outside working hours"
        pref = self.settings.tenant_prefs.get(spec.get("tenant_id"), {}).get("not_before_hour")
        if pref and hour < pref and not emergency:
            return False, f"client asked for nothing before {pref}:00"
        low, high = gov.get("approver_latency_minutes", [0, 0])
        if not emergency and mod + (low + high) / 2 > end * 60:
            return False, "unlikely to be decided today; holding for the morning"
        used = self.store.db.execute(
            "SELECT COUNT(*) FROM proposal WHERE workflow_id=? AND path='approval' AND approval_id IS NOT NULL "
            "AND created_minute>?", (wid, minute - 1440)).fetchone()[0]
        limit = self.request_limit(spec)
        allowed = limit if emergency else max(1, limit - 1)
        if used >= allowed:
            return False, f"{used} of {allowed} requests used in the last 24h"
        return True, ""

    def _submit_approval(self, wid: str, spec: dict, version: int, cfg: dict, items: list, minute: int) -> None:
        emergency = any(i["emergency"] for i in items)
        if emergency:
            # An active routine proposal must not delay a fix, so the fix may go out alongside it.
            if self.store.db.execute("SELECT 1 FROM proposal WHERE workflow_id=? AND emergency=1 AND status IN "
                                     "('draft','pending','approved','applying')", (wid,)).fetchone():
                return
        elif self.has_active(wid):
            return
        ok, why = self.can_submit(wid, spec, minute, emergency)
        if not ok:
            return
        ops = [op for i in items for op in json.loads(i["ops"])]
        new = apply_ops(cfg, ops)
        if new == cfg:
            with self.store.tx():
                for i in items:
                    self.store.db.execute(
                        "UPDATE change_item SET status='applied',status_reason='no-op',applied_version=?,closed_minute=? "
                        "WHERE item_id=?", (version, minute, i["item_id"]))
            return
        pid = self._next_proposal_id(wid, minute)
        body = self._approval_body(pid, wid, version, cfg, new, items, ops)
        with self.store.tx():
            self._create_proposal(pid, wid, "approval", version, items, body, minute, emergency)
            self._intent_start("submit_approval", pid, wid, minute)
        crash_point("before_submit_http")
        try:
            res = self.client.submit_approval(body)
        except ApiError as e:
            if e.code == "duplicate_proposal_id":
                self.reconcile_one_submission(pid, wid, minute)
                return
            with self.store.tx():
                self._transition(pid, "failed", minute, str(e), reasons=dumps(e.detail))
                self._close_items(pid, "dropped", f"submit failed: {e.code}", minute)
                self._intent_end("submit_approval", pid, "failed", str(e))
                self.store.decide(minute, wid, "proposal_submit_failed", str(e), {"detail": e.detail}, proposal_id=pid)
            if e.code == "stale_base_version":
                self.sync_config(wid, minute)
            return
        crash_point("after_submit_http")
        self._mark_submitted(pid, wid, res, minute)

    def _mark_submitted(self, pid: str, wid: str, res: dict, minute: int) -> None:
        with self.store.tx():
            self._transition(pid, "pending", minute, res["approval_id"], approval_id=res["approval_id"])
            self._intent_end("submit_approval", pid, "done", res["approval_id"])
            self.store.decide(minute, wid, "proposal_submitted", f"Sent {pid} as {res['approval_id']}.",
                              {"approval_id": res["approval_id"]}, proposal_id=pid)

    def _approval_body(self, pid: str, wid: str, version: int, cfg: dict, new: dict, items: list, ops: list) -> dict:
        gain = sum(i["expected_daily_gain"] for i in items)
        # Impact is expected extra concluded leads per day on a saturating scale, so it ranks a queue
        # across workflows. An incident floor keeps a safety fix above routine optimisation.
        impact = 1 - math.exp(-max(gain, 0) / 50)
        if any(i["emergency"] for i in items):
            impact = max(impact, 0.8)
        evidence: dict = {"items": {}}
        for i in items:
            e = json.loads(i["evidence"])
            evidence["items"][str(i["item_id"])] = e
            if "validation_run_id" in e:
                evidence["validation_run_id"] = e["validation_run_id"]
        return {"proposal_id": pid, "workflow_id": wid, "base_version": version, "proposed_config": new,
                "change_summary": "; ".join(i["summary"] for i in items),
                "diff": diff_for(cfg, ops),
                "affected_leads": max((i["affected_leads"] for i in items), default=0),
                "dry_run": {str(i["item_id"]): json.loads(i["dry_run"]) for i in items},
                "impact_score": round(min(max(impact, 0.0), 1.0), 4),
                "rationale": " | ".join(i["rationale"] for i in items),
                "evidence": evidence}

    def _create_proposal(self, pid: str, wid: str, path: str, version: int, items: list, body: dict, minute: int,
                         emergency: bool = False) -> None:
        self.store.db.execute(
            "INSERT INTO proposal(proposal_id,workflow_id,path,status,base_version,emergency,body,created_minute) "
            "VALUES(?,?,?,'draft',?,?,?,?)", (pid, wid, path, version, int(emergency), dumps(body), minute))
        self.store.db.execute("INSERT INTO proposal_transition(proposal_id,from_status,to_status,minute,note) "
                              "VALUES(?,NULL,'draft',?,'')", (pid, minute))
        self.store.db.executemany("UPDATE change_item SET status='in_proposal',proposal_id=? WHERE item_id=?",
                                  [(pid, i["item_id"]) for i in items])

    # ---- intents and restart
    def _intent_start(self, kind: str, key: str, wid: str, minute: int) -> None:
        self.store.db.execute("INSERT OR REPLACE INTO side_effect_intent(kind,key,workflow_id,status,created_minute) "
                              "VALUES(?,?,?,'started',?)", (kind, key, wid, minute))

    def _intent_end(self, kind: str, key: str, status: str, result: str) -> None:
        self.store.db.execute("UPDATE side_effect_intent SET status=?,result=? WHERE kind=? AND key=?",
                              (status, result, kind, key))

    def reconcile(self, minute: int) -> dict:
        """Runs at startup. Asks the simulator about each open intent and proposal; returns what it did."""
        out = {"submissions": 0, "applies": 0, "autos": 0, "pending_checked": 0}
        for r in self.store.db.execute("SELECT * FROM side_effect_intent WHERE status='started'").fetchall():
            if r["kind"] == "submit_approval":
                self.reconcile_one_submission(r["key"], r["workflow_id"], minute)
                out["submissions"] += 1
            elif r["kind"] == "apply":
                self._reconcile_apply(r["key"], r["workflow_id"], minute)
                out["applies"] += 1
            elif r["kind"] == "auto_apply":
                self._reconcile_auto(r["key"], r["workflow_id"], minute)
                out["autos"] += 1
        for r in self.store.db.execute("SELECT * FROM proposal WHERE status='pending'").fetchall():
            a = self.client.approval(r["approval_id"])
            out["pending_checked"] += 1
            if a["status"] != "pending":
                with self.store.tx():
                    self._on_decision(r["approval_id"], a["status"], a.get("reasons", []), minute)
        self.store.decide(minute, None, "recovery", f"Reconciled state with the simulator: {out}", out)
        return out

    def reconcile_one_submission(self, pid: str, wid: str, minute: int) -> None:
        found = next((a for a in self.client.approvals(wid) if a.get("proposal_id") == pid), None)
        if found:
            self._mark_submitted(pid, wid, found, minute)
            return
        with self.store.tx():  # never reached the simulator
            self._transition(pid, "failed", minute, "crash before submit reached the simulator")
            self._close_items(pid, "dropped", "submit never landed", minute)
            self._intent_end("submit_approval", pid, "failed", "not found")

    def _reconcile_apply(self, aid: str, wid: str, minute: int) -> None:
        a = self.client.approval(aid)
        row = self.store.db.execute("SELECT * FROM proposal WHERE approval_id=?", (aid,)).fetchone()
        if row["status"] != "applying":
            return
        if a["status"] == "applied":
            hist = self.client.config_history(wid)
            ver = next(h["version"] for h in hist if h.get("approval_id") == aid)
            self._finish_applied(row["proposal_id"], wid, aid, ver, minute)
        elif a["status"] == "approved":
            with self.store.tx():
                self._transition(row["proposal_id"], "approved", minute, "crash before apply landed; retrying")
                self._intent_end("apply", aid, "failed", "did not land")
        else:
            with self.store.tx():
                self._transition(row["proposal_id"], "lapsed", minute, f"found {a['status']} after restart")
                self._close_items(row["proposal_id"], "dropped", a["status"], minute)
                self._intent_end("apply", aid, "failed", a["status"])

    def _reconcile_auto(self, pid: str, wid: str, minute: int) -> None:
        found = next((h for h in self.client.config_history(wid) if h.get("proposal_id") == pid), None)
        with self.store.tx():
            if found:
                self._transition(pid, "applied", minute, "confirmed after restart", applied_version=found["version"])
                self._close_items(pid, "applied", "auto-applied (confirmed after restart)", minute, found["version"])
                self._intent_end("auto_apply", pid, "done", str(found["version"]))
            else:
                self._transition(pid, "failed", minute, "crash before auto-apply landed")
                self._close_items(pid, "dropped", "auto-apply never landed", minute)
                self._intent_end("auto_apply", pid, "failed", "not found")
        self.sync_config(wid, minute)
