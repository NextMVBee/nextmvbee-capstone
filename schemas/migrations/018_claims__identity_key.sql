-- migration 018: claims.identity_key, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN identity_key TEXT;
