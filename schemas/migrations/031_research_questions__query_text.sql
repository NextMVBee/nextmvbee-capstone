-- migration 031: research_questions.query_text, applied by app.repo.migrate() when the column is missing
ALTER TABLE research_questions ADD COLUMN query_text TEXT;
