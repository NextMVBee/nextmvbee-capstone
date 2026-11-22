-- migration 011: decisions.proposed_status, applied by app.repo.migrate() when the column is missing
ALTER TABLE decisions ADD COLUMN proposed_status TEXT;
