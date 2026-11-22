-- migration 038: decisions.product_version, applied by app.repo.migrate() when the column is missing
ALTER TABLE decisions ADD COLUMN product_version TEXT;
