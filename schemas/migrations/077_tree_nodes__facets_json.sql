-- migration 077 (Phase 18 package 18.1, D-A301): tree_nodes gains facets_json, the variants of kind route or approach the model proposed for
-- the node (label, kind, access_route, claim_ids): facets of the node's product identity, folded into the node (their claim ids join its own)
-- and never scored siblings. Nodes written before this migration read '[]'. Applied by migrate() when tree_nodes lacks facets_json.
ALTER TABLE tree_nodes ADD COLUMN facets_json TEXT NOT NULL DEFAULT '[]';
