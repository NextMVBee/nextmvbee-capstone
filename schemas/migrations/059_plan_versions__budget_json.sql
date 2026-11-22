-- Phase 9a (D-A46): the budget of the version at the founder's usage (rows, total, ceiling, worst cases), computed by code from the sourced prices.
ALTER TABLE plan_versions ADD COLUMN budget_json TEXT;
