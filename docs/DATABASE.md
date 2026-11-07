# Database schema

`controller/schema.sql` is the definition. This explains why each table exists and how to query it.
Time columns: `*_minute` is simulated minutes since D0 00:00, and `hour_idx` is `minute // 60` (negative before D0).
All times are IST, because the simulator's are.

## Tables

| Table | Why it exists | Key rule |
|---|---|---|
| `run_meta` | Schema version, scenario, clock epoch, starting credits | A code and schema mismatch refuses to start |
| `ingest_cursor` | Where ingestion got to | Single row, updated in the chunk's own transaction |
| `workflow` | Policy, options and objective, as the API returned them | Re-read on every start |
| `config_version` | Every version of every config, whoever made it | PK `(workflow_id, version)`; must match the simulator exactly |
| `segment` | One row per lead feature combination | At most a few hundred rows, however many leads arrive |
| `campaign` | Daily batches, for working-set eviction and lead counts | |
| `lead_dim` | Leads of campaigns still dialling, for joining calls to features | Older campaigns are deleted |
| `kpi_cube` | Counts by workflow, hour, segment, version and variant | All KPIs read this; `WITHOUT ROWID` |
| `call_log` | One row per live call, with its campaign | Answers "what did this person hear" and gives the business outcome; indexed on `minute` only, see below |
| `inbox` | `config_changed` and `approval_decided` events | Committed with the chunk, consumed afterwards |
| `bad_event` | Malformed lines, kept verbatim | One bad line never blocks a chunk |
| `cohort` | The shared cohort definition and why it was formed | |
| `change_item` | One wanted change, with the values it expects to replace | Owners watch its status |
| `proposal` | Exactly what was sent, including the full config | `proposal_id` and `approval_id` unique |
| `proposal_transition` | Every state change of every proposal | Written by the state machine, which refuses illegal moves |
| `side_effect_intent` | Outside actions in flight | Must have no `started` rows once a run ends |
| `experiment` | State, arms, promoted scopes | |
| `incident` | Detection, candidates tried, fix, resolution | |
| `change_watch` | A schedule change under observation, with its undo | |
| `credit_ledger` | Every credit spent, and validation results | Validations are reused rather than re-bought |
| `decision` | Append-only record of every choice, including doing nothing | Links to experiment, incident, item and proposal |
| `loop_metric` | Per-tick timing, memory, size, credits | Source for `report.py health` |

## Time resolution

`config_version.applied_minute` is the tick at which the controller recorded the change. For a change the controller applied, that is the tick it applied it. For a supervisor's edit it is the next tick after the edit, so it can lag the simulator's own timestamp by up to 15 minutes.

## Why not one row per event as the main store?

At the load scenario's rate of about 840k events a day, a raw event table is the largest thing in the database and
almost nothing needs it. Every decision the controller makes reads counts. So calls are aggregated into the cube, and
only the audit fields go to `call_log`, without history. Lead events are not kept after their campaign ends: features
survive through `segment_id`, and the `call_log` row has the cohort and variant.

## Why `call_log` has no index on person

We measured it. With 3.2M rows already present, a day of load-scenario call writes took 36.0s with an index on
`(person_id, minute)` and 3.4s without one. Random person IDs make each insert touch a different part of the index. The
query it serves is rare and has a 24-hour deadline, and scanning a day's rows takes under a second. On Postgres with monthly
partitions this trade-off would be worth re-measuring.

## Useful queries

The `report.py` commands cover the common ones. For anything else:

```sql
-- Engagement by voice on one workflow since an experiment started, split by version
SELECT config_version, voice, SUM(connected) AS n, 1.0*SUM(engaged)/SUM(connected) AS engagement
FROM kpi_cube WHERE workflow_id='ks_cancel' AND hour_idx>=0 GROUP BY 1,2;

-- Why was a proposal rejected, and what happened to the items in it?
SELECT p.proposal_id, p.status, p.reasons, i.source, i.status_reason
FROM proposal p JOIN change_item i USING (proposal_id) WHERE p.status!='applied';

-- Everything the controller decided about one incident, in order
SELECT minute, kind, summary, evidence FROM decision WHERE incident_id=2 ORDER BY decision_id;

-- Did a restart leave anything unresolved?  Must return 0 at the end of a run.
SELECT COUNT(*) FROM side_effect_intent WHERE status='started';
```

## Schema changes

`SCHEMA_VERSION` in `store.py` is checked at startup, and a mismatch refuses to run rather than guessing. There is no
migration tool, because each scenario run owns a fresh database. In production this becomes real migrations against
Postgres.
