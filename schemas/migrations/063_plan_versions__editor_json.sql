-- Phase 9b (D-A55): the Editor's record of the version (rewritten and dropped fields, merged duplicates, flags).
ALTER TABLE plan_versions ADD COLUMN editor_json TEXT;
