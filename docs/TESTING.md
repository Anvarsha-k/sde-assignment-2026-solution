# Testing

```bash
python -m pytest -q tests                                # everything, about 2 minutes
python -m pytest -q tests/test_decisions.py tests/test_stats.py tests/test_controller_units.py   # no simulator, a few seconds
python scripts/false_winner_rate.py                      # error rates of the winner rule, about 1 minute
```

## What each layer proves

| File | Needs a simulator? | Proves |
|---|---|---|
| `tests/test_stats.py` | No | Stratification, Holm, sequential boundary. A/A simulation holds the false-winner rate down and a real lift is still found. |
| `tests/test_controller_units.py` | No | Cohort matching, config diffs, policy shift limit, state machine refuses illegal moves, request budget and timing, retry policy per operation. |
| `tests/test_decisions.py` | No | On synthetic data: when a winner is and is not promoted, guardrail harm vs. "not proven yet", decay and rollback, supervisor edits, incident thresholds, candidate selection, credit limits, post-fix verification. |
| `tests/test_integration.py` | Starts one | Replays are harmless, an interrupted chunk leaves nothing, recovered totals equal uninterrupted totals, lost HTTP replies are reconciled without repeats, a restarted simulator is detected, a full run matches the simulator's own record, and audit commands work from the database alone. |
| `tests/test_sim.py` | In process | Supplied by the assignment. This is the only test file that imports `sim`, and the controller never does. |

## Scenario runs

`scripts/run_scenario.py` starts the simulator and controller as separate processes, grades the run, and audits the
controller's database against the simulator's record. The audit fails the run if any of these is not true:

- no approval was applied twice;
- every config version and actor matches the simulator exactly;
- every applied proposal has a local record, and every approval the simulator knows is in step with ours;
- no intent is left unfinished;
- the cursor reached the simulator's last event;
- live dials in the cube equal rows in `call_log`, so nothing was double-counted or lost across crashes.

## Crash testing

Two complementary methods, because each one misses what the other finds:

**Random kills** (`--kills N`) hard-kill the process at random moments. They cover mid-tick states broadly, but rarely land
in the few milliseconds around an outside call.

**Crash points** (`--crash-points a,b,c`) exit the process at exactly the dangerous moments, listed in `controller/faults.py`:
before and after each of submit, apply and auto-apply; mid-chunk before commit; between advance and ingest; between ingest and
deciding. Reconcile logs confirm those windows were reached: for example `submissions: 1` on restart means a submission was
really in doubt.

Results are in `reports/crash_recovery.md`, with raw logs in `reports/evidence/`.

## What this does not test

- **Sustained 5xx or 429 from the simulator in a full run.** The retry policy has unit tests with a mock transport, but the
  simulator does not inject faults, so this is not exercised end to end.
- **Hidden-scenario generalisation.** Only three scenarios exist locally, and we can avoid hardcoding but cannot prove
  performance on different truth.
- **Real production timing.** The simulator's clock waits for us. Events arriving during a slow tick cannot happen here.
- **Power failure.** Process kills are tested; a lost disk write is reasoned about from SQLite's WAL guarantees, not tested.
