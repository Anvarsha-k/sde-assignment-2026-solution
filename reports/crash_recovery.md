# Crash-recovery evidence

Scenario: `public_02_incidents` (the one with the most governance activity: three workflows, supervisor edits, an incident, two rollbacks). Everything below was run on the final code and comes from `reports/evidence/`.

Reproduce:

```bash
python scripts/run_scenario.py --scenario scenarios/public_02_incidents.yaml --name crash_random --kills 12 --seed 21
python scripts/run_scenario.py --scenario scenarios/public_02_incidents.yaml --name crash_points \
  --crash-points before_submit_http,after_submit_http,before_apply_http,after_apply_http,before_auto_http,after_auto_http,mid_chunk_before_commit,after_advance_before_ingest,after_ingest_before_decide
```

The runner starts the simulator and the controller as separate processes, kills the controller, restarts it as an orchestrator would, and when the run ends compares the controller's database with the simulator's own record.

## Two methods, because each misses what the other finds

**Random kills.** The process is hard-killed (no cleanup, no flush) at random moments. This covers mid-tick states broadly, but a kill rarely lands in the few milliseconds around an outside call.

**Crash points.** Nine named points exit the process immediately, once each, at the moments that matter most (`controller/faults.py`). Marker files stop a point firing twice.

## Results

| | Random kills | Crash points |
|---|---|---|
| Kills that happened | 8 (requested 12; the run finished first) | 9, all nine points fired |
| Controller exit code | 0 | 0 |
| Scenario finished | yes | yes |
| Integrity audit | **passed** | **passed** |
| Recoveries recorded in the store | 7 | 8 |
| Approvals applied twice | none | none |
| Config versions differing from the simulator's | none | none |
| Approvals lost or out of step | none | none |
| Unfinished intents | 0 | 0 |
| Events behind the simulator | 0 | 0 |
| Cube dials minus call-log rows | 0 | 0 |

The audit also fails the run if an applied config has no local proposal. It did not.

The recoveries are one fewer than the kills in both runs: a kill during the first startup, before anything was committed, has nothing to recover, and that is the correct behaviour.

## Random kills: identical to the uncrashed run

Comparing this run with an uncrashed `public_02` run:

- the decision log is identical (188 of 188 rows, ignoring only the recovery rows and the two rows that record wall-clock durations);
- the proposal table is identical (20 proposals with the same IDs, statuses, approvals and versions);
- the grade table is identical (leads concluded +25.3%, 17 changes applied).

This is the strongest evidence of exactly-once behaviour. A controller that double-applied, lost a decision or re-counted a chunk would diverge.

## Crash points: what each restart did

| Point | Restart found | What recovery did |
|---|---|---|
| `mid_chunk_before_commit` | nothing committed | Chunk fetched again; no recovery needed |
| `before_auto_http` | an auto-apply intent with no effect at the simulator | Marked failed (`crash before auto-apply landed`); items dropped, rebuilt later |
| `after_auto_http` | an auto-apply that did land | Found in config history by `proposal_id`; confirmed (`confirmed after restart`) |
| `after_advance_before_ingest` | time advanced, nothing ingested | Ingested the new chunks, then decided |
| `after_ingest_before_decide` | data committed, no decision | Decided on restart |
| `before_submit_http` | a submit intent that never reached the approver | Looked up by proposal ID; not found; failed; items dropped, rebuilt |
| `after_submit_http` | a submit that did land | Found by proposal ID; adopted as pending |
| `before_apply_http` | an approved proposal marked applying, apply not landed | Returned to approved; apply retried |
| `after_apply_http` | an apply that did land | Approval found `applied`; version taken from history; marked applied |

The reconcile lines in the log show `submissions: 1`, `autos: 1` and `applies: 1` on the matching restarts, so the ambiguous windows were really reached. The mid-chunk, advance-to-ingest and ingest-to-decide points leave no distinct log line; their evidence is the audit passing.

One more path appeared on its own: an apply failed with `409 stale_base_version` because the config had moved. That is an ordinary governance event, handled by dropping the proposal and rebuilding it; it is recorded as a failed intent with the error.

## Why the crash-point run's outcome differs from the uncrashed run

Its grade is lower (conclusion +21.6% against +25.3%) and its supervisor-edit fix took 150 minutes instead of 225. That is not a correctness failure. A crash before a submit loses the request that was about to be sent, so the work is rebuilt later. Here, at minute 600, a crash before a submit lost `nw_renewal`'s batch (its schedule change and welcome and voice tests). The rebuilt batch went out at minute 720, was declined once by the approver as an ordinary judgement-call rejection, and was approved on the third attempt. The welcome-message test therefore started at D1 13:15 instead of D0 13:00, about a day late. Every decision was still correct for the data it saw. The two timelines diverge from that point because approver rejections are random draws.

## What state is lost

Nothing durable. One narrow gap remains: if the process dies after a validation or LLM response arrives and before the result is committed, those credits are spent and the answer is lost (4 credits for a validation at n=400, 3 for an LLM call). They cannot be looked up afterwards, so the call would be bought again, at most once per crash.

## What is not tested

- Power loss or a lost disk. SQLite in WAL mode is designed to survive it, but I reasoned about it and did not test it.
- Sustained 5xx or 429 responses from the simulator across a whole run. The simulator does not inject faults. The retry policy is covered by unit tests with a mock transport.
- A crash on the grading box. These runs were on Windows, where the kill is a hard termination; on Linux the runner uses the same call, which becomes SIGKILL.
