-- migration 044: questions.kind (Phase 05b, C12: value or hardness), applied by app.repo.migrate() when the column is missing
ALTER TABLE questions ADD COLUMN kind TEXT NOT NULL DEFAULT 'value' CHECK (kind IN ('value','hardness'));
