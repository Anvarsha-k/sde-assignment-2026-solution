"""Answers questions from the controller's own database. It never contacts the simulator.

    python -m controller.report --db state/x.db why --workflow ks_cancel --at D3T14:00
    python -m controller.report --db state/x.db status            # supervisor's one screen
    python -m controller.report --db state/x.db heard --person ks_cancel:p0004567 --day 3
    python -m controller.report --db state/x.db health            # on-call view of the loop itself
    python -m controller.report --db state/x.db changes           # weekly summary: everything that changed
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys


def when(minute: int | None) -> str:
    if minute is None:
        return "-"
    return f"D{minute // 1440} {(minute % 1440) // 60:02d}:{minute % 60:02d}"


def parse_when(s: str) -> int:
    m = re.fullmatch(r"D(\d+)(?:[T ](\d{1,2}):(\d{2}))?", s)
    if not m:
        raise SystemExit("time must look like D3T14:00")
    return int(m[1]) * 1440 + int(m[2] or 0) * 60 + int(m[3] or 0)


def kpis(db, wid: str, start_h: int, end_h: int) -> dict:
    r = db.execute("SELECT SUM(dials) d, SUM(connected) c, SUM(engaged) e, SUM(hallucinations) h FROM kpi_cube "
                   "WHERE workflow_id=? AND hour_idx>=? AND hour_idx<?", (wid, start_h, end_h)).fetchone()
    d, c, e, h = r["d"] or 0, r["c"] or 0, r["e"] or 0, r["h"] or 0
    return {"dials": d, "pickup": round(c / d, 4) if d else None, "engagement": round(e / c, 4) if c else None,
            "flag_rate": round(h / c, 4) if c else None}


def cmd_why(db, a) -> None:
    """Why did this workflow change at this time, who approved it, what supported it, what happened next?"""
    t = parse_when(a.at)
    rows = db.execute("SELECT * FROM config_version WHERE workflow_id=? AND applied_minute IS NOT NULL ORDER BY "
                      "ABS(applied_minute-?) LIMIT 1", (a.workflow, t)).fetchall()
    if not rows:
        print("no config change on record for", a.workflow)
        return
    v = rows[0]
    print(f"{a.workflow} v{v['version']} applied {when(v['applied_minute'])}  actor={v['actor']}  changed={v['changed_dims']}")
    if v["actor"] == "supervisor":
        print("  A supervisor made this change directly; it did not pass through this controller.")
    p = db.execute("SELECT * FROM proposal WHERE workflow_id=? AND applied_version=?", (a.workflow, v["version"])).fetchone()
    if p:
        body = json.loads(p["body"])
        print(f"\nPROPOSAL {p['proposal_id']} via {p['path']}  approval_id={p['approval_id'] or 'none (policy-permitted auto-apply)'}")
        print("  who approved:", ("client approver, recorded as " + p["approval_id"] + " (the API does not expose a name)") if p["approval_id"]
              else "nobody needed to: the workflow's policy permits this dimension")
        if body.get("change_summary"):
            print("  summary:", body["change_summary"])
        print("  rationale:", body.get("rationale"))
        if body.get("impact_score") is not None:
            print("  impact score:", body["impact_score"], " affected leads:", body.get("affected_leads"))
        if body.get("diff"):
            print("  diff:", json.dumps(body["diff"])[:600])
        if body.get("dry_run"):
            print("  dry run:", json.dumps(body["dry_run"])[:600])
        print("\nLIFECYCLE")
        for r in db.execute("SELECT * FROM proposal_transition WHERE proposal_id=? ORDER BY id", (p["proposal_id"],)):
            print(f"  {when(r['minute'])}  {r['from_status'] or 'new'} -> {r['to_status']}  {r['note'] or ''}"[:200])
        print("\nEVIDENCE (decisions linked to the items in this proposal)")
        for it in db.execute("SELECT * FROM change_item WHERE proposal_id=?", (p["proposal_id"],)):
            print(f"  item {it['item_id']} from {it['source']}: {it['summary']}")
            m = re.fullmatch(r"(experiment|incident):(\d+)", it["source"])
            if m:
                for d in db.execute(f"SELECT * FROM decision WHERE {m[1]}_id=? AND minute<=? AND kind NOT IN ('decay_check') ORDER BY decision_id DESC LIMIT 4",
                                    (int(m[2]), v["applied_minute"])):
                    print(f"    {when(d['minute'])} {d['kind']}: {d['summary']}"[:220])
                    if d["kind"] in ("winner_selected", "incident_detected", "decay_detected"):
                        print("      data:", d["evidence"][:500])
    h = v["applied_minute"] // 60
    print("\nWHAT HAPPENED NEXT")
    print("  24h before:", kpis(db, a.workflow, h - 24, h))
    print("  24h after: ", kpis(db, a.workflow, h, h + 24))
    for d in db.execute("SELECT * FROM decision WHERE workflow_id=? AND minute>? AND kind IN ('decay_detected','rollback_applied','incident_detected','incident_resolved','fix_failed','schedule_verified','schedule_rollback','supervisor_edit','winner_promoted') ORDER BY decision_id LIMIT 8",
                        (a.workflow, v["applied_minute"])):
        print(f"  {when(d['minute'])} {d['kind']}: {d['summary']}"[:200])


def cmd_status(db, a) -> None:
    """One screen for a supervisor: what's testing, what's winning, what we need, what changed recently."""
    now = int(db.execute("SELECT COALESCE(MAX(minute),0) FROM decision").fetchone()[0])
    wfs = [a.workflow] if a.workflow else [r[0] for r in db.execute("SELECT workflow_id FROM workflow ORDER BY 1")]
    print(f"As of {when(now)}\n")
    # An engaged lead is done, so engaged calls on campaigns uploaded since D0 are leads concluded. One scan for all workflows.
    concluded = {r[0]: r[1] for r in db.execute(
        "SELECT c.workflow_id, COUNT(*) FROM call_log c JOIN campaign k USING (campaign_id) WHERE c.engaged=1 AND k.created_minute>=0 GROUP BY 1")}
    for wid in wfs:
        spec = json.loads(db.execute("SELECT spec FROM workflow WHERE workflow_id=?", (wid,)).fetchone()[0])
        print(f"=== {wid}   primary: {spec['objective']['primary']}")
        print("  last 24h:", kpis(db, wid, now // 60 - 24, now // 60 + 1), " (pickup and engagement are separate KPIs)")
        up = db.execute("SELECT COALESCE(SUM(leads),0) FROM campaign WHERE workflow_id=? AND created_minute>=0", (wid,)).fetchone()[0]
        done = concluded.get(wid, 0)
        print(f"  business outcome since D0: {done}/{up} leads concluded = {done / up:.3f}" if up else "  business outcome since D0: no campaigns yet")
        print("  CHANGES WAITING ON YOU, highest impact first:")
        pend = db.execute("SELECT * FROM proposal WHERE workflow_id=? AND status='pending'", (wid,)).fetchall()
        for p in sorted(pend, key=lambda x: -json.loads(x["body"]).get("impact_score", 0)):
            b = json.loads(p["body"])
            print(f"    [{b.get('impact_score')}] {b.get('change_summary')}  (sent {when(p['created_minute'])})"[:200])
        if not pend:
            print("    nothing")
        print("  BEING TESTED:")
        for e in db.execute("SELECT * FROM experiment WHERE workflow_id=? AND state IN ('queued','running')", (wid,)):
            arms = db.execute("SELECT evidence FROM decision WHERE experiment_id=? AND kind='experiment_look' ORDER BY decision_id DESC LIMIT 1", (e["experiment_id"],)).fetchone()
            lead = ""
            if arms:
                ev = json.loads(arms[0]).get("arms", {})
                if ev:
                    top = max(ev, key=lambda k: ev[k]["estimate"])
                    lead = f"; leading: {top} ({ev[top]['estimate'] * 100:+.1f} pts vs {e['champion']}, not yet proven)"
            print(f"    {e['dim']}: {json.loads(e['arms'])}{lead}")
        print("  LIVE WINNERS:")
        for e in db.execute("SELECT * FROM experiment WHERE workflow_id=? AND state='monitoring'", (wid,)):
            for scope, info in json.loads(e["scopes"]).items():
                if not scope.startswith("_"):
                    print(f"    {e['dim']}/{scope}: {info['winner']} (holdout {info['holdout']})")
        print("  CHANGED IN THE LAST 24H:")
        for v in db.execute("SELECT * FROM config_version WHERE workflow_id=? AND applied_minute>? ORDER BY version", (wid, now - 1440)):
            print(f"    {when(v['applied_minute'])} v{v['version']} by {v['actor']}: {v['changed_dims']}")
        inc = db.execute("SELECT * FROM incident WHERE workflow_id=? AND state IN ('open','fixing','verifying','failed')", (wid,)).fetchall()
        for i in inc:
            print(f"  OPEN INCIDENT: {i['kind']} {i['subject']} since {when(i['detected_minute'])} ({i['state']})")
        print()


def cmd_heard(db, a) -> None:
    """An auditor's question: what did this person hear that day, and why was that variant live?"""
    q = "SELECT * FROM call_log WHERE person_id=?"
    args: list = [a.person]
    if a.day is not None:
        q += " AND minute>=? AND minute<?"
        args += [a.day * 1440, (a.day + 1) * 1440]
    rows = db.execute(q + " ORDER BY minute", args).fetchall()
    print(f"{len(rows)} calls")
    for r in rows:
        print(f"{when(r['minute'])} attempt {r['attempt_no']} cohort={r['cohort_id']} welcome={r['welcome_message']} voice={r['voice']} "
              f"caller_id={r['caller_id']} prompt={r['prompt_version']} config=v{r['config_version']} connected={r['connected']} engaged={r['engaged']}")
    for v in sorted({(r["workflow_id"], r["config_version"]) for r in rows}):
        c = db.execute("SELECT actor,proposal_id,approval_id,applied_minute FROM config_version WHERE workflow_id=? AND version=?", v).fetchone()
        if c:
            print(f"\nv{v[1]} live because: actor={c['actor']} proposal={c['proposal_id']} approval={c['approval_id']} applied={when(c['applied_minute'])}. "
                  f"Run: why --workflow {v[0]} --at D{(c['applied_minute'] or 0) // 1440}T{((c['applied_minute'] or 0) % 1440) // 60}:{(c['applied_minute'] or 0) % 60:02d}")


def cmd_health(db, a) -> None:
    """The on-call view: is the loop healthy, and is anything needing a human?"""
    agg = db.execute(
        "SELECT COUNT(*) ticks, SUM(events) ev, SUM(duplicates) dup, SUM(bad_events) bad, SUM(ingest_s) ing, SUM(decide_s) dec, SUM(advance_s) adv, "
        "MAX(rss_mb) rss, MAX(db_mb) dbm, MIN(credits_remaining) cred, MAX(http_retries) rtr, MAX(ingest_s+decide_s) worst, MAX(minute) last FROM loop_metric").fetchone()
    if not agg["ticks"]:
        print("no ticks recorded")
        return
    days = agg["ticks"] * 15 / 1440
    print(f"Simulated time reached: {when(agg['last'])}, {agg['ticks']} ticks")
    print(f"Events processed: {agg['ev']:,}  duplicates: {agg['dup']}  malformed: {agg['bad']}  HTTP retries: {agg['rtr']}")
    if agg["ing"]:
        print(f"Ingest: {agg['ev'] / agg['ing']:,.0f} events/s while ingesting")
    print(f"Wall time per simulated day: ingest {agg['ing'] / days:.1f}s, decide {agg['dec'] / days:.1f}s, simulator advance {agg['adv'] / days:.1f}s")
    print(f"Decision lag: 1 tick (15 sim minutes) by construction; worst wall time to process one tick: {agg['worst']:.2f}s")
    print(f"Peak memory: {agg['rss']:.0f} MB  DB size: {agg['dbm']:.0f} MB  Credits left: {agg['cred']}")
    print("\nDecisions per simulated day by kind:")
    for r in db.execute("SELECT kind, COUNT(*) n FROM decision GROUP BY 1 ORDER BY 2 DESC LIMIT 15"):
        print(f"  {r['kind']:<28}{r['n'] / days:8.1f}")
    print("\nProposals by status:", {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM proposal GROUP BY 1")})
    lat = db.execute("SELECT AVG(decided_minute-created_minute) FROM proposal WHERE decided_minute IS NOT NULL").fetchone()[0]
    print("Mean approval latency:", f"{lat:.0f} sim minutes" if lat else "n/a")
    print("Recovery events:", db.execute("SELECT COUNT(*) FROM decision WHERE kind='recovery'").fetchone()[0])
    alerts = db.execute("SELECT * FROM decision WHERE kind='alert' ORDER BY decision_id DESC LIMIT 10").fetchall()
    print("\nAlerts:" if alerts else "\nAlerts: none")
    for r in alerts:
        print(f"  {when(r['minute'])} {r['workflow_id']}: {r['summary']} {r['evidence']}"[:220])
    print("Open intents (must be 0 once a run ends):", db.execute("SELECT COUNT(*) FROM side_effect_intent WHERE status='started'").fetchone()[0])


def cmd_changes(db, a) -> None:
    """Weekly summary a compliance head asked for: what changed, by what route, and why."""
    q = "SELECT * FROM config_version WHERE applied_minute IS NOT NULL" + (" AND workflow_id=?" if a.workflow else "") + " ORDER BY applied_minute"
    for v in db.execute(q, (a.workflow,) if a.workflow else ()):
        p = db.execute("SELECT body FROM proposal WHERE workflow_id=? AND applied_version=?", (v["workflow_id"], v["version"])).fetchone()
        why = ""
        if p:
            b = json.loads(p[0])
            why = b.get("change_summary") or b.get("rationale") or ""
        print(f"{when(v['applied_minute'])} {v['workflow_id']} v{v['version']} {v['actor']} {v['changed_dims']} {why}"[:230])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("why")
    w.add_argument("--workflow", required=True)
    w.add_argument("--at", required=True)
    s = sub.add_parser("status")
    s.add_argument("--workflow")
    h = sub.add_parser("heard")
    h.add_argument("--person", required=True)
    h.add_argument("--day", type=int)
    sub.add_parser("health")
    c = sub.add_parser("changes")
    c.add_argument("--workflow")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    {"why": cmd_why, "status": cmd_status, "heard": cmd_heard, "health": cmd_health, "changes": cmd_changes}[a.cmd](db, a)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
