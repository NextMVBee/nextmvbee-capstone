-- NextMVBee application store (SQLite). Applied and exercised offline on 6 Oct 2026 by tests/test_workflow.py.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  title TEXT NOT NULL,
  privacy_mode TEXT NOT NULL DEFAULT 'cloud' CHECK (privacy_mode IN ('cloud','cloud_zdr')),  -- cloud-only; offline is not offered in v1
  retention_days INTEGER,  -- NULL: keep until the founder deletes the project
  requirement_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  deleted_at TEXT,
  rules_version INTEGER NOT NULL DEFAULT 0,           -- Phase 05 round 1: the reconciliation rules last applied to this project's derived state
  derivation_pending INTEGER NOT NULL DEFAULT 0,      -- Phase 05 round 2: a base write happened without the derivation in its transaction; readers treat the project as not yet derived until the reconciliation clears it
  start_key TEXT                                      -- Package 19.4 (D-A451, migration 081): the key of the Start form that created the project; a repeated submission of the same form returns this project and starts nothing; NULL for a project created before the key or without one
);
CREATE UNIQUE INDEX IF NOT EXISTS projects_start_key ON projects(owner_id, start_key) WHERE start_key IS NOT NULL AND deleted_at IS NULL;   -- D-A451: one project per Start form of an account
CREATE TABLE IF NOT EXISTS requirements (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('confirmed','assumed','open','superseded')),
  source TEXT NOT NULL CHECK (source IN ('user','inferred')),
  hard INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL,
  supersedes TEXT REFERENCES requirements(id)
);
CREATE TABLE IF NOT EXISTS assumptions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  impact TEXT NOT NULL CHECK (impact IN ('low','medium','high')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','confirmed','rejected')),
  question_id TEXT
);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  why_blocking TEXT NOT NULL,
  requirement_key TEXT,                      -- the requirement the answer will confirm (ask_user tool)
  round INTEGER NOT NULL DEFAULT 1,
  answer_json TEXT,                          -- the founder's answer, fed back to the exact paused tool call
  tool_call_id TEXT,                         -- the paused tool call that asked
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','skipped')),
  answer_requirement_id TEXT REFERENCES requirements(id),
  kind TEXT NOT NULL DEFAULT 'value' CHECK (kind IN ('value','hardness','confirmation','usage','revision','contradiction'))   -- Phase 11 package 11.2 (D-A191, migration 070 rebuilds the table of an existing store): 'revision' (the founder's yes or no before a revision run spends money on the re-opened decisions, criterion I8) and 'contradiction' (a material contradiction unresolved in a decision, asked at the comparison, criterion I7); Phase 05b (C12, migration 044): a value question or a hardness question (is the requirement hard: answered yes or no against the question's id); Phase 6 (task 1, migration 054 rebuilds the table of an existing store): a confirmation question for a requirement the clarify model marked as the founder's statement (answered yes to confirm, no to reject, or with the corrected value); Phase 7 part 2 (D-A28, migration 055 rebuilds the table of an existing store): the one usage question of the project for its cost-bearing decisions (the founder's words; app/core/derived.py reads them as units per month)
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  title TEXT,
  publisher TEXT,
  tier INTEGER NOT NULL CHECK (tier IN (1,2,3)),
  published_at TEXT,
  retrieved_at TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  freshness TEXT NOT NULL CHECK (freshness IN ('volatile','policy','technical','stable')),
  fetch_status TEXT NOT NULL DEFAULT 'ok' CHECK (fetch_status IN ('ok','partial','failed','blocked')),
  injection_flag INTEGER NOT NULL DEFAULT 0,
  injection_decision TEXT CHECK (injection_decision IN ('include','exclude')),   -- Phase 11 package 11.2 (D-A191, criterion I9, migration 071): the founder's decision on a flagged source; NULL while undecided (the source stays excluded from claims)
  text_path TEXT,
  cache_status TEXT NOT NULL DEFAULT 'unknown' CHECK (cache_status IN ('livecrawl','cached','cache_fallback','unknown')),
  max_age_hours INTEGER,  -- Exa maxAgeHours used for this fetch (NULL = Exa default)
  stale_at TEXT,          -- Phase 05 round 2: set by the stale-freshness invalidation (base event), cleared by the next fetch of the page
  found_by_json TEXT,     -- Phase 9b (D-A52): the engines whose results contained the source (JSON list), for the document's found_by line
  UNIQUE (project_id, canonical_url, content_hash)
);
CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('price','license','availability','capability','version','other')),
  source_id TEXT REFERENCES sources(id),
  quote TEXT,
  status TEXT NOT NULL DEFAULT 'unverified' CHECK (status IN ('supported','partial','contradicted','conflicting','insufficient','unverified')),
  impact TEXT NOT NULL DEFAULT 'medium' CHECK (impact IN ('low','medium','high')),
  checked_at TEXT,
  fresh_until TEXT,
  subject TEXT, attribute TEXT, value TEXT,          -- atomic claim parts
  conditions_json TEXT NOT NULL DEFAULT '[]',         -- conditions or limits stated by the source (every condition; continuation accumulates them, Phase 03b)
  scope_status TEXT NOT NULL DEFAULT 'unchecked' CHECK (scope_status IN ('in_scope','conditional','out_of_scope','unchecked')),
  identity_key TEXT,                                  -- Phase 03b: extraction identity (app/core/roles.py extraction_identity); merges happen on it
  source_version TEXT,                                -- content hash of the stored text the quote was taken from
  range_start INTEGER, range_end INTEGER, batch_no INTEGER,
  provenance_json TEXT NOT NULL DEFAULT '[]',         -- every (source_id, source_version, range, batch_no) that produced this claim
  conditions_status TEXT NOT NULL DEFAULT 'complete', -- complete | in_progress | needs_review (condition continuation quota reached with more_conditions)
  decision_subject TEXT,
  product_id TEXT,                                    -- Phase 4: explicit product identity, normalised from the extractor's vendor and product (fallback: the subject)
  product_version TEXT,                               -- normalised version string ('' when the source states none)
  verified_requirement_version INTEGER,               -- the requirement version the verdict was given against; NULL until verified; coverage counts a claim only at the current version
  verdict TEXT,                                      -- Phase 05 round 1: the verifier's verdict, a base record; status is derived from it by app/core/reconcile.py (conflicting when in an open conflict)
  invalidated_at TEXT,                              -- Phase 05 round 2: set when an invalidation cleared the verdict (awaiting re-verification); cleared by the next verdict
  access_route TEXT NOT NULL DEFAULT '',             -- Phase 18 package 18.1 (D-A300, migration 076): the route the claim speaks about (batch, endpoint, deployment), a facet of the identity
  identity_kind TEXT NOT NULL DEFAULT 'product'      -- product, or approach for a way of using the product
);
CREATE VIRTUAL TABLE IF NOT EXISTS claims_fts USING fts5(claim_id UNINDEXED, text, quote);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'product' CHECK (scope IN ('product','operating')),
  choice TEXT NOT NULL,
  rationale TEXT NOT NULL,
  importance TEXT NOT NULL CHECK (importance IN ('low','medium','high')),
  rejected_json TEXT NOT NULL DEFAULT '[]',
  unresolved_json TEXT NOT NULL DEFAULT '[]',        -- ComparisonOut.unresolved findings, carried into every later plan's unresolved_checks (Phase 4 second correction 4)
  product_id TEXT,                                   -- the product identity the decision selects (Phase 4 fourth correction 1)
  product_version TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','stale','rejected','unresolved')),   -- derived by app/core/reconcile.py from proposed_status and the support records (Phase 05 round 2)
  plan_version INTEGER NOT NULL DEFAULT 1,
  proposed_status TEXT,                              -- the comparison's own outcome (base record): active, unresolved, stale
  derivation_reason TEXT,                            -- why the derived status differs from the proposal (never written into rationale)
  evidence_pack_json TEXT,                           -- Phase 10 package 10.2 (D-A184): the evidence packs the comparison saw (one per eligible candidate), stored with the decision's draft for the audit (migration 068)
  run_id TEXT                                        -- Phase 18 package 18.1 (D-A302, migration 075): the run that wrote the decision; the snapshot attaches that run's tree and no other
);
CREATE TABLE IF NOT EXISTS components (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  responsibility TEXT NOT NULL,
  data_leaves_device INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','stale')),
  product_id TEXT,                           -- the canonical product identity of the decision the component traces to (Phase 4 fifth correction 2)
  product_version TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  component_id TEXT NOT NULL REFERENCES components(id),
  title TEXT NOT NULL,
  depends_on_json TEXT NOT NULL DEFAULT '[]',
  effort_low_h REAL, effort_high_h REAL,
  status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','stale'))
);
CREATE TABLE IF NOT EXISTS acceptance_tests (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(id),
  requirement_id TEXT REFERENCES requirements(id),
  criterion TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current','stale'))
);
-- Requirement -> Decision -> Component -> Task -> AcceptanceTest, plus Claim -> Decision.
CREATE TABLE IF NOT EXISTS trace_edges (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('req_decision','claim_decision','decision_component','component_task','task_test','req_test')),
  PRIMARY KEY (project_id, from_id, to_id)
);
CREATE TABLE IF NOT EXISTS plan_versions (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  requirement_version INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  change_summary TEXT NOT NULL,
  diff_json TEXT NOT NULL DEFAULT '{}',
  export_path TEXT,
  snapshot_json TEXT,                        -- Phase 6 (Addendum 06x section 3, migration 048): the complete signed content of this version (ExportSnapshot, schemas/export_models.py), written in the transaction that creates the version and never updated; NULL for a version created before Phase 6 ("no snapshot")
  snapshot_sig TEXT,                         -- Phase 6 (migration 049): sha256 of the canonical snapshot JSON (sorted keys, no whitespace)
  approved_by TEXT,                          -- Phase 6 (D-A13, migrations 050 to 052): the approval act, bound to snapshot_sig (the signature recomputes at approval); NULL until the owner approves the version for document export
  approved_at TEXT,
  approval_ref TEXT,
  content_sig TEXT,                          -- Phase 6 (D-A7, migration 053): the content signature of the finished ContentDoc per family, computed at approval time (a JSON object {family: sha256}); the production of a family runs against that signature and a changed snapshot stops with CONTENT_HASH_MISMATCH
  lead_json TEXT,                            -- Phase 9b (C9, migration 062): the plan on one page's lead with its claim ids
  editor_json TEXT,                          -- Phase 9b (D-A55, migration 063): the Editor's record of the version
  PRIMARY KEY (project_id, version)
);
CREATE TABLE IF NOT EXISTS plan_drafts (        -- Phase 17 package 17.1 (D-A267): the one permitted draft after a cap or time stop that could not be stored as a version, preserved whole with the reason
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,                      -- the run whose stop permitted the draft
  step_key TEXT NOT NULL,                    -- the plan_generate step key of the draft
  ledger_entry TEXT,                         -- the usage_ledger row of the permitted send, when one settled
  status TEXT NOT NULL,                      -- the stage's outcome word (failed, inconsistent, invalid, truncated, provider_error, stage_failed, ...)
  reason TEXT NOT NULL,                      -- why no version could be written
  content_json TEXT,                         -- the response as the stage held it (the plan's JSON, or the attempt's raw answer text), NULL when nothing came back
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (run_id, step_key)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  thread_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running','awaiting_user','interrupted','completed','partial','failed','cancelled')),
  stop_reason TEXT,
  kind TEXT NOT NULL DEFAULT 'initial' CHECK (kind IN ('initial','revision')),  -- a requirement change opens a revision run
  worker_id TEXT,                            -- server instance that owns the run; a new instance recovers runs of the old one
  parent_run_id TEXT,
  budget_json TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  heartbeat_at TEXT,
  ended_at TEXT,
  active_seconds REAL NOT NULL DEFAULT 0,  -- elapsed time of the run while it was being driven, persisted at every dispatch and reconciled on recovery; paused time excluded (Phase 4 correction 8)
  active_ticked_at TEXT,                   -- wall-clock time of the last persisted tick; recovery adds the time up to the last heartbeat
  draft_permit TEXT                        -- after a time stop: 'permitted' (one draft of the plan may dispatch) then 'used' at the first actual plan dispatch; durable, run-scoped (Phase 4 fifth correction 3)
);
CREATE TABLE IF NOT EXISTS run_steps (
  step_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  node TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('started','completed','failed')),
  output_json TEXT,
  error_category TEXT,
  started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS usage_ledger (
  id TEXT PRIMARY KEY,                       -- idempotency key: step_key:kind:attempt
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('model','search','contents')),
  attempt INTEGER NOT NULL DEFAULT 1,
  model_id TEXT, provider TEXT, generation_id TEXT,
  tokens_in INTEGER, tokens_out INTEGER,
  reserved_usd REAL NOT NULL,                -- worst-case estimate reserved before sending
  reported_usd REAL,
  cost_uncertain INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER,
  state TEXT NOT NULL CHECK (state IN ('planned','sent','completed','failed','unknown')),
  resolution TEXT CHECK (resolution IS NULL OR resolution IN ('retried','skipped','reconciled')),
  provider_status INTEGER,                   -- HTTP status the provider returned when the attempt failed (402 stops the run)
  attempt_kind TEXT NOT NULL DEFAULT 'first' CHECK (attempt_kind IN ('first','repair','fallback')),  -- one repair and one fallback per stage instance, counted here
  outcome TEXT,                              -- typed outcome of a completed model call, written in the settlement transaction: ok, invalid, tool_calls, truncated (Phase 03b)
  reasoning_tokens INTEGER,                  -- usage.completion_tokens_details.reasoning_tokens as the provider reported it (counted in tokens_out and against max_tokens)
  planned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  sent_at TEXT, finished_at TEXT,
  result_json TEXT,                          -- validated output stored durably; a re-run replays it instead of resending
  raw_json TEXT                              -- Phase 17 package 17.1 (D-A267, migration 073): the model's answer text of an attempt settled invalid or truncated, kept so the one permitted draft after a stop is never lost; never replayed
);
CREATE TABLE IF NOT EXISTS production_requests (   -- Phase 06x.2 (D-A12 option A, D-A24): one row per provider request of a document production, beside usage_ledger, never sharing a cap with it
  id TEXT PRIMARY KEY,                       -- the host's request id: <job_id>:<n>, n the request's number within the job
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  job_id TEXT NOT NULL,                      -- the production job (the kit's work directory data/atelier/jobs/<project_id>/<job_id>/): the run reference
  role TEXT NOT NULL,                        -- the kit role the profile routes (content-analyst, composer-print, critic, ...)
  kind TEXT NOT NULL CHECK (kind IN ('chat','image')),
  model TEXT,                                -- the route's model id as sent
  generation_id TEXT,                        -- the provider's id of the answer (OpenRouter generation id); the kit's own request id lives in the job's journal.jsonl
  attempt_kind TEXT NOT NULL DEFAULT 'first' CHECK (attempt_kind IN ('first','validation_repair','length_escalation','structured_output_fallback','provider_retry','image_regeneration','repair_round')),
  tokens_in INTEGER, tokens_out INTEGER,
  reserved_usd REAL NOT NULL,                -- the worst case reserved before the send: the real messages and schema at the input price, max_tokens at the output price, plus 15 percent
  reported_usd REAL,                         -- usage.cost as the provider reported it; an unknown outcome is settled at the reservation and flagged
  estimated INTEGER NOT NULL DEFAULT 0,      -- 1 when reported_usd is the estimate, not the provider's figure
  state TEXT NOT NULL CHECK (state IN ('planned','sent','completed','failed','unknown')),
  outcome TEXT,                              -- ok, provider_error, credit (HTTP 402), latched (refused after a 402, nothing sent), budget (refused by the production budget line), unknown
  provider_status INTEGER,                   -- the HTTP status of a provider error
  planned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  sent_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS production_requests_by_job ON production_requests(job_id, planned_at);
CREATE TABLE IF NOT EXISTS exports (               -- Phase 06x.4 (D-A25, Addendum 06x section 5): one row per project, plan version, family and format of a document export
  id TEXT PRIMARY KEY,                       -- <project_id>.EX-<nnn>, allocated under BEGIN IMMEDIATE from the highest id of the project
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,                  -- the plan version (plan_versions.version)
  family TEXT NOT NULL,                      -- the document family of atelier.profile.json (mvp-plan, prd)
  format TEXT NOT NULL CHECK (format IN ('html','pdf','docx')),
  snapshot_sig TEXT NOT NULL,                -- plan_versions.snapshot_sig of the approved version (the fixture's until Phase 6 lands)
  content_sig TEXT NOT NULL,                 -- the content signature of the approved ContentDoc (D-A7)
  kit_version TEXT NOT NULL,                 -- the vendored kit's VERSION at the request
  routing_revision INTEGER NOT NULL,         -- atelier.profile.json routing.revision at the request (part of the reuse key, T30.18)
  direction_id TEXT, direction_version INTEGER,   -- the direction the production locked (never changed by a repair)
  job_id TEXT, delivery_id TEXT,             -- the kit's job (data/atelier/jobs/<project_id>/<job_id>/) and delivery
  output_hash TEXT,                          -- the kit's recorded hash of this format's delivered file (delivery.json outputHashes)
  attempt INTEGER NOT NULL DEFAULT 1,        -- the production attempt of this key (project, version, family)
  status TEXT NOT NULL CHECK (status IN ('planned','rendering','checked','available','partial','failed','cancelled','rejected')),
  reason TEXT,                               -- why a terminal state was reached (ids, codes and counts; never page text)
  path TEXT,                                 -- the published file of this format under data/exports/<project>/v<version>/<sig>/<export_id>/ (the ZIP for an HTML above single_file_max_mb)
  sha256 TEXT, size_bytes INTEGER, page_count INTEGER,
  manifest_json TEXT,                        -- the host manifest of the publication (the delivery's ids, hashes and gates; no page text)
  warnings_json TEXT NOT NULL DEFAULT '[]',
  requested_by TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS exports_by_key ON exports(project_id, version, family, status);
CREATE TABLE IF NOT EXISTS export_assets (         -- Phase 06x.4: every file published with an export (the package, the SVG sources beside the DOCX, the README, the manifest, the audit copies), with its hash
  export_id TEXT NOT NULL REFERENCES exports(id) ON DELETE CASCADE,
  name TEXT NOT NULL,                        -- the path inside the export directory
  kind TEXT NOT NULL CHECK (kind IN ('delivered','svg','package','readme','manifest','audit')),
  sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
  PRIMARY KEY (export_id, name)
);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
  step_key TEXT,
  category TEXT NOT NULL CHECK (category IN ('validation','provider_transient','provider_limit','credit','timeout','source','policy','internal')),
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS search_cache (
  cache_key TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,   -- the cache belongs to a project and dies with it
  scope TEXT NOT NULL,
  query_norm TEXT NOT NULL,
  filters_json TEXT NOT NULL,
  results_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  fresh_until TEXT NOT NULL
);

-- ---------- and  additions ----------
-- Record ids are '<project_id>.<PREFIX>-<nnn>' (app/core/ids.py): unique across the database, display id per project.
-- One active run per project (single server instance in the first release).
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(project_id) WHERE status IN ('running','awaiting_user');
-- Durable id allocation per project and prefix (app/repo.py, under BEGIN IMMEDIATE): two connections never receive the
-- same display number and a deleted highest id is never reissued. Dies with the project.
CREATE TABLE IF NOT EXISTS id_counters (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  prefix TEXT NOT NULL CHECK (prefix IN ('REQ','ASM','Q','SRC','CLM','DEC','CMP','TSK','ACC')),
  next_n INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (project_id, prefix)
);
-- Server-sent events: the id is the SSE event id used for Last-Event-ID resume.
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  event TEXT NOT NULL CHECK (event IN ('step','claim','decision','budget','pause','done','tool','intervention','branch','worker')),
  data_json TEXT NOT NULL,                   -- rendered to an HTML partial when sent; no prompts or keys
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS run_events_by_run ON run_events(run_id, id);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT,                        -- scrypt, see app/web/security.py; NULL for an AuthKit account (UI-2, migration 064)
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  workos_user_id TEXT UNIQUE,                -- UI-2: the WorkOS user id, filled at the first sign-in through AuthKit (app/web/pages.py callback)
  name TEXT,                                 -- UI-2: the name WorkOS reports
  run_allowance INTEGER NOT NULL DEFAULT 0,  -- UI-2: the runs the owner allows this account (initial and revision runs both count); the owner's own account is unlimited (OWNER_EMAIL)
  allowance_set_at TEXT,                     -- UI-2: when the owner last set the allowance
  allowance_set_by TEXT,                     -- UI-2: the owner's user id
  theme TEXT CHECK (theme IN ('light', 'dark')),  -- UI-3: the account's theme choice; NULL follows the device (migration 065)
  email_verified INTEGER NOT NULL DEFAULT 1  -- UI-2 (migration 066, UI2-2 amended 27 October 2026): 0 for an AuthKit identity whose email WorkOS has not verified, and for every row linked through AuthKit before 066 until a verified callback records it (B-OWNER-UPGRADE); a local row is 1; the owner role needs 1
);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  workos_session TEXT,                       -- UI-2: the sealed WorkOS session (Fernet, COOKIE_PASSWORD); never sent to the browser
  refreshed_at TEXT                          -- UI-2: when the sealed session was last checked against WorkOS (once an hour, app/web/auth.py)
);

-- ---------- course concepts: tool calls, reasoning records, passages, exploration tree, workers ----------
CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY,                       -- <run>:<stage>:<worker>:<branch>:tool:<n>  (durable; replayed on resume)
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  stage TEXT NOT NULL,
  worker TEXT NOT NULL DEFAULT 'w0',
  branch TEXT NOT NULL DEFAULT 'b0',
  n INTEGER NOT NULL,
  tool TEXT NOT NULL CHECK (tool IN ('search','read','re_fetch','ask_user')),
  args_json TEXT NOT NULL,
  fingerprint TEXT NOT NULL DEFAULT '',          -- sha256 of tool + canonical arguments; replay only on an exact match
  ledger_entry TEXT,                             -- billing attempt id in usage_ledger (separate from the logical tool id)
  state TEXT NOT NULL CHECK (state IN ('planned','sent','completed','failed','unknown','rejected','paused')),
  result_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS tool_calls_by_step ON tool_calls(run_id, step_key, n);
CREATE TABLE IF NOT EXISTS reasoning_records (                   -- structured decision summaries, never raw chain of thought
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  step_key TEXT NOT NULL,
  action_id TEXT NOT NULL DEFAULT '',              -- ledger entry id or tool call id this summary belongs to
  purpose TEXT NOT NULL, action TEXT NOT NULL, observation TEXT NOT NULL, change TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS stage_transcripts (                 -- tool-loop transcript per stage instance, replayed after an interruption
  step_key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  round INTEGER NOT NULL,
  messages_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS passages (                            -- chunks of stored source text, the unit of local retrieval
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  range_start INTEGER, range_end INTEGER                           -- character range of this chunk in the stored text (Phase 03b: read with query returns ranges)
);
CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(passage_id UNINDEXED, text);
CREATE TABLE IF NOT EXISTS tree_nodes (                          -- bounded exploration tree, pruned branches included
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  decision_subject TEXT NOT NULL,
  parent_id TEXT,
  depth INTEGER NOT NULL,
  label TEXT NOT NULL,
  hard_status TEXT NOT NULL CHECK (hard_status IN ('pass','fail','unresolved','unchecked')),
  score REAL,
  claim_ids_json TEXT NOT NULL DEFAULT '[]',
  pruned INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  selected INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,          -- one snapshot per run and decision subject; a new snapshot replaces the previous one
  facets_json TEXT NOT NULL DEFAULT '[]',      -- Phase 18 package 18.1 (D-A301, migration 077): the route and approach variants folded into the node (label, kind, access_route, claim_ids), never scored siblings
  hard_check_json TEXT NOT NULL DEFAULT '[]'   -- Phase 18 package 18.3 (D-A383, migration 078): what the hard check read behind hard_status: per binding hard requirement the verdict, the verdict row's id and date and the claim ids it rests on; the coverage_checks upsert keeps replacing rows, the node keeps what it read
);
CREATE TABLE IF NOT EXISTS worker_results (                      -- proposals from research workers, merged by the coordinator
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  worker TEXT NOT NULL,
  task_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('completed','failed')),
  proposals_json TEXT NOT NULL DEFAULT '[]',
  error TEXT,
  finished_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- ---------- Phase 03b: long-content handling (additive; D-28) ----------
-- Every stored text of a source, by content hash: a refresh with new content keeps the source id, writes a new version row
-- and supersedes the previous one. A read names (source_id, source_version); ranges of one version are never served as another.
CREATE TABLE IF NOT EXISTS source_versions (
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  content_hash TEXT NOT NULL,
  chars_total INTEGER NOT NULL,
  text_path TEXT,                                -- this version's own immutable file (the first version SRC-nnn.txt, later ones SRC-nnn.<hash12>.txt); never rewritten
  retrieved_at TEXT NOT NULL,
  superseded_at TEXT,
  PRIMARY KEY (source_id, content_hash)
);
-- Persisted extraction position per source version: batches of research.batch_chars with research.batch_overlap_chars overlap.
CREATE TABLE IF NOT EXISTS extraction_batches (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  source_version TEXT NOT NULL,
  batch_no INTEGER NOT NULL,
  range_start INTEGER NOT NULL,
  range_end INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','in_progress','completed','needs_review')),
  claims_found INTEGER NOT NULL DEFAULT 0,
  continuations INTEGER NOT NULL DEFAULT 0,      -- responses processed on this batch (resume continues from here)
  units_json TEXT NOT NULL DEFAULT '[]',         -- pending work units after a truncation split: [[start, end, bumped], ...]; empty when the whole range is one unit
  no_progress INTEGER NOT NULL DEFAULT 0,        -- consecutive continuations that returned only already-recorded identities (two stop the batch); derived from responses_json
  responses_json TEXT NOT NULL DEFAULT '[]',     -- one entry per response, written with its findings: {step, claims, new, more}; a replayed response reuses its entry
  review_reason TEXT,                            -- why the batch is needs_review (limit reached, no progress, truncated passage, incomplete conditions)
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (project_id, source_id, source_version, batch_no)
);
-- Research questions from the research plan (QueryDraft target_fact per decision subject), persisted so coverage can name them.
CREATE TABLE IF NOT EXISTS research_questions (
  id TEXT PRIMARY KEY,                           -- '<project_id>.RQ-<12 hex>' from the normalised subject and target fact: the same question keeps its id
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  decision_subject TEXT NOT NULL,
  target_fact TEXT NOT NULL,
  query_text TEXT,                               -- the model's query for this question; a search serves the question when its query matches it (Phase 4 correction 1)
  run_id TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- Coverage per requirement and research question: evidence_status from verified claims, processing_status from the extraction
-- batches of the linked sources. Covered only when supported AND complete (app/core/coverage.py).
CREATE TABLE IF NOT EXISTS coverage (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('requirement','research_question')),
  subject_id TEXT NOT NULL,
  requirement_version INTEGER NOT NULL DEFAULT 1,
  evidence_status TEXT NOT NULL DEFAULT 'pending' CHECK (evidence_status IN ('supported','conflicting','insufficient','pending')),
  processing_status TEXT NOT NULL DEFAULT 'not_started' CHECK (processing_status IN ('not_started','in_progress','complete')),
  claim_ids_json TEXT NOT NULL DEFAULT '[]',
  source_versions_json TEXT NOT NULL DEFAULT '[]',   -- [{"source_id":..., "source_version":...}] the sources linked to the subject
  derived_claim_ids_json TEXT NOT NULL DEFAULT '[]',        -- Phase 05 round 10: a requirement's claims computed from its support records (derived; the derive step alone writes it)
  derived_source_versions_json TEXT NOT NULL DEFAULT '[]',  -- the sources of those claims (derived)
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (project_id, subject_type, subject_id)
);
-- Per-requirement entailment from verification (Phase 4 second correction 1): a hard requirement counts as supported only by claims
-- whose verification record names it at the current requirement version. Written by app/core/coverage.record_verification.
CREATE TABLE IF NOT EXISTS claim_requirement_support (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  requirement_id TEXT NOT NULL,
  requirement_version INTEGER NOT NULL,
  verdict TEXT NOT NULL,                         -- the verifier's verdict (base record)
  status TEXT NOT NULL DEFAULT 'counts',         -- derived (app/core/reconcile.py): counts | withdrawn; only counting records support a hard requirement
  reason TEXT,                                   -- why withdrawn (eligibility, freshness by claim kind, conflict)
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (project_id, claim_id, requirement_id, requirement_version)
);
-- Conflicting evidence (Phase 05, Build Book section 5): claims about one product identity and attribute with different
-- values. Both are kept and marked conflicting; the preferred claim is the newer official source; the conflict is listed
-- under the plan's unresolved checks (app/core/evidence.py). Nothing is chosen silently.
-- Phase 7 part 2 (D-25, D-A28): the derived cost table of a cost-bearing decision, computed by app/core/derived.py from the verified price claims
-- the comparison was shown and the founder's usage answer (one row per option and price claim; the budget check against the budget requirement);
-- read into the export snapshot (schemas/export_models.DerivedTable), the Markdown export and the documents' cost table. A base record of the
-- run that made it; deleted with the project.
CREATE TABLE IF NOT EXISTS derived_tables (
  id TEXT PRIMARY KEY,                           -- <project_id>.DRV-<nnn>
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
  run_id TEXT,
  title TEXT NOT NULL,
  fields_json TEXT NOT NULL,
  units_json TEXT NOT NULL DEFAULT '{}',
  labels_json TEXT NOT NULL DEFAULT '{}',
  rows_json TEXT NOT NULL,
  claim_ids_json TEXT NOT NULL,                  -- per row, the claims it rests on
  usage_question_id TEXT,                        -- the usage question answered (NULL: unanswered, the rows say not computable)
  budget_requirement_id TEXT,                    -- the budget requirement checked (NULL: no budget requirement)
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS claim_conflicts (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  conflict_key TEXT NOT NULL,                    -- <product_id>@<product_version>:<attribute>
  claim_ids_json TEXT NOT NULL,                  -- every claim in the conflict, sorted
  preferred_claim_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  run_id TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  status TEXT NOT NULL DEFAULT 'open',           -- open | resolved (never deleted): a conflict fresh detection no longer finds is resolved with a reason and a timestamp
  resolved_at TEXT,
  resolution_reason TEXT,
  rules_version INTEGER,
  PRIMARY KEY (project_id, conflict_key)
);
-- Append-only log of the derived-state reconciliation (app/core/reconcile.py): what changed, why, under which rules version.
CREATE TABLE IF NOT EXISTS reconciliation_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT,
  rules_version INTEGER NOT NULL,
  kind TEXT NOT NULL,                            -- claim_status | support | conflict | decision | coverage | project
  subject_id TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- Phase 05 round 3: the derivation flag is structural. Any write to a base table or column that feeds the derivation
-- (app/core/reconcile.py) sets projects.derivation_pending for that project, whoever wrote it. Derived columns
-- (claims.status, claim_requirement_support.status and reason, claim_conflicts, decisions.status and derivation_reason,
-- coverage statuses) are excluded through the UPDATE OF column lists, so the derivation never re-fires them; it clears
-- the flag inside its own transaction. Recursive triggers stay off: the flag write itself fires nothing.
CREATE TRIGGER IF NOT EXISTS dp_claims_ins AFTER INSERT ON claims BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_claims_upd AFTER UPDATE OF verdict, scope_status, conditions_json, conditions_status, source_id, source_version, verified_requirement_version, invalidated_at, kind, subject, attribute, value, product_id, product_version, quote ON claims
  BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_claims_del AFTER DELETE ON claims BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_sources_ins AFTER INSERT ON sources BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_sources_upd AFTER UPDATE OF cache_status, retrieved_at, content_hash, published_at, tier, stale_at, freshness, max_age_hours, injection_flag, fetch_status ON sources
  BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_sources_del AFTER DELETE ON sources BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_source_versions_ins AFTER INSERT ON source_versions BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_source_versions_upd AFTER UPDATE OF superseded_at, content_hash ON source_versions BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_source_versions_del AFTER DELETE ON source_versions BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_requirements_ins AFTER INSERT ON requirements BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_requirements_upd AFTER UPDATE OF key, value_json, status, hard, version ON requirements BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_requirements_del AFTER DELETE ON requirements BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_questions_ins AFTER INSERT ON questions BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_questions_upd AFTER UPDATE OF status, answer_json ON questions BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_questions_del AFTER DELETE ON questions BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_support_ins AFTER INSERT ON claim_requirement_support BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_support_upd AFTER UPDATE OF verdict, requirement_version, requirement_id ON claim_requirement_support BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_support_del AFTER DELETE ON claim_requirement_support BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_decisions_ins AFTER INSERT ON decisions BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_decisions_upd AFTER UPDATE OF proposed_status, choice, rationale, importance, plan_version, product_id, product_version, unresolved_json, rejected_json ON decisions
  BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_decisions_del AFTER DELETE ON decisions BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_trace_edges_ins AFTER INSERT ON trace_edges BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_trace_edges_del AFTER DELETE ON trace_edges BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_coverage_ins AFTER INSERT ON coverage BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_coverage_upd AFTER UPDATE OF claim_ids_json, source_versions_json ON coverage BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_coverage_del AFTER DELETE ON coverage BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_batches_ins AFTER INSERT ON extraction_batches BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_batches_upd AFTER UPDATE OF status, review_reason, source_version ON extraction_batches BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_batches_del AFTER DELETE ON extraction_batches BEGIN UPDATE projects SET derivation_pending=1 WHERE id=OLD.project_id AND derivation_pending=0; END;
CREATE TRIGGER IF NOT EXISTS dp_projects_upd AFTER UPDATE OF requirement_version ON projects BEGIN UPDATE projects SET derivation_pending=1 WHERE id=NEW.id AND derivation_pending=0; END;
-- Phase 05 round 4: identity keys are immutable. A citation, a support record, a batch or a coverage subject is never
-- re-pointed in place: a reassignment is a delete plus an insert, which the dp_* triggers above flag.
CREATE TRIGGER IF NOT EXISTS im_trace_edges BEFORE UPDATE OF project_id, from_id, to_id, kind ON trace_edges BEGIN SELECT RAISE(ABORT, 'trace_edges identity keys are immutable: delete and insert'); END;
CREATE TRIGGER IF NOT EXISTS im_support BEFORE UPDATE OF project_id, claim_id, requirement_id, requirement_version ON claim_requirement_support BEGIN SELECT RAISE(ABORT, 'claim_requirement_support identity keys are immutable: delete and insert'); END;
CREATE TRIGGER IF NOT EXISTS im_batches BEFORE UPDATE OF project_id, source_id, source_version, batch_no ON extraction_batches BEGIN SELECT RAISE(ABORT, 'extraction_batches identity keys are immutable: delete and insert'); END;
CREATE TRIGGER IF NOT EXISTS im_coverage BEFORE UPDATE OF project_id, subject_type, subject_id ON coverage BEGIN SELECT RAISE(ABORT, 'coverage identity keys are immutable: delete and insert'); END;
CREATE TRIGGER IF NOT EXISTS im_conflicts BEFORE UPDATE OF project_id, conflict_key ON claim_conflicts BEGIN SELECT RAISE(ABORT, 'claim_conflicts identity keys are immutable: delete and insert'); END;
CREATE TABLE IF NOT EXISTS candidates (                 -- Phase 9a (D-A41): the planner's typed candidates per subject; a candidate of another class is dropped with its reason
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  label TEXT NOT NULL,
  product_class TEXT NOT NULL,
  subject_class TEXT NOT NULL,
  specialised_for TEXT,
  status TEXT NOT NULL CHECK (status IN ('kept','dropped')),
  reason TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  kind TEXT NOT NULL DEFAULT 'product',                 -- Phase 18 package 18.1 (D-A299, migration 074): product or approach
  vendor TEXT NOT NULL DEFAULT '',                      -- the identity beside the label: the vendor, the product, the version the lead named
  product TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  access_route TEXT NOT NULL DEFAULT '',                -- the plan, tier, endpoint or deployment the lead named: a facet of the identity, never a candidate of its own
  urls_json TEXT NOT NULL DEFAULT '[]',                 -- the lead's pages
  PRIMARY KEY (project_id, run_id, subject, label)
);
CREATE TABLE IF NOT EXISTS coverage_checks (            -- Phase 9a (D-A42): the coverage step's verdict per product and binding hard requirement at a requirement version; counts_for_hard_constraints is derived from entails
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT,
  requirement_id TEXT NOT NULL,
  requirement_version INTEGER NOT NULL,
  subject TEXT NOT NULL,
  product_id TEXT NOT NULL,
  product_version TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL CHECK (verdict IN ('entails','partial','none','contradicts')),   -- Phase 11 package 11.1 (D-A190): contradicts, a proven violation; none stays not proven
  claim_ids_json TEXT NOT NULL DEFAULT '[]',
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  judged_json TEXT,                              -- Phase 9e (D-A161): the claims the verdict judged, [id, source version]; the verdict counts only while they are unchanged; NULL on a row written before the rule
  prompt_version INTEGER,                        -- Phase 11 package 11.1 (D-A190, migration 069): the coverage prompt's version the verdict was judged under; it counts only under the version in use; NULL on a row written before the rule
  model_id TEXT,                                 -- Phase 11 package 11.3 (D-A192, migration 072): the model identity (the coverage stage's route) the verdict was judged under; it counts only under the model in use; NULL on a row written before the rule
  PRIMARY KEY (project_id, requirement_id, requirement_version, product_id, product_version)
);
CREATE TABLE IF NOT EXISTS entities (                   -- Phase 9a (D-A45): the data model of a plan version (the planner enrichment; every record a planning proposal)
  id TEXT PRIMARY KEY,                                   -- <project_id>.ENT-<nnn>
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  plan_version INTEGER NOT NULL,
  name TEXT NOT NULL,
  fields_json TEXT NOT NULL DEFAULT '[]',
  relations_json TEXT NOT NULL DEFAULT '[]',
  status_label TEXT NOT NULL DEFAULT 'planning proposal'
);
CREATE TABLE IF NOT EXISTS interfaces (                 -- Phase 9a (D-A45): the interfaces between components of a plan version
  id TEXT PRIMARY KEY,                                   -- <project_id>.IFC-<nnn>
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  plan_version INTEGER NOT NULL,
  name TEXT NOT NULL,
  from_key TEXT NOT NULL,
  to_key TEXT NOT NULL,
  direction TEXT NOT NULL DEFAULT 'request',
  kind TEXT NOT NULL DEFAULT 'call',
  description TEXT NOT NULL,
  steps_json TEXT NOT NULL DEFAULT '[]',
  status_label TEXT NOT NULL DEFAULT 'planning proposal'
);
CREATE TABLE IF NOT EXISTS component_relations (        -- Phase 9a (D-A45): the relations between components of a plan version
  id TEXT PRIMARY KEY,                                   -- <project_id>.REL-<nnn>
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  plan_version INTEGER NOT NULL,
  from_key TEXT NOT NULL,
  to_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  decision_ids_json TEXT NOT NULL DEFAULT '[]',
  claim_ids_json TEXT NOT NULL DEFAULT '[]',
  status_label TEXT NOT NULL DEFAULT 'planning proposal'
);
CREATE TABLE IF NOT EXISTS verification_checks (        -- Phase 9a (D-A45): the verification plan of a plan version (check, how, pass, cost; the requirement or the risk it answers)
  id TEXT PRIMARY KEY,                                   -- <project_id>.VCK-<nnn>
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  plan_version INTEGER NOT NULL,
  check_text TEXT NOT NULL,
  how TEXT NOT NULL,
  pass_text TEXT NOT NULL,
  cost TEXT NOT NULL,
  requirement_id TEXT,
  risk_text TEXT,
  result TEXT CHECK (result IN ('pass','fail')),   -- Phase 18 package 18.3 (D-A342, migration 079): the pass record of a run of the product on the founder's own input; NULL until the check ran
  result_at TEXT,
  result_product_id TEXT,                          -- the identity the run exercised (the pick's product id and version): validated binds to it, never to a word
  result_product_version TEXT
);
CREATE TABLE IF NOT EXISTS candidate_exclusions (      -- Phase 18 package 18.3 (D-A383): every cut a record: an identity a stage left out of a decision's weighing, with the stage, the rule and the reason
  id TEXT PRIMARY KEY,                                   -- <project_id>.EXC-<nnn>
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  decision_id TEXT,                                      -- the decision the cut was recorded for; NULL for a cut recorded before the decision exists (the tree's refused alternatives)
  product_id TEXT NOT NULL,
  product_version TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL,                                   -- eligible set (D-A133, D-A143, D-A41), tree (D-A301)
  rule TEXT NOT NULL DEFAULT '',                         -- the D-A id the reason names
  reason TEXT NOT NULL,
  checked_at TEXT NOT NULL,                              -- when the rule was applied
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (project_id, run_id, subject, decision_id, product_id, product_version, stage)   -- round 2 (D-A346, migration 080): one row per decision too, so the final's record never takes the draft's row (NULL decision ids are distinct here; record() looks the tree's rows up by decision_id IS NULL)
);
