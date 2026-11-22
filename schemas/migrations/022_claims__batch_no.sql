-- migration 022: claims.batch_no, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN batch_no INTEGER;
