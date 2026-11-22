-- migration 008: projects.rules_version, applied by app.repo.migrate() when the column is missing
ALTER TABLE projects ADD COLUMN rules_version INTEGER NOT NULL DEFAULT 0;
