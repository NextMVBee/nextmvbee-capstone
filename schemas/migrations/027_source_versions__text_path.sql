-- migration 027: source_versions.text_path, applied by app.repo.migrate() when the column is missing
ALTER TABLE source_versions ADD COLUMN text_path TEXT;
