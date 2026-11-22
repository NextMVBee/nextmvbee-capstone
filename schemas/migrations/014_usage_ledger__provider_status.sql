-- migration 014: usage_ledger.provider_status, applied by app.repo.migrate() when the column is missing
ALTER TABLE usage_ledger ADD COLUMN provider_status INTEGER;
