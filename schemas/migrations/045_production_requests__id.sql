-- migration 045: the production_requests table (Phase 06x.2, D-A12), additive; applied by app.repo.migrate() through schemas/schema.sql (CREATE TABLE IF NOT EXISTS) and kept here as the shipped record of the change
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
