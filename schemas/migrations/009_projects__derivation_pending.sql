-- migration 009: projects.derivation_pending, applied by app.repo.migrate() when the column is missing
ALTER TABLE projects ADD COLUMN derivation_pending INTEGER NOT NULL DEFAULT 0;
