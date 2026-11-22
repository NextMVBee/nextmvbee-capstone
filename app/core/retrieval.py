"""Local retrieval: FTS5 over source passages behind one interface; optional embeddings only if the retrieval experiment (a measurement of the relevant passages FTS5 misses) justifies them.
Rank fusion is deterministic for fixed inputs. The experiment's scoring separates external acquisition, local retrieval and extraction."""
import sqlite3
from typing import Protocol
from app.core.sqlguard import require_guarded


CHUNK_SIZE, CHUNK_OVERLAP = 800, 100


def chunk(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i + size]); i += max(1, size - overlap)
    return out


def chunk_ranges(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, int]]:
    """(range_start, range_end) of every chunk in the stored text; the unit a read with query returns and a batch splits on."""
    out, i = [], 0
    while i < len(text):
        out.append((i, min(i + size, len(text)))); i += max(1, size - overlap)
    return out


def index_source(conn: sqlite3.Connection, project_id: str, source_id: str, text: str) -> int:
    """Replace the source snapshot in one transaction: old passages and their FTS rows go, the new chunks come in, each with
    its character range in the stored text. Runs inside a caller's open transaction when there is one (a refresh writes the
    sources row, the version row and the passages together)."""
    require_guarded(conn)
    out = {"n": 0}
    def body():
        ranges = chunk_ranges(text) if text else []
        is_canonical = canonical_source(conn, project_id, source_id) == source_id   # the canonical and alias reads sit inside the write's transaction, so a refresh on another connection waits or lands after
        if not is_canonical:                                                      # identical content is indexed once; an alias row keeps its own provenance and no passages
            ranges = []                                                           # no passages move between rows; a row promoted to canonical by a refresh of its former canonical is indexed from its own stored text (index_status reads it unindexed until reindex_project does)
        conn.execute("DELETE FROM passages_fts WHERE passage_id IN (SELECT id FROM passages WHERE source_id=?)", (source_id,))
        conn.execute("DELETE FROM passages WHERE source_id=?", (source_id,))
        for k, (a, b) in enumerate(ranges):
            pid = f"{source_id}#{k}"
            conn.execute("INSERT INTO passages (id, project_id, source_id, ordinal, text, range_start, range_end) VALUES (?,?,?,?,?,?,?)", (pid, project_id, source_id, k, text[a:b], a, b))
            conn.execute("INSERT INTO passages_fts (passage_id, text) VALUES (?,?)", (pid, text[a:b]))
        if is_canonical:
            for alias in aliases_of(conn, project_id, source_id):                 # an alias row of this content, legacy rows included, holds no passages
                conn.execute("DELETE FROM passages_fts WHERE passage_id IN (SELECT id FROM passages WHERE source_id=?)", (alias,))
                conn.execute("DELETE FROM passages WHERE source_id=?", (alias,))
        out["n"] = len(ranges)
    if conn.in_transaction:
        body()
    else:
        conn.execute("BEGIN IMMEDIATE")                                            # the immediate-transaction pattern (coverage._tx_immediate): the reads and the writes of one index under one lock
        try:
            body(); conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK"); raise
    return out["n"]


def delete_source(conn: sqlite3.Connection, source_id: str) -> None:
    """Remove a source with its passages and FTS rows (the FTS table has no foreign key)."""
    require_guarded(conn)
    with conn:
        conn.execute("DELETE FROM passages_fts WHERE passage_id IN (SELECT id FROM passages WHERE source_id=?)", (source_id,))
        conn.execute("DELETE FROM passages WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))


def passages_for(conn: sqlite3.Connection, project_id: str, source_id: str, range_start: int, range_end: int) -> list[dict]:
    """The indexed passages of a source that overlap a character range (a claim's quote range, an extraction batch): the
    unit a citation points at and a stored read returns."""
    require_guarded(conn)
    rows = conn.execute("SELECT id, ordinal, range_start, range_end, text FROM passages WHERE project_id=? AND source_id=? AND range_end>? AND range_start<? ORDER BY ordinal",
                        (project_id, source_id, range_start, range_end)).fetchall()
    return [dict(zip(("id", "ordinal", "range_start", "range_end", "text"), r)) for r in rows]


def index_status(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Per stored source: how many passages the index holds and how many its current version needs (chunk_ranges of
    chars_total). A source is indexed when both agree; a mismatch names a source to re-index."""
    require_guarded(conn)
    out = []
    rows = conn.execute("SELECT s.id, s.content_hash, COALESCE(v.chars_total, 0) FROM sources s LEFT JOIN source_versions v ON v.source_id=s.id AND v.content_hash=s.content_hash WHERE s.project_id=? ORDER BY s.id", (project_id,)).fetchall()
    have_of = {sid: int(conn.execute("SELECT COUNT(*) FROM passages WHERE source_id=? AND project_id=?", (sid, project_id)).fetchone()[0]) for sid, _, _ in rows}
    need_of = {sid: (len(chunk_ranges("x" * int(chars))) if chars else 0) for sid, _, chars in rows}
    for sid, hash_, chars in rows:
        canon = canonical_source(conn, project_id, sid)
        if canon != sid:                                                              # an alias counts as indexed through its canonical row, and holds no passages of its own
            out.append({"source_id": sid, "source_version": hash_, "passages": have_of[sid], "expected": 0, "indexed": have_of[sid] == 0 and have_of[canon] == need_of[canon], "alias_of": canon})
            continue
        out.append({"source_id": sid, "source_version": hash_, "passages": have_of[sid], "expected": need_of[sid], "indexed": have_of[sid] == need_of[sid]})
    return out


def reindex_project(conn: sqlite3.Connection, project_id: str, data_dir=None, only_missing: bool = True) -> dict:
    """Index every stored source of a project into passages from its current version's text: sources
    whose passage count differs from what their text needs are re-indexed; with only_missing=False every source is.
    Bytes that fail their version check are skipped and named, never indexed under the wrong version."""
    require_guarded(conn)
    from app.adapters.research import source_text, SourceVersionMismatch
    from app.core.coverage import _tx_immediate                                    # the immediate-transaction helper: joins an open transaction, opens BEGIN IMMEDIATE otherwise
    done, skipped, cleared = [], [], []
    for st in index_status(conn, project_id):
        if st.get("alias_of"):                                                        # an alias is indexed through its canonical; its own passages, if a legacy row has any, go
            if st["passages"]:
                index_source(conn, project_id, st["source_id"], ""); cleared.append({"source_id": st["source_id"], "alias_of": st["alias_of"]})
            continue
        if only_missing and st["indexed"]:
            continue
        def one(source_id=st["source_id"]):
            """The version-verified text read and the index write of one source under one BEGIN IMMEDIATE; index_source joins it. A refresh on another connection is refused while it is open or is read inside it."""
            try:
                found = source_text(conn, project_id, source_id, data_dir)
            except SourceVersionMismatch as e:
                return ("skipped", {"source_id": source_id, "reason": str(e)[:160]})
            if found is None:
                return ("skipped", {"source_id": source_id, "reason": "no stored text"})
            return ("indexed", {"source_id": source_id, "passages": index_source(conn, project_id, source_id, found[1])})
        kind, rec = _tx_immediate(conn, one)
        (done if kind == "indexed" else skipped).append(rec)
    return {"indexed": done, "skipped": skipped, "aliases_cleared": cleared, "sources": len(index_status(conn, project_id))}


class Retriever(Protocol):
    def retrieve(self, project_id: str, query: str, k: int = 8) -> list[tuple[str, float]]: ...


class FTS5Retriever:
    def __init__(self, conn: sqlite3.Connection):
        require_guarded(conn)
        self.conn = conn

    def retrieve(self, project_id: str, query: str, k: int = 8, source_id: str | None = None) -> list[tuple[str, float]]:
        """Best passages of the project, or of one source when source_id is given (read with query)."""
        q = " OR ".join(w for w in "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in query).split() if len(w) > 2)
        if not q:
            return []                                                   # nothing searchable in the query: no matches, never a raw FTS syntax error
        sql = ("SELECT f.passage_id, bm25(passages_fts) FROM passages_fts f JOIN passages p ON p.id=f.passage_id "
               "WHERE passages_fts MATCH ? AND p.project_id=?" + (" AND p.source_id=?" if source_id else "") + " ORDER BY bm25(passages_fts), f.passage_id LIMIT ?")
        rows = self.conn.execute(sql, (q, project_id, source_id, k) if source_id else (q, project_id, k)).fetchall()
        return [(pid, -float(score)) for pid, score in rows]           # higher is better

    def retrieve_rows(self, project_id: str, query: str, k: int = 8, source_id: str | None = None) -> list[dict]:
        """The same ranking with the passage text and ranges (what the evidence board and the retrieval experiment's harness show)."""
        out = []
        for pid, score in self.retrieve(project_id, query, k, source_id):
            r = self.conn.execute("SELECT source_id, ordinal, range_start, range_end, text FROM passages WHERE id=? AND project_id=?", (pid, project_id)).fetchone()
            if r:
                out.append({"passage_id": pid, "score": round(score, 4), "source_id": r[0], "ordinal": r[1], "range_start": r[2], "range_end": r[3], "text": r[4]})
        return out


class EmbeddingRetriever:
    """Placeholder: enabled only if the retrieval experiment shows FTS5 missing more than 10% of relevant passages."""
    def __init__(self, *a, **k):
        raise NotImplementedError("embeddings are not enabled; run E2 first (docs/experiments/E2.md)")


def rank_fusion(result_lists: list[list[tuple[str, float]]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal rank fusion. Deterministic: ties broken by passage id, ascending."""
    fused: dict[str, float] = {}
    for results in result_lists:
        for rank, (pid, _score) in enumerate(results, start=1):
            fused[pid] = fused.get(pid, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: (-round(x[1], 12), x[0]))


def miss_rate(retrieved: dict[str, list[str]], expected: dict[str, set[str]]) -> dict:
    """The retrieval experiment's local-retrieval score: a relevant passage is missed when it is absent from the retrieved list for its query.
    Denominator: every relevant passage of every fixture query."""
    total = sum(len(v) for v in expected.values()); missed = sum(len(exp - set(retrieved.get(q, []))) for q, exp in expected.items())
    return {"relevant": total, "missed": missed, "miss_rate": (missed / total) if total else 0.0, "threshold": 0.10, "embeddings_justified": total > 0 and missed / total > 0.10}


# ---------------------------------------------------------------- identical content, candidate lists and the order key
def canonical_source(conn: sqlite3.Connection, project_id: str, source_id: str) -> str:
    """The canonical row of a content hash within a project is the one with the lowest source id; every other row with the
    same hash is its alias (derived from (project_id, content_hash); no column). A source with no row is its own canonical."""
    require_guarded(conn)
    r = conn.execute("SELECT MIN(s2.id) FROM sources s1 JOIN sources s2 ON s2.project_id=s1.project_id AND s2.content_hash=s1.content_hash WHERE s1.id=? AND s1.project_id=?", (source_id, project_id)).fetchone()
    return r[0] if r and r[0] else source_id


def aliases_of(conn: sqlite3.Connection, project_id: str, source_id: str) -> list[str]:
    """The other rows of the project that share this source's content hash, in id order (the alias ids recorded in provenance)."""
    require_guarded(conn)
    return [r[0] for r in conn.execute("SELECT s2.id FROM sources s1 JOIN sources s2 ON s2.project_id=s1.project_id AND s2.content_hash=s1.content_hash AND s2.id<>s1.id WHERE s1.id=? AND s1.project_id=? ORDER BY s2.id", (source_id, project_id)).fetchall()]


def candidate_sources(conn: sqlite3.Connection, project_id: str, query: str, k: int, scan_limit: int, exclude: tuple = ()) -> dict:
    """The candidate list of one subject. The FTS5 ranking of passages for the subject's key facts (retrieve_rows)
    is scanned in rank order over at most scan_limit passages; the list is the distinct sources (canonical rows) of
    those passages in passage-rank order, cut at k distinct sources. mark: k_reached when the cut was reached, scan_limited
    when the scan limit stopped the scan while the ranking still held passages, ranking_exhausted when the ranking ran out.
    exclude: sources already admitted (an extension of the list takes the next distinct sources beyond them)."""
    require_guarded(conn)
    rows = FTS5Retriever(conn).retrieve_rows(project_id, query, k=int(scan_limit) + 1)
    more = len(rows) > int(scan_limit); rows = rows[:int(scan_limit)]
    seen, best, mark, inspected = [], {}, None, 0
    for rank, r in enumerate(rows, start=1):
        inspected = rank                                                               # the passages actually inspected, the loop's iterations (fewer than the rows fetched when k is reached early)
        src = canonical_source(conn, project_id, r["source_id"])
        if src in exclude:
            continue
        if src not in best:
            best[src] = rank; seen.append(src)
            if len(seen) >= int(k):
                mark = "k_reached"; break
    if mark is None:
        mark = "scan_limited" if (more and len(rows) == int(scan_limit)) else "ranking_exhausted"
    return {"sources": [(src, best[src]) for src in seen], "mark": mark, "scanned": len(rows), "inspected": inspected, "more": more}


def slice_around(text: str, start: int, end: int, margin: int = 1200) -> str:
    """The verification slice: the quote's range widened by exactly margin characters either side, clipped to the text: a
    qualification whose last character is the margin-th after the quote is inside, one character later it is cut."""
    a, b = int(start or 0), int(end or 0)
    return text[max(0, a - margin): min(len(text), b + margin)]


def order_key(priority: int, best_rank: int, source_id: str) -> tuple:
    """(the lowest priority class among the subjects whose list holds the source, the best passage rank it holds for a subject of that class, the source id), ascending."""
    return (int(priority), int(best_rank), source_id)


def base_set(candidates: list[dict]) -> list[dict]:
    """The union of the candidate lists with one order key per source. candidates: [{"subject_id", "priority", "sources": [(source_id, best_rank), ...]}].
    Returns [{"source_id", "key", "subjects": [(subject_id, priority, rank), ...]}] sorted by key."""
    by_src: dict[str, dict] = {}
    for c in candidates:
        for src, rank in c["sources"]:
            key = order_key(c["priority"], rank, src)
            d = by_src.setdefault(src, {"source_id": src, "key": key, "subjects": []})
            if key < d["key"]:
                d["key"] = key
            d["subjects"].append((c["subject_id"], int(c["priority"]), int(rank)))
    return sorted(by_src.values(), key=lambda d: d["key"])
