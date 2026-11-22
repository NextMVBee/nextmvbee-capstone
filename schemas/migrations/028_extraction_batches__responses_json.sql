-- migration 028: extraction_batches.responses_json, applied by app.repo.migrate() when the column is missing
ALTER TABLE extraction_batches ADD COLUMN responses_json TEXT NOT NULL DEFAULT '[]';
