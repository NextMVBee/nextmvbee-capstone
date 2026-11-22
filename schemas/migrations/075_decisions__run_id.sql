-- migration 075 (Phase 18 package 18.1, D-A302): decisions gains run_id, the run that wrote the decision (written at every upsert; a later
-- upsert without a run keeps it). The snapshot attaches to a decision the tree of that run and no other (app/core/snapshot.py); a decision
-- without a run id (an older store) falls back to the subject's latest tree version across runs, as before. Existing rows are backfilled
-- from the run's own announcement of the decision (run_events, event 'decision', data_json naming the decision id): the first run that
-- announced it; a decision no run announced stays NULL. Applied by migrate() when decisions lacks run_id.
ALTER TABLE decisions ADD COLUMN run_id TEXT;
UPDATE decisions SET run_id = (SELECT e.run_id FROM run_events e WHERE e.event = 'decision' AND e.data_json LIKE '%"decision_id": "' || decisions.id || '"%' ORDER BY e.created_at, e.id LIMIT 1) WHERE run_id IS NULL;
