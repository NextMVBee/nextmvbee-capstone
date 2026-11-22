-- migration 049: plan_versions.snapshot_sig (Phase 6, sha256 of the canonical snapshot JSON), additive; applied by app.repo.migrate() when the column is missing
ALTER TABLE plan_versions ADD COLUMN snapshot_sig TEXT;
