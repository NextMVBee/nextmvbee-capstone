-- migration 025: claims.decision_subject, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN decision_subject TEXT;
