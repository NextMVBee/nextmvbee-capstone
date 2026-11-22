-- migration 005: claim_conflicts.resolved_at, applied by app.repo.migrate() when the column is missing
ALTER TABLE claim_conflicts ADD COLUMN resolved_at TEXT;
