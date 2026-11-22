-- migration 064 (UI-2): users gains workos_user_id (UNIQUE), name, run_allowance, allowance_set_at and allowance_set_by, and
-- password_hash becomes NULL-able; sessions gains workos_session and refreshed_at. SQLite cannot add a UNIQUE column or drop a
-- NOT NULL by ALTER TABLE, so the users table is rebuilt by SQLite's documented twelve-step procedure: foreign keys off outside
-- the transaction, the new table, the rows copied, the old table dropped, the new one renamed, the foreign-key check, then foreign
-- keys on again (app.repo.migrate() raises when the check reports a row). Applied by migrate() when users lacks workos_user_id.
-- Every user keeps its id and every projects.owner_id stays as it was; sessions keep their rows (the drop runs with foreign keys off).
PRAGMA foreign_keys = OFF;
BEGIN;
CREATE TABLE users_ui2 (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  workos_user_id TEXT UNIQUE,
  name TEXT,
  run_allowance INTEGER NOT NULL DEFAULT 0,
  allowance_set_at TEXT,
  allowance_set_by TEXT
);
INSERT INTO users_ui2 (id, email, password_hash, created_at) SELECT id, email, password_hash, created_at FROM users;
DROP TABLE users;
ALTER TABLE users_ui2 RENAME TO users;
ALTER TABLE sessions ADD COLUMN workos_session TEXT;
ALTER TABLE sessions ADD COLUMN refreshed_at TEXT;
PRAGMA foreign_key_check;
COMMIT;
PRAGMA foreign_keys = ON;
