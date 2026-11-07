# Outcome-First Agent Loop — Submission

**Name:** Anvarsha K · **Date:** 2026-11-07 · **Hours actually spent (be honest):** 8–9 hours · **Language / storage:** Python 3.12 / SQLite (WAL), no new dependencies

**To run it:** `pip install -r requirements.txt`, start a simulator, then `./run.sh <SIM_URL>`. Debugging, alerts and "something is wrong" steps are in [docs/RUNBOOK.md](docs/RUNBOOK.md). Design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Schema: [docs/DATABASE.md](docs/DATABASE.md). Tests: [docs/TESTING.md](docs/TESTING.md). Evidence: [reports/](reports/).

---

## 0. TL;DR

- One single-process controller. Events go into a SQLite store in one transaction per chunk; the controller learns call-time cohorts from history, runs sequential experiments on welcome message, voice and caller ID, and promotes only statistically proven winners with a small holdout. It then watches each winner and rolls it back if it decays.
- Every config change goes through one governance module: approval, or policy-permitted auto-apply for caller ID and call timing only. Customer-facing changes never auto-apply, even where policy allows it.
- `public_01`: leads concluded ÷ uploaded **+16.0%** over baseline. `public_02`: **+25.3%**, hallucinated calls cut from 416 to 168, the supervisor's bad prompt fixed in 225 minutes. `public_03`: **+20.7%**, one simulated day in about 48 s of wall time. Each run has one change the grader labels harmful; section 11 explains them.
- Killed at random moments and at nine deliberate crash points, the controller finished every time with no double-applied approval and no lost event. The random-kill run produced a decision log identical to the uncrashed run.
- Not built: per-person call time, any web UI, cross-tenant approver fairness, push notifications. Section 2 says why.

## 1. Assumptions

1. `docs/SIMULATOR.md` is the contract. Where a memo disagrees with the policy the API returns for a workflow, the policy wins. Policies are read at runtime, never assumed.
2. `event_id` strictly increases and chunks are ordered. Anything at or below the stored cursor is a duplicate.
3. Caller ID and call time act on **pickup**. Welcome message and voice act on **engagement**. So caller-ID tests target pickup and welcome/voice tests target engagement, and the other KPI is the guardrail. I did not assume welcome and voice cannot move pickup, and the data showed they can (section 11).
4. Repeat dials to one lead are not independent. I inflate variance by 1.10. This is an assumption, not a measurement.
5. History says nothing about variants: on `public_01` every historical call used the defaults. The controller uses history only for baselines and for time-of-day patterns.
6. The only identity the API gives for an approver is an approval ID. "Named human approval" is therefore recorded as approval ID plus decision time, and I tell Northwind that (section 3).
7. Tenant preferences stricter than policy come from memos, not the API. Kestrel's "two requests a day, none before 11:00" is configured by tenant ID in `controller/settings.py` and can be overridden by environment variable.
8. The decision grain is one 15-minute tick. The controller advances time one tick at a time.
9. Credits: each workflow may spend 1.5× an equal share of the starting budget. Unused credits are not redistributed.
10. A promoted winner keeps a 15% holdout until it decays or the scenario ends. That costs up to 15% of traffic on the older variant.
11. A supervisor's edit is authoritative. It is replaced only for a safety incident, and only through approval, with the proposal saying so.
12. The state directory is on a persistent volume. If it is deleted, in-flight approvals are orphaned (section 13).
13. `mid_flight: next_campaign_only` is not modelled specially. See section 13.
14. Hidden scenarios share the schema. Nothing in the controller names a workflow, variant, date or tenant outcome.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Ingestion at load scale | **Built** | Floor requirement, and everything depends on it. Measured in section 8. | Split ingest from decide for real-time production (section 14). |
| C2 | Joins and one shared cohort object | **Built** | Product asked for it, and it prevents three definitions of one group. Segments carry features; cohorts are rules over segments. | Multi-feature cohorts once traffic justifies them. |
| C3 | Bootstrap from history | **Built** | Baselines, hour-of-day patterns and cohort choice all come from the 5.5M-event history. About 72–120 s for `public_03` across three runs. | Incremental history refresh. |
| C4 | Call-time optimisation | **Partial** | Per-cohort built. Per-person cut: median 3–4 dials per person over 14 days at about 26% pickup is roughly one answered call per person, too little to estimate a preferred hour. | Per-person once a person has enough answered calls; needs a shrinkage model. |
| C5 | Caller-ID health | **Built** | Pickup is compared with the hour-adjusted expectation. A flagged number is rotated out, auto-applied up to the policy shift limit, with the remainder through approval. | A recovery probe so a repaired number can come back. |
| C6 | Welcome and voice experiments | **Built** | The core of the primary-KPI lift. Factorial by independent sampling, daily looks, Holm, interaction check. | Adaptive allocation; ramped exposure (section 13). |
| C7 | Promotion, decay, rollback | **Built** | Winners decay in the scenarios, and the grader measures how fast we leave. | Per-cohort decay thresholds. |
| C8 | Hallucination incident response | **Built** | Required by `public_02`. Known-good prompt first; LLM only if needed; validation; verification after applying. | Exercise the LLM path in a graded scenario (section 13). |
| C9 | Governance client | **Built** | The floor. Batching, timing, expiry, lapse, stale base, re-proposal, auto-apply. | Notification channel (the API has none). |
| C10 | Honest impact, affected leads, dry runs | **Partial** | Built, but a dry run cannot replay an arm that never ran. It reports exposure instead and says so. | Predictive model for new arms. |
| C11 | Supervisor view | **Partial** | `report.py status`, one screen per workflow. No service or UI. | A small read-only web page over the same queries. |
| C12 | Decision and audit store | **Built** | The floor, and the review's main exercise. | Retention and export for IRDAI-style requests. |
| C13 | Crash recovery, demonstrated | **Built** | Stakeholder SRE's first non-negotiable. | Fault injection against a real Postgres. |
| C14 | Observability of the loop | **Partial** | Per-tick metrics table, `report.py health`, alerts as log lines and audit rows. No metrics endpoint or paging integration. | Prometheus exporter over `loop_metric`. |
| C15 | Fair credits and attention | **Partial** | Credits: equal share ×1.5 per workflow. Approver attention: each workflow has its own policy limit already. Cross-tenant fairness cut. | A shared scheduler once approvers serve several workflows. |

**Principle.** I built the capabilities where a wrong decision hurts a client and can be checked: governance, statistics, rollback, recovery and audit. Those are what the constraints protect, and what the review probes. I cut what needs data the platform doesn't have (per-person timing), or adds surface without improving a decision (UI, fairness scheduler). I added C5 and C8 to my original list because `public_02` and the grader's time-to-fix measure require them.

## 3. Stakeholders

### 01 Sales — Nikhil Rao

- **Accepted:** one headline number per client. It is the business outcome, leads concluded ÷ uploaded, which is not a blend of the two KPIs. `report.py status` shows it with pickup and engagement beside it. Fewer, batched requests: one proposal per workflow carries everything queued, and a slot is held back for emergencies.
- **Declined:** a blended score, because it hides trade-offs and the README forbids it. Live "by evening": sequential tests need days. At +8 points the winner rule fires on average on day 3 (section 5), and approvers decide inside working hours. Rolling the persuasive prompt out "wherever available": a prompt change needs a validation run at or above the workflow's pass rate, and it is not even on every workflow's list.
- **Wrong against the contract:** "Kestrel has turned on auto-apply for everything." `ks_cancel` allows schedule, caller ID, welcome and voice with a 0.2 shift cap; `kest_renewal_0` allows schedule and caller ID only; `kest_kyc_1` has auto-apply off; no workflow allows prompt changes. "Northwind is fine with auto-apply on KYC": `nw_kyc` has auto-apply disabled with no allowed dimensions. The persuasive prompt P4 is not on the KYC workflow's list.

> **Reply:** Nikhil, for the summit we can show one headline per client: the share of uploaded leads that concluded, with pickup and engagement lift beside it. We won't blend pickup and engagement into one score; it hides trade-offs and Product has ruled it out. Winners go live in days, not hours, because a variant has to be proven and approvers decide in working hours. Two facts to fix before you present: Kestrel's auto-apply covers only some dimensions with a shift cap, and Northwind KYC has auto-apply off. We also can't roll the persuasive prompt out everywhere; each workflow needs its own passing safety validation.

### 02 Compliance — Meera Iyer, Northwind Life

- **Accepted:** anything a customer hears (welcome, voice, prompt) always goes through approval, even where policy would allow auto-apply. Prompt changes carry `validation_run_id`. `report.py changes` is the weekly summary of everything that changed and why. `report.py heard --person P --day N` answers the auditors' question down to variant, config version, proposal and approval. Nothing is sent outside approver hours (10–19, KYC 10–18).
- **Declined / qualified:** "never worse for a cohort, even briefly." An experiment necessarily puts some customers on an unproven variant. What we do: a welcome or voice experiment starts only with an approval request showing the exposure; the champion keeps 40%; an arm is removed once it falls behind (z ≤ −2) or breaches the guardrail; and a winner is checked against every cohort before promotion. We cannot promise zero exposure, and one exploration start in `public_02` was graded harmful.
- **Wrong against the contract:** call timing and caller-ID changes are "covered by the policy we signed" for the renewal workflow only. `nw_kyc` has auto-apply off, so those changes also need approval there. And "named human approval" cannot be produced: the API returns no approver name, so we record approval ID and time. Validation measures safety only, so a passing run is evidence that a script is safe, not that it is better; we verify engagement after applying.

> **Reply:** Meera, agreed on all five points with two corrections. Customer-facing changes always go through approval. Call timing and caller-ID changes may be auto-applied only where your policy enables it, which is the renewal flow; KYC has auto-apply off, so there they also wait for your approvers. The system records an approval ID and time, not a person's name, because the platform doesn't expose one; please confirm that meets your audit needs. Experiments do put some customers on untested variants; each welcome or voice test starts with an approval request showing the exposure, and weak variants are removed early. The weekly summary and 24-hour customer lookup already exist.

### 03 Platform & SRE — Kabir Menon

- **Accepted:** kill and restart tests (section 9), idempotent applies, actionable alerts (section 10), and a health view without logs. One simulated `public_03` day takes about 48 s of wall time here (section 8), against the 10-minute target.
- **Qualified:** "no new stateful infrastructure." SQLite is a file, not a service, so there is nothing to operate at 2 a.m. except a volume that must survive node recycling. If that volume is lost, in-flight approvals are orphaned. In production this moves to your Postgres; Redis is not needed.
- **Wrong against the contract:** nothing found. The 10-minute target is met with large margin on this machine, but this machine is faster than 4 vCPU and the clock here waits for us.

> **Reply:** Kabir, the kill test is done: killed 8 times at random moments and 9 times at deliberate crash points, it finished every time with no duplicate applies and no lost events. One simulated load day takes about 48 s of wall time here, but this machine is faster than your 4-vCPU box, so please re-run it there. I used SQLite, which is a file, not a service; the one operational requirement is a volume that survives node recycling, because losing it orphans pending approvals. For production I'd move it to your Postgres and skip Redis. Alerts and what to do about each are in docs/RUNBOOK.md.

### 04 Client operations — Anita Desai, Kestrel Finance

- **Accepted:** never silently undo her edits. A routine change queued against values she then edited is dropped, and an experiment on a dimension she touched ends and is not restarted. At most two requests a day and none before 11:00 for Kestrel, as an explicit per-tenant setting. One request per workflow carrying everything queued. One screen: `report.py status`. The compliance-script trap is handled: prompts whose catalog text reads as scripted are tried last, and every prompt change is checked for lost engagement after it goes live, with automatic follow-up.
- **Qualified:** "tell me before anything changes on a running campaign." Customer-facing changes wait for approval, but policy lets `ks_cancel` auto-apply caller-ID and timing changes. The API has no way to notify her first, so those appear in her "changed in the last 24h" list. A one-line tenant setting routes every Kestrel change through approval instead, at the cost of hours of delay on caller-ID emergencies.
- **Her edits and safety:** if a supervisor's prompt starts producing hallucinations, we propose replacing it, with the proposal stating that it replaces her edit and why. It never applies without approval. That is the one case where her change is overridden.
- **Wrong against the contract:** none; her limits are stricter than policy (4 requests a day, approver from 10:00), which is allowed. Emergency fixes are exempt from the 11:00 rule but still respect the two-a-day cap and approver hours.

> **Reply:** Anita, your edits are never reverted silently. If a queued change would overwrite something you changed, it is dropped, and an experiment on a setting you touched stops. Kestrel requests are capped at two a day, none before 11:00, and each is one request listing everything. The one exception: if a prompt starts producing hallucinations, we'll propose replacing it, say plainly that it replaces your edit, and apply nothing until approved. I can't notify you before a caller-ID rotation the policy lets us make automatically; it shows on your screen afterwards, or we can route those through approval too, at the cost of slower fixes.

### 05 Product — Tara Kulkarni

- **Accepted:** separate KPIs everywhere. One cohort object (`cohort` table, config cohorts and `segment_to_cohort`). A proposal always carries impact score, diff, affected leads and dry run. An expired or rejected proposal is dropped and rebuilt from fresh data after a two-hour pause, never resubmitted as it was. Only observed calls count: an arm that never ran has no rows, and a unit test shows it adds nothing to an estimate. A winner updates only that workflow.
- **Chosen and costed (minimum cohort size and window):** a cohort needs at least 8% of dials; at most one splitting feature is used; an arm needs 300 target-metric trials before any test; the review is once a day for up to 7 looks. Section 5 gives the speed vs false-winner trade-off from simulation.
- **Declined:** per-person call time as a P0. Caller-ID health is built and the supervisor view is minimal.
- **Wrong against the contract:** "seniors in Tamil Nadu" cannot be expressed. Lead features are language, city tier, age band and value band; there is no state. "Tamil-speaking seniors" can.
- **Honesty about the impact score:** `1 − exp(−expected extra concluded leads per day / 50)`, with a 0.8 floor for emergency fixes. It is monotonic in expected gain, so the queue ranks sensibly, but the constant 50 is a scale choice and is not calibrated. Experiment starts score 0, because no lift is known yet; their value is information.

> **Reply:** Tara, separate KPIs, one shared cohort object, proposals with impact, diff, affected leads and dry run, expiry treated as rejection, and evidence only from calls that happened are all in. My choices: a cohort needs 8% of dials, an arm needs 300 trials, and we review daily for up to seven looks, giving about a 2.5% false-winner rate in simulation and a win at +8 points on day 3 on average. Two things you should know: per-person call time is cut, because most people answer about once, and "seniors in Tamil Nadu" can't be expressed since leads have no state field. Impact score ranks by expected extra concluded leads but isn't calibrated yet.

### 06 CTO — Aryamann

- **Accepted:** all of it. Regulated outcomes over raw lift, so customer-facing changes need approval. Credits spent where they change a decision: the previously used prompt is tried before the LLM is paid for, and a validation is never bought twice. Scale honesty: section 8 measures what exists and labels what is estimated. Docs for the engineer on call: `docs/RUNBOOK.md`. Choosing: section 2.
- **Wrong against the contract:** the README's "about 30k events/day" for the dev scenario does not match `public_01`'s history, which has 187,400 events over 14 days (about 13.4k a day). I did not rely on it.
- **Who I said no to:** Sales on speed and blending; Compliance on never-worse-even-briefly; Product on per-person timing.

> **Reply:** Aryamann, I took the constraints as the order of priority. Customer-facing changes need approval, and credits go only where they change a decision. I said no to Sales on a blended score and same-day rollouts, to Compliance on a guarantee that no customer ever hears an untested variant, and to Product on per-person call timing, because the data supports about one answered call per person. Section 8 separates what I measured on the load scenario from what I'm estimating at 10×. One README figure, 30k dev events a day, didn't match the history file, so I didn't use it. I built caller-ID health and a CLI supervisor view; the UI is cut.

## 4. Architecture

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

- **State** lives only in the SQLite file `state/<scenario>.db`: ingest cursor, KPI cube, per-call log, cohorts, experiments, incidents, change items, proposals with every transition, in-flight intents, credit ledger, the decision log and per-tick metrics. Nothing important is held only in memory; caches (lead→segment, baselines) are rebuilt on start.
- **One owner for config.** Owners (`experiments`, `incidents`, `schedule`) never call the simulator to change anything. They queue a change item and read its status next tick. `governance` batches, submits, applies and reconciles. So two modules can never race a workflow's config, and a restart needs no callbacks to replay.
- **On restart:** the store is reopened; policies and any config versions made while we were down (such as supervisor edits) are re-read; `catch_up` ingests after the cursor; `reconcile` asks the simulator about every in-flight action and every pending approval; the loop continues. If the simulator's clock or event IDs are behind ours, the simulator restarted, which is a new world: the old database is set aside instead of resumed.

More detail, with the module table and the reasons for each choice, is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## 5. The learning loop

- **Cohorts.** From history, the controller picks the one lead feature whose values differ most in *when* they answer, using a chi-square for the feature × hour interaction in pickup (per degree of freedom ≥ 2, else no cohorts). Values with at least 8% of dials become cohorts; the largest stays in `default`. One feature keeps cohorts large: crossing features would starve every test of samples. On a 3,600-dial day that threshold is about 290 dials, roughly 80 connected calls. A cohort holding a quarter of traffic yields about 240 connected calls a day, so an arm with 20–30% of them needs 4–6 days to reach 300 trials; a minimum-size cohort needs two to three weeks. So cohorts mainly drive call timing and the harm check before promotion, and per-cohort variant winners are possible but rare in short scenarios. On `public_01` it chose `age_band`.
- **Experiment design.** One state machine per workflow and dimension. Welcome message and voice are sampled independently per dial, so running both at once is a factorial design: every dial informs both, and one approval carries both. The champion keeps 40% and challengers share 60% equally. Caller-ID tests keep incumbents and give each new number 10%. Allocation is fixed with elimination, not a bandit, which keeps decisions auditable. Interactions: a winner must not lose badly (z ≤ −2.5) when paired with any arm of the other dimension.
- **Deciding a winner.** Arms are compared only inside strata of (config version, cohort), so a supervisor edit or traffic-mix change cannot produce a Simpson's-paradox winner. The test is one daily look, at the hour the approver's day opens, with a Pocock-inflated boundary for 7 looks (z ≈ 2.49 for one test at one-sided 2.5%), Holm step-down across challengers, a minimum lift of 1.5 points, 300 trials per arm, and a guardrail non-inferiority test (the other KPI's 80% one-sided lower bound must clear the allowed drop). If the other KPI is shown to fall past its margin (z ≤ −2), the arm is removed. If it is merely unproven, we wait. After 7 looks without a winner, arms behind the champion are removed and nothing is declared a winner.
- **False-winner rate and the speed trade-off.** Measured by simulating the exact decision functions (`scripts/false_winner_rate.py`; 3 challengers, 200 trials per arm per day, base rate 0.54, 4,000 A/A runs, independence assumed). Without correcting for daily looks the false-winner rate is **10.2%**. With the correction:

| one-sided α (family-wise) | A/A false-winner rate | power at +5 pts | power at +8 pts | power at +12 pts | mean day of win at +8 |
|---|---|---|---|---|---|
| 0.01 | 0.002 | 0.34 | 0.92 | 1.00 | 3.9 |
| **0.05 (used)** | **0.025** | **0.64** | **0.99** | **1.00** | **3.1** |
| 0.10 | 0.061 | 0.77 | 0.99 | 1.00 | 2.7 |
| 0.20 | 0.165 | 0.90 | 1.00 | 1.00 | 2.4 |

  A looser α finds smaller lifts sooner and promotes more false winners. With this rule, small real lifts (+5 points) are missed about a third of the time inside a short scenario, which is the cost of caution.
- **Decay and rollback.** A promoted winner keeps 15% of traffic on the previous variant. Every morning the winner is compared with that concurrent holdout over the last 48 hours. It is rolled back if z ≤ −2.24, or if z ≤ −1 against the holdout *and* the winner's last 24 hours are significantly below the holdout's whole-run rate (z ≤ −2.8). It needs at least 100 holdout trials. The rollback is an emergency item, and if it is rejected the check is repeated within the hour. In `public_02` the decayed welcome message was caught on day 5 at 10:00.
- **Call time and caller ID.** Call time: per-cohort hour weights from history, shrunk toward the no-interaction estimate so noise cannot create a preference, bounded to 0.5–2.0, never 0 (a zero would leave capacity unused). The dry run reweights history and reads the real pickup in those cells. A schedule change is watched for 24–48 hours against the hour-adjusted pre-change baseline and undone automatically if pickup or engagement falls significantly. Caller ID: flagged when pickup over 12 hours is far below what the hour-of-day history predicts (z ≤ −3.5, at least 150 dials, at least 20% below expectation); a simultaneous drop on every number is treated as systemic and reported, not rotated.
- **Guardrail KPI.** Every test has a guardrail on the other KPI. A change that helps the primary and breaches the guardrail is removed; section 11 shows the case where this cost engagement.

## 6. Governance

**Proposal lifecycle.**

```
draft ──► pending ──► approved ──► applying ──► applied
  │          │            │            │
  │          │            └─► lapsed   ├─► approved (crash before the apply landed; retry)
  │          ├─► rejected             ├─► stale_base
  │          └─► expired              └─► lapsed
  ├─► applied   (auto-apply path)
  ├─► refused   (auto-apply path, 403)
  └─► failed    (submit error, or a crash before the request reached the simulator)
```

Every transition is recorded, and the code refuses any move not in the table (unit-tested). On any terminal state except `applied` the items are dropped with the reason and the owner regenerates from the then-current config and data after a two-hour pause. A stale base re-reads the config first.

**Batching and timing.** One proposal per workflow carries everything queued. Routine requests are held until the approver's hours open, and held again if the mean approver latency would push the decision past closing. One of the policy's daily slots is kept for emergencies: routine work may use all but one. If an emergency is waiting, a routine approved proposal is not applied first, since that would make the emergency stale. Emergencies can go out alongside an active routine proposal.

**Auto-apply, and when I don't use it.** Used only for caller ID and call timing, only where the workflow's policy enables those dimensions, and only when the shift is within `max_shift`. If a fix needs a larger move, the controller moves as far as the limit allows and asks approval for the rest; it never splits changes to walk around the limit. Welcome message, voice and prompt are never auto-applied, even on `ks_cancel` where policy would allow welcome and voice, because Compliance requires approval for anything a customer hears. Zero auto-apply refusals in every run.

**Impact, affected leads, dry run: how computed, and how honest.**
- *Impact score* is `1 − exp(−expected extra concluded leads per day / 50)`; emergencies floor at 0.8; experiment starts are 0. Not calibrated.
- *Affected leads* is the workflow's average daily batch size, scaled by the share of traffic that changes.
- *Dry run* is one of three kinds, labelled in the payload: a **historical reweighting** (schedule) that reads real outcomes in the reweighted cells; a **replay of the observed arms** (promotions), which are concurrent comparisons and not forecasts; an **exposure estimate** (new arms), since there is no history to replay. For a new arm it does not claim an outcome.

**Supervisor edit mid-experiment.** Config is re-read when a `config_changed` event arrives. Experiments on a touched dimension end with outcome `supervisor_override` and are not restarted. Routine queued items whose target values changed are dropped with a decision-log entry. Emergency items are not dropped but are proposed openly, naming the supervisor edit they replace. Added cohorts are appended after the supervisor's, never reordering them. In `public_02` a supervisor changed `nw_renewal`'s voice at D5 15:15, which ended that experiment, and `ks_cancel`'s prompt at D3 11:30, which led to the incident in section 7.

## 7. Incidents

**Detection and false alarms.**
- *Hallucination:* over a 6-hour window, at least 100 connected calls, flag rate at least 2.5× the history baseline and z ≥ 5 (z ≥ 3.5 and 2× if the prompt changed in the last 24 hours, since it is the prime suspect). Exact binomial false-alarm probability per check at a 1.4% baseline: 8.8e-5 at 100 calls, 6.4e-5 at 250, 1.2e-5 at 500, 5.1e-7 at 1,000. The risk is concentrated in the thinnest windows. With 96 overlapping checks a day that is roughly 1% per workflow-day in the worst case, and much lower on busy workflows. Observed: no false hallucination incident in any final run.
- *Caller ID:* hour-adjusted pickup z ≤ −3.5, at least 150 dials and at least 20% below expectation. Flags that appear on all numbers at once are reported as systemic.

**Picking a fix.** First, the most-exposed other prompt that already ran on this workflow with an acceptable flag rate; a validation run confirms it (4 credits at n=400; a result already paid for is reused). Only if none passes does it ask the LLM (3 credits) and validate each suggestion. Anything below the policy pass rate is out. Among passing candidates, those whose description reads as scripted ("strict", "verbatim") rank last, since safe-but-scripted is the known way to lose customers. Higher safety scores above the bar don't rank anything, because validation says nothing about engagement.

**Credits.** A global check (never go below zero) and a per-workflow allowance (1.5× an equal share). A refused spend raises a page-level alert. In `public_02` the whole incident cost **4 credits** of 200 (one validation run, zero LLM calls).

**Verification after applying.** After at least 300 connected calls on the new prompt: flags must be back to normal (z < 3) and engagement must not be significantly below its pre-incident level. If either fails, the incident reopens and another candidate is tried.

**Timeline from `public_02`, `ks_cancel` (times are simulated, from the audit store).**

| Time | Event |
|---|---|
| D3 11:30 | Supervisor ships prompt P2 (v6), per the simulator; the controller records it at its next tick, 11:45. Not reverted. |
| D3 13:45 | Detected: flags at 4.5% vs 1.4% over 247 connected calls (z = 4.2, threshold 3.5 because the prompt changed). Previous prompt P1 selected; validation passes (0.985, n=400). |
| D3 13:45 | Proposal submitted: states that it replaces a supervisor's edit; impact 0.8. |
| D3 15:15 | Approved and applied (v8). Grader time to fix: **225 minutes**. |
| D3 18:00 | Verified: flags normal, engagement held. Resolved. |

The detection delay (135 minutes) is data-limited: z = 4.2 needed 247 calls. Approval was another 90 minutes. A lower threshold would be faster and would cost more false alarms.

Caller ID, `ks_cancel`: pickup of D2 fell at D2 10:00; flagged D2 11:15 (16.4% vs 25.8% expected over 402 dials); auto-applied the policy-limited step in the same tick; the remainder was approved and applied by 13:00. The grader's flagged-number share fell from 12.1% on that day to 0 afterwards.

The LLM path was not needed in the final graded runs because the known-good prompt passed. It is covered by unit tests with a fake LLM and was exercised end to end in an earlier development run; I do not present that as a graded result.

## 8. Scale

Measured on `public_03_load` (12 workflows, 5 simulated days, 4,200,164 live events plus 5,537,379 history events). Full detail, three runs and caveats: [reports/load_test.md](reports/load_test.md).

| Quantity | Measured |
|---|---|
| Ingest throughput | **43,300 events/s** while ingesting; 30,300 events/s including decisions |
| History bootstrap | 5.5M events in 120 s (72 s in two earlier runs) |
| Wall time per simulated day | **47.6 s**: ingest 19.4 + deciding 8.3 + the simulator's own advance 19.6. Target was under 10 minutes. |
| Time to process a 15-minute tick | median 0.33 s, p95 0.49 s, p99 3.2 s, max 4.8 s (the 08:00 lead upload, 192,012 events) |
| End-to-end decision lag | at most one tick (15 simulated minutes) plus the tick's processing; real latency is dominated by approvers (mean 213 simulated minutes) and statistics (days) |
| Peak memory | **261 MB**, flat across all five days |
| Database | 589 MB after 5 days, about 102 MB a day |
| Retries, duplicates, malformed events | 0, 0, 0 |

The scenario averages about 19 events a second over the dialling window; the controller handled around 1,500 times that. Throughput is not what limits the design. Inside ingest, a microbenchmark puts JSON parsing at roughly 8 s of the 19 s and call-log writes at roughly 3 s; the rest is per-event Python I did not break down.

**Honest limits of these numbers.** This was a 12-thread, 32 GB Windows machine, launched with a four-CPU affinity mask that I did not independently confirm took effect; the grading box is 4 vCPU / 8 GB Linux. The controller is single-threaded, so single-core speed is what matters. The simulator's clock waits for the controller, so real-time pressure never occurs. Startup moved from 72 s to 120 s between runs with no relevant code change.

**What breaks first at 10× (estimate, not run).** Not CPU: ten times the events is about 190 s of ingest per simulated day. First is **storage**: about 1 GB a day, 30 GB a month in one SQLite file. Second is **memory**: 960,000 active leads cost roughly 220 MB in a dictionary, so ten times needs about 2 GB. Then the **single writer** once hundreds of workflows share one process. Then **deciding**, which costs about 0.7 s per workflow per simulated day and needs incremental aggregates at hundreds of workflows. Beyond compute, approver limits (2–4 requests per workflow per day) and credits (about 33 per workflow) are policy limits that hardware does not move.

**How I'd partition it.** By tenant. Each worker holds a lease on a set of tenants and owns their ingestion and decisions, since every workflow belongs to one tenant and nothing reads across tenants except the credit budget, which becomes a table with an atomic decrement. Storage moves to Postgres with `call_log` partitioned monthly; ingestion runs continuously and deciding on a timer, meeting only in the database.

## 9. Reliability

**Method.** `scripts/run_scenario.py` runs the simulator and controller as separate processes and audits the result against the simulator's own record. (a) **Random kills**: `--kills 12` hard-kills the controller at random moments; with the random timing, 8 kills landed before it finished. (b) **Deliberate crash points**: nine named points exit the process with no cleanup, once each, at the dangerous moments: before and after each of submit, apply and auto-apply; mid-chunk before commit; between advance and ingest; between ingest and decide. Details and logs: [reports/crash_recovery.md](reports/crash_recovery.md).

**Results.** Both runs finished with exit 0 and passed the audit: no approval applied twice; every config version and actor matches the simulator's; no applied config without a local proposal; no approval lost or out of step; no unfinished intents; cursor at the simulator's last event; cube dials equal call-log rows. The random-kill run's decision log and proposal table were **identical** to the uncrashed run, and the grade table matched. The crash-point run's outcome differed (conclusion +21.6% vs +25.3%) because a crash before an outside call lost that unsent action, and the work was regenerated later; this is the expected cost of such a crash, not a correctness failure.

**State lost on a crash:** nothing durable. At most one credit-spending response (a validation, 4 credits, or an LLM call, 3 credits) can be lost if the process dies between the reply and the commit, and it will be bought again. In-memory caches are rebuilt.

**Idempotency keys.**

| Operation | Key | If repeated |
|---|---|---|
| Chunk ingestion | `event_id` high-water mark, committed with the data | Duplicate events are counted and skipped |
| Submit approval | `proposal_id` = `{workflow}-pNNNN`, unique in the database and at the simulator | 409 `duplicate_proposal_id` → look it up by proposal ID and adopt it |
| Apply | `approval_id`; intent written first | 409 `not_approved {applied}` → recorded as applied |
| Auto-apply | `proposal_id` | Confirmed from config history on restart |
| Validation / LLM | not retriable | Never repeated blindly; a recorded result is reused |

**When the simulator returns errors.**

| Condition | Behaviour |
|---|---|
| Timeout, 429, 5xx on reads | Exponential backoff with full jitter, up to 6 retries, capped at 30 s |
| Same, on advance | Re-read `/sim/state` before any retry, so time cannot move twice |
| Same, on credit-spending calls | Not retried after the request may have landed |
| Sustained outage | Loop restarts from durable state up to 30 times; then exit 2 with a page |
| 409 stale base | Re-read config, drop items, regenerate |
| 410 lapsed, rejected, expired | Items dropped with reason; regenerate after a pause |
| 402 out of credits | Page-level alert; no spend |
| 403 auto-apply refused | Logged; owner regenerates (observed: none) |
| Malformed event line | Kept verbatim in `bad_event`; chunk continues; ticket alert |
| Chunk line count short | Transaction rolls back; chunk refetched (5 attempts) |
| Simulator restarted | Detected by a clock or event ID going backwards; old DB set aside |
| Clock goes backwards mid-run | Exit 3 with a page; does not act on a new world |

Not tested end to end: sustained 5xx/429 in a full run (the simulator does not inject faults; the retry policy has unit tests), and power loss.

## 10. Audit & observability

**"Why did workflow X change at time T, who approved it, and what happened after?"** Real output from the `public_02` run:

```
$ python -m controller.report --db state/public_02/public_02_incidents.db why --workflow ks_cancel --at D3T15:15
ks_cancel v8 applied D3 15:15  actor=controller_approved  changed=["prompt_version"]

PROPOSAL ks_cancel-p0007 via approval  approval_id=apr_00008
  who approved: client approver, recorded as apr_00008 (the API does not expose a name)
  summary: Replace prompt P2 with P1: hallucination flags at 4.5%, normal is 1.4% (current prompt was set by a supervisor edit, v6)
  rationale: NOTE: this replaces a prompt a supervisor set in v6 (D3 11:45). It is proposed only because of the safety incident below, and takes effect only if approved. Population-level spike (z=4.17, 247 connected calls). Last prompt with a good record on this workflow; validation pass rate 0.985 on n=400. Engagement is checked after applying, with automatic follow-up.
  impact score: 0.8  affected leads: 1500
  diff: {"prompt_version": {"from": "P2", "to": "P1"}}
  dry run: {"21": {"n": 400, "note": "Measures safety only, not engagement.", "pass_rate": 0.985, "run_id": "val_00001", "type": "validation_run"}}

LIFECYCLE
  D3 13:45  new -> draft  
  D3 13:45  draft -> pending  apr_00008
  D3 15:15  pending -> approved  []
  D3 15:15  approved -> applying  
  D3 15:15  applying -> applied  v8

EVIDENCE (decisions linked to the items in this proposal)
  item 21 from incident:2: Replace prompt P2 with P1: hallucination flags at 4.5%, normal is 1.4% (current prompt was set by a supervisor edit, v6)
    D3 15:15 fix_applied: Fix live in v8; verifying on live calls.
    D3 13:45 incident_detected: Hallucination flags on P2 at 4.5% vs baseline 1.4% over 247 connected calls.
      data: {"baseline":0.0137,"connected":247,"engagement_before":0.5069,"flagged":11,"prompt":"P2","prompt_recently_changed":true,"rate":0.0445,"threshold_z":3.5,"window_hours":6,"z":4.17}

WHAT HAPPENED NEXT
  24h before: {'dials': 4800, 'pickup': 0.246, 'engagement': 0.4886, 'flag_rate': 0.0237}
  24h after:  {'dials': 4800, 'pickup': 0.2667, 'engagement': 0.4906, 'flag_rate': 0.0164}
  D3 18:00 incident_resolved: Fix verified: flags normal and engagement held on P1.
  D4 10:00 schedule_verified: Call-time change verified over 2 days.
  D4 16:00 winner_promoted: Promoted in v9, keeping a holdout to detect decay.
  D7 10:00 decay_detected: caller_id: ['D3'] decayed; rolling back.
  D7 11:45 rollback_applied: Rolled back in v10.
```

Times in this store are the tick at which the controller recorded a change, so a supervisor's edit appears up to 15 minutes after the simulator's own timestamp (here 11:45 against 11:30). Other commands, all reading only the controller's database: `status` (the supervisor's one screen), `heard --person P --day N` (what a customer heard and why that variant was live), `health` (ingest lag, throughput, memory, credits, decisions by kind, approval latency, alerts, open intents), `changes` (the weekly summary).

**Page on:** credits exhausted while an incident is open; an incident with no validated fix; every caller ID on a workflow flagged; the simulator clock going backwards; a sustained simulator outage. Each message names what to look at.
**Ticket, not page:** malformed events; all caller IDs falling together (looks systemic); a failing single-workflow experiment.
**Never alert on:** a rejected or expired proposal, a single-day KPI move, or a dropped arm. They are normal and are visible in `status`.

## 11. Results

From `reports/public_01.md`:

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.370 | +11.1% | 0.254 | +6.4% | 0.506 | +3.1% | 12% | 60 (47) |
| nw_kyc | 0.495 | +36.2% | 0.301 | +24.5% | 0.628 | +6.2% | 39% | 20 (21) |
| nw_renewal | 0.463 | +10.2% | 0.281 | +6.6% | 0.560 | +2.6% | 20% | 45 (42) |
| **ALL** | **0.427** | **+16.0%** | **0.272** | **+9.9%** | **0.551** | **+4.0%** | **22%** | **125 (110)** |

12 changes applied: 5 beneficial, 6 neutral, 1 harmful. Governance: 8 approval requests, all approved; 4 changes auto-applied under policy; no auto-apply refusals. Credits used: 0.

From `reports/public_02.md`:

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.366 | +47.8% | 0.245 | +33.2% | 0.511 | +7.2% | 42% | 79 (330) |
| nw_kyc | 0.450 | +20.1% | 0.289 | +19.5% | 0.589 | -0.6% | -27% | 32 (31) |
| nw_renewal | 0.471 | +10.4% | 0.284 | +8.5% | 0.556 | +1.5% | 17% | 57 (55) |
| **ALL** | **0.419** | **+25.3%** | **0.267** | **+20.2%** | **0.544** | **+2.6%** | **26%** | **168 (416)** |

17 changes applied: 12 beneficial, 4 neutral, 1 harmful. Governance: 17 approval requests, 14 approved and applied, 3 declined as judgement calls; 3 more changes auto-applied under policy; no refusals. Credits used: 4 of 200. Time to fix the supervisor's bad prompt: 225 minutes.

From `reports/public_03.md` (all 12 workflows): leads concluded **+20.7%**, pickup +10.0%, engagement +7.3%, 40% of the engagement headroom captured. 58 changes applied: 43 beneficial, 14 neutral, 1 harmful. Governance: 46 approval requests (41 approved, 5 declined, 1 lapsed after approval), 18 changes auto-applied, no refusals. 0 credits used.

**Where I helped.**
- *Pickup is where most of the lift came from.* Pickup rose 6–33% per workflow in `public_01` and `public_02` and 3–27% in `public_03`. The gains came with caller-ID changes the grader scores as health improvements (for example +0.233 for `nw_kyc` in `public_01` and +0.205 in `public_02`) and with call-timing changes. The KYC workflows, whose primary KPI is pickup, show the largest conclusion-rate lifts in `public_03` (+33% to +44%).
- *`public_02`'s hallucinations fell from 416 flagged calls to 168*, mostly the supervisor's bad prompt, fixed in 225 minutes, of which 135 were detection and 90 approval.
- *A decaying welcome message was caught in `public_02`* and the controller was off it by day 6.

**Where I was harmful, and why.** One change per scenario is labelled harmful.
- **`public_01`, `nw_renewal` v6 (removing welcome message W3).** The controller's own look had W3 ahead of W1 by 11.8 points of engagement (z = 3.9), past the winner boundary. It still removed W3, because the same evidence showed W3 cut **pickup** by 4.5 points (z = −3.2) against a 2-point guardrail on this workflow. That is exactly the rule in the constraints: respect the guardrail. The grader scores welcome-message changes on engagement alone, so it counts the removal as harmful. At a pickup near 0.28 and an engagement near 0.54, an 11.8-point engagement gain and a 4.5-point pickup loss roughly cancel in leads concluded. I would defend the decision, and I would also note that a rule weighing the combined outcome could have reached a different answer.
- **`public_02`, `nw_kyc` v2 (the opening bundle).** One approved change started the cohorts, call timing, caller-ID rotation and welcome/voice tests together. The grader measured engagement down 0.024 at that moment, past the workflow's 0.02 guardrail, even though the caller-ID part gained 0.081 pickup. Starting a test puts 60% of traffic on unproven arms immediately. I would ramp exposure up instead.
- **`public_03`, `arav_renewal_0` v6 (an auto-applied caller-ID change).** It removed a caller-ID arm that was behind its champion at z ≤ −2, which has about a 2.3% error rate per equal arm per look. With dozens of arms checked, a few false removals are expected; one in 58 changes is consistent with that.

**What I'd change.**
1. *Decay detection is too slow for short scenarios.* In `public_03`, W3 on the four renewal workflows only began to fall on day 4 and was still ahead of its holdout when the scenario ended, so it kept about 85% share to the end. A winner that decays while still ahead of the holdout needs a test on its own lift over time, in windows of hours.
2. Ramp exposure for new tests.
3. Correct the arm-removal rule for the number of arms checked.
4. Choose each test's target KPI from the workflow's objective (weakness 1).

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Go | Faster | Nothing measured is CPU-bound: ingest runs at tens of thousands of events/s against about 10–20/s needed. Python was quicker to deliver and review. |
| Postgres | Already run by the SRE team | Can't assume one on the grading box. SQL kept portable; named as the production path. |
| Kafka / Redis / Celery | "Event-driven at scale" | The source is a pull API with a clock that waits, so there is no queue to drain. Adds operations burden for no measured gain. |
| Async / worker pool | Concurrency | A single writer removes races. Throughput is not the limit. |
| Per-event commits | Simplicity | Far slower than one transaction per chunk, and no better crash safety. |
| Raw events as the main store | Flexibility | The largest table, and nothing reads it. Counts in a cube answer every decision; per-call rows are kept only for audit. |
| Index on `call_log(person_id)` | The audit query | Measured: 36.0 s vs 3.4 s per load day to write. Dropped; a day scan answers the query in under a second. |
| Bandit allocation | Faster learning | Harder to audit and to explain to a compliance head. Fixed allocation with a sequential test and elimination. |
| Bayesian posterior winner | Natural stopping | A frequentist sequential test with a measured error rate is easier to defend line by line. |
| Revert every arm at "no winner" | Simple | A change with no evidence behind it; moves customers off arms that weren't behind. Arms behind the champion leave; the rest stay. |
| Callbacks from governance to owners | Event-driven | A crash can lose a callback. Owners read item status each tick instead. |
| LLM picks the prompt | Fewer rules | Constraint 4 forbids it deciding winners. The LLM only proposes; validation filters; live data verifies. |
| Per-person call time | Product's headline | About one answered call per person in history. |

## 13. Known weaknesses

1. **Targets by dimension, not by workflow primary.** Welcome and voice tests optimise engagement everywhere. On a workflow whose primary KPI is pickup (KYC), engagement is the guardrail KPI, so those tests optimise the guardrail while guarding pickup at 0.01. The data showed welcome message *can* move pickup (a −4.5 point effect for W3 on `public_01`), so this is a real gap. Fix: pick the target from the workflow's objective and guard the other KPI at its policy margin.
2. **Exploration costs something.** A new test puts 60% of traffic on unproven arms at once. One start bundle on `public_02` (`nw_kyc` v2) was graded harmful (engagement −0.024, past that workflow's 0.02 guardrail) before the weak arms were removed. A ramp (20% first, raise after a clean look) would cut that and slow learning; I didn't tune it on three scenarios.
3. **Futility removal is not corrected for multiplicity.** An arm leaves at z ≤ −2, about a 2.3% error per equal arm per look. One caller-ID arm removal in `public_03` (`arav_renewal_0` v6) was graded harmful (−0.023).
4. **Thresholds were adjusted after seeing the public runs** (incident z, the decay rule, the guardrail confidence, the no-winner behaviour). The public numbers therefore flatter hidden-scenario performance.
5. **Dry runs for new arms are exposure estimates, not outcomes.** Impact scores are not calibrated.
6. **`mid_flight: next_campaign_only` is not modelled.** Experiments on such workflows begin effectively at the next 08:00 batch. Stratifying by config version keeps the analysis valid, but days-left reasoning ignores the delay.
7. **The scripted-prompt rule is a keyword match on the catalog text.** The real defence is the engagement check after applying.
8. **Single process, single SQLite file.** A lost state directory orphans in-flight approvals and loses the audit trail. Untested at 10×.
9. **No notification channel.** Supervisors have only `report.py status`. Alerts are logs and audit rows.
10. **The LLM path is not in the final graded runs** (unit-tested only).
11. **The test machine is not the target.** Windows, 12 threads, 32 GB, faster than 4 vCPU. The simulator clock waits for the controller, so real-time pressure never occurs.
12. **Hidden-scenario generalisation is untested** beyond not hardcoding. Tenant preferences are keyed by tenant ID in settings.
13. **Disclosure.** While exploring the repository I ran a search over the scenario files and one line of hidden-truth commentary (that a `public_01` caller ID starts degraded) appeared in the output. Nothing in the controller reads scenario files or refers to that caller ID; the `public_01` handling of it comes from the generic detector.

## 14. If this went to production at Formi next month

1. **Week 1, day 1–2:** Run read-only against production events with no write path enabled. Reconcile ingest counts with the dialler's own counts. Fix every assumption that breaks (timestamps, late events, duplicates).
2. **Day 3–4:** Move the store to your Postgres. Same schema, `call_log` partitioned monthly, retention agreed with Compliance. Put the controller on a node with a persistent volume or none at all.
3. **Day 5:** Separate ingestion (continuous) from deciding (timer) with the database as their only contact. Add per-tenant leases so workers can split the load.
4. **Week 2, day 1–2:** Wire alerts to your paging tool with the runbook's page/ticket split. Export `loop_metric` to your metrics system.
5. **Day 3:** Shadow mode: the controller writes proposals and decisions to the store but never submits. Compare its choices with what humans did.
6. **Day 4:** Enable one low-risk tenant, caller-ID and timing only, with approval required for everything.
7. **Day 5:** Review false alarms and rejections with Compliance. Only then widen to welcome and voice experiments.
8. **After that:** Calibrate the impact score against actual approver decisions, add a notification channel for supervisors, and fix weaknesses 1–3.
