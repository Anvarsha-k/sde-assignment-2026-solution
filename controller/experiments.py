"""Variant experiments: one state machine per workflow and dimension.

    queued -> running -> promoting -> monitoring -> rolling_back -> concluded
                 |                         |
                 +-> concluded (no winner) +-> concluded (still winning at scenario end)

welcome_message and voice are sampled independently per dial, so running them at once is a factorial
design: each dial informs both dimensions, and one approval carries both. caller_id runs the same way
with pickup as its target, because a customer decides whether to answer before hearing anything.

Evidence is read only from the cube, so an arm that never dialled has no rows and cannot affect anything.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict

from . import stats
from .cohorts import DEFAULT, cohort_shares, cube_rows, history_baseline, rate, segment_to_cohort
from .governance import Governance
from .settings import Settings
from .store import Store, dumps

log = logging.getLogger("controller.experiments")
DIMS = ("caller_id", "welcome_message", "voice")
TARGET = {"caller_id": "pickup_rate", "welcome_message": "engagement_rate", "voice": "engagement_rate"}
OPEN = ("queued", "running", "promoting", "monitoring", "rolling_back")
ALL = "all_traffic"  # scope name; internal bookkeeping keys in scopes start with an underscore


def guard_margin(spec: dict, metric: str) -> float:
    """How far the other KPI may fall: from the workflow's guardrail, or tight if that KPI is the primary."""
    other = "engagement_rate" if metric == "pickup_rate" else "pickup_rate"
    guard = spec.get("objective", {}).get("guardrail", {})
    return guard.get(other + "_max_drop", 0.01)


def metric_counts(row, metric: str) -> tuple[int, int]:
    return (row["connected"], row["dials"]) if metric == "pickup_rate" else (row["engaged"], row["connected"])


class Experiments:
    def __init__(self, store: Store, gov: Governance, settings: Settings, end_minute: int):
        self.store, self.gov, self.settings, self.end_minute = store, gov, settings, end_minute

    # ---- read model
    def _strata(self, wid: str, dim: str, metric: str, since_hour: int, min_version: int,
                segments: set[int] | None = None, extra_col: str | None = None) -> dict:
        """Returns stratum -> arm -> [x, n], where a stratum is one config version and cohort."""
        seg_map = segment_to_cohort(self.store.segments(), self.store.cohorts(wid))
        group = ["config_version", "segment_id", dim] + ([extra_col] if extra_col else [])
        out: dict = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in cube_rows(self.store, wid, group, since_hour=since_hour, where="config_version>=?", params=(min_version,)):
            if segments is not None and r["segment_id"] not in segments:
                continue
            x, n = metric_counts(r, metric)
            key = (r["config_version"], seg_map.get(r["segment_id"], DEFAULT)) + ((r[extra_col],) if extra_col else ())
            cell = out[key][r[dim]]
            cell[0] += x
            cell[1] += n
        return out

    def _diff(self, strata: dict, arm: str, control: str) -> stats.Diff:
        rows = []
        for arms in strata.values():
            a, b = arms.get(arm), arms.get(control)
            if a and b:
                rows.append((a[0], a[1], b[0], b[1]))
        return stats.stratified_diff(rows, self.settings.design_effect)

    def _arm_totals(self, wid: str, dim: str, metric: str, since_hour: int, segments: set[int] | None) -> dict[str, list[int]]:
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in cube_rows(self.store, wid, ["segment_id", dim], since_hour=since_hour):
            if segments is None or r["segment_id"] in segments:
                x, n = metric_counts(r, metric)
                out[r[dim]][0] += x
                out[r[dim]][1] += n
        return out

    def _segments_of(self, wid: str, cohort: str) -> set[int] | None:
        if cohort == ALL:
            return None
        seg_map = segment_to_cohort(self.store.segments(), self.store.cohorts(wid))
        return {s for s, c in seg_map.items() if c == cohort}

    # ---- tick
    def tick(self, minute: int, specs: dict[str, dict], blocked_arms: dict[str, set[str]]) -> None:
        for wid, spec in specs.items():
            if not self.store.db.execute("SELECT 1 FROM kpi_cube WHERE workflow_id=? LIMIT 1", (wid,)).fetchone():
                continue
            for dim in DIMS:
                if len(spec["options"].get(dim, [])) < 2:
                    continue
                row = self.store.db.execute(
                    f"SELECT * FROM experiment WHERE workflow_id=? AND dim=? AND state IN ({','.join('?' * len(OPEN))}) "
                    "ORDER BY experiment_id DESC LIMIT 1", (wid, dim, *OPEN)).fetchone()
                if row is None:
                    self._maybe_start(wid, spec, dim, minute, blocked_arms.get(wid, set()))
                else:
                    self._step(row, spec, minute, blocked_arms.get(wid, set()))

    def _maybe_start(self, wid: str, spec: dict, dim: str, minute: int, blocked: set[str]) -> None:
        if self.end_minute - minute < self.settings.min_days_left_to_start * 1440:
            return
        last = self.store.db.execute(
            "SELECT * FROM experiment WHERE workflow_id=? AND dim=? ORDER BY experiment_id DESC LIMIT 1",
            (wid, dim)).fetchone()
        excluded: set[str] = set(blocked)
        if last is not None:
            if last["outcome"] != "rolled_back":
                return  # a settled question stays settled, and a supervisor's choice is not re-litigated
            excluded |= set(json.loads(last["excluded"]))
        _, cfg = self.store.latest_config(wid)
        current = cfg["allocations"].get(DEFAULT, {}).get(dim, {})
        if not current:
            return
        champion = max(sorted(current), key=lambda k: current[k])
        if champion in excluded:
            return
        arms = [o for o in spec["options"][dim] if o not in excluded]
        if len(arms) < 2:
            return
        exp_id = self.store.db.execute(
            "INSERT INTO experiment(workflow_id,dim,metric,state,champion,arms,excluded,created_minute) "
            "VALUES(?,?,?,'queued',?,?,?,?)",
            (wid, dim, TARGET[dim], champion, dumps(arms), dumps(sorted(excluded)), minute)).lastrowid
        self._queue_start(exp_id, wid, spec, dim, champion, arms, current, minute)

    def _start_dist(self, dim: str, champion: str, arms: list[str], current: dict) -> dict:
        s = self.settings
        if dim == "caller_id":
            # Operational and hard to reverse in the customer's eyes: keep incumbents, probe the rest.
            total = sum(current.values()) or 1.0
            live = {k: v / total for k, v in current.items() if k in arms and v > 0}
            new = [a for a in arms if a not in live]
            probe = 0.10
            keep = max(0.0, 1 - probe * len(new))
            dist = {k: round(v * keep, 4) for k, v in live.items()}
            dist.update({a: probe for a in new})
            return dist
        others = [a for a in arms if a != champion]
        share = (1 - s.control_share) / len(others)
        return {champion: s.control_share, **{a: round(share, 4) for a in others}}

    def _queue_start(self, exp_id: int, wid: str, spec: dict, dim: str, champion: str, arms: list[str], current: dict,
                     minute: int) -> None:
        dist = self._start_dist(dim, champion, arms, current)
        base = history_baseline(self.store, wid)
        metric = TARGET[dim]
        rate0 = base["pickup"] if metric == "pickup_rate" else base["engagement"]
        daily = base["dials_per_day"] if metric == "pickup_rate" else base["connected_per_day"]
        min_share = min(dist.values())
        n_5pp = stats.samples_per_arm(rate0, 0.05)
        exposed = 1 - dist.get(champion, 0)
        dry = {"type": "exposure_estimate",
               "note": "These arms have no history, so no outcome can be replayed. This is the traffic at risk.",
               "baseline_rate": round(rate0, 4), "trials_per_day": round(daily),
               "share_on_new_arms": round(exposed, 3),
               "days_to_detect_5pp_at_80pct_power": round(n_5pp / max(daily * min_share, 1), 1)}
        item = self.gov.queue(
            wid, f"experiment:{exp_id}", [{"op": "alloc", "cohort": DEFAULT, "dim": dim, "dist": dist}],
            f"Test {dim} options {sorted(dist)} ({round(dist.get(champion, 0) * 100)}% stays on {champion})",
            f"No evidence exists for {dim} alternatives: history ran only the default. Measures {metric} with the "
            "other KPI as a guardrail. Arms falling behind are removed at a daily review.",
            {"experiment_id": exp_id, "design": dist, "metric": metric, "max_looks": self.settings.max_looks},
            dry, minute, affected_leads=round(base["leads_per_day"] * exposed))
        self.store.db.execute("UPDATE experiment SET pending_item=? WHERE experiment_id=?", (item, exp_id))

    def _set(self, exp_id: int, **cols) -> None:
        sets = ",".join(f"{k}=?" for k in cols)
        self.store.db.execute(f"UPDATE experiment SET {sets} WHERE experiment_id=?", (*cols.values(), exp_id))

    def _supervisor_touched(self, row, dim: str) -> int | None:
        scopes = json.loads(row["scopes"])
        seen = scopes.get("_seen_version", row["start_version"] or 0)
        for r in self.store.db.execute(
                "SELECT version,changed_dims FROM config_version WHERE workflow_id=? AND actor='supervisor' AND version>? "
                "ORDER BY version", (row["workflow_id"], seen)):
            if dim in json.loads(r["changed_dims"]):
                return r["version"]
        return None

    def _step(self, row, spec: dict, minute: int, blocked: set[str]) -> None:
        exp_id, wid, dim, state = row["experiment_id"], row["workflow_id"], row["dim"], row["state"]

        if row["pending_item"]:
            item = self.gov.item(row["pending_item"])
            if item["status"] in ("queued", "in_proposal"):
                return
            with self.store.tx():
                self._on_item_closed(row, item, spec, minute)
            return

        if state in ("running", "monitoring"):
            v = self._supervisor_touched(row, dim)
            if v is not None:
                with self.store.tx():
                    self._set(exp_id, state="concluded", outcome="supervisor_override", closed_minute=minute)
                    self.store.decide(minute, wid, "experiment_stopped",
                                      f"Supervisor changed {dim} in v{v}. Their choice stands, so this experiment ends; "
                                      "results so far remain on record but are not promoted.",
                                      {"version": v}, experiment_id=exp_id)
                return

        if state == "queued":
            # The start was refused, expired or outdated; rebuild it from the current config after a pause.
            item = self.store.db.execute("SELECT closed_minute FROM change_item WHERE source=? ORDER BY item_id DESC LIMIT 1",
                                         (f"experiment:{exp_id}",)).fetchone()
            wait = self.settings.apply_retry_cooldown_ticks * self.settings.tick_minutes
            if item and item["closed_minute"] is not None and minute - item["closed_minute"] < wait:
                return
            _, cfg = self.store.latest_config(wid)
            current = cfg["allocations"].get(DEFAULT, {}).get(dim, {})
            arms = [a for a in json.loads(row["arms"]) if a not in blocked]
            if len(arms) >= 2 and row["champion"] in arms and current:
                with self.store.tx():
                    self._queue_start(exp_id, wid, spec, dim, row["champion"], arms, current, minute)
            return

        retry_at = json.loads(row["scopes"]).get("_rollback_retry_at")
        if state == "monitoring" and retry_at is not None and minute >= retry_at:
            scopes = json.loads(row["scopes"])
            del scopes["_rollback_retry_at"]
            self._set(exp_id, scopes=dumps(scopes))
            self._watch_for_decay(self.store.db.execute("SELECT * FROM experiment WHERE experiment_id=?", (exp_id,)).fetchone(), spec, minute)
            return
        if not self._is_look_time(row, spec, minute):
            return
        if state == "running":
            self._look(row, spec, minute, blocked)
        if state == "monitoring":
            self._watch_for_decay(row, spec, minute)

    def _is_look_time(self, row, spec: dict, minute: int) -> bool:
        """One review a day, as the approver's day opens, so a result can be acted on the same day."""
        open_hour = spec["governance"]["approver_hours"][0]
        h = minute // 60
        if h % 24 != open_hour or minute % 60 != 0:
            return False
        return row["last_look_hour"] is None or row["last_look_hour"] < h

    def _on_item_closed(self, row, item, spec: dict, minute: int) -> None:
        exp_id, wid, dim, state = row["experiment_id"], row["workflow_id"], row["dim"], row["state"]
        applied = item["status"] == "applied"
        if state == "queued":
            if applied:
                scopes = {"_seen_version": item["applied_version"]}
                self._set(exp_id, state="running", start_hour=minute // 60, start_version=item["applied_version"],
                          pending_item=None, scopes=dumps(scopes))
                self.store.decide(minute, wid, "experiment_started", f"{dim} experiment live in v{item['applied_version']}.",
                                  {"item": item["item_id"]}, experiment_id=exp_id)
            else:
                self._set(exp_id, pending_item=None)
                self.store.decide(minute, wid, "experiment_start_retry",
                                  f"Start was not applied ({item['status_reason']}); will rebuild from fresh config.",
                                  {}, experiment_id=exp_id)
        elif state == "running":  # an arm-removal or cohort promotion finished
            scopes = json.loads(row["scopes"])
            if applied:
                scopes["_seen_version"] = item["applied_version"]
                scopes.update(json.loads(item["evidence"]).get("activate_scopes", {}))
                arms = json.loads(item["evidence"]).get("arms_after")
                self._set(exp_id, pending_item=None, scopes=dumps(scopes), **({"arms": dumps(arms)} if arms else {}))
            else:
                self._set(exp_id, pending_item=None)
        elif state == "promoting":
            scopes = json.loads(row["scopes"])
            if applied:
                ev = json.loads(item["evidence"])
                scopes.update(ev.get("activate_scopes", {}))
                scopes["_seen_version"] = item["applied_version"]
                for s, val in ev.get("activate_scopes", {}).items():
                    val["since_hour"] = minute // 60
                    val["version"] = item["applied_version"]
                self._set(exp_id, state="monitoring", pending_item=None, scopes=dumps(scopes))
                self.store.decide(minute, wid, "winner_promoted",
                                  f"Promoted in v{item['applied_version']}, keeping a holdout to detect decay.",
                                  ev.get("activate_scopes", {}), experiment_id=exp_id)
            else:
                # Back to running: the next daily look re-decides on fresh data.
                self._set(exp_id, state="running", pending_item=None)
                self.store.decide(minute, wid, "promotion_retry", f"Not applied: {item['status_reason']}", {}, experiment_id=exp_id)
        elif state == "rolling_back":
            if applied:
                ev = json.loads(item["evidence"])
                excluded = sorted(set(json.loads(row["excluded"])) | set(ev.get("decayed", [])))
                self._set(exp_id, state="concluded", outcome="rolled_back", excluded=dumps(excluded), pending_item=None,
                          closed_minute=minute)
                self.store.decide(minute, wid, "rollback_applied", f"Rolled back in v{item['applied_version']}.", ev,
                                  experiment_id=exp_id)
            else:
                # Customers are on a variant known to be failing, so this does not wait for tomorrow's review.
                scopes = json.loads(row["scopes"])
                scopes["_rollback_retry_at"] = minute + 60
                self._set(exp_id, state="monitoring", pending_item=None, scopes=dumps(scopes))
                self.store.decide(minute, wid, "rollback_retry", f"Not applied: {item['status_reason']}. Re-checking in an hour.", {}, experiment_id=exp_id)

    # ---- the daily look
    def _guardrail_ok(self, wid: str, spec: dict, row, arm: str, control: str, segments: set[int] | None) -> dict:
        metric = row["metric"]
        other = "engagement_rate" if metric == "pickup_rate" else "pickup_rate"
        strata = self._strata(wid, row["dim"], other, row["start_hour"], row["start_version"], segments)
        d = self._diff(strata, arm, control)
        margin = guard_margin(spec, metric)
        # Non-inferiority with 80% confidence: a smaller share of traffic could never satisfy 95% inside a
        # scenario. Three outcomes, because "not proven safe yet" is different from "shown to do harm".
        lower = d.lower(self.settings.guard_confidence_z)
        harmful = d.estimate <= -margin and d.z <= -2.0
        return {"kpi": other, "margin": margin, "lower_bound": round(lower, 5), "ok": lower > -margin, "harmful": harmful, **d.as_dict()}

    def _interaction_ok(self, row, winner: str, control: str) -> tuple[bool, dict]:
        """The winner must not lose badly when paired with some other dimension's arm that is live."""
        partner = {"welcome_message": "voice", "voice": "welcome_message"}.get(row["dim"])
        if not partner:
            return True, {}
        strata = self._strata(row["workflow_id"], row["dim"], row["metric"], row["start_hour"], row["start_version"],
                              extra_col=partner)
        by_arm: dict[str, list] = defaultdict(list)
        for key, arms in strata.items():
            a, b = arms.get(winner), arms.get(control)
            if a and b:
                by_arm[key[-1]].append((a[0], a[1], b[0], b[1]))
        out, ok = {}, True
        for arm, rows in sorted(by_arm.items()):
            d = stats.stratified_diff(rows, self.settings.design_effect)
            out[arm] = d.as_dict()
            if d.na >= 100 and d.nb >= 100 and d.z <= -2.5:
                ok = False
        return ok, out

    def _look(self, row, spec: dict, minute: int, blocked: set[str]) -> None:
        s = self.settings
        exp_id, wid, dim, metric = row["experiment_id"], row["workflow_id"], row["dim"], row["metric"]
        champion = row["champion"]
        arms = [a for a in json.loads(row["arms"]) if a != champion]
        scopes = json.loads(row["scopes"])
        strata = self._strata(wid, dim, metric, row["start_hour"], row["start_version"])
        diffs = {a: self._diff(strata, a, champion) for a in arms}
        ready = {a: d for a, d in diffs.items() if d.na >= s.min_arm_samples and d.nb >= s.min_arm_samples}
        looks = row["looks"] + (1 if ready else 0)
        evidence: dict = {"look": looks, "metric": metric, "champion": champion, "arms": {a: d.as_dict() for a, d in diffs.items()}}
        self._set(exp_id, looks=looks, last_look_hour=minute // 60)
        if not ready:
            self.store.decide(minute, wid, "experiment_look", f"{dim}: not enough samples yet.", evidence, experiment_id=exp_id)
            return

        boundary = stats.critical_z(s.alpha_family / max(1, len(ready)), s.max_looks)
        evidence["first_step_boundary_z"] = round(boundary, 3)
        winners = stats.holm_winners({a: d.z for a, d in ready.items()}, s.alpha_family, s.max_looks)
        winners = {a for a in winners if ready[a].estimate >= s.min_effect}
        evidence["significant"] = sorted(winners)

        harmful_guard = []
        for a in sorted(winners, key=lambda x: -ready[x].estimate):
            g = self._guardrail_ok(wid, spec, row, a, champion, None)
            ok_i, inter = self._interaction_ok(row, a, champion)
            evidence.setdefault("guardrail", {})[a] = g
            evidence.setdefault("interaction", {})[a] = inter
            if g["ok"] and ok_i:
                self._promote_all(row, spec, a, ready[a], evidence, minute, blocked)
                return
            if g["harmful"]:
                harmful_guard.append(a)
        evidence["waiting_on_guardrail"] = sorted(winners - set(harmful_guard))
        evidence["guardrail_harm"] = harmful_guard

        cohort_item = self._cohort_winners(row, spec, arms, evidence, minute, scopes)
        if cohort_item:
            return

        losers = [a for a, d in ready.items() if d.z <= s.drop_arm_z and d.estimate <= -s.min_effect] + \
                 [a for a in arms if a in blocked] + harmful_guard
        remaining = [a for a in arms if a not in losers]
        out_of_looks = looks >= s.max_looks or self.end_minute - minute < 1440
        if not remaining or out_of_looks:
            self._conclude_no_winner(row, spec, diffs, blocked, evidence, minute)
            return
        if losers:
            self._remove_arms(row, spec, remaining, sorted(set(losers)), blocked, evidence, minute)
            return
        self.store.decide(minute, wid, "experiment_look", f"{dim}: no decision at look {looks}; continuing.", evidence, experiment_id=exp_id)

    def _expected_rate_dry_run(self, row, winner: str, d: stats.Diff) -> dict:
        strata = self._strata(row["workflow_id"], row["dim"], row["metric"], row["start_hour"], row["start_version"])
        tot: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for arms in strata.values():
            for a, (x, n) in arms.items():
                tot[a][0] += x
                tot[a][1] += n
        return {"type": "replay_of_observed_arms", "observed": {a: {"rate": round(rate(*v), 4), "n": v[1]} for a, v in tot.items()},
                "estimated_lift_vs_champion": round(d.estimate, 4), "lift_95pct_low": round(d.lower(1.96), 4),
                "lift_95pct_high": round(d.lower(-1.96), 4), "note": "Concurrent arms under the same configs; not a forecast."}

    def _holdout_dist(self, winner: str, row, blocked: set[str]) -> tuple[dict, str | None]:
        """Winner plus a holdout to compare against. Never holds traffic on a number already flagged."""
        s = self.settings
        options = [row["champion"]] + [a for a in json.loads(row["arms"]) if a != row["champion"]]
        holdout = next((a for a in options if a != winner and a not in blocked), None)
        if holdout is None:
            return {winner: 1.0}, None
        return {winner: round(1 - s.holdout_share, 4), holdout: s.holdout_share}, holdout

    def _promote_all(self, row, spec: dict, winner: str, d: stats.Diff, evidence: dict, minute: int, blocked: set[str]) -> None:
        s = self.settings
        exp_id, wid, dim, champion = row["experiment_id"], row["workflow_id"], row["dim"], row["champion"]
        base = history_baseline(self.store, wid)
        # An improvement that hurts a cohort is worth less than nothing, so such a cohort keeps its champion.
        ops, harmed = [], {}
        _, cfg = self.store.latest_config(wid)
        live = {c["id"] for c in cfg["cohorts"]}
        for c in self.store.cohorts(wid):
            if c["id"] not in live:
                continue
            strata = self._strata(wid, dim, row["metric"], row["start_hour"], row["start_version"], self._segments_of(wid, c["id"]))
            cd = self._diff(strata, winner, champion)
            if cd.z <= -2.0 and cd.estimate <= -s.min_effect:
                harmed[c["id"]] = cd.as_dict()
                ops.append({"op": "alloc", "cohort": c["id"], "dim": dim, "dist": {champion: 1.0}})
        dist, holdout = self._holdout_dist(winner, row, blocked)
        ops.insert(0, {"op": "alloc", "cohort": DEFAULT, "dim": dim, "dist": dist})
        evidence["harmed_cohorts_kept_on_champion"] = harmed
        daily = base["connected_per_day"] if row["metric"] == "engagement_rate" else base["dials_per_day"]
        gain = max(d.estimate, 0) * daily * (base["engagement"] if row["metric"] == "pickup_rate" else 1.0)
        ev = {**evidence, "activate_scopes": {ALL: {"winner": winner, "holdout": holdout}} if holdout else {}}
        item = self.gov.queue(
            wid, f"experiment:{exp_id}", ops,
            f"Promote {winner} for {dim} ({d.estimate * 100:+.1f} pts {row['metric']}), holdout {holdout}",
            f"{winner} beat {champion} at look {evidence['look']} (z={d.z:.2f}) past a boundary corrected for repeated looks "
            "and multiple arms, with the other KPI shown non-inferior.",
            ev, self._expected_rate_dry_run(row, winner, d), minute, expected_daily_gain=gain,
            affected_leads=round(base["leads_per_day"]))
        self._set(exp_id, state="promoting", pending_item=item)
        self.store.decide(minute, wid, "winner_selected", f"{dim}: {winner} beats {champion}.", evidence, experiment_id=exp_id, item_id=item)

    def _cohort_winners(self, row, spec: dict, arms: list[str], evidence: dict, minute: int, scopes: dict) -> bool:
        """Finds an arm that wins for one cohort though not overall. The family grows, so the bar rises."""
        s = self.settings
        exp_id, wid, dim, champion, metric = row["experiment_id"], row["workflow_id"], row["dim"], row["champion"], row["metric"]
        _, cfg = self.store.latest_config(wid)
        live = [c["id"] for c in cfg["cohorts"] if c["id"] in {x["id"] for x in self.store.cohorts(wid)}]
        if not live:
            return False
        ops, activate, gain, parts = [], {}, 0.0, []
        shares = cohort_shares(self.store, wid)
        base = history_baseline(self.store, wid)
        alpha = s.alpha_family / (len(live) + 1)
        for cid in live:
            if cid in scopes:
                continue
            strata = self._strata(wid, dim, metric, row["start_hour"], row["start_version"], self._segments_of(wid, cid))
            ds = {a: self._diff(strata, a, champion) for a in arms}
            ready = {a: d for a, d in ds.items() if d.na >= s.min_arm_samples and d.nb >= s.min_arm_samples}
            evidence.setdefault("cohorts", {})[cid] = {a: d.as_dict() for a, d in ds.items()}
            won = stats.holm_winners({a: d.z for a, d in ready.items()}, alpha, s.max_looks)
            won = [a for a in won if ready[a].estimate >= s.min_effect]
            for a in sorted(won, key=lambda x: -ready[x].estimate):
                g = self._guardrail_ok(wid, spec, row, a, champion, self._segments_of(wid, cid))
                if g["ok"]:
                    ops.append({"op": "alloc", "cohort": cid, "dim": dim,
                                "dist": {a: round(1 - s.holdout_share, 4), champion: s.holdout_share}})
                    activate[cid] = {"winner": a, "holdout": champion, "since_hour": minute // 60}
                    gain += ready[a].estimate * base["connected_per_day"] * shares.get(cid, 0)
                    parts.append(f"{a} for {cid} ({ready[a].estimate * 100:+.1f} pts)")
                    break
        if not ops:
            return False
        ev = {**evidence, "activate_scopes": activate}
        item = self.gov.queue(
            wid, f"experiment:{exp_id}", ops, f"{dim}: promote " + ", ".join(parts),
            "Arm wins within a cohort past a boundary corrected for repeated looks, arms and cohorts; other cohorts keep testing.",
            ev, {"type": "replay_of_observed_arms", "cohorts": evidence.get("cohorts")}, minute,
            expected_daily_gain=max(gain, 0), affected_leads=round(base["leads_per_day"] * sum(shares.get(c, 0) for c in activate)))
        self._set(exp_id, pending_item=item)
        self.store.decide(minute, wid, "cohort_winner_selected", ", ".join(parts), evidence, experiment_id=exp_id, item_id=item)
        return True

    def _remove_arms(self, row, spec: dict, remaining: list[str], losers: list[str], blocked: set[str], evidence: dict, minute: int) -> None:
        exp_id, wid, dim, champion = row["experiment_id"], row["workflow_id"], row["dim"], row["champion"]
        arms_after = [champion] + remaining
        if champion in blocked:  # a flagged caller ID stays a reference in the data but gets no new traffic
            dist = {a: round(1 / len(remaining), 4) for a in remaining}
        else:
            share = (1 - self.settings.control_share) / max(len(remaining), 1)
            dist = {champion: self.settings.control_share, **{a: round(share, 4) for a in remaining}}
        ev = {**evidence, "removed": losers, "arms_after": arms_after}
        item = self.gov.queue(wid, f"experiment:{exp_id}", [{"op": "alloc", "cohort": DEFAULT, "dim": dim, "dist": dist}],
                              f"Stop testing {losers} for {dim}: behind {champion}",
                              "Arm is significantly behind the champion or failed its guardrail; removing it limits exposure.",
                              ev, {"type": "replay_of_observed_arms", "arms": evidence["arms"]}, minute)
        self._set(exp_id, pending_item=item)
        self.store.decide(minute, wid, "arm_removed", f"{dim}: removing {losers}.", evidence, experiment_id=exp_id, item_id=item)

    def _conclude_no_winner(self, row, spec: dict, diffs: dict, blocked: set[str], evidence: dict, minute: int) -> None:
        """Keeps the champion and any arm not behind it; nothing is declared a winner.

        Reverting everything to the champion would be a change with no evidence behind it, and it would
        move customers off arms that were ahead. Arms behind the champion leave; the rest keep their
        traffic, and the decision log says plainly that nothing was proven.
        """
        exp_id, wid, dim, champion = row["experiment_id"], row["workflow_id"], row["dim"], row["champion"]
        keep = [a for a, d in diffs.items() if d.estimate >= 0 and a not in blocked]
        pool = keep + ([champion] if champion not in blocked else [])
        _, cfg = self.store.latest_config(wid)
        current = cfg["allocations"].get(DEFAULT, {}).get(dim, {})
        live = {a: current[a] for a in pool if current.get(a, 0) > 0} or {a: 1.0 for a in pool}
        total = sum(live.values()) or 1.0
        dist = {a: round(v / total, 4) for a, v in live.items()}
        if dist and dist != current:
            self.gov.queue(wid, f"experiment:{exp_id}", [{"op": "alloc", "cohort": DEFAULT, "dim": dim, "dist": dist}],
                           f"Close {dim} test without a winner: remove arms behind {champion}",
                           "No arm cleared the bar within the planned looks, so none is promoted. Arms behind the champion stop receiving traffic.",
                           evidence, {"type": "replay_of_observed_arms", "arms": evidence["arms"]}, minute)
        has_cohort_scopes = any(not k.startswith("_") for k in json.loads(row["scopes"]))
        if has_cohort_scopes:
            self._set(exp_id, state="monitoring")
        else:
            self._set(exp_id, state="concluded", outcome="no_winner", closed_minute=minute)
        self.store.decide(minute, wid, "experiment_concluded", f"{dim}: no winner after {evidence['look']} looks; keeping {sorted(dist)}.", evidence, experiment_id=exp_id)

    # ---- decay
    def _watch_for_decay(self, row, spec: dict, minute: int) -> None:
        exp_id, wid, dim, metric = row["experiment_id"], row["workflow_id"], row["dim"], row["metric"]
        self._set(exp_id, last_look_hour=minute // 60)
        scopes = {k: v for k, v in json.loads(row["scopes"]).items() if not k.startswith("_")}
        ops, decayed, findings = [], [], {}
        for scope, info in scopes.items():
            s = self.settings
            segs = self._segments_of(wid, scope)
            since = max(info.get("since_hour", 0), minute // 60 - 48)
            d = self._diff(self._strata(wid, dim, metric, since, row["start_version"] or 0, segs), info["winner"], info["holdout"])
            # The holdout gets little traffic, so its recent rate is noisy. Its rate over the whole run has
            # far more samples. That comparison is open to drift, so it only counts if the concurrent one agrees.
            recent = self._arm_totals(wid, dim, metric, minute // 60 - 24, segs).get(info["winner"], [0, 0])
            pooled = self._arm_totals(wid, dim, metric, row["start_hour"], segs).get(info["holdout"], [0, 0])
            long_z = stats.two_sample_z(recent[0], recent[1], pooled[0], pooled[1])
            findings[scope] = {"winner": info["winner"], "holdout": info["holdout"], **d.as_dict(),
                               "winner_last_24h_vs_holdout_all_run_z": round(long_z, 3)}
            concurrent_says_decayed = d.nb >= 100 and d.z <= s.decay_z
            both_agree = d.nb >= 100 and d.z <= -1.0 and recent[1] >= 200 and long_z <= s.decay_long_z
            if concurrent_says_decayed or both_agree:
                cohort = DEFAULT if scope == ALL else scope
                ops.append({"op": "alloc", "cohort": cohort, "dim": dim, "dist": {info["holdout"]: 1.0}})
                decayed.append(info["winner"])
        if not ops:
            self.store.decide(minute, wid, "decay_check", f"{dim}: winner still holding.", findings, experiment_id=exp_id)
            return
        item = self.gov.queue(wid, f"experiment:{exp_id}", ops, f"Roll back {dim}: {sorted(set(decayed))} now behind holdout",
                              "The promoted variant is significantly behind the concurrent holdout over the last 48 hours.",
                              {"decayed": sorted(set(decayed)), "findings": findings},
                              {"type": "replay_of_observed_arms", "findings": findings}, minute, emergency=True,
                              affected_leads=round(history_baseline(self.store, wid)["leads_per_day"]))
        self._set(exp_id, state="rolling_back", pending_item=item)
        self.store.decide(minute, wid, "decay_detected", f"{dim}: {sorted(set(decayed))} decayed; rolling back.", findings, experiment_id=exp_id, item_id=item)
