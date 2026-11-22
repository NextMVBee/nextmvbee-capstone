-- migration 046: the exports table (Phase 06x.4, D-A25), additive; applied by app.repo.migrate() through schemas/schema.sql (CREATE TABLE IF NOT EXISTS) and kept here as the shipped record of the change
CREATE TABLE IF NOT EXISTS exports (               -- Phase 06x.4 (D-A25, Addendum 06x section 5): one row per project, plan version, family and format of a document export
  id TEXT PRIMARY KEY,                       -- <project_id>.EX-<nnn>, allocated under BEGIN IMMEDIATE from the highest id of the project
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,                  -- the plan version (plan_versions.version)
  family TEXT NOT NULL,                      -- the document family of atelier.profile.json (mvp-plan, prd)
  format TEXT NOT NULL CHECK (format IN ('html','pdf','docx')),
  snapshot_sig TEXT NOT NULL,                -- plan_versions.snapshot_sig of the approved version (the fixture's until Phase 6 lands)
  content_sig TEXT NOT NULL,                 -- the content signature of the approved ContentDoc (D-A7)
  kit_version TEXT NOT NULL,                 -- the vendored kit's VERSION at the request
  routing_revision INTEGER NOT NULL,         -- atelier.profile.json routing.revision at the request (part of the reuse key, T30.18)
  direction_id TEXT, direction_version INTEGER,   -- the direction the production locked (never changed by a repair)
  job_id TEXT, delivery_id TEXT,             -- the kit's job (data/atelier/jobs/<project_id>/<job_id>/) and delivery
  output_hash TEXT,                          -- the kit's recorded hash of this format's delivered file (delivery.json outputHashes)
  attempt INTEGER NOT NULL DEFAULT 1,        -- the production attempt of this key (project, version, family)
  status TEXT NOT NULL CHECK (status IN ('planned','rendering','checked','available','partial','failed','cancelled','rejected')),
  reason TEXT,                               -- why a terminal state was reached (ids, codes and counts; never page text)
  path TEXT,                                 -- the published file of this format under data/exports/<project>/v<version>/<sig>/<export_id>/ (the ZIP for an HTML above single_file_max_mb)
  sha256 TEXT, size_bytes INTEGER, page_count INTEGER,
  manifest_json TEXT,                        -- the host manifest of the publication (the delivery's ids, hashes and gates; no page text)
  warnings_json TEXT NOT NULL DEFAULT '[]',
  requested_by TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS exports_by_key ON exports(project_id, version, family, status);
