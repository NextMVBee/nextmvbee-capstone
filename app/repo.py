"""Repository layer: typed records in SQLite, keyed by project-scoped ids, surviving restarts.

Four jobs:
1. Connections. connect() opens data/app.db (or NMB_APP_DB) with WAL, foreign keys on and a 5 s busy timeout on
   every connection; init_db() applies schemas/schema.sql, which is idempotent (CREATE ... IF NOT EXISTS).
2. Checkpoints. open_checkpointer() returns a LangGraph SqliteSaver over data/checkpoints.db (or NMB_CHECKPOINT_DB),
   a separate file from the application records (records and checkpoints never share a file).
3. Records. Repo upserts and reads the durable records of schemas/models.py.
   Ids: every record id is '<project_id>.<PREFIX>-<nnn>' (app/core/ids.py): the repository refuses an id without a project prefix and an id
   whose project part differs from the record's project_id, so two projects can both show REQ-001 without colliding
   and a write can never cross projects.
   Display numbers: they come from the id_counters table, allocated under
   BEGIN IMMEDIATE, so two connections never receive the same id and a deleted highest id is never reissued.
   References: every reference field (supersedes, question_id, answer_requirement_id, source_id, requirement and claim ids,
   component_id, depends_on, task_id, trace edge endpoints) is checked inside the upsert transaction for the same
   project, the expected prefix and an existing target; a violation raises IntegrityViolation and nothing is written.
   Upserts: they are idempotent (INSERT ... ON CONFLICT DO UPDATE); JSON columns hold lists and dicts; a decision's
   requirement and claim links live in trace_edges as a canonical set, replaced on each upsert and read back sorted
   by id (order and duplicates in the input list carry no meaning); the claims_fts row follows its claim.
   Deletion: deleting a project is app/core/privacy.delete_project, not duplicated here; no reference ever blocks that cascade.
   Counter advancement: every write path that stores an id (an allocated id, an explicit id passed to an upsert, or an
   id found in an existing database by migrate()) raises the counter to at least one above that number, so a deleted
   highest id is never reissued whichever way it was created.
4. Migration. init_db() calls migrate(): schemas/schema.sql is applied idempotently (every statement is CREATE ... IF
   NOT EXISTS, so a database that predates a table gains it), then id_counters is seeded from the highest existing id
   per project and prefix. Safe to run on every start.
Transaction ownership: Repo never commits or rolls back a caller's open transaction. When conn.in_transaction is true
on entry, each operation runs inside a SAVEPOINT: on success the savepoint is released into the caller's transaction,
on failure it is rolled back to, so a rejected operation leaves the caller's pending work exactly as it was, and the
caller decides whether to commit or roll back. Without a caller transaction each operation is its own BEGIN IMMEDIATE.
No model calls, no interface, no secrets: paths only come from the environment.
"""
from __future__ import annotations
from app.core.sqlguard import require_guarded, apply_shipped_schema, BoundaryRefusal

import json
import os
import re
import pathlib
import sqlite3
from contextlib import contextmanager

from app.core.ids import PATTERN, PREFIXES, display_id, make_id, prefix_of
from schemas import models as M

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "schema.sql"
DEFAULT_APP_DB = "data/app.db"
DEFAULT_CHECKPOINT_DB = "data/checkpoints.db"
BUSY_TIMEOUT_MS = 120000   # 120 s: parallel source workers that derive and write after their sources hold the write lock in turn, and a worker that fails with 'database is locked' turns its completed send into an unknown call and its source runs again in sequence; a write waits, never fails, inside this bound

TABLES = {"REQ": "requirements", "ASM": "assumptions", "Q": "questions", "SRC": "sources", "CLM": "claims",
          "DEC": "decisions", "CMP": "components", "TSK": "tasks", "ACC": "acceptance_tests"}
MODELS = {"REQ": M.Requirement, "ASM": M.Assumption, "Q": M.OpenQuestion, "SRC": M.Source, "CLM": M.Claim,
          "DEC": M.Decision, "CMP": M.Component, "TSK": M.Task, "ACC": M.AcceptanceTest}
# trace edge kind -> (from prefix, to prefix), as schemas/schema.sql defines the chain
EDGE_ENDPOINTS = {"req_decision": ("REQ", "DEC"), "claim_decision": ("CLM", "DEC"), "decision_component": ("DEC", "CMP"),
                  "component_task": ("CMP", "TSK"), "task_test": ("TSK", "ACC"), "req_test": ("REQ", "ACC")}
EDGE_KINDS = tuple(EDGE_ENDPOINTS)
MIGRATIONS_DIR = SCHEMA_PATH.parent / "migrations"                        # one shipped file per column added since the table was created, applied when the column is missing
CHECKPOINTER_SCHEMA = SCHEMA_PATH.parent / "checkpointer.sql"               # the LangGraph checkpointer's own tables, as the pinned library creates them
FIXTURES_DIR = SCHEMA_PATH.parent.parent / "tests" / "fixtures" / "sql"     # the suites' SQL fixtures, applied only under NMB_TEST_FIXTURES=1




CLAIM_EXTRACTION_COLUMNS = ("identity_key", "source_version", "range_start", "range_end", "batch_no", "provenance_json", "conditions_status", "decision_subject", "verified_requirement_version", "product_id", "product_version", "verdict", "invalidated_at", "access_route", "identity_kind")   # access_route and identity_kind are extraction bookkeeping beside product_id


class IntegrityViolation(ValueError):
    """A reference points outside the project, to the wrong record type or to a record that does not exist."""


# ---------------------------------------------------------------- connections and files
def app_db_path() -> str:
    return os.environ.get("NMB_APP_DB", DEFAULT_APP_DB)


def checkpoint_db_path() -> str:
    return os.environ.get("NMB_CHECKPOINT_DB", DEFAULT_CHECKPOINT_DB)


def connect(path: str | os.PathLike | None = None, mode: str = "raise", production: bool = False) -> sqlite3.Connection:
    """Open the application store with the per-connection settings the schema relies on. A production handle (the runner's
    and the CLI's, production=True) refuses to open while NMB_TEST_FIXTURES is set: fixtures and production never mix."""
    path = pathlib.Path(path or app_db_path())                        # a production handle refuses to open while NMB_TEST_FIXTURES is set: the handle's constructor enforces it
    path.parent.mkdir(parents=True, exist_ok=True)
    from app.core.sqlguard import open_connection
    conn = open_connection(path, mode=mode, production=production, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)   # every connection in app/ is a GuardedConnection with its mode; production marks the runner's and the CLI's
    try:                                                                # cleanup from the moment of acquisition: a refused setting closes the handle before the refusal propagates
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    except BaseException:
        conn.close(); raise
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    """The schema installer: schemas/schema.sql through sqlguard.apply_shipped_schema (the manifest-bound schema application), whose triggers become the trusted triggers."""
    require_guarded(conn)
    apply_shipped_schema(conn, "schemas/schema.sql")
    conn.execute("PRAGMA foreign_keys = ON")   # executescript ends the transaction; make the setting explicit again


def apply_fixture(conn: sqlite3.Connection, name: str, temp: bool = False) -> str:
    """A suite's SQL fixture (tests/fixtures/sql/<name>, listed in the manifest) through sqlguard.apply_shipped_schema, only
    under NMB_TEST_FIXTURES=1 and never on a production handle; temp=True opens the inspection context as well, for temp
    views and triggers. Returns the relative path applied."""
    require_guarded(conn)
    if "/" in name or name.startswith(".") or not name.endswith(".sql"):
        raise BoundaryRefusal("a fixture is named by its file name under tests/fixtures/sql/")
    return apply_shipped_schema(conn, f"tests/fixtures/sql/{name}", temp=temp)


def migration_files() -> list[tuple[pathlib.Path, str, str]]:
    """The shipped migration files in order: (path, table, column), from the file name NNN_<table>__<column>.sql."""
    out = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"\d{3}_([a-z_]+)__([a-z_]+)\.sql$", f.name)
        if not m:
            raise RuntimeError(f"migration file name not understood: {f.name}")
        out.append((f, m.group(1), m.group(2)))
    return out


def migrate(conn: sqlite3.Connection) -> dict[str, int]:
    """Bring an existing database up to the current schema and seed id_counters from the highest id in use.
    Idempotent. Returns the counter value now held per (project_id, prefix)."""
    require_guarded(conn)
    for path, table, column in migration_files():                       # CREATE TABLE IF NOT EXISTS never adds a column to an existing table; columns first, so the triggers find them
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if have and column not in have:
            apply_shipped_schema(conn, f"schemas/migrations/{path.name}")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()   # a migration may rebuild a table (064 does): a rebuild ends with the foreign-key check
            if broken:
                raise RuntimeError(f"migration {path.name} left {len(broken)} foreign-key violation(s)")
    questions_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='questions'").fetchone()
    if questions_sql and "'confirmation'" not in questions_sql[0]:       # migration 054: the question kind gains a value; SQLite cannot alter a CHECK, so the table of an existing store is rebuilt once
        apply_shipped_schema(conn, "schemas/migrations/054_questions__kind.sql")
    questions_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='questions'").fetchone()
    if questions_sql and "'usage'" not in questions_sql[0]:              # migration 055: the usage question's kind, the same rebuild once
        apply_shipped_schema(conn, "schemas/migrations/055_questions__kind.sql")
    questions_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='questions'").fetchone()
    if questions_sql and "'revision'" not in questions_sql[0]:           # migration 070: the revision and contradiction kinds, the same rebuild once
        apply_shipped_schema(conn, "schemas/migrations/070_questions__kind.sql")
    exclusions_sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='candidate_exclusions'").fetchone()
    if exclusions_sql and "decision_id, product_id" not in exclusions_sql[0]:   # migration 080: the unique key gains decision_id; SQLite cannot alter a constraint, so the table of a store with the key without it is rebuilt once
        apply_shipped_schema(conn, "schemas/migrations/080_candidate_exclusions__decision_id.sql")
        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"migration 080_candidate_exclusions__decision_id.sql left {len(broken)} foreign-key violation(s)")
    apply_schema(conn)
    seeded = {}
    with Repo(conn)._tx():
        for prefix, table in TABLES.items():
            for project_id, record_id in conn.execute(f"SELECT project_id, id FROM {table}"):
                if not PATTERN.match(record_id) or prefix_of(record_id) != prefix:
                    continue   # rows written without a project prefix are left as they are
                n = int(display_id(record_id).split("-", 1)[1])
                key = (project_id, prefix)
                seeded[key] = max(seeded.get(key, 0), n + 1)
        current = {(r[0], r[1]): int(r[2]) for r in conn.execute("SELECT project_id, prefix, next_n FROM id_counters").fetchall()}
        for (project_id, prefix), next_n in seeded.items():
            if current.get((project_id, prefix), -1) < next_n:                             # a counter is written only when its value changes, so a no-op command performs zero writes
                conn.execute("INSERT INTO id_counters (project_id, prefix, next_n) VALUES (?,?,?) "
                             "ON CONFLICT(project_id, prefix) DO UPDATE SET next_n=max(next_n, excluded.next_n)", (project_id, prefix, next_n))
    rows = conn.execute("SELECT project_id, prefix, next_n FROM id_counters").fetchall()
    return {f"{r[0]}.{r[1]}": r[2] for r in rows}


def init_db(path: str | os.PathLike | None = None, mode: str = "raise", production: bool = False) -> sqlite3.Connection:
    conn = connect(path, mode=mode, production=production)
    try:
        migrate(conn)
    except BaseException:
        conn.close(); raise                                             # a refused migration closes the handle it would have returned
    return conn


def open_checkpointer(path: str | os.PathLike | None = None):
    """LangGraph SqliteSaver over the separate checkpoint file; tables are created on first use."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    path = pathlib.Path(path or checkpoint_db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = __import__('app.core.sqlguard', fromlist=['open_connection']).open_connection(path, timeout=BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    try:                                                                # cleanup from the moment of acquisition, until the saver owns the handle
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        apply_shipped_schema(conn, "schemas/checkpointer.sql")          # the library's own setup script, shipped byte for byte; nothing in its tables is derived state
        saver = SqliteSaver(conn)
        saver.is_setup = True                                           # the library would otherwise run its own script (definitions outside the installer: refused)
    except BaseException:
        conn.close(); raise
    return saver


def tables(conn: sqlite3.Connection) -> list[str]:
    require_guarded(conn)
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


# ---------------------------------------------------------------- id checks
def project_of(record_id: str) -> str:
    if not PATTERN.match(record_id):
        raise ValueError(f"{record_id!r} is not a project-scoped id (<project_id>.<PREFIX>-<nnn>)")
    return record_id.split(".", 1)[0]


def _owned(record_id: str, project_id: str, prefix: str | None = None) -> str:
    """The id must carry this project's prefix (and this record prefix, when given)."""
    if project_of(record_id) != project_id:
        raise ValueError(f"{record_id} belongs to project {project_of(record_id)!r}, not {project_id!r}")
    if prefix is not None and prefix_of(record_id) != prefix:
        raise ValueError(f"{record_id} is not a {prefix} id")
    return record_id


def _j(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        require_guarded(conn)                                                      # application entry points accept only the guarded type
        self.conn = conn

    _savepoints = 0

    @contextmanager
    def _tx(self):
        """Own transaction: BEGIN IMMEDIATE ... COMMIT, rolled back on any error (as app/core/ledger.py does).
        Inside a caller's transaction: a SAVEPOINT, released on success and rolled back to on error, so the caller's
        pending work is never committed or discarded by the repository."""
        if self.conn.in_transaction:
            Repo._savepoints += 1
            name = f"repo_sp_{Repo._savepoints}"
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self.conn.execute(f"RELEASE SAVEPOINT {name}")
                raise
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _advance(self, project_id: str, prefix: str, record_id: str) -> None:
        """Raise the counter to at least one above this id's number. Called inside the write transaction."""
        n = int(display_id(record_id).split("-", 1)[1])
        self.conn.execute("INSERT INTO id_counters (project_id, prefix, next_n) VALUES (?,?,?) "
                          "ON CONFLICT(project_id, prefix) DO UPDATE SET next_n=max(next_n, excluded.next_n)", (project_id, prefix, n + 1))

    def _ref(self, record_id: str | None, project_id: str, prefix: str, field: str) -> str | None:
        """A reference must be in the same project, carry the expected prefix and exist. Called inside the transaction."""
        if record_id is None:
            return None
        try:
            _owned(record_id, project_id, prefix)
        except ValueError as e:
            raise IntegrityViolation(f"{field}: {e}") from None
        if not self.conn.execute(f"SELECT 1 FROM {TABLES[prefix]} WHERE id=? AND project_id=?", (record_id, project_id)).fetchone():
            raise IntegrityViolation(f"{field}: {record_id} does not exist in project {project_id!r}")
        return record_id

    # ------------------------------------------------------------ projects
    def create_project(self, project: M.Project) -> M.Project:
        d = project.model_dump(mode="json")
        with self._tx():
            self.conn.execute(
                "INSERT INTO projects (id, owner_id, title, privacy_mode, retention_days, requirement_version) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET owner_id=excluded.owner_id, title=excluded.title, privacy_mode=excluded.privacy_mode, "
                "retention_days=excluded.retention_days, requirement_version=excluded.requirement_version",
                (d["id"], d["owner_id"], d["title"], d["privacy_mode"], d["retention_days"], d["requirement_version"]))
        return project

    upsert_project = create_project

    def get_project(self, project_id: str) -> M.Project | None:
        row = self.conn.execute("SELECT id, owner_id, title, privacy_mode, retention_days, requirement_version FROM projects "
                                "WHERE id=? AND deleted_at IS NULL", (project_id,)).fetchone()
        return M.Project(**dict(row)) if row else None

    def list_projects(self, owner_id: str) -> list[M.Project]:
        rows = self.conn.execute("SELECT id, owner_id, title, privacy_mode, retention_days, requirement_version FROM projects "
                                 "WHERE owner_id=? AND deleted_at IS NULL ORDER BY created_at, id", (owner_id,)).fetchall()
        return [M.Project(**dict(r)) for r in rows]

    # ------------------------------------------------------------ ids
    def next_id(self, project_id: str, prefix: str) -> str:
        """Allocate the next display number for this project and prefix from the durable id_counters row, under
        BEGIN IMMEDIATE: concurrent connections are serialised, an allocated number is consumed even if the record is
        never written, and a deleted highest id is never reissued. The counter also never falls below one above the
        highest id already in the table, so rows written by other code (app.core.ids.make_id) are respected."""
        if prefix not in PREFIXES:
            raise ValueError(f"unknown prefix {prefix!r}")
        with self._tx():
            row = self.conn.execute("SELECT next_n FROM id_counters WHERE project_id=? AND prefix=?", (project_id, prefix)).fetchone()
            rows = self.conn.execute(f"SELECT id FROM {TABLES[prefix]} WHERE project_id=? AND id LIKE ?",
                                     (project_id, f"{project_id}.{prefix}-%")).fetchall()
            highest = max((int(display_id(r[0]).split("-", 1)[1]) for r in rows), default=0)
            n = max(row[0] if row else 1, highest + 1)
            self.conn.execute("INSERT INTO id_counters (project_id, prefix, next_n) VALUES (?,?,?) "
                              "ON CONFLICT(project_id, prefix) DO UPDATE SET next_n=excluded.next_n", (project_id, prefix, n + 1))
        return make_id(project_id, prefix, n)

    # ------------------------------------------------------------ upserts (idempotent, keyed by the project-scoped id)
    def upsert_requirement(self, r: M.Requirement) -> M.Requirement:
        d = r.model_dump(mode="json"); _owned(d["id"], d["project_id"], "REQ")
        with self._tx():
            self._advance(d["project_id"], "REQ", d["id"])
            self._ref(d["supersedes"], d["project_id"], "REQ", "supersedes")
            if "applies_to" in {r[1] for r in self.conn.execute("PRAGMA table_info(requirements)")}:       # the applies_to map column (migration 056); a store without it (a suite's fixture schema) takes the statement without the column
                self.conn.execute(
                    "INSERT INTO requirements (id, project_id, key, value_json, status, source, hard, version, supersedes, applies_to) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET key=excluded.key, value_json=excluded.value_json, status=excluded.status, source=excluded.source, "
                    "hard=excluded.hard, version=excluded.version, supersedes=excluded.supersedes, applies_to=COALESCE(excluded.applies_to, requirements.applies_to)",   # the map is kept when an upsert carries none
                    (d["id"], d["project_id"], d["key"], _j(d["value"]), d["status"], d["source"], int(d["hard"]), d["version"], d["supersedes"], _j(d["applies_to"]) if d.get("applies_to") is not None else None))
            else:
                self.conn.execute(
                    "INSERT INTO requirements (id, project_id, key, value_json, status, source, hard, version, supersedes) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET key=excluded.key, value_json=excluded.value_json, status=excluded.status, source=excluded.source, "
                    "hard=excluded.hard, version=excluded.version, supersedes=excluded.supersedes",
                    (d["id"], d["project_id"], d["key"], _j(d["value"]), d["status"], d["source"], int(d["hard"]), d["version"], d["supersedes"]))
        return r

    def upsert_assumption(self, a: M.Assumption) -> M.Assumption:
        d = a.model_dump(mode="json"); _owned(d["id"], d["project_id"], "ASM")
        with self._tx():
            self._advance(d["project_id"], "ASM", d["id"])
            self._ref(d["question_id"], d["project_id"], "Q", "question_id")
            self.conn.execute(
                "INSERT INTO assumptions (id, project_id, text, impact, status, question_id) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET text=excluded.text, impact=excluded.impact, status=excluded.status, question_id=excluded.question_id",
                (d["id"], d["project_id"], d["text"], d["impact"], d["status"], d["question_id"]))
        return a

    def upsert_question(self, q: M.OpenQuestion) -> M.OpenQuestion:
        d = q.model_dump(mode="json"); _owned(d["id"], d["project_id"], "Q")
        with self._tx():
            self._advance(d["project_id"], "Q", d["id"])
            self._ref(d["answer_requirement_id"], d["project_id"], "REQ", "answer_requirement_id")
            self.conn.execute(
                "INSERT INTO questions (id, project_id, text, why_blocking, status, answer_requirement_id, kind) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET text=excluded.text, why_blocking=excluded.why_blocking, status=excluded.status, "
                "answer_requirement_id=excluded.answer_requirement_id, kind=excluded.kind",
                (d["id"], d["project_id"], d["text"], d["why_blocking"], d["status"], d["answer_requirement_id"], d.get("kind", "value")))   # the insert carries the kind column
        return q

    def upsert_source(self, s: M.Source) -> M.Source:
        d = s.model_dump(mode="json"); _owned(d["id"], d["project_id"], "SRC")
        with self._tx():
            self._advance(d["project_id"], "SRC", d["id"])
            self.conn.execute(
                "INSERT INTO sources (id, project_id, url, canonical_url, title, publisher, tier, published_at, retrieved_at, content_hash, freshness, "
                "fetch_status, injection_flag, cache_status, max_age_hours) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET url=excluded.url, canonical_url=excluded.canonical_url, title=excluded.title, publisher=excluded.publisher, "
                "tier=excluded.tier, published_at=excluded.published_at, retrieved_at=excluded.retrieved_at, content_hash=excluded.content_hash, "
                "freshness=excluded.freshness, fetch_status=excluded.fetch_status, injection_flag=excluded.injection_flag, cache_status=excluded.cache_status, "
                "max_age_hours=excluded.max_age_hours",
                (d["id"], d["project_id"], d["url"], d["canonical_url"], d["title"], d["publisher"], d["tier"], d["published_at"], d["retrieved_at"],
                 d["content_hash"], d["freshness"], d["fetch_status"], int(d["injection_flag"]), d["cache_status"], d["max_age_hours"]))
        return s

    def upsert_claim(self, c: M.Claim) -> M.Claim:
        d = c.model_dump(mode="json"); _owned(d["id"], d["project_id"], "CLM")
        with self._tx():
            self._advance(d["project_id"], "CLM", d["id"])
            self._ref(d["source_id"], d["project_id"], "SRC", "source_id")
            self.conn.execute(
                "INSERT INTO claims (id, project_id, text, kind, source_id, quote, impact, checked_at, fresh_until, subject, attribute, value, "
                "conditions_json, scope_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET text=excluded.text, kind=excluded.kind, source_id=excluded.source_id, quote=excluded.quote, "
                "impact=excluded.impact, checked_at=excluded.checked_at, fresh_until=excluded.fresh_until, subject=excluded.subject, "
                "attribute=excluded.attribute, value=excluded.value, conditions_json=excluded.conditions_json, scope_status=excluded.scope_status",
                (d["id"], d["project_id"], d["text"], d["kind"], d["source_id"], d["quote"], d["impact"], d["checked_at"], d["fresh_until"],
                 d["subject"], d["attribute"], d["value"], _j(d["conditions"]), d["scope_status"]))
            self.conn.execute("DELETE FROM claims_fts WHERE claim_id=?", (d["id"],))
            self.conn.execute("INSERT INTO claims_fts (claim_id, text, quote) VALUES (?,?,?)", (d["id"], d["text"], d["quote"] or ""))
        return c

    def record_verification_result(self, check_id: str, result: str, product_id: str, product_version: str = "", at: str | None = None) -> None:
        """The pass record of a run of the product on the founder's own input, on the verification check that states it:
        result pass or fail, the date and the identity the run exercised (the pick's product id and version). The documents mark an option validated
        from this record alone; nothing else writes it. A result outside pass and fail is refused before any write."""
        if result not in ("pass", "fail"):
            raise ValueError("a verification result is pass or fail")
        with self._tx():
            row = self.conn.execute("SELECT id FROM verification_checks WHERE id=?", (check_id,)).fetchone()
            if row is None:
                raise ValueError(f"no verification check {check_id!r}")
            self.conn.execute("UPDATE verification_checks SET result=?, result_at=COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')), result_product_id=?, result_product_version=? WHERE id=?",
                              (result, at, product_id, product_version or "", check_id))

    def upsert_decision(self, dec: M.Decision) -> M.Decision:
        d = dec.model_dump(mode="json"); _owned(d["id"], d["project_id"], "DEC")
        with self._tx():
            self._advance(d["project_id"], "DEC", d["id"])
            links = [(self._ref(i, d["project_id"], "REQ", "requirement_ids"), "req_decision") for i in sorted(set(d["requirement_ids"]))]
            links += [(self._ref(i, d["project_id"], "CLM", "claim_ids"), "claim_decision") for i in sorted(set(d["claim_ids"]))]
            self.conn.execute(                                                   # status is the record's proposal on insert; the derivation (app/core/reconcile.py) is its only later writer
                "INSERT INTO decisions (id, project_id, subject, scope, choice, rationale, importance, rejected_json, plan_version, proposed_status, run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET subject=excluded.subject, scope=excluded.scope, choice=excluded.choice, rationale=excluded.rationale, "
                "importance=excluded.importance, rejected_json=excluded.rejected_json, proposed_status=excluded.proposed_status, plan_version=excluded.plan_version, "
                "run_id=COALESCE(excluded.run_id, decisions.run_id)",                    # the run that wrote the decision; an upsert without one keeps the row's
                (d["id"], d["project_id"], d["subject"], d["scope"], d["choice"], d["rationale"], d["importance"], _j(d["rejected"]),
                 d["plan_version"], d["status"], d.get("run_id")))
            self.conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (d["project_id"],))
            self.conn.execute("DELETE FROM trace_edges WHERE project_id=? AND to_id=? AND kind IN ('req_decision','claim_decision')", (d["project_id"], d["id"]))
            self.conn.executemany("INSERT OR IGNORE INTO trace_edges (project_id, from_id, to_id, kind) VALUES (?,?,?,?)",
                                  [(d["project_id"], src, d["id"], kind) for src, kind in links])
        return dec

    def upsert_component(self, c: M.Component) -> M.Component:
        d = c.model_dump(mode="json"); _owned(d["id"], d["project_id"], "CMP")
        with self._tx():
            self._advance(d["project_id"], "CMP", d["id"])
            self.conn.execute(
                "INSERT INTO components (id, project_id, name, responsibility, data_leaves_device, status) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, responsibility=excluded.responsibility, data_leaves_device=excluded.data_leaves_device, "
                "status=excluded.status",
                (d["id"], d["project_id"], d["name"], d["responsibility"], int(d["data_leaves_device"]), d["status"]))
        return c

    def upsert_task(self, t: M.Task) -> M.Task:
        d = t.model_dump(mode="json"); _owned(d["id"], d["project_id"], "TSK")
        with self._tx():
            self._advance(d["project_id"], "TSK", d["id"])
            self._ref(d["component_id"], d["project_id"], "CMP", "component_id")
            for dep in d["depends_on"]:
                self._ref(dep, d["project_id"], "TSK", "depends_on")
            self.conn.execute(
                "INSERT INTO tasks (id, project_id, component_id, title, depends_on_json, effort_low_h, effort_high_h, status) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET component_id=excluded.component_id, title=excluded.title, depends_on_json=excluded.depends_on_json, "
                "effort_low_h=excluded.effort_low_h, effort_high_h=excluded.effort_high_h, status=excluded.status",
                (d["id"], d["project_id"], d["component_id"], d["title"], _j(d["depends_on"]), d["effort_low_h"], d["effort_high_h"], d["status"]))
        return t

    def upsert_acceptance_test(self, a: M.AcceptanceTest) -> M.AcceptanceTest:
        d = a.model_dump(mode="json"); _owned(d["id"], d["project_id"], "ACC")
        with self._tx():
            self._advance(d["project_id"], "ACC", d["id"])
            self._ref(d["task_id"], d["project_id"], "TSK", "task_id")
            self._ref(d["requirement_id"], d["project_id"], "REQ", "requirement_id")
            self.conn.execute(
                "INSERT INTO acceptance_tests (id, project_id, task_id, requirement_id, criterion, status) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET task_id=excluded.task_id, requirement_id=excluded.requirement_id, criterion=excluded.criterion, status=excluded.status",
                (d["id"], d["project_id"], d["task_id"], d["requirement_id"], d["criterion"], d["status"]))
        return a

    def upsert_plan_version(self, v: M.PlanVersion) -> M.PlanVersion:
        """The row's mutable fields. The snapshot and approval columns are never part of this update: set_snapshot writes
        the snapshot once, the approval step of the document renderer (not part of this repository) writes the approval act once."""
        d = v.model_dump(mode="json")
        with self._tx():
            self.conn.execute(
                "INSERT INTO plan_versions (project_id, version, requirement_version, change_summary, diff_json, export_path) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(project_id, version) DO UPDATE SET requirement_version=excluded.requirement_version, change_summary=excluded.change_summary, "
                "diff_json=excluded.diff_json, export_path=excluded.export_path",
                (d["project_id"], d["version"], d["requirement_version"], d["change_summary"], _j(d["diff"]), d["export_path"]))
        return v

    def set_snapshot(self, project_id: str, version: int, snapshot_json: str, snapshot_sig: str) -> None:
        """The signed snapshot of a version, written once in the transaction that creates the version (app/core/snapshot.py
        builds and validates it). A version that already holds a snapshot is refused: snapshot_json and snapshot_sig never change."""
        with self._tx():
            row = self.conn.execute("SELECT snapshot_sig FROM plan_versions WHERE project_id=? AND version=?", (project_id, int(version))).fetchone()
            if row is None:
                raise IntegrityViolation(f"plan version {version} of project {project_id!r} does not exist")
            if row[0] is not None:
                raise IntegrityViolation(f"plan version {version} of project {project_id!r} already holds a snapshot ({row[0][:12]}); a snapshot never changes")
            self.conn.execute("UPDATE plan_versions SET snapshot_json=?, snapshot_sig=? WHERE project_id=? AND version=? AND snapshot_sig IS NULL", (snapshot_json, snapshot_sig, project_id, int(version)))

    def set_export_path(self, project_id: str, version: int, export_path: str) -> None:
        with self._tx():
            self.conn.execute("UPDATE plan_versions SET export_path=? WHERE project_id=? AND version=?", (export_path, project_id, int(version)))

    def add_trace_edge(self, project_id: str, from_id: str, to_id: str, kind: str) -> None:
        """Endpoints must match the kind (req_decision: REQ to DEC, and so on), belong to the project and exist."""
        if kind not in EDGE_ENDPOINTS:
            raise ValueError(f"unknown trace edge kind {kind!r}")
        from_prefix, to_prefix = EDGE_ENDPOINTS[kind]
        with self._tx():
            self._ref(from_id, project_id, from_prefix, f"{kind} from_id"); self._ref(to_id, project_id, to_prefix, f"{kind} to_id")
            self.conn.execute("INSERT OR IGNORE INTO trace_edges (project_id, from_id, to_id, kind) VALUES (?,?,?,?)", (project_id, from_id, to_id, kind))

    # ------------------------------------------------------------ reads
    def get(self, record_id: str):
        """The typed record behind a project-scoped id, or None. A decision's status is derived state: the read goes through
        the guarded accessor (app/core/coverage.derived), which derives first when a base write left the flag."""
        prefix = prefix_of(record_id)
        if prefix in ("DEC", "CLM"):                                                     # a decision's status and a claim's status are derived state: read inside the accessor's context
            from app.core.coverage import derived
            with derived(self.conn, project_of(record_id)):
                row = self.conn.execute(f"SELECT * FROM {TABLES[prefix]} WHERE id=?", (record_id,)).fetchone()
            return self._to_model(prefix, row) if row else None
        project_of(record_id)
        row = self.conn.execute(f"SELECT * FROM {TABLES[prefix]} WHERE id=?", (record_id,)).fetchone()
        return self._to_model(prefix, row) if row else None

    def list(self, project_id: str, prefix: str) -> list:
        if prefix not in TABLES:
            raise ValueError(f"unknown prefix {prefix!r}")
        if prefix in ("DEC", "CLM"):
            from app.core.coverage import derived
            with derived(self.conn, project_id):
                rows = self.conn.execute(f"SELECT * FROM {TABLES[prefix]} WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            return [self._to_model(prefix, r) for r in rows]
        rows = self.conn.execute(f"SELECT * FROM {TABLES[prefix]} WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [self._to_model(prefix, r) for r in rows]

    def get_plan_version(self, project_id: str, version: int) -> M.PlanVersion | None:
        row = self.conn.execute("SELECT project_id, version, requirement_version, change_summary, diff_json, export_path, snapshot_sig, approved_by, approved_at, approval_ref, content_sig FROM plan_versions "
                                "WHERE project_id=? AND version=?", (project_id, version)).fetchone()
        if not row:
            return None
        d = dict(row); d["diff"] = json.loads(d.pop("diff_json"))
        return M.PlanVersion(**d)

    def trace_edges(self, project_id: str, to_id: str | None = None, from_id: str | None = None) -> list[tuple[str, str, str]]:
        sql, args = "SELECT from_id, to_id, kind FROM trace_edges WHERE project_id=?", [project_id]
        if to_id:
            sql += " AND to_id=?"; args.append(to_id)
        if from_id:
            sql += " AND from_id=?"; args.append(from_id)
        return [tuple(r) for r in self.conn.execute(sql + " ORDER BY kind, from_id, to_id", args)]

    def _to_model(self, prefix: str, row: sqlite3.Row):
        d = dict(row)
        if prefix == "REQ":
            d["value"] = json.loads(d.pop("value_json")); d["hard"] = bool(d["hard"])
            if "applies_to" in d:
                d["applies_to"] = json.loads(d["applies_to"]) if d["applies_to"] else None   # the applies_to map; None when the column is empty
        elif prefix == "Q":
            d = {k: d[k] for k in ("id", "project_id", "text", "why_blocking", "status", "answer_requirement_id")}
        elif prefix == "SRC":
            d.pop("text_path", None); d.pop("stale_at", None); d.pop("found_by_json", None); d.pop("injection_decision", None); d["injection_flag"] = bool(d["injection_flag"])   # injection_decision (the founder's decision) is a store column and found_by is read by the snapshot; neither is part of the typed record
        elif prefix == "CLM":
            d["conditions"] = json.loads(d.pop("conditions_json"))
            for k in CLAIM_EXTRACTION_COLUMNS:                           # extraction bookkeeping columns, read by app/core/extraction.py and coverage.py, not part of the typed record
                d.pop(k, None)
        elif prefix == "DEC":
            d["rejected"] = json.loads(d.pop("rejected_json")); d.pop("unresolved_json", None); d.pop("product_id", None); d.pop("product_version", None); d.pop("proposed_status", None); d.pop("derivation_reason", None); d.pop("memo_json", None); d.pop("evidence_pack_json", None)   # the memo is read by the snapshot and the screens, not part of the typed record; the evidence packs likewise
            edges = self.trace_edges(d["project_id"], to_id=d["id"])
            d["requirement_ids"] = [f for f, _, k in edges if k == "req_decision"]
            d["claim_ids"] = [f for f, _, k in edges if k == "claim_decision"]
        elif prefix == "CMP":
            d["data_leaves_device"] = bool(d["data_leaves_device"]); d.pop("product_id", None); d.pop("product_version", None)
        elif prefix == "TSK":
            d["depends_on"] = json.loads(d.pop("depends_on_json"))
        return MODELS[prefix](**d)
