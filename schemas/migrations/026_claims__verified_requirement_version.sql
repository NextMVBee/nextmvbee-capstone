-- migration 026: claims.verified_requirement_version, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN verified_requirement_version INTEGER;
