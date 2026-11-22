-- tests/test_evidence_states.py es5: the coverage_checks table as migration 068 left it (three verdict values, no prompt_version), so the
-- migration 069 rebuild is exercised on a store with an old none verdict. Under NMB_TEST_FIXTURES=1 only.
DROP TABLE coverage_checks;
CREATE TABLE coverage_checks (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_id TEXT,
  requirement_id TEXT NOT NULL,
  requirement_version INTEGER NOT NULL,
  subject TEXT NOT NULL,
  product_id TEXT NOT NULL,
  product_version TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL CHECK (verdict IN ('entails','partial','none')),
  claim_ids_json TEXT NOT NULL DEFAULT '[]',
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  judged_json TEXT,
  PRIMARY KEY (project_id, requirement_id, requirement_version, product_id, product_version)
);
