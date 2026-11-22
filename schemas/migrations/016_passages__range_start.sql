-- migration 016: passages.range_start, applied by app.repo.migrate() when the column is missing
ALTER TABLE passages ADD COLUMN range_start INTEGER;
