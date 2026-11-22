-- migration 033: runs.active_ticked_at, applied by app.repo.migrate() when the column is missing
ALTER TABLE runs ADD COLUMN active_ticked_at TEXT;
