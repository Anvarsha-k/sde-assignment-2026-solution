#!/usr/bin/env python
"""Runs one scenario end to end: simulator, controller, optional kills, grade.

This is test tooling, not part of the controller. It starts the simulator and the controller as separate
processes, and the controller still knows only what HTTP tells it.

    python scripts/run_scenario.py --scenario scenarios/public_01_dev.yaml --name public_01
    python scripts/run_scenario.py --scenario scenarios/public_02_incidents.yaml --name crash_02 --kills 8 --seed 7
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CRASH_EXIT = 137


def wait_up(url: str) -> None:
    for _ in range(300):
        try:
            if httpx.get(url + "/health", timeout=2).status_code == 200:
                return
        except httpx.TransportError:
            time.sleep(0.2)
    raise SystemExit("simulator did not start")


def start_controller(url: str, state_dir: Path, log, crash_points: str = "", use_run_sh: bool = False) -> subprocess.Popen:
    env = {**os.environ, "CTRL_STATE_DIR": str(state_dir), "CTRL_CRASH_POINTS": crash_points}
    if use_run_sh:
        return subprocess.Popen(["bash", "./run.sh", url], cwd=ROOT, env=env, stdout=log, stderr=log)
    return subprocess.Popen([sys.executable, "-m", "controller", "--url", url], cwd=ROOT, env=env, stdout=log, stderr=log)


def sim_minute(url: str) -> int | None:
    try:
        return httpx.get(url + "/sim/state", timeout=10).json()["sim_minute"]
    except httpx.TransportError:
        return None


def audit(url: str, state_dir: Path) -> dict:
    """Compares the controller's database with the simulator's own record. Every entry must hold."""
    db = sqlite3.connect(next(state_dir.glob("*.db")))
    client = httpx.Client(base_url=url, timeout=60)
    workflows = [w["workflow_id"] for w in client.get("/workflows").json()]
    state = client.get("/sim/state").json()
    applied_twice, version_mismatch, unknown_proposals = [], [], []
    ours_by_pid = {r[0] for r in db.execute("SELECT proposal_id FROM proposal")}
    lost_pending = []
    for wid in workflows:
        hist = client.get(f"/workflows/{wid}/config/history").json()
        seen = [h["approval_id"] for h in hist if h.get("approval_id")]
        applied_twice += [a for a in set(seen) if seen.count(a) > 1]
        ours = {r[0]: r[1] for r in db.execute("SELECT version,actor FROM config_version WHERE workflow_id=?", (wid,))}
        theirs = {h["version"]: h["actor"] for h in hist}
        if ours != theirs:
            version_mismatch.append({"workflow": wid, "ours": ours, "theirs": theirs})
        unknown_proposals += [h["proposal_id"] for h in hist if h.get("proposal_id") and h["proposal_id"] not in ours_by_pid]
        for ap in client.get("/approvals", params={"workflow_id": wid}).json():
            row = db.execute("SELECT status FROM proposal WHERE approval_id=?", (ap["approval_id"],)).fetchone()
            if row is None:
                lost_pending.append(ap["approval_id"])
            elif ap["status"] == "applied" and row[0] != "applied":
                lost_pending.append({ap["approval_id"]: row[0]})
    cursor_event = db.execute("SELECT last_event_id FROM ingest_cursor").fetchone()[0]
    live_dials = db.execute("SELECT COALESCE(SUM(dials),0) FROM kpi_cube WHERE hour_idx>=0").fetchone()[0]
    logged_calls = db.execute("SELECT COUNT(*) FROM call_log").fetchone()[0]
    out = {
        "approvals_applied_twice": applied_twice,
        "config_versions_differ_from_simulator": version_mismatch,
        "applied_config_with_no_local_proposal": unknown_proposals,
        "approvals_lost_or_out_of_step": lost_pending,
        "unfinished_intents": db.execute("SELECT COUNT(*) FROM side_effect_intent WHERE status='started'").fetchone()[0],
        "events_behind_simulator": state["last_event_id"] - cursor_event,
        "cube_dials_minus_call_log_rows": live_dials - logged_calls,
        "recoveries_recorded": db.execute("SELECT COUNT(*) FROM decision WHERE kind='recovery'").fetchone()[0],
    }
    out["passed"] = not (applied_twice or version_mismatch or unknown_proposals or lost_pending or out["unfinished_intents"]
                         or out["events_behind_simulator"] or out["cube_dials_minus_call_log_rows"])
    db.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--baseline", help="baseline run dir; created if missing")
    ap.add_argument("--kills", type=int, default=0, help="SIGKILL the controller this many times at random moments")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--use-run-sh", action="store_true", help="start through ./run.sh, the graders' entry point (not with --kills)")
    ap.add_argument("--crash-points", default="", help="comma-separated faults.py points; each fires once")
    ap.add_argument("--out", help="grade report path")
    a = ap.parse_args()

    url = f"http://127.0.0.1:{a.port}"
    run_dir = ROOT / "runs" / a.name
    state_dir = ROOT / "state" / a.name
    shutil.rmtree(state_dir, ignore_errors=True)
    state_dir.mkdir(parents=True)
    baseline = Path(a.baseline) if a.baseline else ROOT / "runs" / ("base_" + Path(a.scenario).stem)
    if not baseline.exists():
        subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", a.scenario, "--run-dir", str(baseline)], cwd=ROOT, check=True)

    sim_log = open(ROOT / "runs" / f"{a.name}.sim.log", "w")
    sim = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", a.scenario, "--run-dir", str(run_dir),
                            "--port", str(a.port), "--force"], cwd=ROOT, stdout=sim_log, stderr=sim_log)
    log_path = ROOT / "runs" / f"{a.name}.controller.log"
    rng = random.Random(a.seed)
    kills: list[dict] = []
    t0 = time.perf_counter()
    try:
        wait_up(url)
        with open(log_path, "w") as log:
            proc = start_controller(url, state_dir, log, a.crash_points, a.use_run_sh)
            remaining = a.kills
            while True:
                if remaining > 0:
                    try:
                        proc.wait(timeout=rng.uniform(1.0, 8.0))
                        break
                    except subprocess.TimeoutExpired:
                        proc.kill()  # SIGKILL on Linux, TerminateProcess on Windows: no chance to clean up
                        proc.wait()
                        kills.append({"at_wall_s": round(time.perf_counter() - t0, 1), "sim_minute": sim_minute(url)})
                        remaining -= 1
                        proc = start_controller(url, state_dir, log, a.crash_points)
                else:
                    proc.wait()
                    if proc.returncode == CRASH_EXIT:  # a crash point fired: restart, as an orchestrator would
                        kills.append({"crash_point": True, "at_wall_s": round(time.perf_counter() - t0, 1), "sim_minute": sim_minute(url)})
                        proc = start_controller(url, state_dir, log, a.crash_points)
                        continue
                    break
        wall = time.perf_counter() - t0
        final = httpx.get(url + "/sim/state", timeout=30).json()
        checks = audit(url, state_dir)
    finally:
        sim.terminate()
        sim.wait(timeout=30)

    print(json.dumps({"name": a.name, "exit_code": proc.returncode, "wall_s": round(wall, 1), "finished": final["finished"],
                      "credits_remaining": final["credits_remaining"], "kills": kills, "audit": checks}, indent=1))
    out = a.out or str(ROOT / "runs" / f"{a.name}.report.md")
    subprocess.run([sys.executable, "-m", "sim", "grade", "--run", str(run_dir), "--baseline", str(baseline), "--out", out], cwd=ROOT, check=True,
                   env={**os.environ, "PYTHONUTF8": "1"}, stdout=subprocess.DEVNULL)
    print("report:", out)
    return proc.returncode or (0 if checks["passed"] else 1)


if __name__ == "__main__":
    sys.exit(main())
