-- migration 002: claim_requirement_support.status, applied by app.repo.migrate() when the column is missing
ALTER TABLE claim_requirement_support ADD COLUMN status TEXT NOT NULL DEFAULT 'counts';
