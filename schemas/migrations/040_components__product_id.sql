-- migration 040: components.product_id, applied by app.repo.migrate() when the column is missing
ALTER TABLE components ADD COLUMN product_id TEXT;
