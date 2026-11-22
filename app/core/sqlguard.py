"""The execution boundary for derived evidence state (the columns of app.core.coverage.DERIVED_COLUMNS, which only the derive
step writes).

Enforcement happens inside SQLite: the guarded handle opens the native connection itself (statement cache off, keyword arguments
allow-listed) and installs an authorizer on it, so every statement SQLite prepares (through the handle, a cursor, a script, a
view resolved to its underlying columns, a CTE, a trigger body, a foreign-key action) reports each access with its action code,
and the callback answers from an explicit decision table (DECISION_TABLE) over every SQLite authorizer action code: allowed,
refused, or decided by the handle's contexts.

The contexts, all counters on the handle:
  the accessor's reads context (derived reads; the accessor is app.core.coverage.derived, the one reader of derived state);
  the derive-step context (derived reads and writes);
  the inspection context for tests and scripts (derived reads, temp and attached objects);
  the schema-application context, which this module alone opens, inside apply_shipped_schema(handle, relative_path): the one
  way to run definitions.

Shipped schema files: apply_shipped_schema applies only files the repository ships and lists in schemas/MANIFEST.json with their
sha256 (schemas/schema.sql, schemas/migrations/*.sql, schemas/checkpointer.sql and the suite fixtures tests/fixtures/sql/*.sql,
the last only under NMB_TEST_FIXTURES=1 and never on a production handle). The manifest is read once at import and frozen; a
file is applied only when its path has no '.' or '..' component, lies in its expected directory, has no symlink in any
component under the repository root, is a regular file and hashes to its entry. Repository files are trusted; the manifest is
one of them (it is regenerated when a listed file changes; a check fails when it and the files disagree).

Trusted triggers: definitions run statement by statement with the object registry (name, kind, SQL hash) refreshed after each
statement; the triggers created there are the trusted triggers, registered by name and the hash of their exact sqlite_master
text (nothing folded) and re-checked whenever the schema version changes.

Handle state: a handle's authorization state lives in module-private storage (_STATE), read-only through the handle;
GuardedConnection is final (no subclass); a test observes decisions and commits through observe(), receiving frozen
events after the decision is final.

Refusals: an action code outside the table is refused. A refusal in mode "raise" is BoundaryRefusal (DerivedAccessViolation for
derived state) with the statement's fingerprint, never its text; in mode "log" (a production handle's setting) the
access is allowed and logged with the fingerprint.

Composition, not inheritance: GuardedConnection and GuardedCursor hold their native objects in a module-private weak-key
map, never in an attribute. The supported boundary: the guard refuses
every route available to ordinary application code, that is attributes, __dict__, vars(), weakref, cursors, iteration,
row and text factories (identity-checked), the trace and function callbacks (they receive statement text and values
only), and the __closure__, __defaults__ and __self__ of the authorizer and trace callbacks, which hold a weak reference
to the handle and nothing native. It does not claim isolation against deliberate interpreter introspection (gc
referent walks, frame walking, the module-private map), which only a separate native owner process could give.

The tokeniser (classify) is a second layer on write statements for what SQLite reports without detail (an INSERT's column
list, a derived table read by a subquery of a write) and serves the static scans.

The only call to sqlite3.connect under app/ is in this module; application entry points accept only a GuardedConnection."""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import posixpath
import re
import sqlite3
import types
import weakref

LOG = logging.getLogger("nmb.sqlguard")
MODES = ("raise", "log")
_OK, _DENY = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY


def mode_from(settings: dict | None) -> str:
    """settings [guard].sql_mode for a production handle: raise (default) or log."""
    mode = ((settings or {}).get("guard") or {}).get("sql_mode") or "raise"
    if mode not in MODES:
        raise ValueError("guard.sql_mode must be 'raise' or 'log'")
    return mode


class BoundaryRefusal(RuntimeError):
    """An access the decision table refuses outside its context (schema definition, attached or temp objects, a pragma)."""


class DerivedAccessViolation(BoundaryRefusal):
    """Derived state touched outside the accessor's reads context, the derive-step context or an inspection context."""


_DERIVED: dict | None = None


def derived_columns() -> dict[str, tuple[str, ...]]:
    global _DERIVED
    if _DERIVED is None:
        from app.core.coverage import DERIVED_COLUMNS
        _DERIVED = DERIVED_COLUMNS
    return _DERIVED


def dict_factory(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


# ---------------------------------------------------------------- tokeniser (second layer: fingerprints, static scans)
_QUOTED = r'"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[(?:[^\]]|\]\])*\]'
_IDENT = r"[A-Za-z_][\w$]*"
_PART = rf"(?:{_IDENT}|{_QUOTED}|\*)"
_TOKEN = re.compile(rf"""--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\?|:\w+|(?:{_IDENT}|{_QUOTED})(?:\s*\.\s*{_PART})*|\d+(?:\.\d+)?|\*|[(),;=<>!+\-/%|&~]""", re.S)
_KEYWORDS = {"select", "from", "join", "left", "right", "inner", "outer", "cross", "natural", "on", "where", "and", "or", "not", "in", "is", "null", "as", "set", "values", "into", "update",
             "delete", "insert", "replace", "group", "by", "order", "limit", "offset", "having", "distinct", "case", "when", "then", "else", "end", "exists", "union", "all", "except",
             "intersect", "like", "between", "with", "returning", "conflict", "do", "nothing", "asc", "desc", "coalesce", "count", "max", "min", "sum", "json", "strftime", "lower",
             "upper", "trim", "substr", "length", "abs", "round", "ifnull", "nullif", "cast", "collate", "escape", "glob", "match", "indexed", "using", "primary", "key", "default", "temp",
             "temporary", "table", "trigger", "index", "view", "create", "drop", "alter", "add", "column", "begin", "commit", "rollback", "savepoint", "release", "pragma", "immediate",
             "exclusive", "deferred", "transaction", "if", "abort", "fail", "ignore", "raise", "new", "old", "for", "each", "row", "after", "before", "of", "true", "false", "sqlite_master",
             "recursive", "excluded", "over", "partition", "window"}


_DELIMITERS = {'"': '"', "`": "`", "[": "]"}


def _unquote(name: str) -> str:
    """The canonical (lower-cased) name of an identifier token: a quoted form loses its delimiters and a doubled closing
    delimiter inside it stands for one ("a""b", `a``b`, [a]]b]); an unquoted form is itself."""
    name = name.strip()
    if name[:1] in _DELIMITERS and len(name) >= 2:
        close = _DELIMITERS[name[0]]
        return name[1:-1].replace(close + close, close).lower()
    return name.lower()


def _split_qualified(tok: str) -> list[str]:
    parts, buf, i = [], "", 0
    while i < len(tok):
        ch = tok[i]
        if ch in '"`[':
            close = _DELIMITERS[ch]
            j = tok.find(close, i + 1)
            while j != -1 and j + 1 < len(tok) and tok[j + 1] == close:                  # a doubled closing delimiter is part of the name
                j = tok.find(close, j + 2)
            if j == -1:
                j = len(tok) - 1
            buf += tok[i: j + 1]; i = j + 1; continue
        if ch == ".":
            parts.append(buf); buf = ""; i += 1; continue
        if ch.isspace():
            i += 1; continue
        buf += ch; i += 1
    parts.append(buf)
    return [_unquote(p) if p != "*" else "*" for p in parts if p != ""]


_CLASSIFIED: dict[str, dict] = {}


def classify(sql: str, views: dict | None = None) -> dict:
    """The tokeniser's classification of one statement over the derived tables and columns (reads, writes, tables), used
    by the static scans and the second layer; SQLite's authorizer, not this function, enforces at execution. Results
    for a statement text are memoised (the map of derived columns is fixed)."""
    if views is None:
        hit = _CLASSIFIED.get(sql)
        if hit is not None:
            return hit
        out = _classify(sql, None)
        if len(_CLASSIFIED) > 4096:
            _CLASSIFIED.clear()
        _CLASSIFIED[sql] = out
        return out
    return _classify(sql, views)


def _classify(sql: str, views: dict | None) -> dict:
    derived = derived_columns()
    toks = [t for t in _TOKEN.findall(sql) if not t.startswith("--") and not t.startswith("/*")]
    words = [(_split_qualified(t)[-1] if t[:1] in '"`[' and "." not in t else t.lower()) for t in toks]

    class Scope:
        __slots__ = ("parent", "tables", "aliases", "kind", "target", "after_set", "in_insert_cols", "saw_cols")
        def __init__(self, parent):
            self.parent, self.tables, self.aliases, self.kind, self.target = parent, [], {}, None, None
            self.after_set, self.in_insert_cols, self.saw_cols = False, False, False

    n = len(words)
    scopes, skip = [], set()
    root = Scope(None); scopes.append(root); scope = root; stack = []
    i = 0
    while i < n:
        w = words[i]
        if w == "(":
            stack.append(scope); scope = Scope(scope); scopes.append(scope); i += 1; continue
        if w == ")":
            scope = stack.pop() if stack else scope; i += 1; continue
        if w in ("select", "update", "delete", "insert", "replace") and scope.kind is None:
            scope.kind = "insert" if w == "replace" else w
        if w in ("from", "join", "update", "into"):
            j = i + 1
            if j < n and words[j] != "(" and words[j] != "select":
                parts = _split_qualified(toks[j]); name = parts[-1] if parts else None
                if name and name not in _KEYWORDS and name != "*":
                    scope.tables.append(name); skip.add(j)
                    if w in ("update", "into"):
                        scope.target = name
                    k = j + 1
                    if k < n and words[k] == "as":
                        skip.add(k); k += 1
                    if k < n and words[k] not in _KEYWORDS and words[k] not in ("(", ")", ",") and re.match(r"^[A-Za-z_\"`\[]", toks[k]) and "." not in toks[k]:
                        scope.aliases[_split_qualified(toks[k])[-1]] = name; skip.add(k)
        i += 1
    reads, writes = set(), set()
    def tables_in_force(sc):
        s = sc
        while s is not None:
            if s.tables:
                return s.tables
            s = s.parent
        return []
    def resolve_alias(sc, a):
        s = sc
        while s is not None:
            if a in s.aliases:
                return s.aliases[a]
            s = s.parent
        return None
    def note(table, col, is_write):
        cols = derived.get(table)
        if not cols:
            return
        if cols == ("*",) or col == "*" or col in cols:
            (writes if is_write else reads).add((table, col if col != "*" else "*"))
    created = iter(scopes[1:]); scope = scopes[0]; stack = []; i = 0
    while i < n:
        w = words[i]; raw = toks[i]
        if i in skip:
            i += 1; continue
        if w == "(":
            stack.append(scope); scope = next(created); i += 1
            if scope.parent is not None and scope.parent.kind == "insert" and not scope.parent.after_set and scope.parent.target and scope.kind is None:
                scope.in_insert_cols = True; scope.parent.saw_cols = True
            continue
        if w == ")":
            scope = stack.pop() if stack else scope; i += 1; continue
        if w == "set" and scope.kind == "update":
            scope.after_set = True; i += 1; continue
        if w in ("where", "from", "returning") and scope.kind == "update":
            scope.after_set = False
        if w in _KEYWORDS or w in ("?", ",", ";", "=", "<", ">", "!", "+", "-", "/", "%", "|", "&", "~") or re.match(r"^\d", w) or raw[:1] in ("'", ":"):
            if w == "values" and scope.kind == "insert":
                scope.after_set = True
            i += 1; continue
        names = _split_qualified(raw)
        if len(names) >= 2 and names[-1] != "*" and names[-2] in ("main", "temp"):
            names = names[-1:]
        if len(names) >= 3 and names[-3] in ("main", "temp"):
            names = names[-2:]
        in_force = tables_in_force(scope)
        if scope.in_insert_cols and scope.parent is not None:
            note(scope.parent.target, names[-1], True); i += 1; continue
        if scope.kind == "update" and scope.after_set and i + 1 < n and words[i + 1] == "=" and (i == 0 or words[i - 1] in ("set", ",")):
            note(scope.target, names[-1], True); i += 1; continue
        if len(names) == 1:
            col = names[0]
            if col == "*":
                if i > 0 and words[i - 1] in ("select", ",", "distinct"):
                    for t in in_force:
                        note(t, "*", False)
            else:
                for t in in_force:
                    note(t, col, False)
        else:
            qual, col = names[-2], names[-1]
            table = resolve_alias(scope, qual) or (qual if qual in derived or (views and qual in views) else None)
            if table:
                note(table, col, False)
        i += 1
    tables = {t for sc in scopes for t in sc.tables}
    for sc in scopes:
        if sc.kind in ("insert", "update", "delete") and sc.target and derived.get(sc.target) == ("*",):
            writes.add((sc.target, "*"))
        if sc.kind == "delete" and sc.tables and derived.get(sc.tables[0]) == ("*",):
            writes.add((sc.tables[0], "*"))
        if sc.kind == "insert" and sc.target and sc.target in derived and not sc.saw_cols:
            writes.add((sc.target, "*"))
    if views:
        for t in tables:
            if t in views:
                reads |= set(views[t]["reads"])
    return {"reads": reads, "writes": writes, "tables": tables}


def fingerprint(sql: str | None) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", (sql or "").strip().lower()).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- the decision table over SQLite's authorizer action codes
ALLOWED, REFUSED, BY_CONTEXT = "allowed", "refused", "decided by context"
SQLITE_COPY = 0                                                                   # the obsolete COPY action: never emitted, refused
_SCHEMA = "the schema-application context only"
_INSPECTION = "the inspection context only"
_TEMP_DEFINITION = "the inspection context and the schema-application context together"
DECISION_TABLE: dict[int, tuple[str, str, str]] = {                               # code -> (name, decision, rule)
    sqlite3.SQLITE_SELECT: ("SQLITE_SELECT", ALLOWED, "data access begins; the own-definition marker is reset so CREATE TABLE ... AS SELECT is data access"),
    sqlite3.SQLITE_READ: ("SQLITE_READ", BY_CONTEXT, "a derived column: the reads, derive-step or inspection context, a trusted trigger body, the table's own definition inside schema application, or (inside a guarded execution) a declared foreign-key column read by a write on its parent; a temp or attached object: the inspection context only; any other column: allowed"),
    sqlite3.SQLITE_UPDATE: ("SQLITE_UPDATE", BY_CONTEXT, "a derived column: the derive-step context only (a trusted trigger body included); a temp or attached object: the inspection context only; any other column: allowed"),
    sqlite3.SQLITE_INSERT: ("SQLITE_INSERT", BY_CONTEXT, "a derived-only table: the derive-step context only, or (inside a guarded execution) a declared foreign-key action of the statement's own target; a temp or attached object: the inspection context only; any other table: allowed (the tokeniser checks the column list)"),
    sqlite3.SQLITE_DELETE: ("SQLITE_DELETE", BY_CONTEXT, "a derived-only table: the derive-step context only, or (inside a guarded execution) a declared foreign-key action of the statement's own target; a temp or attached object: the inspection context only; any other table: allowed"),
    sqlite3.SQLITE_CREATE_TABLE: ("SQLITE_CREATE_TABLE", BY_CONTEXT, _SCHEMA),
    sqlite3.SQLITE_CREATE_INDEX: ("SQLITE_CREATE_INDEX", BY_CONTEXT, _SCHEMA),
    sqlite3.SQLITE_CREATE_VIEW: ("SQLITE_CREATE_VIEW", BY_CONTEXT, _SCHEMA + "; a name held by any other object or by a trusted trigger is refused"),
    sqlite3.SQLITE_CREATE_TRIGGER: ("SQLITE_CREATE_TRIGGER", BY_CONTEXT, _SCHEMA + "; the trigger joins the trusted set once its sqlite_master row is read (its body may then read derived state; it writes derived state only inside the derive step); a name held by a view or any other object is refused"),
    sqlite3.SQLITE_CREATE_VTABLE: ("SQLITE_CREATE_VTABLE", BY_CONTEXT, _SCHEMA + " (the FTS5 tables)"),
    sqlite3.SQLITE_CREATE_TEMP_TABLE: ("SQLITE_CREATE_TEMP_TABLE", BY_CONTEXT, _INSPECTION),
    sqlite3.SQLITE_CREATE_TEMP_INDEX: ("SQLITE_CREATE_TEMP_INDEX", BY_CONTEXT, _INSPECTION),
    sqlite3.SQLITE_CREATE_TEMP_VIEW: ("SQLITE_CREATE_TEMP_VIEW", BY_CONTEXT, _TEMP_DEFINITION + "; a name held by any other object or by a trusted trigger is refused"),
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER: ("SQLITE_CREATE_TEMP_TRIGGER", BY_CONTEXT, _TEMP_DEFINITION + "; registered like a trigger; a name held by a view or any other object is refused"),
    sqlite3.SQLITE_DROP_TABLE: ("SQLITE_DROP_TABLE", BY_CONTEXT, _SCHEMA),
    sqlite3.SQLITE_DROP_INDEX: ("SQLITE_DROP_INDEX", BY_CONTEXT, _SCHEMA),
    sqlite3.SQLITE_DROP_VIEW: ("SQLITE_DROP_VIEW", BY_CONTEXT, _SCHEMA),
    sqlite3.SQLITE_DROP_TRIGGER: ("SQLITE_DROP_TRIGGER", BY_CONTEXT, _SCHEMA + "; the trigger leaves the trusted set"),
    sqlite3.SQLITE_DROP_VTABLE: ("SQLITE_DROP_VTABLE", BY_CONTEXT, _SCHEMA),
    sqlite3.SQLITE_DROP_TEMP_TABLE: ("SQLITE_DROP_TEMP_TABLE", BY_CONTEXT, _INSPECTION),
    sqlite3.SQLITE_DROP_TEMP_INDEX: ("SQLITE_DROP_TEMP_INDEX", BY_CONTEXT, _INSPECTION),
    sqlite3.SQLITE_DROP_TEMP_VIEW: ("SQLITE_DROP_TEMP_VIEW", BY_CONTEXT, _TEMP_DEFINITION),
    sqlite3.SQLITE_DROP_TEMP_TRIGGER: ("SQLITE_DROP_TEMP_TRIGGER", BY_CONTEXT, _TEMP_DEFINITION + "; the trigger leaves the trusted set"),
    sqlite3.SQLITE_ALTER_TABLE: ("SQLITE_ALTER_TABLE", BY_CONTEXT, _SCHEMA + " (the column migrations)"),
    sqlite3.SQLITE_ANALYZE: ("SQLITE_ANALYZE", BY_CONTEXT, _SCHEMA + " (it writes sqlite_stat1)"),
    sqlite3.SQLITE_REINDEX: ("SQLITE_REINDEX", BY_CONTEXT, "the schema-application context, like every other main-schema definition; also the inspection context, where SQLite reports it while it builds a temp index"),
    sqlite3.SQLITE_ATTACH: ("SQLITE_ATTACH", BY_CONTEXT, _INSPECTION),
    sqlite3.SQLITE_DETACH: ("SQLITE_DETACH", BY_CONTEXT, _INSPECTION),
    sqlite3.SQLITE_PRAGMA: ("SQLITE_PRAGMA", BY_CONTEXT, "the listed pragmas (ALLOWED_PRAGMAS) are allowed; any other pragma is refused"),
    sqlite3.SQLITE_FUNCTION: ("SQLITE_FUNCTION", BY_CONTEXT, "every SQL function except load_extension, which is refused"),
    sqlite3.SQLITE_TRANSACTION: ("SQLITE_TRANSACTION", ALLOWED, "BEGIN, COMMIT and ROLLBACK"),
    sqlite3.SQLITE_SAVEPOINT: ("SQLITE_SAVEPOINT", ALLOWED, "SAVEPOINT, RELEASE and ROLLBACK TO"),
    sqlite3.SQLITE_RECURSIVE: ("SQLITE_RECURSIVE", ALLOWED, "a recursive common table expression; its reads are reported separately"),
    SQLITE_COPY: ("SQLITE_COPY", REFUSED, "obsolete, never emitted"),
}
ALLOWED_PRAGMAS = frozenset({"journal_mode", "foreign_keys", "foreign_key_check", "busy_timeout", "database_list", "foreign_key_list", "table_info", "table_xinfo", "index_list",
                             "secure_delete", "wal_autocheckpoint", "wal_checkpoint", "schema_version", "data_version", "user_version", "page_count", "page_size",
                             "integrity_check", "quick_check", "synchronous", "cache_size", "temp_store", "encoding", "compile_options"})
_SCHEMA_ONLY = {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VTABLE,
                sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VTABLE,
                sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_ANALYZE, sqlite3.SQLITE_REINDEX}
_INSPECTION_ONLY = {sqlite3.SQLITE_CREATE_TEMP_TABLE, sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE, sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}
_TEMP_DEFINITIONS = {sqlite3.SQLITE_CREATE_TEMP_VIEW, sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW, sqlite3.SQLITE_DROP_TEMP_TRIGGER}
_DEFINITION_CODES = _SCHEMA_ONLY | _INSPECTION_ONLY | _TEMP_DEFINITIONS
_TRIGGER_CODES = {sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_TRIGGER}
_VIEW_CODES = {sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_TEMP_VIEW}
_DROP_TRIGGER_CODES = {sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_TRIGGER}
_MAIN_VIEW_TRIGGER = {sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_TRIGGER}
_WRITE_CODES = {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE}
_NOT_AUTHORIZED = ("not authorized", "is prohibited")
_WRITE_WORDS = ("insert", "replace", "update", "delete")
_CONNECT_KWARGS = frozenset({"timeout", "detect_types", "isolation_level", "check_same_thread", "uri"})   # what the handle passes to sqlite3.connect; no factory
_LEADING = r"\A(?:\s*(?:--[^\n]*\n|/\*.*?\*/))*\s*"
_DDL_RE = re.compile(_LEADING + r"(?:create|drop|alter|reindex|analyze)\b", re.I | re.S)
_QUOTED_NAME = r'"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[(?:[^\]]|\]\])*\]|[\w$]+'
_OBJECT_NAME = r'((?:(?:' + _QUOTED_NAME + r')\s*\.\s*)?(?:' + _QUOTED_NAME + r'))'                                                  # an optional schema qualifier, then the name; doubled delimiters complete
_DEFINITION_RE = re.compile(_LEADING + r"create\s+(?:(temp|temporary)\s+)?(view|trigger)\s+(?:if\s+not\s+exists\s+)?" + _OBJECT_NAME, re.I | re.S)
_NATIVE = weakref.WeakKeyDictionary()           # handle -> native connection, cursor -> native cursor; module-private, never an attribute
_BOOK = weakref.WeakKeyDictionary()             # handle -> its bookkeeping cursor (plain tuples)
_NAMED_DDL_RE = re.compile(_LEADING + r"(?:create|drop)\s+(?:(temp|temporary|virtual)\s+)?(table|index|view|trigger)\s+(?:if\s+(?:not\s+)?exists\s+)?" + _OBJECT_NAME, re.I | re.S)


def decision_table_rows() -> list[tuple[int, str, str, str]]:
    """The decision table as rows (code, name, decision, rule), sorted by code."""
    return sorted((code, name, decision, rule) for code, (name, decision, rule) in DECISION_TABLE.items())


def _native(obj):
    """The native object behind a handle or cursor (module-private; the tests' refusal probes use it). Reaching for a
    handle's native connection starts afresh: no per-statement state of an earlier preparation survives into it."""
    if isinstance(obj, GuardedConnection):
        obj._reset_statement()
    return _NATIVE[obj]


def _bookkeeping(handle) -> sqlite3.Cursor:
    """The handle's bookkeeping cursor (module-private storage, never returned by a handle method): plain tuples, text as
    str whatever the handle's factories; the caller holds the internal flag and restores the text factory."""
    cur = _BOOK.get(handle)
    if cur is None:
        cur = _BOOK[handle] = _NATIVE[handle].cursor(); cur.row_factory = None
    return cur


def _statements(script: str) -> list[str]:
    """The statements of a script, split where sqlite3.complete_statement says one ends (strings, comments and trigger bodies respected)."""
    out, buf = [], ""
    for piece in script.split(";"):
        buf += piece + ";"
        if sqlite3.complete_statement(buf):
            out.append(buf); buf = ""
    if buf.strip(";\n\t "):
        out.append(buf)
    return out


_HASHES: dict[str, str] = {}


def _sql_hash(sql) -> str:
    """The hash of a definition's exact sqlite_master text: nothing is folded, so a change inside any literal (single- or
    double-quoted) or in the whitespace changes the hash. Memoised by the exact text."""
    if isinstance(sql, bytes):
        sql = sql.decode("utf-8", "replace")
    sql = sql or ""
    h = _HASHES.get(sql)
    if h is None:
        h = _HASHES[sql] = hashlib.sha256(sql.encode()).hexdigest()
        if len(_HASHES) > 4096:
            _HASHES.clear()
    return h


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


class _State:
    """A handle's authorization and bookkeeping state, in module-private storage (_STATE); the handle exposes it read-only
    through __getattr__ for the suites' probes and holds nothing an observer or application code can assign."""
    __slots__ = ("guard_reads", "guard_derive", "guard_inspection", "guard_schema", "guard_mode", "last_denial", "last_sql", "in_run", "ddl_table", "stmt_target", "cascade", "fk_reads", "trace", "fk_pairs", "trusted", "pending_triggers", "objects", "schema_version", "temp_used", "schema_dirty", "internal", "callbacks", "observers", "commits", "production")


_STATE = weakref.WeakKeyDictionary()            # handle -> _State


def _snapshot(value):
    """A detached, immutable copy of a state value: a mapping proxy over a copied dict, a frozenset, a tuple, or the scalar."""
    if isinstance(value, dict):
        return types.MappingProxyType(dict(value))
    if isinstance(value, (set, frozenset)):
        return frozenset(value)
    if isinstance(value, list):
        return tuple(value)
    return value


def _state(handle) -> types.MappingProxyType:
    """A detached snapshot of the state behind a handle (module-private; the suites' probes read it): every value copied and frozen."""
    st = _STATE[handle]
    return types.MappingProxyType({f: _snapshot(getattr(st, f)) for f in _State.__slots__})


@dataclasses.dataclass(frozen=True)
class DecisionEvent:
    """What an observer sees of a decision, after it is final: a frozen copy of the facts, no reference to any state."""
    allowed: bool
    action: str | None
    table: str | None
    column: str | None
    problem: str | None
    derived: bool | None
    logged: bool = False


@dataclasses.dataclass(frozen=True)
class CommitEvent:
    """A commit that already happened on the native connection (an observer may raise here: the suites' crash injection)."""
    commits: int


def _authorize_callback(ref):
    """The authorizer callback: a closure over a weak reference to the handle, nothing else (no default, no bound self)."""
    def authorize(action, arg1, arg2, dbname, source):
        h = ref()
        if h is None:
            return _DENY
        r = h._authorize(action, arg1, arg2, dbname, source)
        if _STATE[h].observers and r == _OK:
            h._notify(DecisionEvent(True, DECISION_TABLE.get(action, (str(action),))[0], arg1, arg2, None, None))   # after the decision; a frozen copy
        return r                                                             # the observers' answer plays no part
    return authorize


def _trace_callback(ref):
    """The trace callback: statement text in, per-statement state reset, the handle's own trace chained; weak reference only."""
    def trace(statement):
        h = ref()
        if h is not None:
            h._reset_markers()                                           # a statement started running: the next top-level write belongs to the next statement; a pending refusal stays
            if _STATE[h].trace is not None:
                _STATE[h].trace(statement)
    return trace


def _register_pending_names(handle):
    """The triggers created in the schema-application context join the trusted set with the hash of their exact
    sqlite_master text. Module-private; called from the guarded execution path alone, after a definition ran."""
    st = _STATE[handle]
    names, st.pending_triggers = st.pending_triggers, []
    native = _NATIVE[handle]; st.internal = True; text_factory = native.text_factory; native.text_factory = str
    try:
        cur = _bookkeeping(handle)
        for name in names:
            for master in ("sqlite_master", "sqlite_temp_master"):
                rows = cur.execute(f"SELECT sql FROM {master} WHERE type='trigger' AND lower(name)=?", (name,)).fetchall()   # consumed fully: no read lock stays on a bookkeeping statement
                if rows:
                    st.trusted[name] = _sql_hash(rows[0][0])
    finally:
        native.text_factory = text_factory; st.internal = False
    st.schema_dirty = True


def _trust_script_triggers(handle, script: str):
    """Schema application over an existing store: CREATE TRIGGER IF NOT EXISTS creates nothing and reports nothing, so a
    trigger of the script already in sqlite_master with exactly the definition text the script carries is registered
    too. Module-private; called from the schema-application context alone, inside apply_shipped_schema."""
    st = _STATE[handle]
    native = _NATIVE[handle]; st.internal = True; text_factory = native.text_factory; native.text_factory = str
    try:
        cur = _bookkeeping(handle)
        for stmt in _statements(script):
            m = _DEFINITION_RE.match(stmt)
            if not m or m.group(2).lower() != "trigger":
                continue
            body = re.sub(_LEADING, "", stmt, flags=re.S).strip().rstrip(";")                               # leading comments are not part of the stored definition
            name = _split_qualified(m.group(3))[-1]; wanted = _sql_hash(re.sub(r"IF\s+NOT\s+EXISTS\s+", "", body, count=1, flags=re.I))
            for master in ("sqlite_master", "sqlite_temp_master"):
                rows = cur.execute(f"SELECT sql FROM {master} WHERE type='trigger' AND lower(name)=?", (name,)).fetchall()
                if rows and _sql_hash(rows[0][0]) == wanted:
                    st.trusted[name] = wanted
    finally:
        native.text_factory = text_factory; st.internal = False
    st.schema_dirty = True


class GuardedConnection:
    """A handle that opens and owns its native sqlite3 connection; no attribute, method, __dict__, cursor or iteration exposes
    it. The class is final; a test observes commits and decisions through sqlguard.observe()."""
    __slots__ = ("__weakref__",)                                             # no state on the handle: _STATE holds it; nothing can be assigned here

    def __getattr__(self, name):
        """Detached, immutable snapshots of the private state for the suites' probes (conn._trusted, conn._objects, ...): a
        copy frozen at the moment of the read, never the live collection; assignment is refused by the empty slots."""
        if name.startswith("_") and not name.startswith("__"):
            st = _STATE.get(self)
            if st is not None and name[1:] in _State.__slots__:
                return _snapshot(getattr(st, name[1:]))
        raise AttributeError(name)

    def __init_subclass__(cls, **kwargs):
        raise TypeError("GuardedConnection is final: no subclass can replace its rules; a test observes it through sqlguard.observe()")

    def __init__(self, path, mode: str = "raise", production: bool = False, **kwargs):
        if self in _STATE or self in _NATIVE:
            raise BoundaryRefusal("a GuardedConnection is constructed exactly once; reinitialising an issued handle is refused before anything opens or changes")
        st = _State(); st.observers = []; st.commits = 0; st.production = bool(production)
        if production and os.environ.get(TEST_FIXTURES_VAR) == "1":
            raise BoundaryRefusal(f"a production handle refuses to open while {TEST_FIXTURES_VAR} is set")   # before any native store is opened; open_connection and direct construction alike
        if isinstance(path, sqlite3.Connection):
            raise TypeError("GuardedConnection opens its own native connection; an externally supplied native connection is refused")
        unknown = set(kwargs) - _CONNECT_KWARGS
        if unknown:
            raise TypeError(f"GuardedConnection accepts {sorted(_CONNECT_KWARGS)} for sqlite3.connect; refused: {sorted(unknown)} (no factory, no cache size)")
        if type(mode) is not str or mode not in MODES:
            raise ValueError("mode must be the plain str 'raise' or 'log'")
        st.guard_reads = 0; st.guard_derive = 0; st.guard_inspection = 0; st.guard_schema = 0
        st.guard_mode = mode; st.last_denial = None; st.last_sql = None; st.trace = None; st.in_run = False
        st.ddl_table = None; st.stmt_target = None; st.cascade = set(); st.fk_reads = set()
        st.fk_pairs = None; st.trusted = {}; st.pending_triggers = []; st.objects = {}; st.schema_version = None
        st.temp_used = False; st.schema_dirty = True; st.internal = False
        native = sqlite3.connect(path, cached_statements=0, **kwargs)
        if type(native) is not sqlite3.Connection:
            native.close(); raise TypeError("the native connection must be exactly sqlite3.Connection")
        _NATIVE[self] = native; _STATE[self] = st
        ref = weakref.ref(self)                                          # the callbacks hold no strong reference: a dropped handle closes its connection
        st.callbacks = (_authorize_callback(ref), _trace_callback(ref))
        native.set_authorizer(st.callbacks[0]); native.set_trace_callback(st.callbacks[1])

    # -- the schema snapshot: every object by name (kind, hash of its exact sqlite_master text), refreshed when the schema version changes
    def _refresh_schema(self, force: bool = False):
        """The object registry (every object by name: kind, hash of its exact sqlite_master text) follows the schema version,
        read with one pragma per statement (two once temp objects exist) and rebuilt when it changed or a definition ran.
        The foreign-key pairs are rebuilt with it, except inside the schema-application context, where only definitions
        run: there they wait for the first guarded execution outside it."""
        st = _STATE[self]
        native = _NATIVE[self]; st.internal = True; text_factory = native.text_factory
        if text_factory is not str:
            native.text_factory = str
        try:
            cur = _bookkeeping(self)
            version = cur.execute("PRAGMA main.schema_version").fetchall()[0][0]
            if st.temp_used:
                version = (version, cur.execute("PRAGMA temp.schema_version").fetchall()[0][0])
            changed = force or st.schema_dirty or version != st.schema_version
            if changed:
                objects = {}
                for master in (("sqlite_master", "sqlite_temp_master") if st.temp_used else ("sqlite_master",)):
                    for kind, name, sql in cur.execute(f"SELECT type, name, sql FROM {master}").fetchall():
                        key = name.lower(); entry = (kind, _sql_hash(sql) if kind in ("trigger", "view") else None)
                        objects[key] = ("ambiguous", None) if key in objects and objects[key][0] != kind else entry   # one name, two kinds: never resolved to either
                st.objects = objects
                st.trusted = {name: h for name, h in st.trusted.items() if objects.get(name) == ("trigger", h)}   # re-checked against sqlite_master
                st.schema_version = version; st.schema_dirty = False; st.fk_pairs = None
            if st.fk_pairs is None and not st.guard_schema:
                pairs = {}
                for (table,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                    for row in cur.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
                        pairs[(table.lower(), row[3].lower())] = row[2].lower()                  # (child table, column) -> parent table
                st.fk_pairs = pairs
        finally:
            if text_factory is not str:
                native.text_factory = text_factory
            st.internal = False

    def _update_registry(self, stmt: str | None):
        """After a definition ran: the registry entry of the object it named is reread from sqlite_master (created,
        replaced or gone), its trust re-checked; a definition the parser does not name (ALTER, ANALYZE, a virtual table's
        shadow objects) rebuilds the whole registry."""
        st = _STATE[self]
        m = _NAMED_DDL_RE.match(stmt or "")
        if m is None or (m.group(1) or "").lower() == "virtual":
            self._refresh_schema(force=True); return
        parts = _split_qualified(m.group(3)); name = parts[-1]
        if (m.group(1) or "").lower() in ("temp", "temporary") or (len(parts) > 1 and parts[0] == "temp"):
            st.temp_used = True
        native = _NATIVE[self]; st.internal = True; text_factory = native.text_factory
        if text_factory is not str:
            native.text_factory = str
        try:
            cur = _bookkeeping(self)
            rows = cur.execute("SELECT type, sql FROM sqlite_master WHERE lower(name)=?", (name,)).fetchall()
            if st.temp_used:
                rows += cur.execute("SELECT type, sql FROM sqlite_temp_master WHERE lower(name)=?", (name,)).fetchall()
            kinds = {kind for kind, _ in rows}
            if not rows:
                st.objects.pop(name, None)
            elif len(kinds) > 1:
                st.objects[name] = ("ambiguous", None)
            else:
                kind, sql = rows[0]; st.objects[name] = (kind, _sql_hash(sql) if kind in ("trigger", "view") else None)
            if name in st.trusted and st.objects.get(name) != ("trigger", st.trusted[name]):
                del st.trusted[name]
            st.schema_version = cur.execute("PRAGMA main.schema_version").fetchall()[0][0]
            if st.temp_used:
                st.schema_version = (st.schema_version, cur.execute("PRAGMA temp.schema_version").fetchall()[0][0])
            st.schema_dirty = False
            if m.group(2).lower() == "table":
                st.fk_pairs = None
        finally:
            if text_factory is not str:
                native.text_factory = text_factory
            st.internal = False

    def _reset_markers(self):
        """The per-statement markers: the statement's own write target, the foreign-key children whose key it read, the
        cascade chain and the definition marker. Cleared when a statement starts running and whenever a refusal is pending."""
        st = _STATE[self]
        st.stmt_target = None; st.cascade = set(); st.fk_reads = set(); st.ddl_table = None

    def _reset_statement(self):
        """The markers and the pending refusal: cleared before and after every guarded execution (a finally block, whatever
        the error) and on native access."""
        st = _STATE[self]
        self._reset_markers(); st.last_denial = None

    def _check_definition(self, stmt: str):
        """Before a view or trigger definition runs: its name must be free, or the statement is refused (both creation
        orders, both schemas, the trusted set included)."""
        st = _STATE[self]
        m = _DEFINITION_RE.match(stmt)
        if not m:
            return
        parts = _split_qualified(m.group(3)); kind, name = m.group(2).lower(), parts[-1]          # the schema qualifier apart from the name
        if (m.group(1) or "").lower() in ("temp", "temporary") or (len(parts) > 1 and parts[0] == "temp"):
            if not st.guard_inspection:
                raise BoundaryRefusal(f"a temp {kind} is defined inside the inspection context only (fingerprint {fingerprint(stmt)})")
            st.temp_used = True                                                            # the temp catalogue joins the registry before the check
        self._refresh_schema(force=True)
        held = st.objects.get(name)
        if held is not None and held[0] != kind or name in st.trusted and kind == "view":
            raise BoundaryRefusal(f"the name {name} is already held by a {held[0] if held else 'trusted trigger'}; a {kind} of that name is refused (fingerprint {fingerprint(stmt)})")

    # -- the authorizer: one explicit decision per action code
    def _authorize(self, action, arg1, arg2, dbname, source):
        st = _STATE[self]
        if st.internal:
            return _OK
        if st.last_denial is not None:                                           # a refusal is pending: nothing of that preparation carries over, and
            self._reset_markers(); return _DENY                                     # everything prepared until a guarded execution clears it stays refused
        entry = DECISION_TABLE.get(action)
        if entry is None:
            return self._refuse(f"action code {action} is not in the decision table", derived=False)
        name, decision, _rule = entry
        if decision == ALLOWED:
            if action == sqlite3.SQLITE_SELECT:
                st.ddl_table = None
            return _OK
        if decision == REFUSED:
            return self._refuse(f"{name} is refused", derived=False)
        if action in _DEFINITION_CODES:
            if action in _SCHEMA_ONLY and not st.guard_schema and not (action == sqlite3.SQLITE_REINDEX and st.guard_inspection):
                return self._refuse(f"{name} outside the schema-application context", derived=False)
            if action in _INSPECTION_ONLY and not st.guard_inspection:
                return self._refuse(f"{name} outside the inspection context", derived=False)
            temp_definition = action in _TEMP_DEFINITIONS or (action in _MAIN_VIEW_TRIGGER and dbname is not None and dbname.lower() == "temp")   # SQLite reports a temp-qualified view or trigger under the main action code with dbname "temp"
            if temp_definition and not (st.guard_inspection and st.guard_schema):
                return self._refuse(f"{name} on the temp schema outside the inspection and schema-application contexts", derived=False)
            st.schema_dirty = True
            if action in _INSPECTION_ONLY or temp_definition:
                st.temp_used = True
            target = (arg1 or "").lower(); held = st.objects.get(target)
            if action in _TRIGGER_CODES:
                if held is not None and held[0] != "trigger":
                    return self._refuse(f"{name}: the name {arg1} is held by a {held[0]}", derived=False)
                st.pending_triggers.append(target); st.ddl_table = (arg2 or "").lower()
            elif action in _VIEW_CODES:
                if target in st.trusted or (held is not None and held[0] != "view"):
                    return self._refuse(f"{name}: the name {arg1} is held by a {held[0] if held else 'trusted trigger'}", derived=False)
                st.ddl_table = None
            elif action in _DROP_TRIGGER_CODES:
                st.trusted.pop(target, None)
            elif action in (sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_ALTER_TABLE):
                st.ddl_table = (arg2 or "").lower()
            else:
                st.ddl_table = target
            return _OK
        if action == sqlite3.SQLITE_PRAGMA:
            return _OK if (arg1 or "").lower() in ALLOWED_PRAGMAS else self._refuse(f"pragma {arg1} is not listed", derived=False)
        if action == sqlite3.SQLITE_FUNCTION:
            return _OK if (arg2 or "").lower() != "load_extension" else self._refuse("load_extension is refused", derived=False)
        # the data codes
        table = (arg1 or "").lower(); cols = (_DERIVED or derived_columns()).get(table)
        if dbname is not None and dbname != "main" and dbname.lower() != "main" and not st.guard_inspection and not table.startswith("sqlite_"):
            return self._refuse(f"{name} on a {dbname} object outside the inspection context: {arg1}", derived=False)   # SQLite's own catalogues (sqlite_temp_master) are read by definitions
        if action == sqlite3.SQLITE_READ:
            if st.stmt_target is not None and st.stmt_target != table and source is None and st.in_run:
                fk_parent = (st.fk_pairs or {}).get((table, (arg2 or "").lower()))
                if fk_parent is not None and fk_parent in ({st.stmt_target} | st.cascade):
                    st.fk_reads.add(table)                                     # a declared foreign-key column read by a write on its parent, inside a guarded execution
                    return _OK
            if not cols:
                return _OK
        elif source is None and st.stmt_target is None:
            st.stmt_target = table                                             # the statement's own target; later top-level writes may be its foreign-key actions
        if not cols:
            return _OK
        kind, source_hash = st.objects.get((source or "").lower(), (None, None))
        trusted_trigger = bool(source) and kind == "trigger" and st.trusted.get(source.lower()) == source_hash
        if action == sqlite3.SQLITE_READ:
            col = (arg2 or "").lower()
            if not (cols == ("*",) or col in cols):
                return _OK
            if trusted_trigger or st.guard_reads or st.guard_derive or st.guard_inspection or (st.guard_schema and st.ddl_table == table):
                return _OK
            return self._refuse(f"derived read outside the accessor's reads context: {arg1}.{arg2}")
        if source is None and st.in_run and st.stmt_target != table and table in st.fk_reads:
            st.cascade.add(table); return _OK                                  # a declared foreign-key action of the statement's own target: its key was read first, in this guarded preparation
        derived_write = cols == ("*",) or (action == sqlite3.SQLITE_UPDATE and (arg2 or "").lower() in cols)
        if not derived_write or st.guard_derive:
            return _OK                                                            # a trusted trigger body may read derived state; it writes it only inside the derive step
        what = {sqlite3.SQLITE_UPDATE: "update", sqlite3.SQLITE_INSERT: "insert into", sqlite3.SQLITE_DELETE: "delete from"}[action]
        return self._refuse(f"derived write outside the derive step: {what} {arg1}" + (f".{arg2}" if arg2 else ""))

    def _refuse(self, problem: str, derived: bool = True):
        """The refusal is final (the pending denial stored, the return code chosen) before any observer hears of it."""
        st = _STATE[self]
        if st.guard_mode == "log":
            LOG.warning("sqlguard: %s (fingerprint %s)", problem, fingerprint(st.last_sql))
            code = _OK
        else:
            st.last_denial = (problem, derived); code = _DENY
        if st.observers:
            self._notify(DecisionEvent(code == _OK, None, None, None, problem, derived, logged=code == _OK))
        return code

    def _notify(self, event):
        """A decision event to the observers: a frozen copy of the facts, after the decision; a return value is ignored and
        an exception is logged, so an observer can change nothing and skip nothing."""
        st = _STATE[self]
        for cb in tuple(st.observers):
            try:
                cb(self, event)
            except Exception as e:                                               # noqa: BLE001 (an observer never changes a decision)
                LOG.warning("sqlguard: observer raised %s during a decision event; ignored", type(e).__name__)

    def _second_layer(self, sql: str):
        """The tokeniser's check of write statements, for what the authorizer cannot see: the column list of an INSERT
        (SQLite reports an INSERT without its columns) and a derived table read by a subquery of a write (SQLite reports
        it like the foreign-key check that a write on another table performs). Views are resolved by SQLite itself."""
        st = _STATE[self]
        low = sql.lower()
        if st.guard_derive or not any(w in low for w in _WRITE_WORDS):
            return
        for stmt in _statements(sql) if ";" in sql else (sql,):
            if st.guard_schema and _DDL_RE.match(stmt):
                continue                                                          # a definition inside schema application: its body is authorised by SQLite when it runs
            found = classify(stmt)
            problems = [f"insert into {t}.{c}" for t, c in found["writes"] if derived_columns().get(t) != ("*",)]
            if not (st.guard_reads or st.guard_inspection):
                problems += [f"read of {t}.{c} inside a write" for t, c in found["reads"]]
            for problem in problems:
                if self._refuse(f"derived access outside its context: {problem}") == _DENY:
                    st.last_denial = None
                    raise DerivedAccessViolation(f"derived access outside its context: {problem} (fingerprint {fingerprint(sql)})")

    def _run(self, method, sql, *args):
        """Every guarded execution: fresh per-statement state, the schema snapshot current, the tokeniser's second layer,
        then SQLite with the authorizer; the state is cleared again in the finally block whatever the outcome. The
        statement text is reduced to a plain str first, so a str subclass answers nothing on its own."""
        st = _STATE[self]
        if type(sql) is not str and isinstance(sql, str):
            sql = str.__str__(sql)
        st.last_sql = sql; self._reset_statement()
        self._refresh_schema()
        self._second_layer(sql)
        st.in_run = True
        try:
            method(sql, *args)
        except sqlite3.DatabaseError as exc:
            denial, st.last_denial = st.last_denial, None
            if denial and any(m in str(exc) for m in _NOT_AUTHORIZED):
                problem, derived = denial
                raise (DerivedAccessViolation if derived else BoundaryRefusal)(f"{problem} (fingerprint {fingerprint(sql)})") from exc
            raise
        finally:
            st.in_run = False; self._reset_statement()                       # a refused or failed preparation leaves no state behind
            if st.pending_triggers:
                _register_pending_names(self)
            if st.schema_dirty:
                self._update_registry(sql)                                         # the object registry follows every definition at once

    # -- statements
    def cursor(self, factory=None):
        if self not in _STATE:
            raise BoundaryRefusal("a handle that was never constructed (a bare __new__) is refused")
        if factory is not None:
            raise TypeError("a guarded handle issues GuardedCursor only; raw cursor factories are refused")
        return _issue_cursor(self)

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        """Outside the schema-application context a script holds no definition (refused before anything runs); inside it
        the script runs statement by statement through the guarded path, each definition's name checked first and the
        object registry refreshed after it, with executescript's own transaction shape (a pending transaction is
        committed first, the script leaves none open)."""
        st = _STATE[self]
        statements = _statements(sql_script)
        if not st.guard_schema:
            if any(_DDL_RE.match(st) for st in statements):
                raise BoundaryRefusal(f"a script with a definition outside the schema-application context is refused (fingerprint {fingerprint(sql_script)})")
            return self.cursor().executescript(sql_script)
        native = _NATIVE[self]
        if native.in_transaction:
            native.commit()
        cur = self.cursor()
        for st in statements:
            self._check_definition(st)
            cur.execute(st)
        if native.in_transaction:
            native.commit()
        return cur

    # -- transactions and lifecycle
    def commit(self):
        st = _STATE[self]
        out = _NATIVE[self].commit(); st.commits += 1
        for cb in tuple(st.observers):                                        # a lifecycle event, after the native commit; an observer may raise here (a test's crash injection)
            cb(self, CommitEvent(st.commits))
        return out

    def rollback(self):
        return _NATIVE[self].rollback()

    def close(self):
        """Close the native connection after finalising every cursor this handle issued and its bookkeeping cursor: a
        cursor object still referenced elsewhere would otherwise keep a prepared statement, and SQLite would keep the
        connection half-open with its descriptors and locks until that object is collected."""
        for cur, owner in list(_OWNER.items()):
            if owner is self:
                native_cursor = _NATIVE.get(cur)
                if native_cursor is not None:
                    try:
                        native_cursor.close()
                    except sqlite3.ProgrammingError:
                        pass
        book = _BOOK.pop(self, None)
        if book is not None:
            try:
                book.close()
            except sqlite3.ProgrammingError:
                pass
        native = _NATIVE.get(self)
        if native is None:
            return None                                                          # never constructed: nothing to close
        return native.close()                                                    # sqlite3 closes once; a second close is a no-op

    def __enter__(self):
        _NATIVE[self].__enter__(); return self

    def __exit__(self, *a):
        return _NATIVE[self].__exit__(*a)

    @property
    def in_transaction(self):
        return _NATIVE[self].in_transaction

    @property
    def total_changes(self):
        return _NATIVE[self].total_changes

    @property
    def isolation_level(self):
        return _NATIVE[self].isolation_level

    @isolation_level.setter
    def isolation_level(self, value):
        _NATIVE[self].isolation_level = value

    @property
    def row_factory(self):
        return _NATIVE[self].row_factory

    @row_factory.setter
    def row_factory(self, factory):
        if factory is not None and factory is not sqlite3.Row and factory is not dict_factory:                    # identity, never equality
            raise TypeError("row_factory is limited to None, sqlite3.Row and sqlguard.dict_factory on a GuardedConnection; a callable would receive the native cursor")
        _NATIVE[self].row_factory = factory

    @property
    def text_factory(self):
        return _NATIVE[self].text_factory

    @text_factory.setter
    def text_factory(self, factory):
        if factory is not str and factory is not bytes:                                                            # identity, never equality
            raise TypeError("text_factory is limited to str and bytes on a GuardedConnection")
        _NATIVE[self].text_factory = factory

    # -- utilities the application and its tests use (no dump: backups go through backup())
    def backup(self, target, **kwargs):
        """The online backup into a guarded target. Trust transfers only after the target re-verifies: its registry is
        rebuilt from the restored store's sqlite_master, and a source-trusted name is trusted on the target only when the
        restored row is a trigger whose exact text hashes to the same value; nothing else is trusted."""
        st = _STATE[self]
        require_guarded(target)
        out = _NATIVE[self].backup(_NATIVE[target], **kwargs)
        _STATE[target].temp_used = _STATE[target].temp_used or st.temp_used; _STATE[target].schema_dirty = True; target._refresh_schema()
        _STATE[target].trusted = {n: h for n, h in st.trusted.items() if _STATE[target].objects.get(n) == ("trigger", h)}   # re-verified against the restored store's catalogues
        return out

    def set_trace_callback(self, cb):
        st = _STATE[self]
        st.trace = cb

    def create_function(self, *a, **k):
        return _NATIVE[self].create_function(*a, **k)

    def interrupt(self):
        return _NATIVE[self].interrupt()


_OWNER = weakref.WeakKeyDictionary()            # cursor -> its handle; module-private, never an attribute a caller could assign


def _issue_cursor(handle) -> "GuardedCursor":
    """The one way a guarded cursor comes to be: the handle's cursor() asks for it, the native cursor is made here from the
    handle's own native connection (its row and text factories are the handle's), and the owner and native records go into
    the module-private maps. The class itself refuses construction and initialisation."""
    require_guarded(handle)
    cur = object.__new__(GuardedCursor)
    _OWNER[cur] = handle; _NATIVE[cur] = _NATIVE[handle].cursor()
    return cur


def _owner(cur) -> GuardedConnection:
    handle = _OWNER.get(cur)
    if handle is None:
        raise BoundaryRefusal("a cursor that no handle issued is refused")
    return handle


class GuardedCursor:
    """A cursor over a private native cursor, issued by its handle's cursor() alone: execution goes through the handle's
    boundary (the handle is read from module-private storage, never from an attribute), rows come back as rows, and
    iteration yields the guarded cursor itself. GuardedCursor(...) and cursor.__init__(...) are refused: no supplied cursor,
    no factory, no reinitialisation."""
    __slots__ = ("__weakref__",)                                             # weak-reference-only: no owner, no state, no __dict__

    def __new__(cls, *args, **kwargs):
        raise TypeError("a GuardedCursor is issued by its handle's cursor() only; construction with any argument is refused")

    def __init__(self, *args, **kwargs):
        raise TypeError("a GuardedCursor is issued by its handle's cursor() only; initialisation with a supplied object is refused")

    def execute(self, sql, parameters=()):
        _owner(self)._run(_NATIVE[self].execute, sql, parameters); return self

    def executemany(self, sql, seq_of_parameters):
        _owner(self)._run(_NATIVE[self].executemany, sql, seq_of_parameters); return self

    def executescript(self, sql_script):
        handle = _owner(self)
        if _STATE[handle].guard_schema or any(_DDL_RE.match(st) for st in _statements(sql_script)):
            return handle.executescript(sql_script)                            # the handle's statement-by-statement path and its refusal rule
        handle._run(_NATIVE[self].executescript, sql_script); return self

    def fetchone(self):
        return _NATIVE[self].fetchone()

    def fetchall(self):
        return _NATIVE[self].fetchall()

    def fetchmany(self, size=None):
        return _NATIVE[self].fetchmany(size) if size is not None else _NATIVE[self].fetchmany()

    def close(self):
        return _NATIVE[self].close()

    def __iter__(self):
        return self

    def __next__(self):
        return next(_NATIVE[self])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    @property
    def description(self):
        return _NATIVE[self].description

    @property
    def rowcount(self):
        return _NATIVE[self].rowcount

    @property
    def lastrowid(self):
        return _NATIVE[self].lastrowid

    @property
    def arraysize(self):
        return _NATIVE[self].arraysize

    @property
    def connection(self):
        return _owner(self)


def open_connection(path, mode: str = "raise", production: bool = False, **kwargs) -> GuardedConnection:
    """The one place in app/ that opens a SQLite store: exactly a GuardedConnection over exactly a sqlite3.Connection with
    the statement cache off; mode is the handle's enforcement mode; production marks the runner's and the CLI's handles
    (no suite fixture is ever applied on them); the keyword arguments are the allow-listed sqlite3.connect options."""
    return GuardedConnection(path, mode, production, **kwargs)


def observe(conn, callback) -> None:
    """A test's observer: callback(conn, event) receives DecisionEvent (after the decision is final; a frozen copy; return
    value ignored, exceptions logged) and CommitEvent (after the native commit; an exception propagates, the suites' crash
    injection). It can change no decision and skip no derivation."""
    require_guarded(conn)
    if not callable(callback):
        raise TypeError("an observer is a callable")
    _STATE[conn].observers.append(callback)


def unobserve(conn, callback) -> None:
    require_guarded(conn)
    _STATE[conn].observers = [cb for cb in _STATE[conn].observers if cb is not callback]


def require_guarded(conn) -> None:
    """Application entry points accept only a guarded handle, and only one that was constructed (a bare __new__ owns nothing)."""
    if type(conn) is not GuardedConnection:
        raise TypeError(f"application code needs a GuardedConnection (app.repo.connect or app.core.sqlguard.open_connection), not {type(conn).__name__}")
    if conn not in _STATE:
        raise BoundaryRefusal("a handle that was never constructed (a bare __new__) is refused")


@contextlib.contextmanager
def reads(conn):
    """The accessor's reads context: derived reads allowed on this handle while open."""
    require_guarded(conn)
    _STATE[conn].guard_reads += 1
    try:
        yield conn
    finally:
        _STATE[conn].guard_reads -= 1


@contextlib.contextmanager
def derive_step(conn):
    """The derive-step context: derived writes (and reads) allowed on this handle while open."""
    require_guarded(conn)
    _STATE[conn].guard_derive += 1
    try:
        yield conn
    finally:
        _STATE[conn].guard_derive -= 1


@contextlib.contextmanager
def inspection(conn):
    """The inspection context for tests and scripts: derived reads, temp and attached objects allowed, derived writes still refused. No module under app/ uses it."""
    require_guarded(conn)
    _STATE[conn].guard_inspection += 1
    try:
        yield conn
    finally:
        _STATE[conn].guard_inspection -= 1


# ---------------------------------------------------------------- schema application: shipped files under the manifest only
_ROOT = pathlib.Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _ROOT / "schemas" / "MANIFEST.json"
_SHIPPED_DIRS = ("schemas/", "tests/fixtures/sql/")
_FIXTURE_PREFIX = "tests/fixtures/sql/"
TEST_FIXTURES_VAR = "NMB_TEST_FIXTURES"


def _load_manifest() -> types.MappingProxyType:
    """schemas/MANIFEST.json, read once at import and frozen: relative path -> sha256 of the file's bytes."""
    data = json.loads(_MANIFEST_PATH.read_text()) if _MANIFEST_PATH.exists() else {"files": {}}
    return types.MappingProxyType({str(k): str(v) for k, v in data.get("files", {}).items()})


MANIFEST = _load_manifest()


def manifest_entries(root: pathlib.Path | None = None) -> dict[str, str]:
    """The files the manifest must list, with their current hashes (regenerating the manifest writes them; a check compares)."""
    root = root or _ROOT; out = {}
    for rel in ("schemas/schema.sql", "schemas/checkpointer.sql"):
        out[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    for d in ("schemas/migrations", "tests/fixtures/sql"):
        for f in sorted((root / d).glob("*.sql")):
            out[f"{d}/{f.name}"] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def _plain_relative_path(relative_path) -> str:
    """Exactly a str (no subclass, whose methods could answer differently), in its normalised form already (no '.', '..',
    '//' or trailing slash), relative, forward slashes: anything else is refused before any policy check."""
    if type(relative_path) is not str:
        raise BoundaryRefusal("a shipped file is named by a plain str relative path")
    if not relative_path or relative_path.startswith("/") or "\\" in relative_path or posixpath.normpath(relative_path) != relative_path:
        raise BoundaryRefusal(f"a shipped path is relative and already normalised; refused: {relative_path!r}")
    return str.__str__(relative_path)


def _shipped_file(relative_path: str) -> tuple[pathlib.Path, bytes]:
    """A listed file by its relative path: inside its expected directory, no '.' or '..' component, no symlink in any
    component under the repository root, a regular file whose bytes hash to the manifest entry. Anything else is refused."""
    relative_path = _plain_relative_path(relative_path)
    parts = relative_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BoundaryRefusal(f"a shipped path has no '.' or '..' component: {relative_path} is refused")
    if not relative_path.startswith(_SHIPPED_DIRS):
        raise BoundaryRefusal(f"{relative_path} is outside the shipped directories")
    expected = MANIFEST.get(relative_path)
    if expected is None:
        raise BoundaryRefusal(f"{relative_path} is not in schemas/MANIFEST.json")
    root = _ROOT.resolve(); full = root / relative_path
    for i in range(1, len(parts) + 1):
        if os.path.islink(root.joinpath(*parts[:i])):
            raise BoundaryRefusal(f"a symlink in the path of {relative_path} is refused")
    if not full.is_file() or full.resolve() != full:
        raise BoundaryRefusal(f"{relative_path} is not a regular file at its listed place")
    data = full.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise BoundaryRefusal(f"the content of {relative_path} differs from its manifest entry; refused")
    return full, data


@contextlib.contextmanager
def _schema_application(conn, script: str | None = None, temp: bool = False):
    """The schema-application context, private to this module: apply_shipped_schema alone opens it. Definitions allowed,
    scripts statement by statement, the triggers created here trusted (with the script, triggers it defines that already
    exist with exactly that text are trusted too); temp=True opens the inspection context with it for temp objects."""
    require_guarded(conn); st = _STATE[conn]
    st.guard_schema += 1; st.guard_inspection += 1 if temp else 0
    try:
        yield conn
        if script is not None:
            _trust_script_triggers(conn, script)
    finally:
        st.guard_schema -= 1; st.guard_inspection -= 1 if temp else 0


def apply_shipped_schema(conn, relative_path: str, temp: bool = False) -> str:
    """The one way into the schema-application context: a file the repository ships, named by its path relative to the
    repository root and listed with its hash in schemas/MANIFEST.json (schemas/schema.sql, schemas/migrations/*.sql,
    schemas/checkpointer.sql, tests/fixtures/sql/*.sql). A suite fixture (tests/fixtures/sql/) is applied only while
    NMB_TEST_FIXTURES is "1" and never on a production handle. The script runs statement by statement through the guarded
    path; temp=True opens the inspection context as well (temp views and triggers). Returns the relative path applied."""
    require_guarded(conn)
    relative_path = _plain_relative_path(relative_path)
    if relative_path.startswith(_FIXTURE_PREFIX):
        if os.environ.get(TEST_FIXTURES_VAR) != "1":
            raise BoundaryRefusal(f"a SQL fixture is applied only under {TEST_FIXTURES_VAR}=1 (the Makefile test target sets it)")
        if _STATE[conn].production:
            raise BoundaryRefusal("a production handle applies no suite fixture")
    _full, data = _shipped_file(relative_path)
    script = data.decode("utf-8")
    with _schema_application(conn, script, temp=temp):
        conn.executescript(script)
    return relative_path


_INSPECTED = weakref.WeakKeyDictionary()        # inspector -> its handle; module-private, no attribute to delete or assign


class Inspector:
    """A test's inspection handle over a guarded connection: each execute runs inside the inspection context; everything
    else forwards to the handle. Passing it to application code is a type error. Constructed exactly once over one handle,
    which lives in module-private storage behind a read-only property: a second __init__, any assignment and any deletion
    (del, delattr, object.__delattr__) are refused."""
    __slots__ = ("__weakref__",)

    def __init__(self, conn):
        if self in _INSPECTED:
            raise BoundaryRefusal("an Inspector is constructed exactly once; reinitialising it is refused")
        require_guarded(conn); _INSPECTED[self] = conn

    @property
    def conn(self):
        handle = _INSPECTED.get(self)
        if handle is None:
            raise BoundaryRefusal("an Inspector that was never constructed is refused")
        return handle

    def __setattr__(self, name, value):
        raise AttributeError("an Inspector's handle is fixed at construction")

    def __delattr__(self, name):
        raise AttributeError("an Inspector's handle is fixed at construction; deleting it is refused")

    def execute(self, sql, parameters=()):
        with inspection(self.conn):
            return self.conn.execute(sql, parameters)
    def executemany(self, sql, seq):
        with inspection(self.conn):
            return self.conn.executemany(sql, seq)
    def executescript(self, script):
        with inspection(self.conn):
            return self.conn.executescript(script)
    def __getattr__(self, name):
        if name == "conn":
            raise AttributeError(name)
        return getattr(self.conn, name)
    def __enter__(self):
        return self.conn.__enter__()
    def __exit__(self, *a):
        return self.conn.__exit__(*a)


def inspect(conn):
    """The inspection handle for a guarded connection; a raw sqlite3 connection (a documented inspection-only handle in a
    test) is returned as it is, since nothing enforces it and nothing in app/ accepts it."""
    if isinstance(conn, Inspector) or not isinstance(conn, GuardedConnection):
        return conn
    return Inspector(conn)
