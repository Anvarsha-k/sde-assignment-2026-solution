-- Controller store. See docs/DATABASE.md for why each table exists.
-- Time columns named *_minute are simulated minutes since D0 00:00; hour_idx is minute // 60.

CREATE TABLE IF NOT EXISTS run_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Advanced in the same transaction as the chunk's effects, which is what makes ingestion exactly-once.
CREATE TABLE IF NOT EXISTS ingest_cursor (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  last_chunk INTEGER NOT NULL,
  last_event_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow (
  workflow_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  spec TEXT NOT NULL                 -- GET /workflows/{id}: policy, options, objective
);

CREATE TABLE IF NOT EXISTS config_version (
  workflow_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  actor TEXT NOT NULL,
  changed_dims TEXT NOT NULL,
  proposal_id TEXT,
  approval_id TEXT,
  config TEXT,                       -- null only if two edits landed in one tick
  applied_minute INTEGER,
  PRIMARY KEY (workflow_id, version)
);

-- One row per distinct feature combination. At most a few hundred rows, however many leads arrive.
CREATE TABLE IF NOT EXISTS segment (
  segment_id INTEGER PRIMARY KEY,
  features TEXT NOT NULL UNIQUE      -- canonical JSON
);

CREATE TABLE IF NOT EXISTS campaign (
  campaign_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  leads INTEGER NOT NULL,
  config_version INTEGER NOT NULL,
  created_minute INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS campaign_wf ON campaign (workflow_id, created_minute);

-- Working set: leads of campaigns that can still be dialled. Older campaigns are deleted.
CREATE TABLE IF NOT EXISTS lead_dim (
  campaign_id TEXT NOT NULL,
  lead_id TEXT NOT NULL,
  segment_id INTEGER NOT NULL,
  PRIMARY KEY (campaign_id, lead_id)
) WITHOUT ROWID;

-- The analytical core. All KPIs, experiments and detectors read this table.
CREATE TABLE IF NOT EXISTS kpi_cube (
  workflow_id TEXT NOT NULL,
  hour_idx INTEGER NOT NULL,
  segment_id INTEGER NOT NULL,
  config_version INTEGER NOT NULL,
  welcome_message TEXT NOT NULL,
  voice TEXT NOT NULL,
  caller_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  dials INTEGER NOT NULL,
  connected INTEGER NOT NULL,
  engaged INTEGER NOT NULL,
  hallucinations INTEGER NOT NULL,
  PRIMARY KEY (workflow_id, hour_idx, segment_id, config_version,
               welcome_message, voice, caller_id, prompt_version)
) WITHOUT ROWID;

-- Per-call record for 'what did this customer hear, and why was it live?'
-- Indexed on time, not person. Random person IDs made a person index about 90% of write cost (measured:
-- 36.0s vs 3.4s per load-scenario day) for a rare query. A scan of one day answers an auditor in under a second.
CREATE TABLE IF NOT EXISTS call_log (
  event_id INTEGER PRIMARY KEY,
  minute INTEGER NOT NULL,
  workflow_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  lead_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  attempt_no INTEGER NOT NULL,
  config_version INTEGER NOT NULL,
  cohort_id TEXT NOT NULL,
  welcome_message TEXT NOT NULL,
  voice TEXT NOT NULL,
  caller_id TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  connected INTEGER NOT NULL,
  engaged INTEGER NOT NULL,
  disposition TEXT
);
CREATE INDEX IF NOT EXISTS call_log_minute ON call_log (minute);

-- Transactional inbox: control events commit with the chunk and are consumed afterwards.
CREATE TABLE IF NOT EXISTS inbox (
  event_id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,
  payload TEXT NOT NULL,
  processed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS inbox_todo ON inbox (processed, event_id);

CREATE TABLE IF NOT EXISTS bad_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chunk INTEGER NOT NULL,
  reason TEXT NOT NULL,
  raw TEXT NOT NULL
);

-- The single shared cohort object. 'where' uses the same grammar as the simulator's config.
CREATE TABLE IF NOT EXISTS cohort (
  workflow_id TEXT NOT NULL,
  cohort_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  where_clause TEXT NOT NULL,
  rationale TEXT NOT NULL,
  created_minute INTEGER NOT NULL,
  PRIMARY KEY (workflow_id, cohort_id)
);

-- One queued change to one part of a config. A proposal bundles several; owners watch their status.
CREATE TABLE IF NOT EXISTS change_item (
  item_id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  source TEXT NOT NULL,              -- experiment:<id> | incident:<id> | schedule | cohorts
  emergency INTEGER NOT NULL DEFAULT 0,
  ops TEXT NOT NULL,
  expected_old TEXT NOT NULL,        -- values these paths held when the item was created
  summary TEXT NOT NULL,
  rationale TEXT NOT NULL,
  evidence TEXT NOT NULL,
  dry_run TEXT NOT NULL,
  expected_daily_gain REAL NOT NULL DEFAULT 0,
  affected_leads INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('queued','in_proposal','applied','dropped')),
  status_reason TEXT,
  proposal_id TEXT,
  applied_version INTEGER,
  created_minute INTEGER NOT NULL,
  closed_minute INTEGER
);
CREATE INDEX IF NOT EXISTS item_wf ON change_item (workflow_id, status);

CREATE TABLE IF NOT EXISTS proposal (
  proposal_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  path TEXT NOT NULL CHECK (path IN ('approval','auto')),
  status TEXT NOT NULL,
  approval_id TEXT UNIQUE,
  base_version INTEGER NOT NULL,
  applied_version INTEGER,
  emergency INTEGER NOT NULL DEFAULT 0,
  body TEXT NOT NULL,                -- exactly what was sent, including the full proposed config
  reasons TEXT,
  created_minute INTEGER NOT NULL,
  decided_minute INTEGER,
  apply_by_minute INTEGER
);
CREATE INDEX IF NOT EXISTS proposal_wf ON proposal (workflow_id, status);

CREATE TABLE IF NOT EXISTS proposal_transition (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL REFERENCES proposal (proposal_id),
  from_status TEXT,
  to_status TEXT NOT NULL,
  minute INTEGER NOT NULL,
  note TEXT
);

-- Write-ahead record of outside actions. Unfinished rows are reconciled on restart.
CREATE TABLE IF NOT EXISTS side_effect_intent (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  workflow_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('started','done','failed')),
  result TEXT,
  created_minute INTEGER NOT NULL,
  UNIQUE (kind, key)
);

CREATE TABLE IF NOT EXISTS experiment (
  experiment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  dim TEXT NOT NULL,                 -- welcome_message | voice | caller_id
  metric TEXT NOT NULL,              -- pickup_rate | engagement_rate
  state TEXT NOT NULL CHECK (state IN
    ('queued','running','promoting','monitoring','rolling_back','concluded')),
  outcome TEXT,
  champion TEXT NOT NULL,
  arms TEXT NOT NULL,                -- arms still receiving traffic
  excluded TEXT NOT NULL DEFAULT '[]',
  scopes TEXT NOT NULL DEFAULT '{}', -- promoted scope -> winner, holdout, since_hour
  start_hour INTEGER,
  start_version INTEGER,
  looks INTEGER NOT NULL DEFAULT 0,
  last_look_hour INTEGER,
  pending_item INTEGER,
  created_minute INTEGER NOT NULL,
  closed_minute INTEGER
);
CREATE INDEX IF NOT EXISTS experiment_wf ON experiment (workflow_id, dim, state);

CREATE TABLE IF NOT EXISTS incident (
  incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  kind TEXT NOT NULL,                -- prompt | caller_id
  subject TEXT NOT NULL,             -- the prompt or caller ID under suspicion
  state TEXT NOT NULL CHECK (state IN ('open','fixing','verifying','resolved','failed')),
  evidence TEXT NOT NULL,
  candidates TEXT NOT NULL DEFAULT '[]',
  tried TEXT NOT NULL DEFAULT '[]',
  pending_item INTEGER,
  fixed_version INTEGER,
  detected_minute INTEGER NOT NULL,
  fixed_minute INTEGER,
  resolved_minute INTEGER,
  retry_after_minute INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS incident_wf ON incident (workflow_id, kind, state);

-- A schedule change under observation, with its automatic rollback.
CREATE TABLE IF NOT EXISTS change_watch (
  watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  item_id INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('watching','verified','rolling_back','rolled_back')),
  since_hour INTEGER NOT NULL,
  baseline TEXT NOT NULL,
  undo_ops TEXT NOT NULL,
  pending_item INTEGER,
  created_minute INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  prompt_version TEXT,
  cost REAL NOT NULL,
  credits_remaining REAL,
  run_id TEXT,
  pass_rate REAL,
  n INTEGER,
  minute INTEGER NOT NULL
);

-- Append-only audit log. Every choice the controller makes, including choices to do nothing.
CREATE TABLE IF NOT EXISTS decision (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  minute INTEGER NOT NULL,
  workflow_id TEXT,
  kind TEXT NOT NULL,
  summary TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}',
  experiment_id INTEGER,
  incident_id INTEGER,
  item_id INTEGER,
  proposal_id TEXT
);
CREATE INDEX IF NOT EXISTS decision_wf ON decision (workflow_id, minute);

CREATE TABLE IF NOT EXISTS loop_metric (
  minute INTEGER PRIMARY KEY,
  events INTEGER NOT NULL,
  duplicates INTEGER NOT NULL,
  bad_events INTEGER NOT NULL,
  ingest_s REAL NOT NULL,
  decide_s REAL NOT NULL,
  advance_s REAL NOT NULL,
  backlog_chunks INTEGER NOT NULL,
  rss_mb REAL,
  db_mb REAL,
  credits_remaining REAL,
  http_retries INTEGER NOT NULL
);
