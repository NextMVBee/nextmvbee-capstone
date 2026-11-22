"""Exa research adapter: request builders (price pinned to the request actually sent) and the live client.

Policy: type deep for candidate discovery and auto otherwise, at most 10 results (no extra-result charge), no AI summaries, text included,
freshness by class from app/core/freshness.py (maxAgeHours and livecrawlTimeout).

Client: field names follow the Exa REST reference; the live client speaks to the documented REST endpoints over httpx with a finite
timeout and no retries (the exa-py SDK has no timeout control).

Pages: every page comes back as untrusted text with retrieved_at, cache_status and max_age_hours;
store_pages() writes the text to data/sources/<project>/ and a sources row through the repository; a refresh with new
content keeps the source id and starts a new source_version.

Cost: the provider's costDollars is kept on the adapter (last_cost_usd) for reconciliation; the ledger reserves and settles from the request.

Secrets: the key is passed in by the caller (config/.env), never logged.
"""
from __future__ import annotations
from app.core.sqlguard import require_guarded

import hashlib
import os
import pathlib
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from app.core.freshness import exa_content_options
from app.core.ledger import estimate_exa_search_usd, estimate_exa_contents_usd

ALLOWED_TYPES = ("auto", "fast", "deep", "lite")   # deep for candidate discovery (additionalQueries), auto for follow-ups; fast and lite stay allowed
EXA_BASE_URL = "https://api.exa.ai"
EXA_DEEP_USD = 0.012                                  # per deep request, ten results included (the provider's documentation)
HIGHLIGHT_CHARS = 2000


def build_search_body(query: str, freshness: str, num_results: int = 8, include_domains=None, start_published_date=None, search_type="auto",
                      additional_queries=None, highlights: bool | None = None) -> dict:
    if search_type not in ALLOWED_TYPES:
        raise ValueError(f"search type {search_type!r} is not one of {ALLOWED_TYPES}")
    body = {"query": query[:200], "type": search_type, "numResults": max(1, min(num_results, 10)), "contents": exa_content_options(freshness)}
    if highlights or (highlights is None and search_type.startswith("deep")):   # highlights ride on the discovery (deep) requests; a plain request keeps its shape
        body["contents"]["highlights"] = {"maxCharacters": HIGHLIGHT_CHARS}
    if additional_queries and search_type.startswith("deep"):           # the documented rule: additionalQueries works with the deep types only
        body["additionalQueries"] = [q[:200] for q in additional_queries if isinstance(q, str) and q.strip()][:10]
    if include_domains:
        body["includeDomains"] = list(include_domains)[:10]
    if start_published_date:
        body["startPublishedDate"] = start_published_date
    return body


def build_contents_body(urls: list[str], freshness: str) -> dict:
    return {"ids": list(urls), **exa_content_options(freshness)}


def estimate_body_usd(endpoint: str, body: dict) -> float:
    if endpoint == "search":
        base = estimate_exa_search_usd(body["numResults"], summaries=0)
        if str(body.get("type", "")).startswith("deep"):
            base = round(base - estimate_exa_search_usd(10) + EXA_DEEP_USD + max(0, body["numResults"] - 10) * 0.001, 6)
        return base
    types = sum(1 for k in ("text", "highlights", "summary") if body.get(k))
    return estimate_exa_contents_usd(len(body["ids"]), content_types=max(types, 1))


# ---------------------------------------------------------------- pages as untrusted text
def canonical_url(url: str) -> str:
    """Scheme and host lower-cased, fragment dropped, trailing slash on an empty path."""
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def cache_status_from(source: str | None, max_age_hours: int | None) -> str:
    """Map Exa's content status source to the schema's cache_status: the values the provider's documentation gives are cached and crawled
    (the /contents statuses), and a crawled copy is a live fetch. A cached copy returned when a live fetch was demanded (maxAgeHours 0) is a
    cache fallback; no status at all is unknown, never assumed live."""
    s = (source or "").lower()
    if "live" in s or "crawl" in s:
        return "livecrawl"
    if "cache" in s:
        return "cache_fallback" if max_age_hours == 0 else "cached"
    return "unknown"


OBJECT_TEXT = re.compile(r"\[object [A-Za-z]+\]")    # the text a script makes of an object it did not read ("[object Object]")


def author_name(value):
    """A page's author as the source's record keeps it (its publisher). A value that is no text, is empty, or is the text form of a
    programming object is not a name and is not stored: such a value once printed "[object Object]" as a source's publisher in the
    delivered documents."""
    if not isinstance(value, str) or not value.strip() or OBJECT_TEXT.fullmatch(value.strip()):
        return None
    return value


def page_record(result, freshness: str, max_age_hours: int | None, status_source: str | None, retrieved_at: datetime | None = None) -> dict:
    """One page as the application stores and shows it: untrusted text plus provenance. JSON-serialisable."""
    text = getattr(result, "text", None) or ""
    url = getattr(result, "url", "") or ""
    ts = (retrieved_at or datetime.now(timezone.utc)).isoformat()
    return {"url": url, "canonical_url": canonical_url(url) if url else "", "title": getattr(result, "title", None), "author": author_name(getattr(result, "author", None)),
            "published_date": getattr(result, "published_date", None), "text": text, "content_hash": content_hash(text), "retrieved_at": ts,
            "freshness": freshness, "max_age_hours": max_age_hours, "cache_status": cache_status_from(status_source, max_age_hours), "untrusted": True}


class ExaHTTPError(Exception):
    """Exa answered with an HTTP error before any content: 402 stops the run, other statuses fail the call (nothing billed)."""
    def __init__(self, status: int, message: str):
        super().__init__(f"Exa HTTP {status}: {message[:200]}"); self.status = status


class ExaTimeout(Exception):
    """No answer within the timeout: the request may have reached Exa, so the call stays unknown."""


class ExaResearch:
    """Live adapter over the documented REST endpoints (POST /search, POST /contents) with httpx: a finite timeout
    (app.core.ledger.SEND_TIMEOUT_S per class, search and contents, in code) and no retries of any kind. search() and contents() return the
    list the tool runner stores as the observation. The exa-py SDK
    offers no timeout or retry control, so the REST form is used; request bodies are the builders above."""

    def __init__(self, api_key: str, base_url: str = EXA_BASE_URL, search_type: str = "auto", timeout_s: float | None = None):
        import httpx
        if search_type not in ALLOWED_TYPES:
            raise ValueError(f"search type {search_type!r} is not one of {ALLOWED_TYPES}")   # the refusal names the refused type and the allowed ones
        from app.core.ledger import SEND_TIMEOUT_S
        self.base_url, self.search_type = base_url.rstrip("/"), search_type
        self.timeouts = {"search": float(timeout_s) if timeout_s else float(SEND_TIMEOUT_S["search"]), "contents": float(timeout_s) if timeout_s else float(SEND_TIMEOUT_S["contents"])}   # one timeout per class, in code; an explicit timeout_s (the test suites) overrides both
        self.timeout_s = max(self.timeouts.values())
        self.client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(self.timeout_s), headers={"x-api-key": api_key, "content-type": "application/json"})
        self.simulated = False                      # a stub adapter sets True; the runner then never settles its calls as charges
        self.last_request: dict | None = None       # the REST body the price is pinned to
        self.last_estimate_usd: float | None = None
        self.last_cost_usd: float | None = None     # provider figure (costDollars.total), for reconciliation only
        self.last_statuses: list[tuple] = []        # (status, source) per result as the provider reported them

    def _post(self, path: str, body: dict) -> dict:
        import httpx
        cls = "search" if path.rstrip("/").endswith("search") else "contents"
        from app.core.ledger import SendDeadline, send_within
        try:
            r = send_within(lambda: self.client.post(path, json=body, timeout=httpx.Timeout(self.timeouts[cls])), self.timeouts[cls])   # the class's own bound per request (the elapsed time of the send, beside httpx's inactivity timeout)
        except (httpx.TimeoutException, SendDeadline) as e:
            raise ExaTimeout(f"Exa {path} timed out after {self.timeouts[cls]:.0f} s") from e
        if r.status_code >= 400:
            raise ExaHTTPError(r.status_code, r.text)
        data = r.json()
        self.last_cost_usd = (data.get("costDollars") or {}).get("total")
        self.last_statuses = [(st.get("status"), st.get("source")) for st in (data.get("statuses") or [])]
        return data

    @staticmethod
    def _pages(data: dict, freshness: str, max_age: int | None) -> list[dict]:
        statuses = {st.get("id"): st.get("source") for st in (data.get("statuses") or [])}
        out = []
        for r in data.get("results", []):
            rec = type("R", (), {"url": r.get("url", ""), "id": r.get("id"), "title": r.get("title"), "author": r.get("author"),
                                 "published_date": r.get("publishedDate"), "text": r.get("text") or ""})()
            page = page_record(rec, freshness, max_age, statuses.get(r.get("id")) or statuses.get(r.get("url")))
            if r.get("highlights"):
                page["highlights"] = [h for h in r["highlights"] if isinstance(h, str)][:8]
            out.append(page)
        return out

    def search(self, query: str, freshness: str, num_results: int = 8, include_domains=None, search_type: str | None = None, additional_queries=None) -> list[dict]:
        deep = str(search_type or self.search_type).startswith("deep")
        body = build_search_body(query, freshness, num_results, include_domains, search_type=search_type or self.search_type, additional_queries=additional_queries, highlights=deep)
        self.last_request, self.last_estimate_usd = body, estimate_body_usd("search", body)
        data = self._post("/search", body)
        return self._pages(data, freshness, body["contents"].get("maxAgeHours"))

    def contents(self, urls: list[str], freshness: str) -> list[dict]:
        body = build_contents_body(urls, freshness)
        self.last_request, self.last_estimate_usd = body, estimate_body_usd("contents", body)
        wire = {"urls": body["ids"], **{k: v for k, v in body.items() if k != "ids"}}   # the documented field is urls; ids is the legacy alias
        data = self._post("/contents", wire)
        return self._pages(data, freshness, body.get("maxAgeHours"))

def normalise_page(p: dict, freshness: str = "technical") -> dict:
    """Fill the provenance fields a minimal page dict (a stub adapter's) lacks, so every page can be stored and versioned."""
    out = dict(p)
    url = out.get("url") or ""
    out.setdefault("canonical_url", canonical_url(url) if url else "")
    out.setdefault("text", out.get("text") or "")
    out.setdefault("content_hash", content_hash(out["text"]))
    out.setdefault("retrieved_at", datetime.now(timezone.utc).isoformat())
    out.setdefault("freshness", freshness)
    out.setdefault("cache_status", "unknown")
    out.setdefault("untrusted", True)
    return out


def data_root(data_dir: str | os.PathLike | None = None, conn=None) -> pathlib.Path:
    """Where source text files live: the given directory, else the directory of the database file itself (data/app.db keeps
    data/sources next to it; a test or experiment database in its own directory keeps its sources there too and never touches
    the real data directory, whatever NMB_DATA_DIR says: the files of an isolated store stay
    with it), else NMB_DATA_DIR, else ./data."""
    if conn is not None:
        require_guarded(conn)
    if data_dir:
        return pathlib.Path(data_dir)
    if conn is not None:
        try:
            for _, name, file in conn.execute("PRAGMA database_list"):
                if name == "main" and file:
                    return pathlib.Path(file).parent
        except Exception:
            pass
    if os.environ.get("NMB_DATA_DIR"):
        return pathlib.Path(os.environ["NMB_DATA_DIR"])
    return pathlib.Path("data")


def write_version_file(path: pathlib.Path, text: str) -> None:
    """Line-ending policy: no normalisation anywhere. The text is stored, hashed, indexed and ranged
    as the very same string; files are written and read with newline='' so CR, LF and CRLF survive byte for byte."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text or "")


def read_version_file(path: pathlib.Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


class SourceVersionMismatch(Exception):
    """The bytes on disk do not hash to the version the database names: nothing is returned under that identity."""


def source_text(conn, project_id: str, source_id: str, data_dir: str | os.PathLike | None = None) -> tuple[str, str] | None:
    """(content_hash, text) of the current version of a stored source in this project, or None when it does not exist here.
    The bytes are verified against the recorded hash before they are returned: a mismatch raises
    SourceVersionMismatch instead of serving text under the wrong version."""
    require_guarded(conn)
    row = conn.execute("SELECT content_hash, text_path FROM sources WHERE id=? AND project_id=?", (source_id, project_id)).fetchone()
    if not row:
        return None
    path = data_root(data_dir, conn) / row[1] if row[1] else None
    text = read_version_file(path) if path and path.exists() else ""
    if content_hash(text) != row[0]:
        raise SourceVersionMismatch(f"{source_id}: stored bytes do not match version {row[0][:15]}…")
    return row[0], text


def version_path(root: pathlib.Path, source_id: str, hash_: str, first: bool) -> pathlib.Path:
    """Immutable file per version: the first version keeps SRC-nnn.txt, every later version is SRC-nnn.<hash12>.txt."""
    display = source_id.split(".", 1)[1]
    return root / (f"{display}.txt" if first else f"{display}.{hash_[7:19]}.txt")


def record_found_by(conn, source_id: str, engines) -> list[str]:
    """The engines whose results contained the source, merged into sources.found_by_json (a JSON list, no duplicates)."""
    require_guarded(conn)
    import json as _json
    if "found_by_json" not in {r[1] for r in conn.execute("PRAGMA table_info(sources)")}:
        return []
    row = conn.execute("SELECT found_by_json FROM sources WHERE id=?", (source_id,)).fetchone()
    have = []
    if row and row[0]:
        try:
            have = [x for x in _json.loads(row[0]) if isinstance(x, str)]
        except ValueError:
            have = []
    merged = list(dict.fromkeys(have + [str(e).lower() for e in (engines or []) if e]))
    if merged != have:
        with conn:
            conn.execute("UPDATE sources SET found_by_json=? WHERE id=?", (_json.dumps(merged), source_id))
    return merged


def store_pages(conn, project_id: str, pages: list[dict], data_dir: str | os.PathLike | None = None, tier: int = 2, found_by=None, refresh: bool = True) -> list[str]:
    """Persist pages as untrusted text files plus sources rows (retrieved_at, cache_status, max_age_hours, content_hash) and
    index their passages. The same page (project, canonical url, hash) is stored once. A page already stored under the same
    canonical url with different content is a refresh: the source keeps its id, the text file is rewritten,
    the new content hash becomes the current source_version, the previous version row is marked superseded, the passages are
    re-indexed and coverage of the subjects linked to the source is invalidated, all in one transaction. With refresh False
    (the verify stage's re_fetch) a page already stored under the same canonical url with different content is not
    stored: the record stays at its version, the live bytes are the caller's observation alone, and the page dict gains the
    stored source_id, its own content hash as source_version, stored False and stored_version (the record's hash); the same
    bytes still refresh the provenance and a page not yet stored is stored as usual. Returns the source
    ids in page order; each page dict gains source_id and source_version."""
    require_guarded(conn)
    from app.repo import Repo
    from app.core.retrieval import index_source
    from app.core import coverage
    from schemas import models as M
    require_guarded(conn)
    root = data_root(data_dir, conn) / "sources" / project_id
    root.mkdir(parents=True, exist_ok=True)
    repo, ids = Repo(conn), []
    for p in pages:
        if not p.get("url"):
            continue
        for k, v in normalise_page(p).items():
            p.setdefault(k, v)
        row = conn.execute("SELECT id FROM sources WHERE project_id=? AND canonical_url=? AND content_hash=?", (project_id, p["canonical_url"], p["content_hash"])).fetchone()
        if row:                                   # same page, same content: keep its identity, refresh provenance in one transaction
            with repo._tx():
                conn.execute("UPDATE sources SET retrieved_at=?, cache_status=?, max_age_hours=?, freshness=?, fetch_status=?, stale_at=NULL WHERE id=?",
                             (p["retrieved_at"], p["cache_status"], p.get("max_age_hours"), p["freshness"], "ok" if p.get("text") else "partial", row[0]))
                # the same bytes keep every verdict whatever the copy's status (live or cached); what a copy of that status may settle
                # (a price needs a live copy) is the derivation's reading of sources.cache_status (evidence.settles_alone), never a cleared verdict
                conn.execute("INSERT INTO source_versions (source_id, project_id, content_hash, chars_total, text_path, retrieved_at) VALUES (?,?,?,?,(SELECT text_path FROM sources WHERE id=?),?) "
                             "ON CONFLICT(source_id, content_hash) DO UPDATE SET retrieved_at=excluded.retrieved_at, superseded_at=NULL",
                             (row[0], project_id, p["content_hash"], len(p.get("text") or ""), row[0], p["retrieved_at"]))
                coverage.touch_read_date(conn, project_id, row[0])                           # every claim of the page read fresh carries the new READ date
                coverage.flag_derivation(conn, project_id)                                 # belt and braces beside the schema triggers
                from app.core.reconcile import derive
                derive(conn, project_id)                                                   # the derivation last, in the same transaction as every provenance write
            p["source_id"], p["source_version"] = row[0], p["content_hash"]; ids.append(row[0]); continue
        prior = conn.execute("SELECT id, content_hash, text_path, stale_at, max_age_hours FROM sources WHERE project_id=? AND canonical_url=?", (project_id, p["canonical_url"])).fetchone()
        published = p.get("published_date") or None
        try:
            published_dt = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
        except ValueError:
            published_dt = None
        if prior:                                 # same page, new content: a refresh keeps the id and starts a new, immutable version
            sid, old_hash = prior[0], prior[1]
            if not refresh:                       # the verify stage's re_fetch observes the live page and moves nothing: no file, no pointer, no version row, no passages, no invalidation
                p["source_id"], p["source_version"], p["stored"], p["stored_version"] = sid, p["content_hash"], False, old_hash
                ids.append(sid); continue
            path = version_path(root, sid, p["content_hash"], first=False)
            write_version_file(path, p.get("text") or "")                   # the new version's own file, written before the pointer moves; the old file is never touched
            rel = str(path.relative_to(root.parents[1]))
            with repo._tx():                                                    # pointer, version rows, passages and coverage move together or not at all
                conn.execute("UPDATE sources SET url=?, title=COALESCE(?, title), publisher=COALESCE(?, publisher), published_at=?, retrieved_at=?, content_hash=?, freshness=?, "
                             "fetch_status=?, cache_status=?, max_age_hours=?, text_path=?, stale_at=NULL WHERE id=?",
                             (p["url"], p.get("title"), p.get("author"), published_dt.isoformat() if published_dt else None, p["retrieved_at"], p["content_hash"], p["freshness"],
                              "ok" if p.get("text") else "partial", p["cache_status"], p.get("max_age_hours"), rel, sid))
                conn.execute("UPDATE source_versions SET superseded_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE source_id=? AND content_hash=? AND superseded_at IS NULL", (sid, old_hash))
                conn.execute("INSERT INTO source_versions (source_id, project_id, content_hash, chars_total, text_path, retrieved_at) VALUES (?,?,?,?,?,?) "
                             "ON CONFLICT(source_id, content_hash) DO UPDATE SET retrieved_at=excluded.retrieved_at, superseded_at=NULL, text_path=excluded.text_path",
                             (sid, project_id, p["content_hash"], len(p.get("text") or ""), rel, p["retrieved_at"]))
                index_source(conn, project_id, sid, p.get("text") or "")
                zero_hour = prior[3] is not None or prior[4] == 0 or p.get("max_age_hours") == 0   # a page under the zero-hour rule, marked for the run's fresh read or not, and whatever the copy's status
                coverage.invalidate_source_refresh(conn, project_id, sid, p["content_hash"], text=(p.get("text") or "") if zero_hour else None)   # a fresh read of a zero-hour page keeps a verified claim whose sentence is byte for byte in the new text (at the new version) and clears a changed one; a refresh of any other page (a re_fetch turn, another connection mid-verification) clears the verdicts of the claims of its older versions, which await re-verification
            p["source_id"], p["source_version"] = sid, p["content_hash"]; ids.append(sid); continue
        sid = repo.next_id(project_id, "SRC")
        path = version_path(root, sid, p["content_hash"], first=True)
        write_version_file(path, p.get("text") or "")
        rel = str(path.relative_to(root.parents[1]))
        with repo._tx():
            repo.upsert_source(M.Source(id=sid, project_id=project_id, url=p["url"], canonical_url=p["canonical_url"], title=p.get("title"), publisher=p.get("author"),
                                        tier=tier, published_at=published_dt, retrieved_at=datetime.fromisoformat(p["retrieved_at"]), content_hash=p["content_hash"],
                                        freshness=p["freshness"], fetch_status="ok" if p.get("text") else "partial", cache_status=p["cache_status"], max_age_hours=p.get("max_age_hours")))
            conn.execute("UPDATE sources SET text_path=? WHERE id=?", (rel, sid))
            conn.execute("INSERT INTO source_versions (source_id, project_id, content_hash, chars_total, text_path, retrieved_at) VALUES (?,?,?,?,?,?)",
                         (sid, project_id, p["content_hash"], len(p.get("text") or ""), rel, p["retrieved_at"]))
            index_source(conn, project_id, sid, p.get("text") or "")
        p["source_id"], p["source_version"] = sid, p["content_hash"]; ids.append(sid)
    for p in pages:
        engines = list(found_by or []) + list(p.get("found_by") or [])
        if p.get("source_id") and engines:
            record_found_by(conn, p["source_id"], engines)
    return ids
