-- Phase 9a (D-A40): the subjects a hard requirement binds, a JSON list of decision subjects; NULL binds every subject (an unmapped requirement, the behaviour before this phase).
ALTER TABLE requirements ADD COLUMN applies_to TEXT;
