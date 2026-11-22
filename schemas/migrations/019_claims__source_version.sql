-- migration 019: claims.source_version, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN source_version TEXT;
