"""Per-cohort call timing learned from history, packaged with the cohorts it needs.

Weights decide who is dialled in an hour, not how many calls happen, so the business's window and capacity
stay untouched. No hour gets weight 0: the simulator would leave that capacity unused. Per-person timing is
deliberately absent; history has about one answered call per person, far too little to estimate a preference.
"""
from __future__ import annotations

import json
from collections import defaultdict

from . import stats
from .cohorts import DEFAULT, choose_timing_cohorts, cube_rows, history_baseline, rate, register, segment_to_cohort
from .experiments import guard_margin
from .governance import Governance
from .settings import Settings
from .store import Store, dumps


class Scheduler:
    def __init__(self, store: Store, gov: Governance, settings: Settings):
        self.store, self.gov, self.settings = store, gov, settings

    def plan(self, wid: str, spec: dict, cohorts: list[dict]) -> tuple[dict, dict]:
        """Returns hour tables per cohort and a dry run: reweighted history, measured on real outcomes."""
        s = self.settings
        seg_map = segment_to_cohort(self.store.segments(), cohorts)
        cell: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0, 0])
        rows = self.store.db.execute(
            "SELECT segment_id, ((hour_idx%24)+24)%24 hour, SUM(dials) d, SUM(connected) c, SUM(engaged) e FROM kpi_cube "
            "WHERE workflow_id=? AND hour_idx<0 GROUP BY 1,2", (wid,)).fetchall()
        for r in rows:
            k = (seg_map.get(r["segment_id"], DEFAULT), r["hour"])
            cell[k][0] += r["d"]
            cell[k][1] += r["c"]
            cell[k][2] += r["e"]
        start, end = spec["dialling_window"]
        hours = list(range(start, end))
        names = [DEFAULT] + [c["id"] for c in cohorts]
        total_n = sum(v[0] for v in cell.values()) or 1
        total_x = sum(v[1] for v in cell.values())
        p = total_x / total_n
        cn = {c: sum(cell[(c, h)][0] for h in hours) for c in names}
        cx = {c: sum(cell[(c, h)][1] for h in hours) for c in names}
        hn = {h: sum(cell[(c, h)][0] for c in names) for h in hours}
        hx = {h: sum(cell[(c, h)][1] for c in names) for h in hours}
        tables: dict[str, dict[str, float]] = {c: {} for c in names}
        prior_strength = 100
        for c in names:
            for h in hours:
                n, x, _ = cell[(c, h)]
                expected = rate(cx[c], cn[c]) * rate(hx[h], hn[h]) / p if p else 0
                # Shrinks thin cells to the no-interaction estimate, so noise cannot create a preference.
                est = (x + prior_strength * expected) / (n + prior_strength)
                w = (est / expected) ** s.schedule_gamma if expected else 1.0
                tables[c][str(h)] = round(min(max(w, s.schedule_min_weight), s.schedule_max_weight), 3)
        # Dry run: reweight each hour's history by the new weights and read the real pickup in those cells.
        now_x = new_x = new_n = now_e = new_e = now_c = new_c = 0.0
        for h in hours:
            weights = {c: cell[(c, h)][0] * tables[c][str(h)] for c in names}
            tot = sum(weights.values()) or 1
            for c in names:
                n, x, e = cell[(c, h)]
                if not n:
                    continue
                scaled = hn[h] * weights[c] / tot
                now_x += x
                new_x += scaled * x / n
                new_n += scaled
                now_e += e
                now_c += x
                new_e += scaled * e / n
                new_c += scaled * x / n
        dry = {"type": "historical_reweighting", "days_replayed": "all history",
               "pickup_now": round(rate(now_x, total_n), 4), "pickup_projected": round(rate(new_x, new_n), 4),
               "engagement_now": round(rate(now_e, now_c), 4), "engagement_projected": round(rate(new_e, new_c), 4),
               "note": "Assumes enough eligible leads of each cohort in each hour; real lift is usually smaller."}
        return tables, dry

    def tick(self, minute: int, specs: dict[str, dict], did_incident_open: set[str]) -> None:
        for wid, spec in specs.items():
            self._maybe_propose(wid, spec, minute)
            self._watch(wid, spec, minute, wid in did_incident_open)

    def _maybe_propose(self, wid: str, spec: dict, minute: int) -> None:
        last = self.store.db.execute("SELECT * FROM change_item WHERE workflow_id=? AND source='schedule' ORDER BY item_id DESC LIMIT 1",
                                     (wid,)).fetchone()
        if last is not None:
            if last["status"] in ("queued", "in_proposal", "applied"):
                return
            if minute - (last["closed_minute"] or minute) < self.settings.apply_retry_cooldown_ticks * self.settings.tick_minutes:
                return
        cohorts = self.store.cohorts(wid)
        if not cohorts and self.store.meta(f"cohorts_chosen:{wid}") is None:
            with self.store.tx():
                cohorts, report = choose_timing_cohorts(self.store, wid, self.settings)
                register(self.store, wid, cohorts, dumps(report), minute)
                self.store.set_meta(f"cohorts_chosen:{wid}", "1")
                self.store.decide(minute, wid, "cohorts_formed",
                                  f"Formed {[c['id'] for c in cohorts]} from history.", report)
        if not cohorts:
            return
        tables, dry = self.plan(wid, spec, cohorts)
        gain = dry["pickup_projected"] - dry["pickup_now"]
        margin = guard_margin(spec, "pickup_rate")
        eng_drop = dry["engagement_now"] - dry["engagement_projected"]
        if gain < self.settings.schedule_min_pickup_gain or eng_drop > margin / 2:
            if last is None:
                self.store.decide(minute, wid, "schedule_skipped", "Projected gain is too small or risks engagement.", dry)
                self.store.db.execute(
                    "INSERT INTO change_item(workflow_id,source,ops,expected_old,summary,rationale,evidence,dry_run,status,"
                    "status_reason,created_minute,closed_minute) VALUES(?,'schedule','[]','[]','none','none','{}','{}','applied','skipped',?,?)",
                    (wid, minute, minute))
            return
        base = history_baseline(self.store, wid)
        ops = [{"op": "cohorts", "add": cohorts}, {"op": "hours", "tables": tables}]
        self.gov.queue(
            wid, "schedule", ops,
            f"Call {len(cohorts)} cohorts at the hours they answer (projected pickup {dry['pickup_now']:.3f} to {dry['pickup_projected']:.3f})",
            "History shows cohorts answer at different hours. Weights change who is dialled when, not how many calls happen.",
            {"tables": tables}, dry, minute, expected_daily_gain=gain * base["dials_per_day"] * base["engagement"],
            affected_leads=round(base["leads_per_day"]))

    def _watch(self, wid: str, spec: dict, minute: int, did_incident: bool) -> None:
        item = self.store.db.execute(
            "SELECT * FROM change_item WHERE workflow_id=? AND source='schedule' AND status='applied' AND status_reason!='skipped' "
            "AND status_reason!='no-op' ORDER BY item_id DESC LIMIT 1", (wid,)).fetchone()
        if item is None:
            return
        w = self.store.db.execute("SELECT * FROM change_watch WHERE workflow_id=? ORDER BY watch_id DESC LIMIT 1", (wid,)).fetchone()
        if w is None:
            base = history_baseline(self.store, wid)
            prev = self.store.db.execute("SELECT config FROM config_version WHERE workflow_id=? AND version<? AND config IS NOT NULL "
                                         "ORDER BY version DESC LIMIT 1", (wid, item["applied_version"])).fetchone()
            undo = json.loads(prev["config"])["schedule"].get("hour_weights", {}) if prev else {}
            self.store.db.execute(
                "INSERT INTO change_watch(workflow_id,item_id,state,since_hour,baseline,undo_ops,created_minute) VALUES(?,?,'watching',?,?,?,?)",
                (wid, item["item_id"], minute // 60, dumps(base), dumps([{"op": "hours_restore", "tables": undo}]), minute))
            return
        if w["state"] == "rolling_back":
            it = self.gov.item(w["pending_item"])
            if it["status"] == "applied":
                self.store.db.execute("UPDATE change_watch SET state='rolled_back' WHERE watch_id=?", (w["watch_id"],))
            elif it["status"] == "dropped":
                self.store.db.execute("UPDATE change_watch SET state='watching',pending_item=NULL WHERE watch_id=?", (w["watch_id"],))
            return
        if w["state"] != "watching" or minute % 60 or (minute // 60) % 24 != spec["governance"]["approver_hours"][0]:
            return
        since = w["since_hour"]
        if minute // 60 - since < 24:
            return
        base = json.loads(w["baseline"])
        by_hour = {int(k): v for k, v in base["pickup_by_hour"].items()}
        exp = var = obs = n = 0.0
        for r in self.store.db.execute(
                "SELECT ((hour_idx%24)+24)%24 hour, SUM(dials) d, SUM(connected) c FROM kpi_cube WHERE workflow_id=? AND hour_idx>=? GROUP BY 1",
                (wid, since)):
            ph = by_hour.get(r["hour"], base["pickup"])
            exp += r["d"] * ph
            var += r["d"] * ph * (1 - ph)
            obs += r["c"]
            n += r["d"]
        c = cube_rows(self.store, wid, [], since_hour=since)[0]
        zp = (obs - exp) / var ** 0.5 if var else 0.0
        ze = stats.one_sample_z(c["engaged"] or 0, c["connected"] or 0, base["engagement"])
        drop_e = base["engagement"] - rate(c["engaged"] or 0, c["connected"] or 0)
        margin = guard_margin(spec, "pickup_rate")
        finding = {"pickup_vs_expected_z": round(zp, 2), "engagement_vs_history_z": round(ze, 2), "engagement_drop": round(drop_e, 4)}
        harmed = (zp <= -3.0 and not did_incident) or (ze <= -3.0 and drop_e > margin)
        if harmed:
            item_id = self.gov.queue(wid, f"schedule_rollback:{w['watch_id']}", json.loads(w["undo_ops"]),
                                     "Undo call-time change: KPIs fell after it", "Post-change KPIs are significantly below the pre-change baseline.",
                                     finding, {"type": "observed_after_change", **finding}, minute, emergency=True,
                                     affected_leads=round(base["leads_per_day"]))
            self.store.db.execute("UPDATE change_watch SET state='rolling_back',pending_item=? WHERE watch_id=?", (item_id, w["watch_id"]))
            self.store.decide(minute, wid, "schedule_rollback", "Call-time change appears harmful; undoing.", finding, item_id=item_id)
        elif minute // 60 - since >= 48:
            self.store.db.execute("UPDATE change_watch SET state='verified' WHERE watch_id=?", (w["watch_id"],))
            self.store.decide(minute, wid, "schedule_verified", "Call-time change verified over 2 days.", finding, item_id=item["item_id"])
