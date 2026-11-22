-- migration 006: claim_conflicts.resolution_reason, applied by app.repo.migrate() when the column is missing
ALTER TABLE claim_conflicts ADD COLUMN resolution_reason TEXT;
