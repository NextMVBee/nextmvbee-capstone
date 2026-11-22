-- migration 053: plan_versions.content_sig (Phase 6, the content signature per family of D-A7, computed at approval time), additive; applied by app.repo.migrate() when the column is missing
ALTER TABLE plan_versions ADD COLUMN content_sig TEXT;
