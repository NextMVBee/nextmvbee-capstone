-- migration 042: coverage.derived_claim_ids_json (Phase 05 round 10: computed links kept apart from the run's base links), applied by app.repo.migrate() when the column is missing
ALTER TABLE coverage ADD COLUMN derived_claim_ids_json TEXT NOT NULL DEFAULT '[]';
