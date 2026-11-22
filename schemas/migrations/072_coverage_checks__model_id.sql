-- migration 072 (Phase 11 package 11.3, D-A192): coverage_checks gains model_id, the model identity (the coverage stage's route) a verdict
-- was judged under. A verdict counts only under the model in use, as it counts only under the prompt version in use (D-A190) and only
-- while the claims it judged are unchanged (D-A161); a row written before this rule holds NULL, has nothing to show and is judged again.
-- Applied by migrate() when coverage_checks lacks model_id.
ALTER TABLE coverage_checks ADD COLUMN model_id TEXT;
