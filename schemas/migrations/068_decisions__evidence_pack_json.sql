-- Phase 10 package 10.2 (D-A184): the evidence packs the comparison saw, one per eligible candidate, stored beside the decision's draft for the audit
ALTER TABLE decisions ADD COLUMN evidence_pack_json TEXT;
