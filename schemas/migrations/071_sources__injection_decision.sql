-- migration 071 (Phase 11 package 11.2, D-A191, criterion I9): the founder's decision on a source flagged for instruction-like text
-- (sources.injection_flag): NULL while undecided (the source stays excluded from claims), 'include' (the founder read the page and admits it:
-- the next extraction keeps its claims and never flags it again) or 'exclude' (the source stays out and is not read again). Written by
-- app.core.interventions.decide_source only.
ALTER TABLE sources ADD COLUMN injection_decision TEXT CHECK (injection_decision IN ('include','exclude'));
