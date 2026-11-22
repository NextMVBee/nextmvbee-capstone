-- migration 030: usage_ledger.reasoning_tokens, applied by app.repo.migrate() when the column is missing
ALTER TABLE usage_ledger ADD COLUMN reasoning_tokens INTEGER;
