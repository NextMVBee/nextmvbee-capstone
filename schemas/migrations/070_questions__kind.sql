-- migration 070 (Phase 11 package 11.2, D-A191): questions.kind gains two values, 'revision' (the founder's yes or no before a revision run spends
-- money on the decisions a requirement change re-opened; criterion I8) and 'contradiction' (a material contradiction unresolved in a decision of
-- the run, asked at the comparison; criterion I7). SQLite cannot alter a CHECK constraint, so the table of an existing store is rebuilt as
-- migrations 054 and 055 did (a new table, the rows copied, the old table dropped, the new one renamed); applied by app.repo.migrate() when the
-- table's definition lacks the value 'revision'. The dp_questions_* triggers are dropped with the old table and created again by
-- schemas/schema.sql in the same migrate() call. Nothing else of the table changes.
-- Phase 13 package 13.2 (D-A234): the rebuilt table carries usage_json (migration 057), which app.repo.migrate() applies before every rebuild,
-- so the column and its values are copied; before, the rebuild dropped them with their values on every store migrated through 057
-- before Phase 11 (the D-21 rerun of 5 November 2026, PLANNING/reports/PHASE_10_5_FULL3_REPORT.md).
CREATE TABLE questions_p11 (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  why_blocking TEXT NOT NULL,
  requirement_key TEXT,
  round INTEGER NOT NULL DEFAULT 1,
  answer_json TEXT,
  tool_call_id TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered','skipped')),
  answer_requirement_id TEXT REFERENCES requirements(id),
  kind TEXT NOT NULL DEFAULT 'value' CHECK (kind IN ('value','hardness','confirmation','usage','revision','contradiction')),
  usage_json TEXT
);
INSERT INTO questions_p11 (id, project_id, text, why_blocking, requirement_key, round, answer_json, tool_call_id, status, answer_requirement_id, kind, usage_json)
  SELECT id, project_id, text, why_blocking, requirement_key, round, answer_json, tool_call_id, status, answer_requirement_id, kind, usage_json FROM questions;
DROP TABLE questions;
ALTER TABLE questions_p11 RENAME TO questions;
