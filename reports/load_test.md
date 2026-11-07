# Load test: `public_03_load`

Everything marked **measured** comes from the run's own database (`loop_metric`, `kpi_cube`, `call_log`) or the controller log in `reports/evidence/public_03.controller.log`. Everything marked **estimate** is reasoning from those numbers and has not been run. The grader's output for this run is in [public_03.md](public_03.md).

## Setup

- Scenario: 12 workflows, 5 simulated days, 5,537,379 history events and 4,200,164 live events (about 840k a day).
- Machine: Windows 11, 12 logical CPUs, 31.7 GB RAM. The grading box is 4 vCPU / 8 GB Linux, which I did not have. The run was launched through `start /affinity F` to restrict it to four logical CPUs, but I did not independently confirm that the mask took effect. The controller is single-threaded, so CPU count matters less than single-core speed, which here is probably higher than a cloud vCPU.
- Command: `python scripts/run_scenario.py --scenario scenarios/public_03_load.yaml --name public_03`. The controller started through `python -m controller`, the same entry point `run.sh` uses.
- Integrity audit after the run: passed (no double-applied approval, versions match the simulator's, no open intents, cursor at the last event, cube dials equal call-log rows).

## Results (measured)

| Quantity | Value |
|---|---|
| Ingest throughput while ingesting | 43,300 events/s (4,200,164 events in 97.0 s) |
| Ingest plus decisions | 30,300 events/s |
| History bootstrap before D0 | 5,537,379 events in 119.8 s (46,200 events/s) |
| Wall time per simulated day | 47.6 s: ingest 19.4 s + deciding 8.3 s + simulator's own advance 19.6 s |
| Controller-attributable per day | 27.7 s (ingest + deciding) |
| Time to process one 15-minute tick | median 0.33 s, p95 0.49 s, p99 3.2 s, max 4.8 s |
| Largest single tick | 192,012 events: the 08:00 upload of 192,000 leads and 12 campaigns |
| Peak memory | 261 MB, flat: 252 MB after day 1, 261 MB after day 5 |
| Database after 5 days | 589 MB, growing about 102 MB a day |
| Tables | `call_log` 3,240,000 rows; `kpi_cube` 826,365 rows; `lead_dim` 960,000 rows |
| HTTP retries / duplicates / malformed events | 0 / 0 / 0 |
| Decisions made | 24.6 changes queued, 9.2 proposals submitted, 8.0 configs applied per simulated day |
| Proposals | 58 applied, 5 rejected, 1 lapsed |
| Mean approval latency | 213 simulated minutes |
| Credits used | 0 of 400 |
| Alerts raised | 0 |

**Against the stakeholder target** (one simulated day in under 10 wall-minutes): 47.6 s per day is about 8% of that.

**Against the load itself.** The scenario averages about 840,000 events a day, roughly 19 a second over the 12-hour dialling window. The controller processed 30,300 a second, around 1,500 times the average rate. Throughput is not what limits this design.

**Decision lag.** By construction, a decision uses data at most one tick (15 simulated minutes) old, plus a median 0.33 s to process the tick. In real time the lag is dominated by other things: approvers (mean 213 simulated minutes) and the statistics (a winner needs days of data).

**Run-to-run variation (measured).** Three complete runs of this scenario on this machine:

| Run | Code | Startup ingest | Ingest per day | Wall per day | DB size |
|---|---|---|---|---|---|
| A | per-person index on `call_log`, unpinned | 73.5 s | 36.8 s | about 66 s | 628 MB |
| B | index removed | 72.2 s | 18.8 s | about 46 s | 524 MB |
| C (reported above) | index removed, campaign added to `call_log` | 119.8 s | 19.4 s | 47.6 s | 589 MB |

Removing the index halved ingest time. The startup time moved from 72 s to 120 s between runs B and C with no code change that should explain it, so treat startup as roughly 70–120 s on this machine. The call-log row grew by one column in run C, which accounts for the larger database.

## Where the time goes

The ingest figure of 19.4 s a day is the largest controller-side cost. In separate microbenchmarks (not a profile of this run), parsing 650,000 call events with `json.loads` took 8.3 s, and writing a day of call-log rows with no person index took 3.4 s. That leaves roughly 7–8 s of per-event Python work (building the cube, mapping segments) that I did not break down further. The measured bottleneck is single-core Python per-event work, not the database.

## What breaks at 10× (estimate, not run)

At 10× the load, about 8.4 million events and 6.5 million dials a day:

1. **Storage first.** About 1 GB a day, 30 GB a month, in one SQLite file, mostly `call_log` and `lead_dim`. Backup, vacuum and recovery of a file that size become the operational problem before CPU does.
2. **Memory second.** The lead-to-segment working set is held in a Python dictionary: 960,000 leads cost roughly 220 MB of the 261 MB peak. Ten times the leads would need about 2 GB or more. That fits in 8 GB but is the first memory limit.
3. **Ingest CPU is not the limit.** Ten times the events would take about 190 s of ingest per simulated day, still a small fraction of real time. The 08:00 upload tick would grow from about 5 s to roughly 50 s of processing, which is acceptable for a once-a-day spike.
4. **Deciding scales with workflows, not events.** It cost 8.3 s per simulated day for 12 workflows, about 0.7 s per workflow. Hundreds of workflows would take minutes per simulated day, fine in aggregate but needing incremental aggregates instead of rescanning the cube.
5. **The single writer** becomes the real ceiling when hundreds of workflows write through one process.
6. **Not compute at all:** approver request limits (2–4 per workflow per day), credit budgets (about 33 per workflow here), and statistical power are policy limits that more hardware does not move.

## How I would partition it

Workflows are independent and each belongs to exactly one tenant, so partition by tenant: each worker holds a lease on a set of tenants and owns their ingestion and decisions. The only state shared across tenants is the credit budget, which becomes a table with an atomic decrement. Move storage to Postgres with `call_log` partitioned by month and a retention period agreed with Compliance, and separate continuous ingestion from timer-driven deciding with the database as their only point of contact.

## What this test does not show

- Performance on the 4 vCPU / 8 GB Linux box.
- Behaviour when events arrive during a slow tick. The simulator's clock waits for the controller, so real-time pressure never occurs here.
- Any figure at 10× or at the real 20 million dials a month. Those are reasoned, not run.
- Decay detection under a short horizon. In this scenario the decaying welcome message W3 on the four renewal workflows only began to fall on day 4 (engagement 0.629 to 0.579) and was still ahead of its holdout (0.562) when the scenario ended, so a rule that compares a winner with its holdout over 48 hours could not fire in time. The grader's table shows W3 still holding about 85% share on days 3 and 4. A faster test would compare a winner's recent lift with its own earlier lift in windows of hours, not days.
