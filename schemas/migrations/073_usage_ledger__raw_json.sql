-- migration 073 (Phase 17 package 17.1, D-A267): usage_ledger gains raw_json, the model's answer text of an attempt settled invalid or
-- truncated (the attempt's text is otherwise held by the gateway alone and lost when the stage stops before a repair: the one permitted
-- draft after a time stop, run p6-R41). Never replayed: completed_result reads result_json only. Applied by migrate() when usage_ledger
-- lacks raw_json.
ALTER TABLE usage_ledger ADD COLUMN raw_json TEXT;
