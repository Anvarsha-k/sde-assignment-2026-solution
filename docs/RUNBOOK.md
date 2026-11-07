# Runbook

For the engineer who gets paged. Everything below reads the controller's own database; you do not need the simulator
to be up.

## Run it

```bash
pip install -r requirements.txt
python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine --force    # terminal 1
./run.sh http://127.0.0.1:8700                                                             # terminal 2
```

State goes to `state/<scenario>.db`; set `CTRL_STATE_DIR` to change that. Any field in `controller/settings.py` can be
overridden with `CTRL_<FIELD_NAME>`. Logs go to stderr.

To run a scenario with a grade and integrity audit in one step:

```bash
python scripts/run_scenario.py --scenario scenarios/public_02_incidents.yaml --name mine
python scripts/run_scenario.py --scenario scenarios/public_02_incidents.yaml --name crash --kills 10 --seed 7
```

## First three commands when something looks wrong

```bash
python -m controller.report --db state/X.db health      # is the loop itself healthy?
python -m controller.report --db state/X.db status      # what is it doing per workflow?
python -m controller.report --db state/X.db why --workflow W --at D3T14:00
```

## Alerts

| Alert | Pages? | What it means | What to do |
|---|---|---|---|
| Credits exhausted during an incident | **Page** | A failing prompt may stay live | Check `credit_ledger`, add budget or handle by hand |
| Incident open with no validated fix | **Page** | No candidate passed validation | `SELECT * FROM decision WHERE incident_id=?`; escalate to the client |
| Every caller ID flagged | **Page** | No safe number to dial from | Client needs to provide or clear numbers |
| Simulator clock went backwards (exit 3) | **Page** | Upstream restarted | Do not restart blindly. Confirm upstream is healthy, then run again; old state is set aside |
| Simulator unreachable (exit 2 after 30 recoveries) | **Page** | Sustained outage | Fix upstream; restart resumes safely |
| Malformed events above zero | Ticket | Upstream data problem | `SELECT * FROM bad_event` |
| All caller IDs fell together | Ticket | Looks systemic, so nothing was rotated | Check for a carrier issue in the morning |
| Proposal rejected or expired | No alert | Normal; the owner regenerates from fresh data | Visible in `status` |
| Single-day KPI movement | No alert | Noise until a detector says otherwise | Nothing |

## Common questions

**The controller was killed. What do I do?** Start it again. Nothing else. `health` then shows a recovery event, and the
`recovery` row in `decision` says what it reconciled.

**A change went out that should not have.** `why` shows the proposal and evidence. A rollback must go through governance
like any other change: queue it as a `change_item`, or for an emergency use the client's approval process. Do not edit
config directly, because that bypasses the record.

**A supervisor edit seems to have been ignored.** It should not be possible. Look for `supervisor_edit`, then
`experiment_stopped` or `change_dropped`, in `decision`. Queued changes whose target values moved are dropped, unless they
are an emergency fix, which goes through approval in the open.

**An experiment never concludes.** `SELECT evidence FROM decision WHERE experiment_id=? AND kind='experiment_look'` shows
each daily look, including the boundary and each arm's z. Most often traffic is too low for the effect size.

## Adding things

- **A new detector:** add to `incidents.py`, require a minimum population, and record a `decision` even when acting is not
  possible. Queue changes through `Governance.queue`; never call the simulator to change config.
- **A new optimisable dimension:** add it to `DIMS` and `TARGET` in `experiments.py` and to `ALLOC_DIMS` in `governance.py`.
- **A client preference stricter than policy:** add it to `DEFAULT_TENANT_PREFS` in `settings.py`.
