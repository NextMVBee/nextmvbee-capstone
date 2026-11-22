-- migration 013: sources.stale_at, applied by app.repo.migrate() when the column is missing
ALTER TABLE sources ADD COLUMN stale_at TEXT;
