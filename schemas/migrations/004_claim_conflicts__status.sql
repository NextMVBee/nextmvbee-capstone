-- migration 004: claim_conflicts.status, applied by app.repo.migrate() when the column is missing
ALTER TABLE claim_conflicts ADD COLUMN status TEXT NOT NULL DEFAULT 'open';
