-- migration 080 (Phase 18 package 18.3 round 2, D-A346; Codex round 1 SF-3): candidate_exclusions' unique key gains decision_id, so the final
-- decision's record of a cut never takes the draft decision's row (under the Package 18.3 key the second record of the same run, subject,
-- identity and stage replaced the row and moved it to the later decision with the later time). SQLite cannot alter a table constraint, so
-- the table is rebuilt by SQLite's documented twelve-step procedure: foreign keys off outside the transaction, the new table, every row
-- copied with its id, the old table dropped, the new one renamed, the foreign-key check, then foreign keys on again (app.repo.migrate()
-- raises when the check reports a row). Applied by migrate() when the table's definition lacks decision_id in its unique key. The file's
-- name follows the migration convention (table, column); migrate() skips it in the column loop because decision_id already exists and
-- applies it by the key's text, as the questions rebuilds (054, 055, 070) are applied by their CHECK text.
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE candidate_exclusions_r2 (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  decision_id TEXT,
  product_id TEXT NOT NULL,
  product_version TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  stage TEXT NOT NULL,
  rule TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  UNIQUE (project_id, run_id, subject, decision_id, product_id, product_version, stage)
);
INSERT INTO candidate_exclusions_r2 (id, project_id, run_id, subject, decision_id, product_id, product_version, label, stage, rule, reason, checked_at, created_at)
  SELECT id, project_id, run_id, subject, decision_id, product_id, product_version, label, stage, rule, reason, checked_at, created_at FROM candidate_exclusions;
DROP TABLE candidate_exclusions;
ALTER TABLE candidate_exclusions_r2 RENAME TO candidate_exclusions;
PRAGMA foreign_key_check;
COMMIT;
PRAGMA foreign_keys = ON;
