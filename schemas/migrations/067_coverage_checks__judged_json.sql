-- migration 067 (Phase 9e, D-A161): coverage_checks gains judged_json, the claims a verdict judged (the product's supported, in-scope claims,
-- [id, source version] in id order). A stored verdict counts only while they are unchanged; a row written before this migration holds NULL,
-- has no record to show and is judged again. Applied by migrate() when coverage_checks lacks judged_json.
ALTER TABLE coverage_checks ADD COLUMN judged_json TEXT;
