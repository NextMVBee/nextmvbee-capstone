-- migration 015: usage_ledger.attempt_kind, applied by app.repo.migrate() when the column is missing
ALTER TABLE usage_ledger ADD COLUMN attempt_kind TEXT NOT NULL DEFAULT 'first';
