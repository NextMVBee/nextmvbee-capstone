-- migration 023: claims.provenance_json, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '[]';
