"""Decision logic on synthetic cubes: promotion, guardrails, decay, supervisor edits, incidents.

No simulator runs here, so each test states exactly what the data shows and what must follow.
"""
import json

import pytest

from controller.client import ApiError
from controller.cohorts import cube_rows, history_baseline
from controller.experiments import ALL, Experiments
from controller.governance import Governance
from controller.incidents import Incidents
from controller.settings import Settings
from controller.store import Store, dumps

CFG = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}, "caller_id": {"D1": 1.0}}},
       "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
SPEC = {"tenant_id": "t", "options": {"welcome_message": ["W1", "W2"], "voice": ["V1"], "caller_id": ["D1"], "prompt_version": ["P1", "P3", "P4", "P5"]},
        "objective": {"primary": "engagement_rate", "guardrail": {"pickup_rate_max_drop": 0.02}},
        "governance": {"auto_apply": {"enabled": False, "allowed_dims": [], "max_shift": 0}, "approver_hours": [10, 19],
                       "approver_latency_minutes": [30, 240], "max_requests_per_24h": 3, "min_validation_pass_rate": 0.97}}
LOOK = 2 * 1440 + 600  # D2 10:00


@pytest.fixture()
def world(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.db.execute("INSERT INTO workflow VALUES('w','t',?)", (dumps(SPEC),))
    store.db.execute("INSERT INTO config_version VALUES('w',1,'business_default','[]',NULL,NULL,?,0)", (dumps(CFG),))
    store.db.execute("INSERT INTO segment(segment_id,features) VALUES(1,?)", (dumps({"age_band": "60+"}),))
    gov = Governance(store, None, Settings())
    # Stable history before D0.
    cube(store, hour=-100, dials=20000, conn=5000, eng=2700, hall=70)
    yield store, gov, Experiments(store, gov, Settings(), end_minute=8 * 1440)
    store.close()


def cube(store, hour, dials, conn, eng, hall=0, version=1, welcome="W1", voice="V1", did="D1", prompt="P1", seg=1):
    store.db.execute("INSERT INTO kpi_cube VALUES('w',?,?,?,?,?,?,?,?,?,?,?)", (hour, seg, version, welcome, voice, did, prompt, dials, conn, eng, hall))


def running(store, arms=("W1", "W2"), start_hour=0, version=2):
    return store.db.execute("INSERT INTO experiment(workflow_id,dim,metric,state,champion,arms,scopes,start_hour,start_version,created_minute) "
                            "VALUES('w','welcome_message','engagement_rate','running','W1',?,?,?,?,0)",
                            (dumps(list(arms)), dumps({"_seen_version": version}), start_hour, version)).lastrowid


def row(store, exp_id):
    return store.db.execute("SELECT * FROM experiment WHERE experiment_id=?", (exp_id,)).fetchone()


def arm(store, name, conn, rate, dials=None, hour=5):
    dials = dials if dials is not None else conn * 4
    cube(store, hour, dials, conn, round(conn * rate), version=2, welcome=name)


def step(exps, store, exp_id, minute=LOOK):
    exps._step(row(store, exp_id), SPEC, minute, set())
    return row(store, exp_id)


# ---- KPIs
def test_kpis_stay_separate_and_use_the_right_denominators(world):
    store, _, _ = world
    base = history_baseline(store, "w")
    assert base["pickup"] == pytest.approx(5000 / 20000)     # connected / dials
    assert base["engagement"] == pytest.approx(2700 / 5000)  # engaged / connected, never engaged / dials
    assert base["hall"] == pytest.approx(70 / 5000)


def test_cube_rows_filter_by_time(world):
    store, _, _ = world
    cube(store, 3, 100, 20, 10, version=2)
    assert cube_rows(store, "w", [], since_hour=0)[0]["dials"] == 100
    assert cube_rows(store, "w", [], until_hour=0)[0]["dials"] == 20000


# ---- winners
def test_clear_winner_is_promoted_with_a_holdout(world):
    store, gov, exps = world
    e = running(store)
    arm(store, "W1", 1500, 0.50)
    arm(store, "W2", 1500, 0.60)
    after = step(exps, store, e)
    assert after["state"] == "promoting"
    ops = json.loads(gov.item(after["pending_item"])["ops"])
    assert ops[0]["dist"] == {"W2": 0.85, "W1": 0.15}


def test_higher_percentage_alone_is_not_enough(world):
    store, gov, exps = world
    e = running(store)
    arm(store, "W1", 400, 0.50)
    arm(store, "W2", 400, 0.55)  # 5 points higher, well within noise at this size
    assert step(exps, store, e)["state"] == "running"
    assert store.db.execute("SELECT COUNT(*) FROM change_item").fetchone()[0] == 0


def test_too_few_samples_never_decides_however_large_the_gap(world):
    store, _, exps = world
    e = running(store)
    arm(store, "W1", 100, 0.20)
    arm(store, "W2", 100, 0.90)
    assert step(exps, store, e)["state"] == "running"
    assert row(store, e)["looks"] == 0  # a look with no usable data does not use up the look budget


def test_significant_but_tiny_lift_is_not_promoted(world):
    store, _, exps = world
    e = running(store)
    arm(store, "W1", 400000, 0.500)
    arm(store, "W2", 400000, 0.510)  # huge sample: significant, yet below the minimum effect
    assert step(exps, store, e)["state"] == "running"


def test_guardrail_harm_blocks_the_winner_and_removes_it(world):
    store, gov, exps = world
    e = running(store)
    arm(store, "W1", 2000, 0.50, dials=8000)
    arm(store, "W2", 2000, 0.60, dials=12000)  # engagement up, but pickup falls from 0.25 to 0.167
    after = step(exps, store, e)
    assert after["state"] != "promoting"
    assert store.db.execute("SELECT COUNT(*) FROM decision WHERE kind='winner_selected'").fetchone()[0] == 0
    ev = json.loads(store.db.execute("SELECT evidence FROM decision WHERE kind IN ('arm_removed','experiment_concluded') ORDER BY decision_id DESC").fetchone()[0])
    assert ev["guardrail_harm"] == ["W2"]


def test_unproven_guardrail_waits_instead_of_discarding_the_winner(world):
    store, _, exps = world
    e = running(store)
    arm(store, "W1", 1500, 0.50, dials=1500 * 4)
    arm(store, "W2", 1500, 0.60, dials=round(1500 * 4.1))  # small pickup dip, too noisy to clear the margin
    # Non-inferiority cannot be shown yet, and no harm is shown either.
    strict = Experiments(store, exps.gov, Settings(guard_confidence_z=3.0), end_minute=exps.end_minute)
    after = step(strict, store, e)
    assert after["state"] == "running"
    assert json.loads(after["arms"]) == ["W1", "W2"]


# ---- decay
def monitoring(store, version=2):
    scopes = {ALL: {"winner": "W2", "holdout": "W1", "since_hour": 0, "version": version}, "_seen_version": version}
    return store.db.execute("INSERT INTO experiment(workflow_id,dim,metric,state,champion,arms,scopes,start_hour,start_version,created_minute) "
                            "VALUES('w','welcome_message','engagement_rate','monitoring','W1','[\"W1\",\"W2\"]',?,0,?,0)",
                            (dumps(scopes), version)).lastrowid


def test_decayed_winner_is_rolled_back_as_an_emergency(world):
    store, gov, exps = world
    e = monitoring(store)
    arm(store, "W2", 1700, 0.45, hour=40)
    arm(store, "W1", 300, 0.58, hour=40)
    after = step(exps, store, e)
    assert after["state"] == "rolling_back"
    item = gov.item(after["pending_item"])
    assert item["emergency"] == 1
    assert json.loads(item["ops"])[0]["dist"] == {"W1": 1.0}


def test_winner_that_still_wins_stays(world):
    store, _, exps = world
    e = monitoring(store)
    arm(store, "W2", 1700, 0.60, hour=40)
    arm(store, "W1", 300, 0.55, hour=40)
    assert step(exps, store, e)["state"] == "monitoring"


def test_no_rollback_on_a_tiny_holdout(world):
    store, _, exps = world
    e = monitoring(store)
    arm(store, "W2", 1700, 0.45, hour=40)
    arm(store, "W1", 50, 0.90, hour=40)
    assert step(exps, store, e)["state"] == "monitoring"


def test_rejected_rollback_retries_within_the_hour(world):
    store, gov, exps = world
    e = monitoring(store)
    arm(store, "W2", 1700, 0.45, hour=40)
    arm(store, "W1", 300, 0.58, hour=40)
    after = step(exps, store, e)
    store.db.execute("UPDATE change_item SET status='dropped',status_reason='rejected',closed_minute=? WHERE item_id=?", (LOOK, after["pending_item"]))
    assert step(exps, store, e)["state"] == "monitoring"
    assert step(exps, store, e, LOOK + 30)["state"] == "monitoring"  # not yet
    assert step(exps, store, e, LOOK + 75)["state"] == "rolling_back"


# ---- supervisor
def test_supervisor_edit_on_the_tested_dimension_ends_the_experiment_and_is_not_undone(world):
    store, gov, exps = world
    e = running(store)
    store.db.execute("INSERT INTO config_version VALUES('w',3,'supervisor',?,NULL,NULL,?,100)", (dumps(["welcome_message"]), dumps(CFG)))
    after = step(exps, store, e)
    assert after["state"] == "concluded" and after["outcome"] == "supervisor_override"
    assert store.db.execute("SELECT COUNT(*) FROM change_item").fetchone()[0] == 0
    exps._maybe_start("w", SPEC, "welcome_message", LOOK + 5000, set())  # and it is not restarted later
    assert store.db.execute("SELECT COUNT(*) FROM experiment").fetchone()[0] == 1


def test_supervisor_edit_elsewhere_lets_the_experiment_continue(world):
    store, _, exps = world
    e = running(store)
    store.db.execute("INSERT INTO config_version VALUES('w',3,'supervisor',?,NULL,NULL,?,100)", (dumps(["prompt_version"]), dumps(CFG)))
    assert step(exps, store, e)["state"] == "running"


# ---- incidents
class FakeLLM:
    def __init__(self, suggestions, pass_rates):
        self.suggestions, self.pass_rates, self.credits, self.validated, self.llm_calls = suggestions, pass_rates, 100.0, [], 0

    def suggest_fixes(self, wid, problem):
        self.llm_calls += 1
        self.credits -= 3
        return {"suggestions": self.suggestions, "credits_remaining": self.credits}

    def validate(self, wid, prompt, n):
        self.validated.append(prompt)
        self.credits -= n // 100
        if prompt not in self.pass_rates:
            raise ApiError(402, "out_of_credits", None)
        return {"validation_run_id": f"val_{prompt}", "pass_rate": self.pass_rates[prompt], "credits_remaining": self.credits}


SUGGESTIONS = [{"prompt_version": "P3", "summary": "Grounded: tighter objection handling"},
               {"prompt_version": "P4", "summary": "Persuasive: freer objection handling"},
               {"prompt_version": "P5", "summary": "Strict: near-verbatim compliance script"}]


def incidents(world, client):
    store, gov, _ = world
    store.set_meta("credits_start", "100")
    inc = Incidents(store, client, gov, Settings())
    inc.credits_remaining = 100.0
    return inc


def spike(store, conn=400, hall=30, hour=100, prompt="P1"):
    cube(store, hour, conn * 4, conn, conn // 2, hall=hall, version=2, prompt=prompt)


def test_one_flagged_call_never_opens_an_incident(world):
    store, gov, _ = world
    inc = incidents(world, FakeLLM([], {}))
    cube(store, 100, 40, 10, 5, hall=1, version=2)  # 10% flag rate, on ten calls
    inc._detect_prompt("w", SPEC, 100 * 60)
    assert store.db.execute("SELECT COUNT(*) FROM incident").fetchone()[0] == 0


def test_sustained_spike_opens_one_incident_with_evidence(world):
    store, _, _ = world
    inc = incidents(world, FakeLLM([], {}))
    spike(store)
    inc._detect_prompt("w", SPEC, 100 * 60)
    inc._detect_prompt("w", SPEC, 100 * 60 + 15)
    rows = store.db.execute("SELECT * FROM incident").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["evidence"])["connected"] == 400


def _open_incident(store):
    return store.db.execute("INSERT INTO incident(workflow_id,kind,subject,state,evidence,detected_minute) VALUES('w','prompt','P1','open',?,0)",
                            (dumps({"baseline": 0.014, "rate": 0.05, "z": 6, "connected": 400, "engagement_before": 0.54}),)).lastrowid


def _inc(store, iid):
    return store.db.execute("SELECT * FROM incident WHERE incident_id=?", (iid,)).fetchone()


def test_prompt_with_a_good_record_is_used_without_paying_for_the_llm(world):
    store, _, _ = world
    cube(store, 50, 4000, 1000, 500, hall=10, version=2, prompt="P3")
    llm = FakeLLM(SUGGESTIONS, {"P3": 0.99})
    inc = incidents(world, llm)
    cand = inc._pick_candidate(_inc(store, _open_incident(store)), SPEC, 6000)
    assert cand["prompt"] == "P3" and llm.llm_calls == 0 and llm.validated == ["P3"]


def test_llm_suggestions_must_pass_validation(world):
    store, _, _ = world
    llm = FakeLLM(SUGGESTIONS[:2], {"P3": 0.95, "P4": 0.98})
    inc = incidents(world, llm)
    cand = inc._pick_candidate(_inc(store, _open_incident(store)), SPEC, 6000)
    assert cand["prompt"] == "P4"  # P3 scored lower than policy allows, so it is out


def test_scripted_prompt_loses_to_a_passing_alternative_despite_a_better_safety_score(world):
    store, _, _ = world
    llm = FakeLLM(SUGGESTIONS, {"P3": 0.98, "P4": 0.975, "P5": 0.999})
    inc = incidents(world, llm)
    assert inc._pick_candidate(_inc(store, _open_incident(store)), SPEC, 6000)["prompt"] == "P3"


def test_validation_is_not_bought_twice(world):
    store, _, _ = world
    llm = FakeLLM(SUGGESTIONS[:1], {"P3": 0.98})
    inc = incidents(world, llm)
    iid = _open_incident(store)
    inc._pick_candidate(_inc(store, iid), SPEC, 6000)
    inc._pick_candidate(_inc(store, iid), SPEC, 6060)
    assert llm.validated == ["P3"] and llm.llm_calls == 1


def test_out_of_credits_raises_a_page_instead_of_crashing(world):
    store, _, _ = world
    llm = FakeLLM(SUGGESTIONS, {})
    inc = incidents(world, llm)
    inc.credits_remaining = 1.0
    assert inc._pick_candidate(_inc(store, _open_incident(store)), SPEC, 6000) is None
    alert = store.db.execute("SELECT evidence FROM decision WHERE kind='alert'").fetchone()
    assert json.loads(alert[0])["severity"] == "page"


def test_safe_fix_that_loses_engagement_fails_verification(world):
    store, _, _ = world
    iid = _open_incident(store)
    store.db.execute("UPDATE incident SET state='verifying',fixed_version=3,fixed_minute=6000 WHERE incident_id=?", (iid,))
    store.db.execute("INSERT INTO config_version VALUES('w',3,'controller_approved','[\"prompt_version\"]',NULL,NULL,?,6000)",
                     (dumps({**CFG, "prompt_version": "P5"}),))
    cube(store, 101, 4000, 1000, 400, hall=5, version=3, prompt="P5")  # flags normal, engagement 0.40 vs 0.54
    inc = incidents(world, FakeLLM([], {}))
    inc._step_prompt(_inc(store, iid), SPEC, 6100)
    assert _inc(store, iid)["state"] == "open"
    assert store.db.execute("SELECT COUNT(*) FROM decision WHERE kind='fix_failed'").fetchone()[0] == 1


def test_good_fix_resolves(world):
    store, _, _ = world
    iid = _open_incident(store)
    store.db.execute("UPDATE incident SET state='verifying',fixed_version=3,fixed_minute=6000 WHERE incident_id=?", (iid,))
    store.db.execute("INSERT INTO config_version VALUES('w',3,'controller_approved','[\"prompt_version\"]',NULL,NULL,?,6000)",
                     (dumps({**CFG, "prompt_version": "P3"}),))
    cube(store, 101, 4000, 1000, 550, hall=12, version=3, prompt="P3")
    inc = incidents(world, FakeLLM([], {}))
    inc._step_prompt(_inc(store, iid), SPEC, 6100)
    assert _inc(store, iid)["state"] == "resolved"


def test_all_caller_ids_falling_together_is_not_blamed_on_one_number(world):
    store, gov, _ = world
    spec = {**SPEC, "options": {**SPEC["options"], "caller_id": ["D1", "D2"]}}
    store.db.execute("DELETE FROM kpi_cube")
    cube(store, -24 + 10, 20000, 5000, 2500)  # history at hour 10: pickup 0.25
    cube(store, 100 - (100 % 24) + 10, 1000, 100, 50, version=2, did="D1")
    cube(store, 100 - (100 % 24) + 10, 1000, 100, 50, version=2, did="D2")
    inc = incidents(world, FakeLLM([], {}))
    now = (100 - (100 % 24) + 10) * 60
    inc._detect_caller_ids("w", spec, now)
    assert store.db.execute("SELECT COUNT(*) FROM incident").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM decision WHERE kind='alert'").fetchone()[0] == 1


def test_replacing_a_supervisors_prompt_says_so_in_the_proposal(world):
    store, gov, _ = world
    store.db.execute("INSERT INTO config_version VALUES('w',2,'supervisor','[\"prompt_version\"]',NULL,NULL,?,500)", (dumps({**CFG, "prompt_version": "P1"}),))
    cube(store, 50, 4000, 1000, 500, hall=10, version=2, prompt="P3")
    inc = incidents(world, FakeLLM(SUGGESTIONS, {"P3": 0.99}))
    iid = _open_incident(store)
    inc._step_prompt(_inc(store, iid), SPEC, 6000)
    item = store.db.execute("SELECT * FROM change_item").fetchone()
    assert "supervisor" in item["rationale"].lower() and "supervisor" in item["summary"].lower()
    assert json.loads(item["evidence"])["overrides_supervisor_edit"]["version"] == 2
    assert item["emergency"] == 1  # still a proposal that waits for approval, never applied silently


def test_a_prompt_the_controller_chose_is_not_described_as_a_supervisors(world):
    store, gov, _ = world
    cube(store, 50, 4000, 1000, 500, hall=10, version=2, prompt="P3")
    inc = incidents(world, FakeLLM(SUGGESTIONS, {"P3": 0.99}))
    inc._step_prompt(_inc(store, _open_incident(store)), SPEC, 6000)
    item = store.db.execute("SELECT * FROM change_item").fetchone()
    assert "supervisor" not in item["rationale"].lower()
