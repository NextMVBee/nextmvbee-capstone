-- migration 078 (Phase 18 package 18.3, D-A383): tree_nodes gains hard_check_json, what the node's hard check read behind its hard_status: per binding
-- hard requirement the verdict, the id and date of the coverage_checks row it read and the claim ids the verdict rests on. The coverage_checks upsert
-- keeps replacing rows (PRIMARY KEY on project, requirement, version, product, product version), so the node keeps what it read. Nodes written
-- before this migration read '[]'. Applied by migrate() when tree_nodes lacks hard_check_json.
ALTER TABLE tree_nodes ADD COLUMN hard_check_json TEXT NOT NULL DEFAULT '[]';
