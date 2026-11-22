-- migration 012: decisions.derivation_reason, applied by app.repo.migrate() when the column is missing
ALTER TABLE decisions ADD COLUMN derivation_reason TEXT;
