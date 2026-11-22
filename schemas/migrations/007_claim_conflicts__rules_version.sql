-- migration 007: claim_conflicts.rules_version, applied by app.repo.migrate() when the column is missing
ALTER TABLE claim_conflicts ADD COLUMN rules_version INTEGER;
