-- migration 036: claims.product_version, applied by app.repo.migrate() when the column is missing
ALTER TABLE claims ADD COLUMN product_version TEXT;
