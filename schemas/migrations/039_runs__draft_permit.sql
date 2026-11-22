-- migration 039: runs.draft_permit, applied by app.repo.migrate() when the column is missing
ALTER TABLE runs ADD COLUMN draft_permit TEXT;
