-- migration 024: claims.conditions_status, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN conditions_status TEXT NOT NULL DEFAULT 'complete';
