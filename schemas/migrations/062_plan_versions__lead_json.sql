-- Phase 9b (C9): the plan on one page's lead (text and claim ids), written by the plan model from the decisions.
ALTER TABLE plan_versions ADD COLUMN lead_json TEXT;
