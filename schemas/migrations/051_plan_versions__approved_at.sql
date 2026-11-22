-- migration 051: plan_versions.approved_at (Phase 6, the approval act of D-A13), additive; applied by app.repo.migrate() when the column is missing
ALTER TABLE plan_versions ADD COLUMN approved_at TEXT;
