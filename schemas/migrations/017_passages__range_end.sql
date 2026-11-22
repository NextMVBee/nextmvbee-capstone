-- migration 017: passages.range_end, applied by app.repo.migrate() when the column is missing
ALTER TABLE passages ADD COLUMN range_end INTEGER;
