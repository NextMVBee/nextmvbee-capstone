-- migration 001: claims.verdict, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN verdict TEXT;
