-- migration 079 (Phase 18 package 18.3, D-A342): verification_checks gains the pass record of a run of the product on the founder's own input:
-- result (pass or fail), result_at and the identity the run exercised (result_product_id, result_product_version). The documents mark an option
-- validated from this record alone (app/export/document_model.py); every check written before it carries NULL and marks nothing. Applied by
-- migrate() when verification_checks lacks result.
ALTER TABLE verification_checks ADD COLUMN result TEXT CHECK (result IN ('pass','fail'));
ALTER TABLE verification_checks ADD COLUMN result_at TEXT;
ALTER TABLE verification_checks ADD COLUMN result_product_id TEXT;
ALTER TABLE verification_checks ADD COLUMN result_product_version TEXT;
