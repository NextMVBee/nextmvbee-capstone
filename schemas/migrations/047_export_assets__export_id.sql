-- migration 047: the export_assets table (Phase 06x.4, D-A25), additive; applied by app.repo.migrate() through schemas/schema.sql (CREATE TABLE IF NOT EXISTS) and kept here as the shipped record of the change
CREATE TABLE IF NOT EXISTS export_assets (         -- Phase 06x.4: every file published with an export (the package, the SVG sources beside the DOCX, the README, the manifest, the audit copies), with its hash
  export_id TEXT NOT NULL REFERENCES exports(id) ON DELETE CASCADE,
  name TEXT NOT NULL,                        -- the path inside the export directory
  kind TEXT NOT NULL CHECK (kind IN ('delivered','svg','package','readme','manifest','audit')),
  sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
  PRIMARY KEY (export_id, name)
);
