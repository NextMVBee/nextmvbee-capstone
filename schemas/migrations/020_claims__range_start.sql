-- migration 020: claims.range_start, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN range_start INTEGER;
