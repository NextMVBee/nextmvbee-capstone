-- migration 048: plan_versions.snapshot_json (Phase 6, the complete signed content of the version (Addendum 06x section 3)), additive; applied by app.repo.migrate() when the column is missing
ALTER TABLE plan_versions ADD COLUMN snapshot_json TEXT;
