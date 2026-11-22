-- migration 029: usage_ledger.outcome, applied by app.repo.migrate() when the column is missing
ALTER TABLE usage_ledger ADD COLUMN outcome TEXT;
