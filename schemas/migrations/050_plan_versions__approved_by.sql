-- migration 050: plan_versions.approved_by (Phase 6, the approval act of D-A13, bound to snapshot_sig), additive; applied by app.repo.migrate() when the column is missing
ALTER TABLE plan_versions ADD COLUMN approved_by TEXT;
