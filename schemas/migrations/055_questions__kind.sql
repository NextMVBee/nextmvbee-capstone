-- migration 055: questions.kind gains the value 'usage' (Phase 7 part 2, D-A28: the project's one usage question for its cost-bearing
-- decisions, asked by the comparison node and answered through the existing question path). SQLite cannot alter a CHECK constraint, so the
-- table of an existing store is rebuilt as migration 054 did (a new table, the rows copied, the old table dropped, the new one renamed);
-- applied by app.repo.migrate() when the table's definition lacks the value. The dp_questions_* triggers are dropped with the old table and
-- created again by schemas/schema.sql in the same migrate() call. Nothing else of the table changes.
-- Phase 13 package 13.2 (D-A234): the rebuilt table carries usage_json (migration 057), which app.repo.migrate() applies before every rebuild,
-- so the column and its values are copied; before, the rebuild dropped them on any store that held the column.
CREATE TABLE questions_p7 (
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
  kind TEXT NOT NULL DEFAULT 'value' CHECK (kind IN ('value','hardness','confirmation','usage')),
  usage_json TEXT
);
INSERT INTO questions_p7 (id, project_id, text, why_blocking, requirement_key, round, answer_json, tool_call_id, status, answer_requirement_id, kind, usage_json)
  SELECT id, project_id, text, why_blocking, requirement_key, round, answer_json, tool_call_id, status, answer_requirement_id, kind, usage_json FROM questions;
DROP TABLE questions;
ALTER TABLE questions_p7 RENAME TO questions;
