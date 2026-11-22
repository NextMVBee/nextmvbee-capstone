-- migration 010: claims.invalidated_at, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN invalidated_at TEXT;
