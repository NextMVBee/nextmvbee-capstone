-- migration 034: decisions.unresolved_json, applied by app.repo.migrate() when the column is missing
ALTER TABLE decisions ADD COLUMN unresolved_json TEXT NOT NULL DEFAULT '[]';
