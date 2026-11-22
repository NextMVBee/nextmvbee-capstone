-- migration 074 (Phase 18 package 18.1, D-A299): candidates gains the identity fields beside the label: kind (product or approach),
-- vendor, product, version, access_route (the plan, tier, endpoint or deployment the lead named: a facet of the identity, never a
-- candidate of its own) and urls_json (the lead's pages). The label stays the display spelling, composed by code as the vendor then
-- the product and never repeating a vendor the product already names (app.core.discovery.display_label). Rows written before this
-- migration keep their label and class with the fields empty. Applied by migrate() when candidates lacks kind.
ALTER TABLE candidates ADD COLUMN kind TEXT NOT NULL DEFAULT 'product';
ALTER TABLE candidates ADD COLUMN vendor TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN product TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN version TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN access_route TEXT NOT NULL DEFAULT '';
ALTER TABLE candidates ADD COLUMN urls_json TEXT NOT NULL DEFAULT '[]';
