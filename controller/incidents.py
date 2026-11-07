"""Population-level incident detection and response for prompts and caller IDs.

Every detector needs a minimum population and a large z against a baseline, so no single call can trigger
anything. Thresholds are set conservatively because checks repeat every tick over overlapping windows and flag
counts are small, where the normal approximation understates tail risk.

The LLM only proposes candidates. Validation measures safety; live data measures engagement. Neither the
LLM nor the validation score decides a prompt is good for customers: verification after applying does.
"""
from __future__ import annotations

import json
import logging
import re

from . import stats
from .client import ApiError, SimClient, Unavailable
from .cohorts import DEFAULT, cube_rows, history_baseline, rate
from .experiments import guard_margin
from .governance import Governance
from .settings import Settings
from .store import Store, dumps

log = logging.getLogger("controller.incidents")
OPEN = ("open", "fixing", "verifying")
# From a client's written account of a vendor's failed fix: scripted prompts pass safety checks and lose people.
SCRIPTED = re.compile(r"verbatim|word[- ]for[- ]word|strict", re.I)


class Incidents:
    def __init__(self, store: Store, client: SimClient, gov: Governance, settings: Settings):
        self.store, self.client, self.gov, self.settings = store, client, gov, settings
        self.credits_remaining: float | None = None
        self._baseline: dict[str, dict] = {}

    def baseline(self, wid: str) -> dict:
        if wid not in self._baseline:
            self._baseline[wid] = history_baseline(self.store, wid)
        return self._baseline[wid]

    def blocked_arms(self, wid: str) -> set[str]:
        """Caller IDs with a confirmed problem stay out of experiments for the rest of the run."""
        return {r[0] for r in self.store.db.execute(
            "SELECT DISTINCT subject FROM incident WHERE workflow_id=? AND kind='caller_id'", (wid,))}

    def open_did_workflows(self) -> set[str]:
        q = f"SELECT DISTINCT workflow_id FROM incident WHERE kind='caller_id' AND state IN ({','.join('?' * len(OPEN))})"
        return {r[0] for r in self.store.db.execute(q, OPEN)}

    def tick(self, minute: int, specs: dict[str, dict]) -> None:
        for wid, spec in specs.items():
            self._detect_prompt(wid, spec, minute)
            self._detect_caller_ids(wid, spec, minute)
            q = f"SELECT * FROM incident WHERE workflow_id=? AND state IN ({','.join('?' * len(OPEN))})"
            for inc in self.store.db.execute(q, (wid, *OPEN)).fetchall():
                if inc["kind"] == "prompt" and inc["state"] == "open":
                    self._step_prompt(inc, spec, minute)  # spends credits, so HTTP stays outside the transaction
                else:
                    with self.store.tx():
                        if inc["kind"] == "prompt":
                            self._step_prompt(inc, spec, minute)
                        else:
                            self._step_did(inc, spec, minute)

    # ---- prompt incidents
    def _detect_prompt(self, wid: str, spec: dict, minute: int) -> None:
        s = self.settings
        if self.store.db.execute("SELECT 1 FROM incident WHERE workflow_id=? AND kind='prompt' AND state IN ('open','fixing','verifying')",
                                 (wid,)).fetchone():
            return
        _, cfg = self.store.latest_config(wid)
        prompt = cfg["prompt_version"]
        base = self.baseline(wid)
        if not base["connected"]:
            return
        since = minute // 60 - s.incident_window_hours + 1
        last = self.store.db.execute("SELECT MAX(fixed_minute) FROM incident WHERE workflow_id=? AND kind='prompt'", (wid,)).fetchone()[0]
        if last is not None:
            since = max(since, last // 60 + 1)  # judge a fix only on calls made after it
        r = cube_rows(self.store, wid, [], since_hour=max(since, 0), where="prompt_version=?", params=(prompt,))[0]
        con, hall = r["connected"] or 0, r["hall"] or 0
        if con < s.incident_min_connected:
            return
        p0 = max(base["hall"], 1e-3)
        z = stats.one_sample_z(hall, con, p0)
        changed = self.store.db.execute(
            "SELECT MAX(applied_minute) FROM config_version WHERE workflow_id=? AND changed_dims LIKE '%prompt_version%'", (wid,)).fetchone()[0]
        recent_change = changed is not None and minute - changed <= 1440
        need_z = s.incident_z_after_change if recent_change else s.incident_z
        need_ratio = s.hall_min_ratio_after_change if recent_change else s.hall_min_ratio
        if z >= need_z and hall / con >= need_ratio * p0:
            ev = {"prompt": prompt, "connected": con, "flagged": hall, "rate": round(hall / con, 4), "baseline": round(p0, 4),
                  "z": round(z, 2), "window_hours": s.incident_window_hours, "threshold_z": need_z, "prompt_recently_changed": recent_change,
                  "engagement_before": round(self._engagement_before(wid, prompt), 4)}
            with self.store.tx():
                iid = self.store.db.execute(
                    "INSERT INTO incident(workflow_id,kind,subject,state,evidence,detected_minute) VALUES(?,'prompt',?,'open',?,?)",
                    (wid, prompt, dumps(ev), minute)).lastrowid
                self.store.decide(minute, wid, "incident_detected",
                                  f"Hallucination flags on {prompt} at {ev['rate']:.1%} vs baseline {p0:.1%} over {con} connected calls.",
                                  ev, incident_id=iid)

    def _engagement_before(self, wid: str, current_prompt: str) -> float:
        rows = cube_rows(self.store, wid, ["prompt_version"])
        other = [r for r in rows if r["prompt_version"] != current_prompt]
        if other:
            best = max(other, key=lambda r: r["connected"])
            return rate(best["engaged"], best["connected"])
        return self.baseline(wid)["engagement"]

    def _allowance(self, wid: str, specs_count: int) -> float:
        start = float(self.store.meta("credits_start") or 0)
        share = start / max(specs_count, 1) * self.settings.credit_share_multiplier
        spent = self.store.db.execute("SELECT COALESCE(SUM(cost),0) FROM credit_ledger WHERE workflow_id=?", (wid,)).fetchone()[0]
        return share - spent

    def _spend_guard(self, wid: str, cost: float, minute: int, n_workflows: int) -> bool:
        if self.credits_remaining is not None and self.credits_remaining < cost:
            self.store.decide(minute, wid, "alert", f"Credits exhausted: {self.credits_remaining} left, {cost} needed.",
                              {"severity": "page", "look_at": "credit_ledger"})
            return False
        if self._allowance(wid, n_workflows) < cost:
            self.store.decide(minute, wid, "credits_withheld", "Workflow used its share of credits; holding the rest for others.",
                              {"cost": cost})
            return False
        return True

    def _validate(self, wid: str, prompt: str, minute: int, n_workflows: int) -> dict | None:
        """Reuses a validation already paid for. Evidence stays valid while the prompt and workflow are unchanged."""
        r = self.store.db.execute(
            "SELECT * FROM credit_ledger WHERE workflow_id=? AND kind='validation' AND prompt_version=? ORDER BY id DESC LIMIT 1",
            (wid, prompt)).fetchone()
        if r:
            return {"run_id": r["run_id"], "pass_rate": r["pass_rate"], "n": r["n"]}
        n = self.settings.validation_n
        cost = float(-(-n // 100))
        if not self._spend_guard(wid, cost, minute, n_workflows):
            return None
        try:
            v = self.client.validate(wid, prompt, n)
        except (ApiError, Unavailable) as e:
            self.store.decide(minute, wid, "validation_failed", str(e), {"prompt": prompt})
            return None
        self.credits_remaining = v["credits_remaining"]
        self.store.db.execute(
            "INSERT INTO credit_ledger(workflow_id,kind,prompt_version,cost,credits_remaining,run_id,pass_rate,n,minute) "
            "VALUES(?,'validation',?,?,?,?,?,?,?)", (wid, prompt, cost, v["credits_remaining"], v["validation_run_id"], v["pass_rate"], n, minute))
        return {"run_id": v["validation_run_id"], "pass_rate": v["pass_rate"], "n": n}

    def _known_good(self, wid: str, current: str, p0: float) -> list[str]:
        """Prompts that really ran here with an acceptable flag rate. Real exposure beats any suggestion."""
        out = []
        for r in cube_rows(self.store, wid, ["prompt_version"]):
            if r["prompt_version"] != current and r["connected"] >= 500 and r["hall"] / r["connected"] <= 1.5 * p0:
                out.append((r["connected"], r["prompt_version"]))
        return [p for _, p in sorted(out, reverse=True)]

    def _ask_llm(self, inc, spec: dict, minute: int, n_workflows: int, ev: dict) -> list[dict]:
        wid = inc["workflow_id"]
        prior = self.store.db.execute("SELECT payload FROM (SELECT evidence payload FROM decision WHERE workflow_id=? AND kind='llm_suggestions' "
                                      "ORDER BY decision_id DESC LIMIT 1)", (wid,)).fetchone()
        if prior:
            return json.loads(prior[0])["suggestions"]
        if not self._spend_guard(wid, 3.0, minute, n_workflows):
            return []
        problem = f"hallucination flags at {ev['rate']:.1%} on prompt {inc['subject']} vs baseline {ev['baseline']:.1%}"
        try:
            res = self.client.suggest_fixes(wid, problem)
        except (ApiError, Unavailable) as e:
            self.store.decide(minute, wid, "llm_failed", str(e), {}, incident_id=inc["incident_id"])
            return []
        self.credits_remaining = res["credits_remaining"]
        with self.store.tx():
            self.store.db.execute("INSERT INTO credit_ledger(workflow_id,kind,cost,credits_remaining,minute) VALUES(?,'llm',3,?,?)",
                                  (wid, res["credits_remaining"], minute))
            self.store.decide(minute, wid, "llm_suggestions", f"LLM proposed {[x['prompt_version'] for x in res['suggestions']]}.",
                              {"suggestions": res["suggestions"]}, incident_id=inc["incident_id"])
        return res["suggestions"]

    def _pick_candidate(self, inc, spec: dict, minute: int) -> dict | None:
        """Prefers the safest route to a known state, and spends credits only when that is not available."""
        wid = inc["workflow_id"]
        ev = json.loads(inc["evidence"])
        tried = set(json.loads(inc["tried"])) | {inc["subject"]}
        threshold = spec["governance"]["min_validation_pass_rate"]
        n_workflows = len(self.store.workflow_specs())
        for p in self._known_good(wid, inc["subject"], ev["baseline"]):
            if p in tried:
                continue
            v = self._validate(wid, p, minute, n_workflows)
            if v and v["pass_rate"] >= threshold:
                return {"prompt": p, "validation": v, "source": "last prompt with a good record on this workflow", "scripted": False}
        suggestions = self._ask_llm(inc, spec, minute, n_workflows, ev)
        scored = []
        for sug in suggestions:
            p = sug["prompt_version"]
            if p in tried or p not in spec["options"]["prompt_version"]:
                continue
            v = self._validate(wid, p, minute, n_workflows)
            if v is None:
                continue
            self.store.decide(minute, wid, "candidate_validated", f"{p}: pass rate {v['pass_rate']:.3f} on n={v['n']}.",
                              {"prompt": p, "summary": sug.get("summary"), **v}, incident_id=inc["incident_id"])
            if v["pass_rate"] >= threshold:
                scored.append({"prompt": p, "validation": v, "source": "LLM suggestion", "summary": sug.get("summary", ""),
                               "scripted": bool(SCRIPTED.search(sug.get("summary", "")))})
        # Passing is the bar for safety, and above it more safety says nothing about engagement. Scripted
        # candidates go last because they are the known way to pass validation and still lose customers.
        scored.sort(key=lambda c: (c["scripted"], -c["validation"]["pass_rate"], c["prompt"]))
        return scored[0] if scored else None

    def _step_prompt(self, inc, spec: dict, minute: int) -> None:
        iid, wid = inc["incident_id"], inc["workflow_id"]
        if inc["state"] == "open":
            if minute < inc["retry_after_minute"]:
                return
            cand = self._pick_candidate(inc, spec, minute)
            if cand is None:
                wait = 120
                self.store.db.execute("UPDATE incident SET retry_after_minute=? WHERE incident_id=?", (minute + wait, iid))
                self.store.decide(minute, wid, "alert", "Incident open with no validated fix; retrying in 2 hours.",
                                  {"severity": "page", "look_at": f"incident {iid}"}, incident_id=iid)
                return
            ev = json.loads(inc["evidence"])
            base = self.baseline(wid)
            sup = self._supervisor_authored(wid, inc["subject"])
            sup_summary = f" (current prompt was set by a supervisor edit, v{sup['version']})" if sup else ""
            sup_rationale = (f"NOTE: this replaces a prompt a supervisor set in v{sup['version']} ({sup['at']}). It is proposed only because of the "
                             "safety incident below, and takes effect only if approved. ") if sup else ""
            with self.store.tx():
                item = self.gov.queue(
                    wid, f"incident:{iid}", [{"op": "prompt", "value": cand["prompt"]}],
                    f"Replace prompt {inc['subject']} with {cand['prompt']}: hallucination flags at {ev['rate']:.1%}, normal is {ev['baseline']:.1%}{sup_summary}",
                    f"{sup_rationale}Population-level spike (z={ev['z']}, {ev['connected']} connected calls). {cand['source'].capitalize()}; validation pass rate "
                    f"{cand['validation']['pass_rate']:.3f} on n={cand['validation']['n']}. Engagement is checked after applying, with automatic follow-up.",
                    {"validation_run_id": cand["validation"]["run_id"], "incident": ev, "candidate": cand,
                     **({"overrides_supervisor_edit": sup} if sup else {})},
                    {"type": "validation_run", **cand["validation"], "note": "Measures safety only, not engagement."}, minute, emergency=True,
                    expected_daily_gain=0, affected_leads=round(base["leads_per_day"]))
                self.store.db.execute("UPDATE incident SET state='fixing',pending_item=?,tried=? WHERE incident_id=?",
                                      (item, dumps(sorted(set(json.loads(inc["tried"])) | {cand["prompt"]})), iid))
            return
        if inc["state"] == "fixing":
            item = self.gov.item(inc["pending_item"])
            if item["status"] == "applied":
                self.store.db.execute("UPDATE incident SET state='verifying',fixed_version=?,fixed_minute=?,pending_item=NULL WHERE incident_id=?",
                                      (item["applied_version"], minute, iid))
                self.store.decide(minute, wid, "fix_applied", f"Fix live in v{item['applied_version']}; verifying on live calls.", {}, incident_id=iid, item_id=item["item_id"])
            elif item["status"] == "dropped":
                retry_rejected = 30 if str(item["status_reason"]).startswith(("stale", "lapsed", "config")) else 120
                tried = [p for p in json.loads(inc["tried"]) if p != json.loads(item["ops"])[0]["value"]]
                self.store.db.execute("UPDATE incident SET state='open',pending_item=NULL,tried=?,retry_after_minute=? WHERE incident_id=?",
                                      (dumps(tried), minute + retry_rejected, iid))
                self.store.decide(minute, wid, "fix_not_applied", f"{item['status_reason']}; re-proposing with current config.", {}, incident_id=iid)
            return
        self._verify_prompt(inc, spec, minute)

    def _supervisor_authored(self, wid: str, prompt: str) -> dict | None:
        """The supervisor edit that put this prompt live, if the latest prompt change was theirs."""
        r = self.store.db.execute(
            "SELECT version, actor, applied_minute FROM config_version WHERE workflow_id=? AND changed_dims LIKE '%prompt_version%' "
            "ORDER BY version DESC LIMIT 1", (wid,)).fetchone()
        _, cfg = self.store.latest_config(wid)
        if r and r["actor"] == "supervisor" and cfg["prompt_version"] == prompt:
            m = r["applied_minute"] or 0
            return {"version": r["version"], "at": f"D{m // 1440} {(m % 1440) // 60:02d}:{m % 60:02d}"}
        return None

    def _verify_prompt(self, inc, spec: dict, minute: int) -> None:
        s = self.settings
        iid, wid = inc["incident_id"], inc["workflow_id"]
        _, cfg = self.store.latest_config(wid)
        prompt = cfg["prompt_version"]
        ev = json.loads(inc["evidence"])
        r = cube_rows(self.store, wid, [], where="config_version>=? AND prompt_version=?", params=(inc["fixed_version"], prompt))[0]
        con = r["connected"] or 0
        if con < s.verify_min_connected:
            return
        p0 = ev["baseline"]
        zh = stats.one_sample_z(r["hall"] or 0, con, p0)
        eng = rate(r["engaged"] or 0, con)
        ze = stats.one_sample_z(r["engaged"] or 0, con, ev["engagement_before"])
        finding = {"prompt": prompt, "connected": con, "flag_rate": round((r["hall"] or 0) / con, 4), "flag_z": round(zh, 2),
                   "engagement": round(eng, 4), "engagement_before": ev["engagement_before"], "engagement_z": round(ze, 2)}
        margin = guard_margin(spec, "pickup_rate") if spec["objective"]["primary"] == "pickup_rate" else 0.02
        safe = zh < 3.0
        keeps_people = not (ze <= -3.0 and ev["engagement_before"] - eng > margin)
        if safe and keeps_people:
            self.store.db.execute("UPDATE incident SET state='resolved',resolved_minute=? WHERE incident_id=?", (minute, iid))
            self.store.decide(minute, wid, "incident_resolved", f"Fix verified: flags normal and engagement held on {prompt}.", finding, incident_id=iid)
        else:
            why = "flags are still high" if not safe else "engagement fell: safe but people leave"
            self.store.db.execute("UPDATE incident SET state='open',evidence=?,retry_after_minute=? WHERE incident_id=?",
                                  (dumps({**ev, "failed_fix": finding}), minute, iid))
            self.store.decide(minute, wid, "fix_failed", f"{prompt} did not hold: {why}. Trying another candidate.", finding, incident_id=iid)

    # ---- caller-ID incidents
    def _detect_caller_ids(self, wid: str, spec: dict, minute: int) -> None:
        s = self.settings
        base = self.baseline(wid)
        by_hour = {int(k): v for k, v in base["pickup_by_hour"].items()}
        if not by_hour:
            return
        since = max(minute // 60 - s.did_window_hours + 1, 0)
        rows = self.store.db.execute(
            "SELECT caller_id, ((hour_idx%24)+24)%24 hour, SUM(dials) d, SUM(connected) c FROM kpi_cube "
            "WHERE workflow_id=? AND hour_idx>=? GROUP BY 1,2", (wid, since)).fetchall()
        agg: dict[str, list[float]] = {}
        for r in rows:
            ph = by_hour.get(r["hour"], base["pickup"])
            a = agg.setdefault(r["caller_id"], [0.0, 0.0, 0.0, 0.0])
            a[0] += r["d"]
            a[1] += r["c"]
            a[2] += r["d"] * ph
            a[3] += r["d"] * ph * (1 - ph)
        flagged, findings = [], {}
        for did, (n, x, exp, var) in agg.items():
            if n < s.did_min_dials or var <= 0:
                continue
            z = (x - exp) / var ** 0.5
            findings[did] = {"dials": int(n), "pickup": round(x / n, 4), "expected": round(exp / n, 4), "z": round(z, 2)}
            if z <= s.did_z and (exp - x) / exp >= s.did_min_rel_drop:
                flagged.append(did)
        if not flagged:
            return
        _, cfg = self.store.latest_config(wid)
        live = {d for a in cfg["allocations"].values() for d, w in a.get("caller_id", {}).items() if w > 0}
        if len(findings) >= 2 and len(flagged) == len(findings):
            if not self.store.db.execute("SELECT 1 FROM decision WHERE workflow_id=? AND kind='alert' AND minute>? AND summary LIKE 'All caller%'",
                                         (wid, minute - 720)).fetchone():
                self.store.decide(minute, wid, "alert", "All caller IDs fell together: looks systemic, not one bad number, so nothing was rotated.",
                                  {"severity": "ticket", "findings": findings})
            return
        for did in flagged:
            if did in live and not self.store.db.execute(
                    "SELECT 1 FROM incident WHERE workflow_id=? AND kind='caller_id' AND subject=? AND state IN ('open','fixing','verifying')",
                    (wid, did)).fetchone():
                ev = {"caller_id": did, **findings[did], "peers": findings, "window_hours": s.did_window_hours}
                with self.store.tx():
                    iid = self.store.db.execute(
                        "INSERT INTO incident(workflow_id,kind,subject,state,evidence,detected_minute) VALUES(?,'caller_id',?,'open',?,?)",
                        (wid, did, dumps(ev), minute)).lastrowid
                    self.store.decide(minute, wid, "incident_detected",
                                      f"Caller ID {did} pickup {findings[did]['pickup']:.1%} vs expected {findings[did]['expected']:.1%} over {findings[did]['dials']} dials.",
                                      ev, incident_id=iid)

    def _healthy_dids(self, wid: str, spec: dict, bad: set[str], minute: int) -> list[str]:
        return [d for d in spec["options"]["caller_id"] if d not in bad]

    def _dist_without(self, dist: dict, bad: set[str], healthy: list[str]) -> dict:
        keep = {k: v for k, v in dist.items() if k not in bad and v > 0}
        if not keep:
            return {d: round(1 / len(healthy), 4) for d in healthy}
        tot = sum(keep.values())
        return {k: round(v / tot, 4) for k, v in keep.items()}

    def _step_did(self, inc, spec: dict, minute: int) -> None:
        iid, wid, did = inc["incident_id"], inc["workflow_id"], inc["subject"]
        _, cfg = self.store.latest_config(wid)
        bad = self.blocked_arms(wid)
        holders = [c for c, a in cfg["allocations"].items() if a.get("caller_id", {}).get(did, 0) > 0]
        if inc["state"] == "fixing":
            item = self.gov.item(inc["pending_item"])
            if item["status"] in ("queued", "in_proposal"):
                return
            self.store.db.execute("UPDATE incident SET state='open',pending_item=NULL WHERE incident_id=?", (iid,))
            if item["status"] == "dropped":
                self.store.db.execute("UPDATE incident SET retry_after_minute=? WHERE incident_id=?", (minute + 60, iid))
            return
        if inc["state"] == "verifying":
            if minute - (inc["fixed_minute"] or minute) >= 240:
                self.store.db.execute("UPDATE incident SET state='resolved',resolved_minute=? WHERE incident_id=?", (minute, iid))
                self.store.decide(minute, wid, "incident_resolved", f"{did} has had no traffic for 4 hours and stays out of rotation.", {}, incident_id=iid)
            return
        if not holders:
            self.store.db.execute("UPDATE incident SET state='verifying',fixed_minute=? WHERE incident_id=?", (minute, iid))
            self.store.decide(minute, wid, "fix_applied", f"{did} removed from every allocation.", {}, incident_id=iid)
            return
        if minute < inc["retry_after_minute"]:
            return
        healthy = self._healthy_dids(wid, spec, bad, minute)
        if not healthy:
            self.store.decide(minute, wid, "alert", "Every caller ID on this workflow is flagged; needs a human.",
                              {"severity": "page", "look_at": f"incident {iid}"}, incident_id=iid)
            self.store.db.execute("UPDATE incident SET state='failed' WHERE incident_id=?", (iid,))
            return
        ops = [{"op": "alloc", "cohort": c, "dim": "caller_id",
                "dist": self._dist_without(cfg["allocations"][c]["caller_id"], bad, healthy)} for c in holders]
        auto = spec["governance"].get("auto_apply", {})
        partial_used = bool(json.loads(inc["tried"]))
        limit = auto.get("max_shift", 0)
        evidence = json.loads(inc["evidence"])
        if auto.get("enabled") and "caller_id" in auto.get("allowed_dims", []) and not partial_used:
            full_shift = max(stats.total_variation(cfg["allocations"][op["cohort"]]["caller_id"], op["dist"]) for op in ops)
            if full_shift > limit + 1e-9:
                # Move as far as policy allows now. The rest asks a human, so the limit is never walked around.
                ratio = limit / full_shift
                ops = [{**op, "dist": self._blend(cfg["allocations"][op["cohort"]]["caller_id"], op["dist"], ratio)} for op in ops]
                self.store.db.execute("UPDATE incident SET tried=? WHERE incident_id=?", (dumps(["partial_auto_step"]), iid))
        else:
            evidence = {**evidence, "force_approval": partial_used}
        base = self.baseline(wid)
        item = self.gov.queue(wid, f"incident:{iid}", ops, f"Move traffic off caller ID {did}: pickup {evidence['pickup']:.1%}, expected {evidence['expected']:.1%}",
                              f"{did} is significantly below its expected pickup (z={evidence['z']}) while others are not. Rotation is operational, so customers hear nothing different.",
                              evidence, {"type": "observed_population", "caller_ids": evidence["peers"]}, minute, emergency=True,
                              expected_daily_gain=(evidence["expected"] - evidence["pickup"]) * base["dials_per_day"] * base["engagement"] * 0.5,
                              affected_leads=round(base["leads_per_day"]))
        self.store.db.execute("UPDATE incident SET state='fixing',pending_item=? WHERE incident_id=?", (item, iid))

    @staticmethod
    def _blend(old: dict, new: dict, ratio: float) -> dict:
        keys = set(old) | set(new)
        s1, s2 = sum(old.values()) or 1, sum(new.values()) or 1
        out = {k: (1 - ratio) * old.get(k, 0) / s1 + ratio * new.get(k, 0) / s2 for k in keys}
        return {k: round(v, 4) for k, v in out.items() if v > 0}
