-- migration 052: plan_versions.approval_ref (Phase 6, the approval act of D-A13), additive; applied by app.repo.migrate() when the column is missing
ALTER TABLE plan_versions ADD COLUMN approval_ref TEXT;
