-- migration 043: coverage.derived_source_versions_json (Phase 05 round 10: computed links kept apart from the run's base links), applied by app.repo.migrate() when the column is missing
ALTER TABLE coverage ADD COLUMN derived_source_versions_json TEXT NOT NULL DEFAULT '[]';
