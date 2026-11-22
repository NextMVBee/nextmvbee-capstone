-- migration 035: claims.product_id, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN product_id TEXT;
