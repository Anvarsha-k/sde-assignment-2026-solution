"""Controller against a real simulator process, through HTTP only.

Scenario files are used by this test tooling to launch the simulator, never by the controller.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from controller.client import SimClient, Unavailable
from controller.governance import Governance
from controller.ingest import Ingestor, SimClock
from controller.loop import open_store, sync_workflows
from controller.settings import Settings
from controller.store import Store

ROOT = Path(__file__).resolve().parent.parent
PORT = 8791
URL = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def sim(tmp_path):
    log = open(tmp_path / "sim.log", "w")
    p = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", "scenarios/public_01_dev.yaml", "--run-dir", str(tmp_path / "run"),
                          "--port", str(PORT), "--force"], cwd=ROOT, stdout=log, stderr=log)
    for _ in range(200):
        try:
            httpx.get(URL + "/health", timeout=1)
            break
        except httpx.TransportError:
            time.sleep(0.1)
    yield URL
    p.terminate()
    p.wait(timeout=20)
    log.close()


def _stack(tmp_path, name="a"):
    st = Settings(state_dir=str(tmp_path / name))
    client = SimClient(URL)
    state = client.state()
    store = open_store(st, state, client)
    gov = Governance(store, client, st)
    specs = sync_workflows(store, client, gov, state["sim_minute"])
    return store, client, gov, specs, Ingestor(store, client, SimClock("2027-01-04T00:00:00+05:30"), st), st


def _cube_totals(db):
    return tuple(db.execute("SELECT SUM(dials),SUM(connected),SUM(engaged),SUM(hallucinations) FROM kpi_cube").fetchone())


def test_replaying_a_chunk_changes_nothing(sim, tmp_path):
    store, client, gov, specs, ing, _ = _stack(tmp_path)
    ing.catch_up()
    before = _cube_totals(store.db)
    assert before[0] > 0
    # Force the same chunk through again, as a bug or retry might. The cursor must make it a no-op.
    chunk = client.list_chunks(after=-1)[0]
    from controller.ingest import IngestStats
    ing._ingest_chunk(chunk, IngestStats())
    assert _cube_totals(store.db) == before
    store.close()


def test_interrupted_chunk_leaves_no_partial_effects(sim, tmp_path):
    store, client, gov, specs, ing, _ = _stack(tmp_path)
    chunk = client.list_chunks(after=-1)[0]
    real = client.stream_chunk

    def dies_midway(n):
        for i, line in enumerate(real(n)):
            if i == 5000:
                raise httpx.ReadError("connection lost mid-chunk")
            yield line

    client.stream_chunk = dies_midway
    from controller.ingest import IngestStats
    with pytest.raises(httpx.ReadError):
        ing._ingest_chunk(chunk, IngestStats())
    assert store.cursor() == (-1, 0)
    assert store.db.execute("SELECT COUNT(*) FROM lead_dim").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM kpi_cube").fetchone()[0] == 0
    client.stream_chunk = real
    ing._reload_caches()
    ing.catch_up()
    fresh = _cube_totals(store.db)
    assert fresh[0] > 0
    assert store.db.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 0  # ingestion alone decides nothing
    store.close()


def test_totals_after_recovery_equal_totals_without_a_crash(sim, tmp_path):
    store, client, gov, specs, ing, _ = _stack(tmp_path, "whole")
    client.advance(1440, 0)
    ing.catch_up()
    clean = _cube_totals(store.db)
    store.close()

    # Second reader of the same world, killed after the history chunk.
    store2, client2, gov2, specs2, ing2, st2 = _stack(tmp_path, "crashy")
    first = client2.list_chunks(after=-1)[0]
    from controller.ingest import IngestStats
    ing2._ingest_chunk(first, IngestStats())
    store2.close()
    store3 = Store(str(Path(st2.state_dir) / "public_01_dev.db"))
    ing3 = Ingestor(store3, client2, SimClock("2027-01-04T00:00:00+05:30"), st2)
    ing3.catch_up()
    assert _cube_totals(store3.db) == clean
    store3.close()


def test_duplicate_submission_after_lost_response_is_reconciled(sim, tmp_path):
    store, client, gov, specs, ing, _ = _stack(tmp_path)
    ing.catch_up()
    client.advance(600, 0)  # D0 10:00, approver is in
    _, cfg = store.latest_config("nw_renewal")
    item = gov.queue("nw_renewal", "test", [{"op": "alloc", "cohort": "default", "dim": "voice", "dist": {"V1": 0.5, "V2": 0.5}}],
                     "test voices", "because", {}, {"replay": 1}, 600)
    real = client.submit_approval

    def lands_but_reply_lost(body):
        real(body)
        raise Unavailable("reply lost")

    client.submit_approval = lands_but_reply_lost
    with pytest.raises(Unavailable):
        gov.flush(600, {"nw_renewal": specs["nw_renewal"]})
    client.submit_approval = real
    row = store.db.execute("SELECT * FROM proposal").fetchone()
    assert row["status"] == "draft"  # we do not know yet, and the intent row says so
    assert store.db.execute("SELECT status FROM side_effect_intent").fetchone()[0] == "started"
    gov.reconcile(600)  # what a restart runs
    row = store.db.execute("SELECT * FROM proposal").fetchone()
    assert row["status"] == "pending" and row["approval_id"]
    assert store.db.execute("SELECT status FROM side_effect_intent").fetchone()[0] == "done"
    assert len(client.approvals("nw_renewal")) == 1  # exactly one request reached the approver
    store.close()


def test_apply_after_lost_response_is_not_repeated(sim, tmp_path):
    store, client, gov, specs, ing, _ = _stack(tmp_path)
    ing.catch_up()
    client.advance(660, 0)  # 11:00, because this client asked for nothing earlier
    gov.queue("ks_cancel", "test", [{"op": "alloc", "cohort": "default", "dim": "voice", "dist": {"V1": 0.5, "V2": 0.5}}], "voices", "because", {}, {"replay": 1}, 660)
    gov.flush(660, {"ks_cancel": specs["ks_cancel"]})
    aid = store.db.execute("SELECT approval_id FROM proposal").fetchone()[0]
    minute = 660
    while client.approval(aid)["status"] == "pending":
        minute += 15
        client.advance(15, minute - 15)
    if client.approval(aid)["status"] != "approved":
        pytest.skip("approver made a judgement-call rejection on this seed")
    with store.tx():
        gov._on_decision(aid, "approved", [], minute)
    real = client.apply

    def lands_but_reply_lost(wid, a):
        real(wid, a)
        raise Unavailable("reply lost")

    client.apply = lands_but_reply_lost
    r = store.db.execute("SELECT * FROM proposal").fetchone()
    with pytest.raises(Unavailable):
        gov._apply(r, minute)
    client.apply = real
    assert store.db.execute("SELECT status FROM proposal").fetchone()[0] == "applying"
    gov.reconcile(minute)
    assert store.db.execute("SELECT status,applied_version FROM proposal").fetchone()[0] == "applied"
    history = [h for h in client.config_history("ks_cancel") if h.get("approval_id") == aid]
    assert len(history) == 1  # a single config version from this approval
    store.close()


def test_new_world_is_detected_and_old_database_set_aside(sim, tmp_path):
    st = Settings(state_dir=str(tmp_path / "w"))
    client = SimClient(URL)
    state = client.state()
    store = open_store(st, state, client)
    store.set_meta("last_minute", str(state["sim_minute"] + 500))  # we had seen a later time than the world now reports
    store.close()
    open_store(st, client.state(), client).close()
    names = os.listdir(st.state_dir)
    assert any(".old-" in n for n in names)


def test_full_controller_finishes_and_respects_governance(tmp_path, sim):
    env = {**os.environ, "CTRL_STATE_DIR": str(tmp_path / "state")}
    r = subprocess.run([sys.executable, "-m", "controller", "--url", URL], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    db = sqlite3.connect(tmp_path / "state" / "public_01_dev.db")
    client = SimClient(URL)
    assert client.state()["finished"]
    assert db.execute("SELECT COUNT(*) FROM side_effect_intent WHERE status='started'").fetchone()[0] == 0
    # Our record of each workflow's versions matches the simulator's, version for version.
    for wid, in db.execute("SELECT workflow_id FROM workflow").fetchall():
        ours = {r[0]: r[1] for r in db.execute("SELECT version,actor FROM config_version WHERE workflow_id=?", (wid,))}
        theirs = {h["version"]: h["actor"] for h in client.config_history(wid)}
        assert ours == theirs
    # Customer-facing dimensions never went through auto-apply.
    for h in [h for (w,) in db.execute("SELECT workflow_id FROM workflow").fetchall() for h in client.config_history(w)]:
        if h["actor"] == "controller_auto":
            assert set(h["changed_dims"]) <= {"schedule", "caller_id"}
    # No changes to a config came from anywhere but a recorded proposal or a supervisor.
    missing = db.execute("SELECT COUNT(*) FROM config_version WHERE actor LIKE 'controller%' AND proposal_id IS NULL").fetchone()[0]
    assert missing == 0
    # The audit commands answer from the database alone: the simulator is not consulted.
    wid, minute = db.execute("SELECT workflow_id,applied_minute FROM config_version WHERE actor LIKE 'controller%' ORDER BY applied_minute DESC LIMIT 1").fetchone()
    person, day = db.execute("SELECT person_id,minute/1440 FROM call_log ORDER BY event_id DESC LIMIT 1").fetchone()
    path = str(tmp_path / "state" / "public_01_dev.db")
    at = f"D{minute // 1440}T{(minute % 1440) // 60}:{minute % 60:02d}"
    outputs = {}
    for args in (["why", "--workflow", wid, "--at", at], ["status"], ["health"], ["changes"], ["heard", "--person", person, "--day", str(day)]):
        res = subprocess.run([sys.executable, "-m", "controller.report", "--db", path, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                             env={**os.environ, "PYTHONUTF8": "1"}, timeout=120)
        assert res.returncode == 0, res.stderr[-800:]
        outputs[args[0]] = res.stdout
    assert "who approved" in outputs["why"] and "WHAT HAPPENED NEXT" in outputs["why"]
    assert "voice=" in outputs["heard"] and "live because" in outputs["heard"]
    assert "business outcome since D0" in outputs["status"]
    assert "Open intents (must be 0 once a run ends): 0" in outputs["health"]
