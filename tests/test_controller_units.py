"""Unit tests for pure logic and the store-backed rules: no simulator process involved."""
import json

import httpx
import pytest

from controller import cohorts as C
from controller.client import ApiError, SimClient
from controller.governance import ALLOWED, Governance, IllegalTransition, apply_ops, changed_dims, max_shift, read_paths
from controller.ingest import SimClock
from controller.settings import Settings
from controller.store import Store, dumps

CFG = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}, "caller_id": {"D1": 0.5, "D2": 0.5}}},
       "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
SPEC = {"tenant_id": "t", "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["schedule", "caller_id"], "max_shift": 0.2},
                                         "approver_hours": [10, 19], "approver_latency_minutes": [30, 240], "max_requests_per_24h": 3}}


# ---- cohorts
def test_cohort_grammar_is_and_across_features_or_within():
    where = {"age_band": ["60+", "46-60"], "lang": ["ta"]}
    assert C.matches(where, {"age_band": "60+", "lang": "ta"})
    assert not C.matches(where, {"age_band": "60+", "lang": "hi"})
    assert not C.matches(where, {"age_band": "18-30", "lang": "ta"})


def test_first_matching_cohort_wins_otherwise_default():
    cohorts = [{"id": "a", "where": {"lang": ["ta"]}}, {"id": "b", "where": {"age_band": ["60+"]}}]
    segs = {1: {"lang": "ta", "age_band": "60+"}, 2: {"lang": "hi", "age_band": "60+"}, 3: {"lang": "hi", "age_band": "18-30"}}
    assert C.segment_to_cohort(segs, cohorts) == {1: "a", 2: "b", 3: "default"}


# ---- config helpers
def test_apply_ops_does_not_mutate_input_and_preserves_other_dims():
    new = apply_ops(CFG, [{"op": "alloc", "cohort": "default", "dim": "voice", "dist": {"V2": 1.0}}])
    assert CFG["allocations"]["default"]["voice"] == {"V1": 1.0}
    assert new["allocations"]["default"]["voice"] == {"V2": 1.0}
    assert new["allocations"]["default"]["welcome_message"] == {"W1": 1.0}
    assert changed_dims(CFG, new) == {"voice"}


def test_adding_cohorts_keeps_supervisor_cohorts_first():
    theirs = {**CFG, "cohorts": [{"id": "sup", "where": {"lang": ["ta"]}}]}
    new = apply_ops(theirs, [{"op": "cohorts", "add": [{"id": "ours", "where": {"lang": ["hi"]}}, {"id": "sup", "where": {}}]}])
    assert [c["id"] for c in new["cohorts"]] == ["sup", "ours"]
    assert new["cohorts"][0]["where"] == {"lang": ["ta"]}


def test_max_shift_measures_effective_distribution_per_cohort():
    new = apply_ops(CFG, [{"op": "alloc", "cohort": "default", "dim": "caller_id", "dist": {"D1": 0.3, "D2": 0.5, "D3": 0.2}}])
    assert max_shift(CFG, new) == pytest.approx(0.2)


def test_read_paths_detects_a_supervisor_edit():
    ops = [{"op": "alloc", "cohort": "default", "dim": "voice", "dist": {"V2": 1.0}}]
    before = read_paths(CFG, ops)
    edited = apply_ops(CFG, [{"op": "alloc", "cohort": "default", "dim": "voice", "dist": {"V3": 1.0}}])
    assert read_paths(edited, ops) != before


# ---- clock
def test_sim_clock_handles_history_and_live_times():
    clock = SimClock("2027-01-04T00:00:00+05:30")
    assert clock.minute("2027-01-04T10:37:00+05:30") == 637
    assert clock.minute("2027-01-03T23:15:00+05:30") == -45


# ---- governance rules
@pytest.fixture()
def gov(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.db.execute("INSERT INTO config_version VALUES('w',1,'business_default','[]',NULL,NULL,?,0)", (dumps(CFG),))
    store.db.execute("INSERT INTO workflow VALUES('w','t',?)", (dumps(SPEC),))
    yield Governance(store, None, Settings(tenant_prefs={"kest": {"max_requests_per_day": 2, "not_before_hour": 11}}))
    store.close()


def _proposal(gov, pid, status, minute=600, approval_id=None, wf="w"):
    gov.store.db.execute("INSERT INTO proposal(proposal_id,workflow_id,path,status,base_version,body,created_minute,approval_id) "
                         "VALUES(?,?,'approval',?,1,'{}',?,?)", (pid, wf, status, minute, approval_id))


@pytest.mark.parametrize("cur,to", [("draft", "approved"), ("pending", "applied"), ("rejected", "approved"), ("applied", "applying"), ("expired", "pending")])
def test_illegal_transitions_are_refused(gov, cur, to):
    _proposal(gov, "p", cur)
    with pytest.raises(IllegalTransition):
        gov._transition("p", to, 600)


def test_every_approved_path_is_reachable_and_transition_is_recorded(gov):
    _proposal(gov, "p", "pending", approval_id="a")
    gov._transition("p", "approved", 650)
    gov._transition("p", "applying", 660)
    gov._transition("p", "applied", 660)
    rows = gov.store.db.execute("SELECT to_status FROM proposal_transition WHERE proposal_id='p' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["approved", "applying", "applied"]
    assert "applied" not in ALLOWED


def test_requests_wait_for_approver_hours(gov):
    assert not gov.can_submit("w", SPEC, 9 * 60, False)[0]
    assert gov.can_submit("w", SPEC, 10 * 60, False)[0]
    assert not gov.can_submit("w", SPEC, 19 * 60, True)[0]  # emergencies still respect a closed office


def test_routine_request_held_when_it_cannot_be_decided_today(gov):
    assert not gov.can_submit("w", SPEC, 17 * 60, False)[0]  # mean latency runs past closing
    assert gov.can_submit("w", SPEC, 17 * 60, True)[0]


def test_one_slot_is_reserved_for_emergencies(gov):
    _proposal(gov, "a", "applied", 600, "apr1")
    _proposal(gov, "b", "applied", 610, "apr2")
    assert not gov.can_submit("w", SPEC, 700, False)[0]  # routine work may use 2 of 3
    assert gov.can_submit("w", SPEC, 700, True)[0]
    _proposal(gov, "c", "applied", 620, "apr3")
    assert not gov.can_submit("w", SPEC, 700, True)[0]   # the hard limit holds for everyone
    assert gov.can_submit("w", SPEC, 620 + 1440, False)[0]  # the 24-hour window has rolled past all three


def test_client_preference_is_stricter_than_policy(gov):
    spec = {**SPEC, "tenant_id": "kest"}
    assert not gov.can_submit("w", spec, 10 * 60, False)[0]  # before 11:00
    assert gov.can_submit("w", spec, 11 * 60, False)[0]
    assert gov.request_limit(spec) == 2


def test_customer_facing_change_never_auto_applies_even_if_policy_allows(gov):
    spec = {"tenant_id": "t", "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["voice", "caller_id"], "max_shift": 1.0}}}
    voice = gov.queue("w", "x", [{"op": "alloc", "cohort": "default", "dim": "voice", "dist": {"V2": 1.0}}], "s", "r", {}, {"a": 1}, 0)
    did = gov.queue("w", "x", [{"op": "alloc", "cohort": "default", "dim": "caller_id", "dist": {"D1": 1.0}}], "s", "r", {}, {"a": 1}, 0)
    assert not gov._auto_eligible(gov.item(voice), CFG, spec)
    assert gov._auto_eligible(gov.item(did), CFG, spec)


def test_edit_after_queueing_drops_the_routine_item_but_not_the_emergency(gov):
    ops = [{"op": "alloc", "cohort": "default", "dim": "voice", "dist": {"V2": 1.0}}]
    routine = gov.queue("w", "x", ops, "s", "r", {}, {"a": 1}, 0)
    urgent = gov.queue("w", "y", ops, "s", "r", {}, {"a": 1}, 0, emergency=True)
    edited = apply_ops(CFG, [{"op": "alloc", "cohort": "default", "dim": "voice", "dist": {"V3": 1.0}}])
    items = [gov.item(routine), gov.item(urgent)]
    kept = gov._drop_outdated("w", items, edited, 10)
    assert [i["item_id"] for i in kept] == [urgent]
    assert gov.item(routine)["status"] == "dropped"
    assert gov.store.db.execute("SELECT COUNT(*) FROM decision WHERE kind='change_dropped'").fetchone()[0] == 1


# ---- store
def test_transaction_rolls_back_together(tmp_path):
    s = Store(str(tmp_path / "x.db"))
    with pytest.raises(RuntimeError):
        with s.tx():
            s.db.execute("INSERT INTO ingest_cursor VALUES(1,5,500)")
            s.decide(0, None, "k", "should vanish")
            raise RuntimeError("crash before commit")
    assert s.cursor() == (-1, 0)
    assert s.db.execute("SELECT COUNT(*) FROM decision").fetchone()[0] == 0
    s.close()


def test_duplicate_proposal_id_is_refused_by_the_database(gov):
    _proposal(gov, "same", "pending")
    with pytest.raises(Exception):
        _proposal(gov, "same", "pending")


# ---- client retry policy
def _client(handler):
    return SimClient("http://x", max_retries=3, sleep=lambda s: None, transport=httpx.MockTransport(handler))


def test_read_retries_through_429_and_5xx():
    calls = []
    def h(req):
        calls.append(1)
        return httpx.Response(200, json={"ok": True}) if len(calls) == 3 else httpx.Response(503 if len(calls) == 1 else 429)
    assert _client(h).state() == {"ok": True}
    assert len(calls) == 3


def test_client_error_is_not_retried():
    calls = []
    def h(req):
        calls.append(1)
        return httpx.Response(409, json={"error": "stale_base_version", "detail": {"current_version": 3}})
    with pytest.raises(ApiError) as e:
        _client(h).submit_approval({})
    assert e.value.code == "stale_base_version" and len(calls) == 1


def test_credit_spending_calls_are_never_retried_after_they_may_have_landed():
    calls = []
    def h(req):
        calls.append(1)
        return httpx.Response(503)
    with pytest.raises(ApiError):
        _client(h).validate("w", "P1", 100)
    assert len(calls) == 1


def test_advance_does_not_move_time_twice_when_the_response_is_lost():
    posts = []
    def h(req):
        if req.method == "POST":
            posts.append(1)
            raise httpx.ReadTimeout("lost", request=req)  # the server moved time, but we never saw the reply
        return httpx.Response(200, json={"sim_minute": 15, "ts": "t", "finished": False})
    res = _client(h).advance(15, 0)
    assert res["sim_minute"] == 15 and len(posts) == 1


def test_client_can_require_approval_for_everything(gov):
    spec = {"tenant_id": "strict", "governance": {"auto_apply": {"enabled": True, "allowed_dims": ["caller_id"], "max_shift": 1.0}}}
    did = gov.queue("w", "x", [{"op": "alloc", "cohort": "default", "dim": "caller_id", "dist": {"D1": 1.0}}], "s", "r", {}, {"a": 1}, 0)
    assert gov._auto_eligible(gov.item(did), CFG, spec)
    gov.settings = Settings(tenant_prefs={"strict": {"no_auto_apply": True}})
    assert not gov._auto_eligible(gov.item(did), CFG, spec)
