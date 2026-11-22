-- migration 041: components.product_version, applied by app.repo.migrate() when the column is missing
ALTER TABLE components ADD COLUMN product_version TEXT;
