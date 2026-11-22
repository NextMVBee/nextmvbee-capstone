-- migration 021: claims.range_end, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN range_end INTEGER;
