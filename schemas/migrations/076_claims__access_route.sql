-- migration 076 (Phase 18 package 18.1, D-A300): claims gains access_route (the plan, endpoint, batch or deployment route a claim speaks
-- about: a facet of the product identity the claim carries, never an identity of its own; extract_evidence.v5) and identity_kind
-- (product, or approach for a way of using the product). Claims written before this migration read '' and product. Applied by
-- migrate() when claims lacks access_route.
ALTER TABLE claims ADD COLUMN access_route TEXT NOT NULL DEFAULT '';
ALTER TABLE claims ADD COLUMN identity_kind TEXT NOT NULL DEFAULT 'product';
