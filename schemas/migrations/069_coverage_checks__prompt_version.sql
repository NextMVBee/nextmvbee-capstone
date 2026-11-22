-- migration 069 (Phase 11 package 11.1, D-A190): coverage_checks takes the fourth verdict value contradicts (a proven violation) and gains
-- prompt_version, the coverage prompt's version a verdict was judged under; a verdict counts only under the version in use, so a row of an
-- older prompt reads as unchecked and is judged again, and no stored none is ever converted. SQLite cannot alter a CHECK, so the table is
-- rebuilt by the documented twelve-step procedure (as migration 064): foreign keys off outside the transaction, the new table, every row
-- copied as it is (prompt_version NULL: written before this rule), the old table dropped, the new one renamed, the foreign-key check, then
-- foreign keys on again (app.repo.migrate() raises when the check reports a row). Applied by migrate() when coverage_checks lacks prompt_version.
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE coverage_checks_p11 (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT,
  requirement_id TEXT NOT NULL,
  requirement_version INTEGER NOT NULL,
  subject TEXT NOT NULL,
  product_id TEXT NOT NULL,
  product_version TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL CHECK (verdict IN ('entails','partial','none','contradicts')),
  claim_ids_json TEXT NOT NULL DEFAULT '[]',
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  judged_json TEXT,
  prompt_version INTEGER,
  PRIMARY KEY (project_id, requirement_id, requirement_version, product_id, product_version)
);
INSERT INTO coverage_checks_p11 (project_id, run_id, requirement_id, requirement_version, subject, product_id, product_version, verdict, claim_ids_json, note, created_at, judged_json)
  SELECT project_id, run_id, requirement_id, requirement_version, subject, product_id, product_version, verdict, claim_ids_json, note, created_at, judged_json FROM coverage_checks;
DROP TABLE coverage_checks;
ALTER TABLE coverage_checks_p11 RENAME TO coverage_checks;
PRAGMA foreign_key_check;
COMMIT;
PRAGMA foreign_keys = ON;
