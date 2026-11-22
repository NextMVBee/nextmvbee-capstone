-- migration 003: claim_requirement_support.reason, applied by app.repo.migrate() when the column is missing
ALTER TABLE claim_requirement_support ADD COLUMN reason TEXT;
