-- migration 032: runs.active_seconds, applied by app.repo.migrate() when the column is missing
ALTER TABLE runs ADD COLUMN active_seconds REAL NOT NULL DEFAULT 0;
