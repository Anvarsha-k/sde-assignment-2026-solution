"""The one shared cohort object, and every KPI read.

A cohort is a rule over lead features, in the simulator's own grammar. Detection, experiments, dry runs
and the supervisor view all map segments to cohorts through segment_to_cohort, so they cannot disagree.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from .settings import Settings
from .store import Store, dumps

DEFAULT = "default"


def matches(where: dict, features: dict) -> bool:
    """AND across features, OR within a feature's list: the same rule the simulator documents."""
    return all(features.get(k) in vals for k, vals in where.items())


def segment_to_cohort(segments: dict[int, dict], cohorts: list[dict]) -> dict[int, str]:
    """First match wins, in order, otherwise default: the simulator's evaluation order."""
    out = {}
    for sid, feats in segments.items():
        out[sid] = next((c["id"] for c in cohorts if matches(c["where"], feats)), DEFAULT)
    return out


def rate(x: int, n: int) -> float:
    return x / n if n else 0.0


def cube_rows(store: Store, workflow_id: str, group: list[str], since_hour: int | None = None,
              until_hour: int | None = None, where: str = "", params: tuple = ()) -> list:
    """Sums the cube by the given columns. Column names come from code only, never from input."""
    cols = ",".join(group)
    q = (f"SELECT {cols}{',' if cols else ''} SUM(dials) dials, SUM(connected) connected, SUM(engaged) engaged, "
         f"SUM(hallucinations) hall FROM kpi_cube WHERE workflow_id=?")
    args: list = [workflow_id]
    if since_hour is not None:
        q += " AND hour_idx>=?"
        args.append(since_hour)
    if until_hour is not None:
        q += " AND hour_idx<?"
        args.append(until_hour)
    if where:
        q += " AND " + where
        args.extend(params)
    if cols:
        q += " GROUP BY " + cols
    return store.db.execute(q, args).fetchall()


def history_baseline(store: Store, workflow_id: str) -> dict:
    """KPIs from before D0. Falls back to all data if a scenario has no history."""
    r = cube_rows(store, workflow_id, [], until_hour=0)[0]
    if not r["dials"]:
        r = cube_rows(store, workflow_id, [])[0]
    dials, con, eng, hall = r["dials"] or 0, r["connected"] or 0, r["engaged"] or 0, r["hall"] or 0
    by_hour = {x["hour"]: rate(x["connected"], x["dials"]) for x in store.db.execute(
        "SELECT ((hour_idx%24)+24)%24 hour, SUM(dials) dials, SUM(connected) connected FROM kpi_cube "
        "WHERE workflow_id=? AND hour_idx<0 GROUP BY 1", (workflow_id,))}
    days = store.db.execute("SELECT COUNT(DISTINCT (hour_idx+100000)/24) FROM kpi_cube WHERE workflow_id=? AND hour_idx<0",
                            (workflow_id,)).fetchone()[0] or 1
    leads = store.db.execute("SELECT AVG(leads) FROM campaign WHERE workflow_id=?", (workflow_id,)).fetchone()[0] or 0
    return {"dials": dials, "connected": con, "pickup": rate(con, dials), "engagement": rate(eng, con),
            "hall": rate(hall, con), "pickup_by_hour": by_hour, "dials_per_day": dials / days,
            "connected_per_day": con / days, "leads_per_day": leads}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "x"


def choose_timing_cohorts(store: Store, workflow_id: str, settings: Settings) -> tuple[list[dict], dict]:
    """Picks the single feature whose values differ most in *when* they answer.

    The statistic is chi-square for the feature x hour interaction in pickup. Using one feature keeps
    cohorts large; crossing features would fragment traffic and starve every experiment of samples.
    Values under cohort_min_share are too small to test and stay in default, as does the largest value.
    """
    segments = store.segments()
    cells: dict[tuple[str, str, int], list[int]] = defaultdict(lambda: [0, 0])
    rows = store.db.execute(
        "SELECT segment_id, ((hour_idx%24)+24)%24 hour, SUM(dials) d, SUM(connected) c FROM kpi_cube "
        "WHERE workflow_id=? AND hour_idx<0 GROUP BY 1,2", (workflow_id,)).fetchall()
    for r in rows:
        for feat, val in segments.get(r["segment_id"], {}).items():
            cell = cells[(feat, val, r["hour"])]
            cell[0] += r["d"]
            cell[1] += r["c"]
    report: dict[str, dict] = {}
    for feat in sorted({k[0] for k in cells}):
        fc = {k: v for k, v in cells.items() if k[0] == feat}
        vals, hours = sorted({k[1] for k in fc}), sorted({k[2] for k in fc})
        tot_n = sum(v[0] for v in fc.values())
        tot_x = sum(v[1] for v in fc.values())
        if not tot_n or not 0 < tot_x < tot_n:
            continue
        p = tot_x / tot_n
        vn = {x: sum(v[0] for k, v in fc.items() if k[1] == x) for x in vals}
        vx = {x: sum(v[1] for k, v in fc.items() if k[1] == x) for x in vals}
        hn = {h: sum(v[0] for k, v in fc.items() if k[2] == h) for h in hours}
        hx = {h: sum(v[1] for k, v in fc.items() if k[2] == h) for h in hours}
        chi = 0.0
        for (_, x, h), (n, c) in fc.items():
            if n:
                resid = c / n - rate(vx[x], vn[x]) - rate(hx[h], hn[h]) + p
                chi += n * resid * resid / (p * (1 - p))
        df = max(1, (len(vals) - 1) * (len(hours) - 1))
        report[feat] = {"chi2_per_df": round(chi / df, 3), "shares": {x: round(vn[x] / tot_n, 3) for x in vals}}
    if not report:
        return [], {"reason": "no history"}
    best = max(sorted(report), key=lambda f: report[f]["chi2_per_df"])
    if report[best]["chi2_per_df"] < 2.0:
        return [], {"reason": "no feature shows a time-of-day pattern above noise", "features": report}
    shares = report[best]["shares"]
    keep = [v for v, s in sorted(shares.items(), key=lambda kv: -kv[1]) if s >= settings.cohort_min_share]
    named = keep[1:]  # the largest value stays in default
    cohorts = [{"id": f"{_slug(best)}_{_slug(v)}", "where": {best: [v]}} for v in named]
    return cohorts, {"feature": best, "features": report}


def register(store: Store, workflow_id: str, cohorts: list[dict], rationale: str, minute: int) -> None:
    for i, c in enumerate(cohorts):
        store.db.execute(
            "INSERT OR IGNORE INTO cohort(workflow_id,cohort_id,ordinal,where_clause,rationale,created_minute) "
            "VALUES(?,?,?,?,?,?)", (workflow_id, c["id"], i, dumps(c["where"]), rationale, minute))


def cohort_shares(store: Store, workflow_id: str) -> dict[str, float]:
    """Each cohort's share of dials in history, used for honest affected-lead estimates."""
    seg_map = segment_to_cohort(store.segments(), store.cohorts(workflow_id))
    tot: dict[str, int] = defaultdict(int)
    for r in cube_rows(store, workflow_id, ["segment_id"], until_hour=0):
        tot[seg_map.get(r["segment_id"], DEFAULT)] += r["dials"]
    n = sum(tot.values()) or 1
    return {k: v / n for k, v in tot.items()}


def effective_dist(config: dict, cohort: str, dim: str) -> dict[str, float]:
    alloc = config.get("allocations", {})
    return alloc.get(cohort, {}).get(dim) or alloc.get(DEFAULT, {}).get(dim) or {}


def loads(s: str):
    return json.loads(s)
