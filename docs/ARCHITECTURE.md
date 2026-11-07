# Controller architecture

Read this first if you own the controller. `docs/SIMULATOR.md` is the contract with the outside world;
this file explains what we built on it. `docs/RUNBOOK.md` is for when something is wrong right now.

## What it does in one tick

```
                        Simulator (HTTP only)
      advance / chunks        │        ▲  approvals / apply / auto-apply / LLM / validation
                              ▼        │
                     ┌────────────────────────┐
                     │ client.py              │  retries only what is safe to repeat
                     └───┬────────────────▲───┘
                         ▼                │
 ┌───────────┐  one transaction per chunk │       ┌──────────────────────────┐
 │ ingest.py ├───────────┐                └───────┤ governance.py            │
 └───────────┘           ▼                        │ the only writer of config│
                 ┌────────────────┐               │ state machine, batching, │
 ┌───────────┐   │ SQLite         │◄──────────────┤ intents, reconcile       │
 │ loop.py   │   │  kpi_cube      │               └────────────▲─────────────┘
 │ advance   │   │  cursor, inbox │                            │ change items
 │ ingest    │   │  cohorts       │               ┌────────────┴─────────────┐
 │ inbox     │   │  experiments   │◄──────────────┤ incidents.py             │
 │ apply     │   │  proposals     │               │ schedule.py              │
 │ decide    │   │  intents       │               │ experiments.py           │
 │ submit    │   │  decision log  │               │ (stats.py, cohorts.py)   │
 └───────────┘   └───────┬────────┘               └──────────────────────────┘
                         ▼
                    report.py   why / status / heard / health / changes
```

| Module | Responsibility | Depends on |
|---|---|---|
| `client.py` | All HTTP. Decides what may be retried. | nothing |
| `store.py`, `schema.sql` | Connection, transactions, audit helper. | nothing |
| `ingest.py` | Chunks to cube, call log and inbox, with the cursor, atomically. | client, store |
| `cohorts.py` | The one cohort definition; every KPI read. | store |
| `stats.py` | Pure statistics. No I/O or randomness. | nothing |
| `governance.py` | Queue of change items, proposal state machine, intents, reconciliation. | client, store |
| `experiments.py` | Experiment state machines: design, looks, promotion, decay. | stats, cohorts, governance |
| `incidents.py` | Hallucination and caller-ID detection and response. | stats, cohorts, governance, client |
| `schedule.py` | Per-cohort call timing from history; automatic undo. | cohorts, governance |
| `loop.py` | Startup, recovery, the tick. | everything |
| `report.py` | Read-only answers from the database. Never contacts the simulator. | database only |
| `faults.py` | Deterministic crash points for recovery tests. Inactive unless configured. | nothing |

We consolidated rather than making a class per concept. Idempotency, cursor handling and recovery are
behaviours of `store`, `ingest` and `governance`, and separating them would add indirection with no second
implementation behind it.

## The five ideas that hold it together

**1. One owner for config writes.** No module calls the simulator to change anything. Modules queue a
`change_item` and later read its status. So two modules cannot race to change a workflow, and batching comes
for free: everything queued when a request goes out travels in that request.

**2. Exactly-once effect from a single transaction.** A chunk's aggregates, call log rows, control events and
the new cursor commit together. Crash before commit: nothing happened, and the chunk is fetched again. Crash
after: the cursor has moved past it. There is no dedupe table, because `event_id` only increases and a
high-water mark is enough.

**3. Write intent before any outside action.** An HTTP call and a database write cannot be atomic. So we record
the intent, make the call, then record the result. On restart, an intent still open means we do not know what
happened, and `reconcile()` asks the simulator: `GET /approvals` by proposal ID, the approval's status, or the
config history's `proposal_id`.

**4. A cube, not an event store.** Calls reduce to counts keyed by workflow, hour, segment, config version and
variant. A segment is one combination of lead features. A cohort is a rule over features, so any cohort can be
computed from the cube after the fact, including one defined later, and detection, experiments and the
supervisor view cannot disagree about who is in it. Cube size follows the number of variants and hours, not the
number of calls. `call_log` keeps the per-call record that auditors need.

**5. Pull, not callbacks.** Owners find out what happened by reading `change_item.status` every tick. A crash
cannot lose a notification, because there are none.

## What happens on restart

1. `open_store` looks for this scenario's database. If the simulator's clock or event IDs are behind ours, or our
   approvals are not there, the simulator restarted: it is a new world. The old file is set aside, not resumed.
2. `sync_workflows` re-reads policy and any config versions we missed, such as a supervisor edit during downtime.
3. `catch_up` ingests everything after the cursor.
4. `reconcile` resolves every open intent and every pending approval against the simulator.
5. The loop continues. Owners read their item status and carry on, because all their state is in the database.

**What can be lost:** a validation or LLM response if the process dies between the reply and the commit. Those
calls cannot be looked up afterwards, so at most one call's credits are spent again.

## Why time cannot get away from us

The simulator's clock moves only when we call advance, and we call it only after finishing the tick. Falling
behind cannot happen: a slow tick slows the run rather than queueing work. This is real backpressure, but it is
also why the simulator cannot show production's failure mode, where events keep arriving during a slow tick. See
"Production changes" below.

## Technology choices

| Decision | Choice | Rejected, and why |
|---|---|---|
| Language | Python 3 | Go: nothing measured needs it, and it would slow delivery. |
| Storage | SQLite (WAL), one writer | Postgres: cannot assume it on the grading box. Redis, Kafka: no requirement they solve. |
| Concurrency | One synchronous process | Async and workers: they add races, and the measured bottleneck does not call for them. |
| Dependencies added | None | `orjson`: parsing speed is not the constraint. |

## Production changes

The controller is deliberately a single process. For production at Formi:

- **Postgres replaces SQLite.** You already operate it. The SQL is close to portable; `INSERT ... ON CONFLICT`
  and `WITHOUT ROWID` are the main differences. Partition `call_log` by month.
- **Split ingestion from deciding.** Today they alternate because the clock waits. In production, ingestion runs
  continuously and deciding runs on a timer, sharing only the database. The transaction design carries over.
- **Partition by tenant.** Each worker owns a set of tenants via a lease. Every workflow belongs to one tenant, and
  nothing in the loop reads across tenants except the credit budget.
- **Real notifications.** The approval API has no channel to tell a supervisor anything. `report.py status` is the
  stand-in.
