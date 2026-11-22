-- migration 037: decisions.product_id, applied by app.repo.migrate() when the column is missing
ALTER TABLE decisions ADD COLUMN product_id TEXT;
