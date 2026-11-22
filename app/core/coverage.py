"""Coverage per requirement and research question: whether each subject has verified evidence and its sources are fully processed.

Two statuses per subject, kept apart:
  evidence_status (supported, conflicting, insufficient, pending) is derived from the verified claims attached to the subject;
  processing_status (not_started, in_progress, complete) is derived from the extraction batches of the sources linked to the
  subject, complete only when every linked batch of the current source version is completed and no attached claim has an
  incomplete condition extraction.
A subject is covered only when both are met.

Invalidation: a requirement version change, a source refresh (new source_version) or a stale freshness label sets the
affected subjects back to pending and in_progress, and they stay there: a claim counts as evidence only when its
source_version is the current one, its verdict was given against the current requirement version and its scope is
verified, so refresh_all() never restores supported from old claims.

Reporting: coverage_report() lists every subject that is not covered with the reason, and a stop writes it into the plan's
unresolved_checks and the pause payload (the caller emits the pause event).

Transactions: every function writes inside the caller's open transaction when there is one, otherwise in its own."""
import hashlib, json, re, sqlite3
from datetime import datetime, timedelta, timezone
from app.core.sqlguard import require_guarded

SUBJECT_TYPES = ("requirement", "research_question")
COVERED = ("supported", "complete")


def _write(conn, fn):
    if conn.in_transaction:
        return fn()
    with conn:
        return fn()


def _tx_immediate(conn, fn):
    """Run fn inside the caller's transaction, or inside one BEGIN IMMEDIATE of its own."""
    require_guarded(conn)
    if conn.in_transaction:
        return fn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        out = fn()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK"); raise
    return out


def flag_derivation(conn, project_id: str) -> None:
    """A base write happened in this transaction without the derivation: readers treat the project as not yet derived until app/core/reconcile.py clears the flag."""
    require_guarded(conn)
    conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (project_id,))


def derivation_pending(conn, project_id: str) -> bool:
    require_guarded(conn)
    r = conn.execute("SELECT derivation_pending FROM projects WHERE id=?", (project_id,)).fetchone()
    return bool(r and r[0])


# The derived columns, in one place. Any SQL that selects or filters on
# one of them outside ACCESSOR_MODULES must sit after a derived() call in the same function.
DERIVED_COLUMNS = {"claims": ("status",), "claim_requirement_support": ("status", "reason"), "decisions": ("status", "derivation_reason"),
                   "coverage": ("evidence_status", "processing_status", "derived_claim_ids_json", "derived_source_versions_json"), "claim_conflicts": ("*",)}
ACCESSOR_MODULES = ("app/core/coverage.py", "app/core/reconcile.py", "app/core/evidence.py")   # the accessor, the derivation and the rules it applies


class DerivationPending(Exception):
    """A reader that must not derive asked for derived state while a base write left the project's flag set."""


class _Derived:
    """What derived() returns: truthy when a derivation ran, and the reads context manager for the connection."""
    def __init__(self, conn, ran: bool):
        require_guarded(conn)
        self.conn, self.ran = conn, ran
        self._cm = None
    def __bool__(self):
        return self.ran
    def __enter__(self):
        from app.core.sqlguard import reads
        self._cm = reads(self.conn); self._cm.__enter__(); return self
    def __exit__(self, *a):
        return self._cm.__exit__(*a)


def derived(conn, project_id: str, run_id: str | None = None, allow_derive: bool = True) -> _Derived:
    """The one guarded accessor for derived state. Every reader of coverage subjects, support status,
    claim status or decision status calls it first: when the flag is set it derives (inside the caller's transaction, or
    in a BEGIN IMMEDIATE of its own) so no reader ever returns stale derived values; a caller that must not derive passes
    allow_derive=False and gets DerivationPending instead of the old values. Used as a context manager it opens the reads
    context on the connection, which the execution boundary (app/core/sqlguard.py) requires for any derived read."""
    require_guarded(conn)
    ran = False
    if derivation_pending(conn, project_id):
        if not allow_derive:
            raise DerivationPending(f"project {project_id}: derived state not yet derived since its last base write")
        from app.core.reconcile import derive, reconcile_project
        if conn.in_transaction:
            derive(conn, project_id, run_id=run_id)
        else:
            reconcile_project(conn, project_id, run_id=run_id)
        ran = True
    return _Derived(conn, ran)


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def research_question_id(project_id: str, decision_subject: str, target_fact: str) -> str:
    """Stable id from the normalised subject and target fact: the same question planned twice keeps one coverage row."""
    h = hashlib.sha256(f"{_norm(decision_subject)}|{_norm(target_fact)}".encode()).hexdigest()[:12]
    return f"{project_id}.RQ-{h}"


def ensure_subject(conn: sqlite3.Connection, project_id: str, subject_type: str, subject_id: str, requirement_version: int | None = None) -> None:
    require_guarded(conn)
    assert subject_type in SUBJECT_TYPES
    def body():
        rv = requirement_version if requirement_version is not None else (conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [1])[0]
        conn.execute("INSERT INTO coverage (project_id, subject_type, subject_id, requirement_version) VALUES (?,?,?,?) ON CONFLICT DO NOTHING", (project_id, subject_type, subject_id, rv))
    _write(conn, body)


def _tokens(text: str) -> set[str]:
    return {w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in (text or "")).split() if len(w) > 1}


def query_matches_question(search_query: str, question_text: str, target_fact: str = "") -> bool:
    """A search served a research question when its query is the question's own query text or its target fact (normalised).
    Word overlap is not enough: two questions that differ by one vendor name share every other word. So
    sources link per question, never to every question of the stage; an unserved planned query is pursued as it stands."""
    sq = _norm(search_query)
    return bool(sq) and sq in (_norm(question_text), _norm(target_fact))


def subject_matches_question(claim_subject: str | None, question_text: str, target_fact: str = "") -> bool:
    """A claim's product subject names the question when at least two of its tokens, and half of them, appear in the question."""
    cs = _tokens(claim_subject or "")
    if not cs:
        return False
    q = _tokens(question_text) | _tokens(target_fact)
    return len(cs & q) >= max(2, (len(cs) + 1) // 2)


def persist_research_questions(conn: sqlite3.Connection, project_id: str, plan, run_id: str | None = None) -> list[str]:
    """Research questions from a ResearchPlanOut (or its dict): one row per (decision_subject, target_fact), plus a coverage row each."""
    require_guarded(conn)
    queries = plan.get("queries", []) if isinstance(plan, dict) else [q.model_dump() for q in plan.queries]
    ids = []
    def body():
        for q in queries:                                                          # one id per input query, in order; a duplicate repeats its id and never shifts later queries
            rid = research_question_id(project_id, q["decision_subject"], q["target_fact"])
            conn.execute("INSERT INTO research_questions (id, project_id, decision_subject, target_fact, query_text, run_id) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET run_id=COALESCE(excluded.run_id, run_id), query_text=COALESCE(excluded.query_text, query_text)",
                         (rid, project_id, q["decision_subject"], q["target_fact"], q.get("text"), run_id))
            rv = (conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [1])[0]
            conn.execute("INSERT INTO coverage (project_id, subject_type, subject_id, requirement_version) VALUES (?,?,?,?) ON CONFLICT DO NOTHING", (project_id, "research_question", rid, rv))
            ids.append(rid)
    _write(conn, body)
    return ids


def _row(conn, project_id, subject_type, subject_id):
    r = conn.execute("SELECT subject_type, subject_id, requirement_version, evidence_status, processing_status, claim_ids_json, source_versions_json, updated_at, derived_claim_ids_json, derived_source_versions_json FROM coverage WHERE project_id=? AND subject_type=? AND subject_id=?",
                     (project_id, subject_type, subject_id)).fetchone()
    return dict(zip(("subject_type", "subject_id", "requirement_version", "evidence_status", "processing_status", "claim_ids_json", "source_versions_json", "updated_at", "derived_claim_ids_json", "derived_source_versions_json"), r)) if r else None


def _links_row(conn, project_id, subject_type, subject_id) -> dict:
    """The base link columns of a coverage row (what the link writers read; the statuses are derived and not theirs to read)."""
    r = conn.execute("SELECT claim_ids_json, source_versions_json FROM coverage WHERE project_id=? AND subject_type=? AND subject_id=?", (project_id, subject_type, subject_id)).fetchone()
    return {"claim_ids_json": r[0], "source_versions_json": r[1]}


def link_sources(conn: sqlite3.Connection, project_id: str, subject_type: str, subject_id: str, source_ids: list[str]) -> None:
    """Attach sources (at their current version) to a subject; processing is recomputed from their batches."""
    require_guarded(conn)
    def body():
        ensure_subject(conn, project_id, subject_type, subject_id)
        row = _links_row(conn, project_id, subject_type, subject_id)
        linked = {x["source_id"]: x for x in json.loads(row["source_versions_json"])}
        for sid in source_ids:
            cur = conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (sid, project_id)).fetchone()
            if cur:
                linked[sid] = {"source_id": sid, "source_version": cur[0]}
        conn.execute("UPDATE coverage SET source_versions_json=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND subject_type=? AND subject_id=?",
                     (json.dumps(sorted(linked.values(), key=lambda x: x["source_id"])), project_id, subject_type, subject_id))
        _derive_now(conn, project_id)                                                    # a base link, then the derivation in the same BEGIN IMMEDIATE transaction
    _tx_immediate(conn, body)


def attach_claims(conn: sqlite3.Connection, project_id: str, subject_type: str, subject_id: str, claim_ids: list[str]) -> None:
    """Attach verified or extracted claims to a subject; statuses are recomputed."""
    require_guarded(conn)
    def body():
        ensure_subject(conn, project_id, subject_type, subject_id)
        row = _links_row(conn, project_id, subject_type, subject_id)
        ids = sorted(set(json.loads(row["claim_ids_json"])) | set(claim_ids))
        conn.execute("UPDATE coverage SET claim_ids_json=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND subject_type=? AND subject_id=?", (json.dumps(ids), project_id, subject_type, subject_id))
        _derive_now(conn, project_id)
    _tx_immediate(conn, body)


def attach_claims_by_subject(conn: sqlite3.Connection, project_id: str, source_id: str | None = None) -> int:
    """Attach every claim whose decision_subject matches a research question's decision subject, and every
    claim to each subject its provenance names by id (requirements and research questions alike; the ids were validated at
    extraction against the subject list of the send). source_id narrows the provenance pass to one source. Returns the number of links."""
    require_guarded(conn)
    n = 0
    def body():
        nonlocal n
        if source_id is None:                                                                 # the whole-project pass by decision subject; the per-source pass of the extract node attaches by the validated subject ids only
            for rid, subject in conn.execute("SELECT id, decision_subject FROM research_questions WHERE project_id=?", (project_id,)).fetchall():
                ids = [r[0] for r in conn.execute("SELECT id FROM claims WHERE project_id=? AND lower(trim(COALESCE(decision_subject,'')))=?", (project_id, _norm(subject)))]
                if ids:
                    attach_claims(conn, project_id, "research_question", rid, ids); n += len(ids)
        by_subject: dict[tuple[str, str], list[str]] = {}
        sql = "SELECT id, provenance_json FROM claims WHERE project_id=?" + (" AND source_id=?" if source_id else "")
        for cid, prov in conn.execute(sql, (project_id, source_id) if source_id else (project_id,)).fetchall():
            for entry in json.loads(prov or "[]"):
                for sid in entry.get("subject_ids") or []:
                    st = "requirement" if ".REQ-" in sid else "research_question"
                    exists = conn.execute(f"SELECT 1 FROM {'requirements' if st == 'requirement' else 'research_questions'} WHERE id=? AND project_id=?", (sid, project_id)).fetchone()
                    if exists:
                        by_subject.setdefault((st, sid), [])
                        if cid not in by_subject[(st, sid)]:
                            by_subject[(st, sid)].append(cid)
        for (st, sid), ids in by_subject.items():
            srcs = sorted({r[0] for cid in ids for r in conn.execute("SELECT source_id FROM claims WHERE id=? AND source_id IS NOT NULL", (cid,)).fetchall()})
            if srcs:
                link_sources(conn, project_id, st, sid, srcs)                                  # the claim's source is the subject's evidence, for every subject the claim names: a base link
            attach_claims(conn, project_id, st, sid, ids); n += len(ids)
    _tx_immediate(conn, body)                                                                 # the provenance read, the claim source-id read, the subject validation, link_sources, attach_claims and their derivation in one BEGIN IMMEDIATE (joins a caller's open transaction)
    return n


def subjects_for_extraction(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """The run's full subject list, built from coverage, the input of every extraction send. A requirement
    (confirmed, current version) with its key facts, that is its stored value, and its hard flag as the founder stated it;
    a research question with its target fact. Priority classes: hard requirements 0, other requirements 1, research
    questions 2. Each subject carries its evidence status and processing state from its coverage row."""
    require_guarded(conn)
    out = []
    reqs = conn.execute("SELECT id, key, value_json, hard FROM requirements WHERE project_id=? AND status='confirmed' ORDER BY id", (project_id,)).fetchall()
    rqs = conn.execute("SELECT id, decision_subject, target_fact FROM research_questions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
    for rid, *_ in reqs:
        ensure_subject(conn, project_id, "requirement", rid)
    for qid, *_ in rqs:
        ensure_subject(conn, project_id, "research_question", qid)
    with derived(conn, project_id):
        status = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT subject_id, evidence_status, processing_status FROM coverage WHERE project_id=?", (project_id,)).fetchall()}
    for rid, key, vj, hard in reqs:
        v = json.loads(vj); facts = " ".join(str(x) for x in v) if isinstance(v, list) else str(v)
        ev, pr = status.get(rid, ("pending", "not_started"))
        out.append({"id": rid, "type": "requirement", "label": key, "key_facts": facts, "hard": bool(hard), "priority": 0 if hard else 1, "evidence_status": ev, "processing_status": pr})
    for qid, ds, tf in rqs:
        ev, pr = status.get(qid, ("pending", "not_started"))
        out.append({"id": qid, "type": "research_question", "label": ds, "key_facts": tf, "hard": False, "priority": 2, "evidence_status": ev, "processing_status": pr})
    return out


def run_report_marks(conn: sqlite3.Connection, project_id: str) -> dict:
    """What the extract and verify nodes left explicit per run, read from their run_steps marks (no new
    storage): per subject the candidates not processed and the claims not verified, each with its reason, and the candidate
    list marks (scan_limited, ranking_exhausted, candidates_exhausted). The latest run's record wins per subject."""
    require_guarded(conn)
    out = {"unprocessed": {}, "unverified": {}, "subject_reasons": {}}
    row = conn.execute("SELECT rs.output_json, rs.run_id FROM run_steps rs JOIN runs r ON r.id=rs.run_id WHERE r.project_id=? AND rs.node='extract_report' AND rs.status='completed' ORDER BY rs.completed_at DESC, rs.step_key DESC LIMIT 1", (project_id,)).fetchone()
    if not row:
        return out
    d = json.loads(row[0] or "{}"); report_run = row[1]                                 # the latest run's record; an earlier run's gaps are not carried
    for sid, items in (d.get("unverified") or {}).items():
        keep = [x for x in items if conn.execute("SELECT 1 FROM claims WHERE id=? AND project_id=? AND verdict IS NULL", (x["claim_id"], project_id)).fetchone()]   # a claim that has its verdict now is no longer unverified
        if keep:
            out["unverified"][sid] = keep
    def operationally_done(source_id: str) -> bool:
        """No send is owed to the candidate: every batch of its current version completed or
        needs_review, at least one batch, and no verification composition of the report's own run left started that still names a
        pending claim of the current version; compositions of earlier runs or of superseded versions are history, never counted, never changed."""
        cur = conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (source_id, project_id)).fetchone()
        rows = conn.execute("SELECT b.status FROM extraction_batches b WHERE b.project_id=? AND b.source_id=? AND b.source_version=?", (project_id, source_id, cur[0] if cur else "")).fetchall()
        if not cur or not rows or any(r[0] not in ("completed", "needs_review") for r in rows):
            return False
        for (oj,) in conn.execute("SELECT output_json FROM run_steps WHERE run_id=? AND node='verify_group' AND status='started' AND step_key LIKE ?", (report_run, f"{report_run}:verify:%:{source_id}:g%")).fetchall():
            comp = json.loads(oj or "{}")
            for cid in comp.get("claims", []):
                if (comp.get("versions") or {}).get(cid) == cur[0] and conn.execute("SELECT 1 FROM claims WHERE id=? AND project_id=? AND source_version=? AND verdict IS NULL", (cid, project_id, cur[0])).fetchone():
                    return False                                                             # a composition of this run still owes a verdict at the current version
        return True
    for sid, items in (d.get("unprocessed") or {}).items():
        keep = [x for x in items if not operationally_done(x["source_id"])]              # a candidate operationally done since is no longer unprocessed; one with work left keeps its entry and its reason
        if keep:
            out["unprocessed"][sid] = keep
    out["subject_reasons"] = dict(d.get("subject_reasons") or {})
    return out


def claim_rows(conn: sqlite3.Connection, project_id: str, claim_ids: list[str], requirement_version: int, withdrawn: dict[str, str] | None = None) -> list[dict]:
    """The claims of a subject with what coverage needs to judge them: a claim is valid evidence
    only when its source_version is the source's current version, its verdict was given against the current requirement
    version and its scope_status is verified (unchecked never counts). Everything else carries a stale_reason."""
    require_guarded(conn)
    out = []
    superseded = {}                                                                          # the losers of a settled conflict, with the winner and the reason; recorded, never deleted, never counted
    for pref, reason, cj in conn.execute("SELECT preferred_claim_id, reason, claim_ids_json FROM claim_conflicts WHERE project_id=? AND status='settled'", (project_id,)).fetchall():
        for x in json.loads(cj):
            if x != pref:
                superseded[x] = f"superseded by {pref} ({reason})"
    ids = list(claim_ids); found = {}
    for k in range(0, len(ids), _IN_CHUNK):                                                  # one read for the subject's claims, lifted out of the per-claim loop (per-claim reads can run to tens of millions in one batch of three sources)
        chunk = ids[k:k + _IN_CHUNK]
        for r in conn.execute(f"SELECT c.id, c.status, c.scope_status, c.source_id, c.source_version, c.verified_requirement_version, s.content_hash "
                              f"FROM claims c LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=? AND c.id IN ({','.join('?' * len(chunk))})", (project_id, *chunk)).fetchall():
            found[r[0]] = r
    moved = sorted({r[3] for r in found.values() if r[3] and r[4] != r[6]}); extracted_at = set()
    for k in range(0, len(moved), _IN_CHUNK):                                                # the one batch read for the claims whose source moved on, lifted out of the loop too (a project whose stale claims sit at superseded versions has many of them)
        chunk = moved[k:k + _IN_CHUNK]
        extracted_at.update((a, b) for a, b in conn.execute(f"SELECT DISTINCT source_id, source_version FROM extraction_batches WHERE project_id=? AND status IN ('completed','needs_review') AND source_id IN ({','.join('?' * len(chunk))})", (project_id, *chunk)).fetchall())
    for cid in ids:
        r = found.get(cid)
        if not r:
            continue
        d = dict(zip(("id", "status", "scope_status", "source_id", "source_version", "verified_requirement_version", "current_version"), r))
        why = []
        if d["scope_status"] in (None, "unchecked"):
            why.append("scope not verified")
        notes = []
        if d["source_id"] and d["source_version"] != d["current_version"]:
            extracted = (d["source_id"], d["current_version"]) in extracted_at
            if extracted:
                why.append("quote taken from a superseded source version")             # the new version was extracted and this claim was not found again in it
            else:
                notes.append("source changed since this quote; re-extraction pending")  # until the new version is extracted, the prior version's claims stay usable with the note, never dropped to unverified
        if d["verified_requirement_version"] != requirement_version:
            why.append(f"verdict not given against requirement version {requirement_version}")
        if withdrawn and cid in withdrawn and d["status"] != "conflicting":          # support withdrawn by the reconciliation (freshness by kind, eligibility); a conflict shows as conflicting instead
            why.append(withdrawn[cid])
        if cid in superseded:
            why.append(superseded[cid])                                                  # a superseded claim is not evidence; only an unresolved tie leaves its question conflicting
        d["valid"] = not why; d["stale_reason"] = "; ".join(why); d["note"] = "; ".join(notes)
        out.append(d)
    return out


def open_conflict_claims(conn: sqlite3.Connection, project_id: str) -> set[str]:
    """The claims of the project's open conflicts, the only conflicts that take a claim out of what a comparison may cite."""
    require_guarded(conn)
    return {x for (cj,) in conn.execute("SELECT claim_ids_json FROM claim_conflicts WHERE project_id=? AND status='open'", (project_id,)).fetchall() for x in json.loads(cj)}


def derive_evidence_status(claims: list[dict]) -> tuple[str, str]:
    """From claim rows as claim_rows() returns them. Only valid rows count: conflicting if any is contradicted or conflicting;
    supported if any is supported and not out of scope; insufficient if every valid row is partial or insufficient; pending
    while no valid row exists (no claim yet, verification outstanding, or every claim stale against the current requirement
    or source version). A row without a 'valid' key counts only if its scope_status is verified."""
    if not claims:
        return "pending", "no claim attached yet"
    valid = [c for c in claims if c.get("valid", c.get("scope_status") not in (None, "unchecked"))]
    stale = [c for c in claims if c not in valid]
    if not valid:
        reasons = sorted({c.get("stale_reason") or ("awaits verification" if c["status"] == "unverified" else "scope not verified") for c in stale})
        return "pending", f"{len(stale)} claim(s) do not count yet: " + "; ".join(reasons)
    statuses = [c["status"] for c in valid]
    if any(s in ("contradicted", "conflicting") for s in statuses):
        return "conflicting", f"{sum(s in ('contradicted', 'conflicting') for s in statuses)} claim(s) contradicted or conflicting"
    if any(c["status"] == "supported" and c.get("scope_status") != "out_of_scope" for c in valid):
        return "supported", "a supported in-scope claim exists at the current requirement and source versions"
    if any(s == "unverified" for s in statuses):
        return "pending", f"{statuses.count('unverified')} claim(s) await verification"
    return "insufficient", f"{len(valid)} claim(s) verified partial, insufficient or out of scope, none supported"


def record_verification(conn: sqlite3.Connection, project_id: str, claim_id: str, verdict: str, scope_status: str, requirement_version: int | None = None, conditions_found: list[str] | None = None,
                        requirements_entailed: list[str] | None = None, run_id: str | None = None, derive: bool = True) -> dict | None:
    """What the verify node stores on a claim: the verdict, the verified scope and the requirement version
    it was judged against; conditions found by verification join the claim's conditions; the requirements the quote entails
    are persisted per requirement version in claim_requirement_support, the only source of hard-
    requirement support. Coverage counts the claim from here."""
    require_guarded(conn)
    def body():
        rv = requirement_version if requirement_version is not None else (conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [1])[0]
        previously = {r[0] for r in conn.execute("SELECT DISTINCT requirement_id FROM claim_requirement_support WHERE project_id=? AND claim_id=?", (project_id, claim_id)).fetchall()}   # captured before the withdrawal
        conn.execute("DELETE FROM claim_requirement_support WHERE project_id=? AND claim_id=? AND requirement_version=?", (project_id, claim_id, rv))
        for req in requirements_entailed or []:
            if conn.execute("SELECT 1 FROM requirements WHERE id=? AND project_id=?", (req, project_id)).fetchone():   # only requirements of this project; anything else is ignored
                conn.execute("INSERT OR REPLACE INTO claim_requirement_support (project_id, claim_id, requirement_id, requirement_version, verdict) VALUES (?,?,?,?,?)", (project_id, claim_id, req, rv, verdict))
        if conditions_found:
            have = json.loads(conn.execute("SELECT conditions_json FROM claims WHERE id=? AND project_id=?", (claim_id, project_id)).fetchone()[0])
            seen = {_norm(c) for c in have}
            have += [c for c in conditions_found if _norm(c) not in seen]
            conn.execute("UPDATE claims SET conditions_json=? WHERE id=? AND project_id=?", (json.dumps(have), claim_id, project_id))
        conn.execute("UPDATE claims SET verdict=?, scope_status=?, verified_requirement_version=?, checked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), invalidated_at=NULL WHERE id=? AND project_id=?",
                     (verdict, scope_status, rv, claim_id, project_id))            # the verdict is the base record; status, support status and coverage are derived below (the derivation is their single writer)
        for req in sorted(previously | set(requirements_entailed or [])):
            if conn.execute("SELECT 1 FROM requirements WHERE id=? AND project_id=?", (req, project_id)).fetchone():
                ensure_subject(conn, project_id, "requirement", req)
        flag_derivation(conn, project_id)
        if derive:
            from app.core.reconcile import derive as _derive
            return _derive(conn, project_id, run_id=run_id)                             # same BEGIN IMMEDIATE transaction as the base write
        return None
    return _tx_immediate(conn, body)


_IN_CHUNK = 500   # the ids one chunked read carries (well under SQLite's bound variable limit)


def derive_processing_status(conn: sqlite3.Connection, project_id: str, linked: list[dict], claim_ids: list[str]) -> tuple[str, list[str]]:
    """From the extraction batches of the linked sources at their current version and the condition state of the claims."""
    require_guarded(conn)
    if not linked:
        return "not_started", ["no source linked yet"]
    reasons, notes, started, incomplete = [], [], False, False
    sids = list(dict.fromkeys(x["source_id"] for x in linked)); src, batches_of = {}, {}
    for k in range(0, len(sids), _IN_CHUNK):                                                 # the linked sources' versions, stale marks and batch rows in one read each, lifted out of the per-source loop (per-source reads can run to millions of each per batch)
        chunk = sids[k:k + _IN_CHUNK]; marks = ",".join("?" * len(chunk))
        for sid, h, st in conn.execute(f"SELECT id, content_hash, stale_at FROM sources WHERE project_id=? AND id IN ({marks})", (project_id, *chunk)).fetchall():
            src[sid] = (h, st)
        for sid, ver, bno, bst, why in conn.execute(f"SELECT source_id, source_version, batch_no, status, review_reason FROM extraction_batches WHERE project_id=? AND source_id IN ({marks}) ORDER BY source_id, source_version, batch_no", (project_id, *chunk)).fetchall():
            batches_of.setdefault((sid, ver), []).append((bno, bst, why))
    for x in linked:
        sid = x["source_id"]
        cur = src.get(sid)
        if not cur:
            reasons.append(f"{sid} was deleted"); incomplete = True; continue
        if cur[0] != x["source_version"]:
            notes.append(f"{sid} source changed; re-extraction pending"); started = True; continue             # a note, not incomplete processing
        if cur[1]:
            reasons.append(f"{sid} stale since {cur[1][:10]} (freshness window passed); re-fetch pending"); incomplete = True; started = True
        batches = batches_of.get((sid, cur[0]), [])
        if not batches:
            earlier = any(s0 == sid for (s0, _v) in batches_of) or \
                      conn.execute("SELECT 1 FROM source_versions WHERE project_id=? AND source_id=? AND superseded_at IS NOT NULL LIMIT 1", (project_id, sid)).fetchone()   # a refresh happened: re-extraction pending
            if earlier:
                notes.append(f"{sid} source changed; re-extraction pending"); started = True; continue        # a note here too
            reasons.append(f"{sid}: extraction not started"); incomplete = True; continue
        started = True
        for bno, st, why in batches:
            if st == "needs_review" and partial_review(why):
                notes.append(f"{sid} batch {bno} partial ({why or 'limit reached'}); the claims extracted so far are usable")   # the per-source budget was reached: partial, never a state a decision waits on
            elif st == "needs_review":
                reasons.append(f"{sid} batch {bno} needs review ({why or 'see batch'})"); incomplete = True
            elif st != "completed":
                reasons.append(f"{sid} batch {bno} {st}"); incomplete = True
    rv = int((conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [1])[0])
    ids = list(claim_ids); rows = {}
    for k in range(0, len(ids), _IN_CHUNK):                                                  # the subject's claims in one read, lifted out of the per-claim loop
        chunk = ids[k:k + _IN_CHUNK]
        for r in conn.execute(f"SELECT id, conditions_status, verdict, invalidated_at, verified_requirement_version FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(chunk))})", (project_id, *chunk)).fetchall():
            rows[r[0]] = r[1:]
    for cid in ids:
        r = rows.get(cid)
        if r and r[0] != "complete":
            reasons.append(f"{cid}: condition extraction {r[0]} (conditions may be missing)"); incomplete = True
        if r and r[3] is not None and int(r[3]) != rv:                                  # a requirement change: re-verification at the new version is pending work
            reasons.append(f"{cid} verified against requirement version {r[3]}; re-verification at version {rv} pending"); incomplete = True; started = True
    if not started:
        return "not_started", reasons
    return ("in_progress", reasons + notes) if incomplete else ("complete", notes)


PARTIAL_REASONS = ("continuation limit", "no progress", "no-progress", "truncated after split", "condition continuation limit", "condition quota")


def partial_review(review_reason: str | None) -> bool:
    """A batch stopped by a limit of the per-source budget (the continuation limit, the no-progress guard, a passage still
    truncated after the split and the bump, the condition quota) is partial: its claims are usable. A batch left needs_review for another
    reason (an injection flag, a failed version check, a failure shape) still needs review."""
    low = (review_reason or "").lower()
    return any(k in low for k in PARTIAL_REASONS)


def requirement_support(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int) -> list[str]:
    """Claims with a verification record naming the requirement at this version (verdict supported): the only source of a
    requirement's evidence; zero current records means not covered."""
    require_guarded(conn)
    with derived(conn, project_id):                                                      # the accessor derives first when a base write left the flag, and opens the reads context
        return [r[0] for r in conn.execute("SELECT DISTINCT claim_id FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version=? AND verdict='supported' AND status='counts' ORDER BY claim_id",
                                           (project_id, requirement_id, requirement_version)).fetchall()]


def calculated_requirement(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int) -> dict:
    """The verdicts code computed for a requirement (the budget by calculation), by verdict: {verdict: [the claim ids of each product's row]};
    {} for a requirement no calculation judged at this version."""
    require_guarded(conn)
    from app.core.budget import CALCULATED
    out: dict = {}
    for verdict, ids in conn.execute("SELECT verdict, claim_ids_json FROM coverage_checks WHERE project_id=? AND requirement_id=? AND requirement_version=? AND note LIKE ?", (project_id, requirement_id, int(requirement_version), CALCULATED + "%")).fetchall():
        out.setdefault(verdict, []).append(json.loads(ids or "[]"))
    return out


def withdrawn_support(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int) -> dict[str, str]:
    """Claims whose supported verdict names the requirement but whose record was withdrawn by the reconciliation, with the reason."""
    require_guarded(conn)
    with derived(conn, project_id):
        return {r[0]: r[1] or "withdrawn" for r in conn.execute("SELECT claim_id, reason FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version=? AND verdict='supported' AND status='withdrawn'",
                                                                (project_id, requirement_id, requirement_version)).fetchall()}


def _withdrawn_rows(conn, project_id, requirement_id, requirement_version) -> dict[str, str]:
    return {r[0]: r[1] or "withdrawn" for r in conn.execute("SELECT claim_id, reason FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version=? AND verdict='supported' AND status='withdrawn'",
                                                            (project_id, requirement_id, requirement_version)).fetchall()}


def requirement_records(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int) -> list[str]:
    """Claims with a verification record naming the requirement at this version whose verdict is supported, whatever the
    record's derived status (counts or withdrawn): the claims a requirement's coverage row is derived from. A support
    record only ever holds the verifier's supported verdict. A disputed requirement still reads
    conflicting, never pending, and that comes from the claim, not from this query: the reconciliation
    sets the status of a claim in an open conflict to conflicting, claim_rows carries that status and
    derive_evidence_status reads it. requirement_support (supported and counting) stays the only source of
    hard-constraint support."""
    require_guarded(conn)
    return [r[0] for r in conn.execute("SELECT DISTINCT claim_id FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version=? AND verdict='supported' ORDER BY claim_id",
                                       (project_id, requirement_id, requirement_version)).fetchall()]


def judged_claims(conn: sqlite3.Connection, project_id: str, product_id: str, product_version: str) -> list[list[str]]:
    """A product identity's supported, in-scope (or conditional) claims now: [id, source version] in id order, the record a coverage verdict is kept against."""
    require_guarded(conn)
    with derived(conn, project_id):
        return [[r[0], r[1]] for r in conn.execute("SELECT id, COALESCE(source_version, '') FROM claims WHERE project_id=? AND product_id=? AND COALESCE(product_version, '')=? AND status='supported' AND scope_status IN ('in_scope','conditional') ORDER BY id",
                                                   (project_id, product_id, product_version or "")).fetchall()]


def older_records(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int) -> list[str]:
    """Claims whose supported record names the requirement at an older version only: re-verification at the current version is pending work."""
    require_guarded(conn)
    return [r[0] for r in conn.execute("SELECT DISTINCT claim_id FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version<? AND verdict='supported' "
                                       "AND claim_id NOT IN (SELECT claim_id FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version=?) ORDER BY claim_id",
                                       (project_id, requirement_id, requirement_version, project_id, requirement_id, requirement_version)).fetchall()]


_JOINER = re.compile(r"\s*(?:[/+&;]|,(?!\d{3}\b))\s*")                                                 # the comma of a grouped number (50,000) joins nothing
_VERSION_JOINER = re.compile(r"\s*(?:[/+&;\u2012-\u2015\u2212]|,(?!\d{3}\b))\s*|\s+[-\u2010\u2011]\s+")                 # in a version a dash joins too, a hyphen only with spaces round it (3-5-pro is 3.5-pro)
_SIZE_WORD = r"(?:[kmgt]i?b|v?cpus?|cores?|ram)\b"
_VERSION_PART = re.compile(rf"(?:^|[\s_-])v?\d+(?:\.\d+)*(?!{_SIZE_WORD})[a-z]*(?:[\s_-](?!{_SIZE_WORD})[a-z][a-z0-9]*)*$", re.I)   # a part that is a version, a suffix allowed: 4, v4, 4.5, 4o, GPT-4.1, Chat 2, 4.1-mini; never a size (8GB, 2 vCPU) or a word
_NUMBER_RANGE = re.compile(r"(v?\d+(?:\.\d+)*)\s*[-\u2010-\u2015\u2212]\s*(v?\d+(?:\.\d+)*)", re.I)
_DATE = re.compile(r"\d{4}[-/.\u2010-\u2015\u2212]\d{1,2}(?:[-/.\u2010-\u2015\u2212]\d{1,2})?|\d{1,2}/\d{1,2}/\d{4}")
_RUN_ON_JOIN = re.compile(r"(?<=\d)(?i:or|and|to|vs|versus)(?=[vV]?\d)|(?<=[a-z0-9])(?:Or|And|To|Vs|Versus)(?=[A-Z\d])")
_PRODUCT_JOIN = re.compile(r"\b(?:vs|versus)\b|(?<=\d)\s+to\s+(?=v?\d)", re.I)                         # the join words names_two_models does not read; "Speech to Text 2" is one name


def _split_joins(text: str | None) -> str:
    """The shared join tokenisation, on the raw text, before lower case and storage: a join word
    run on between two numbers (4orv5) or at a camelCase boundary (v4OrV5, Text4OrChat5) gets its spaces; nothing else moves (IPv4 stays IPv4)."""
    return _RUN_ON_JOIN.sub(lambda m: f" {m.group(0)} ", str(text or ""))


def _joins_versions(text: str | None) -> bool:
    """A join word (or, and, to, vs, versus) with a number on either side, after the shared tokenisation and budget.words_of."""
    from app.core.budget import words_of
    words = words_of(re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", _split_joins(text)))
    has_digit = lambda ws: any(ch.isdigit() for w in ws for ch in w)
    return any(w in ("or", "and", "to", "vs", "versus") and has_digit(words[:i]) and has_digit(words[i + 1:]) for i, w in enumerate(words))


def _one_shape(first: str, second: str) -> bool:
    """A range joins two numbers of one shape: as many dotted parts, none over four digits. Digits are counted, never punctuation (12.34-12.35);
    a build number with a suffix (20241022-v1.0, 3-20240229) is one version."""
    a, b = first.lstrip("vV").split("."), second.lstrip("vV").split(".")
    return len(a) == len(b) and max(len(x) for x in a + b) <= 4


def keep_joins(text: str | None, version: bool = False) -> str:
    """The recorded identity keeps that it names more than
    one (the normalisation below would make "4/5" the version 4.5, "v4OrV5" the version 4orv5). A joiner (a slash, a comma, a plus, an ampersand, a
    semicolon) between parts that are each a version is written "or", and in a version a dash or a bare range of two numbers too; a join word stays
    a word, its boundaries kept. A date, provider/model notation and two figures of a plan (8GB/2 vCPU) stay."""
    s = str(text or "").strip()
    if _DATE.fullmatch(s):
        return s
    pair = _NUMBER_RANGE.fullmatch(s) if version else None
    if pair and _one_shape(*pair.groups()):
        return " or ".join(pair.groups())
    if _joins_versions(s):
        return _split_joins(s) if version else _PRODUCT_JOIN.sub(" or ", _split_joins(s))
    parts = (_VERSION_JOINER if version else _JOINER).split(s)
    return " or ".join(parts) if len(parts) > 1 and all(_VERSION_PART.search(p) for p in parts) else s


def holds_many_versions(version: str | None) -> bool:
    """Whether a product_version holds more than one version: after the one tokenisation (budget.words_of), a join
    word (or, and, to, vs, versus) with a version on either side, the raw spellings of keep_joins included. A single version, a date or a version
    with words ("2.5-flash-8b", "2-api-and-smtp") holds one."""
    return _joins_versions(keep_joins(version, version=True))


def product_identity(vendor: str | None, product: str | None, version: str | None, subject: str | None = None) -> tuple[str, str]:
    """Explicit product identity: product_id from the vendor and product the extractor names (fallback:
    the claim's subject), product_version from the version it names; both normalised (lowercase, alphanumeric tokens
    joined by '-', a leading 'v' or 'version' dropped from the version). Citations and requirement support bind on exact
    equality of both, never on token overlap."""
    name = " ".join(x for x in (keep_joins(vendor), keep_joins(product)) if x) or keep_joins(subject)   # the joiners of a two-model name stay in the recorded identity, in each field the name comes from
    pid = "-".join(w for w in "".join(ch.lower() if ch.isalnum() else " " for ch in name).split())
    ver = "".join(ch.lower() if ch.isalnum() or ch == "." else " " for ch in keep_joins(version, version=True)).split()   # "4/5" and a bare "4-5" keep that they name two versions
    ver = "-".join(ver)
    for prefix in ("version-", "version", "v"):
        if ver.startswith(prefix) and len(ver) > len(prefix) and ver[len(prefix)].isdigit():
            ver = ver[len(prefix):]; break
    ver = re.sub(r"(?<=\d)-(?=\d)", ".", ver)                                               # a hyphen between digits in a version is a point ("3-5-pro" is "3.5-pro")
    return pid, ver


def normalise_identity(product_id: str | None, product_version: str | None) -> tuple[str, str]:
    """The canonical pair two identities are compared by: lower case, separators as hyphens, a hyphen between digits as a
    point ("universal-3-5-pro" is "universal-3.5-pro"), a leading v or version dropped from the version."""
    pid = "-".join(w for w in "".join(ch.lower() if ch.isalnum() or ch == "." else " " for ch in (product_id or "")).split())
    pid = re.sub(r"(?<=\d)-(?=\d)", ".", pid)
    ver = "-".join("".join(ch.lower() if ch.isalnum() or ch == "." else " " for ch in (product_version or "")).split())
    for prefix in ("version-", "version", "v"):
        if ver.startswith(prefix) and len(ver) > len(prefix) and ver[len(prefix)].isdigit():
            ver = ver[len(prefix):]; break
    ver = re.sub(r"(?<=\d)-(?=\d)", ".", ver)
    return pid, ver


def identity_groups(claims: list[dict]) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """The identities a set of claims names: the normalised pair -> each claim's identity as stored."""
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for c in claims:
        if c.get("product_id"):
            groups.setdefault(normalise_identity(c.get("product_id"), c.get("product_version")), []).append((c.get("product_id") or "", c.get("product_version") or ""))
    return groups


def tied_identities(claims: list[dict]) -> list[tuple[str, str]]:
    """The identities that share the most claims, as stored, when more than one does; [] when one identity leads or none is named."""
    groups = identity_groups(claims)
    top = max((len(v) for v in groups.values()), default=0)
    tied = [v[0] for _, v in sorted(groups.items()) if len(v) == top]
    return tied if len(tied) > 1 else []


LABEL_WORDS = {"api": "API", "stt": "STT", "ai": "AI", "gpt": "GPT", "llm": "LLM"}


def identity_label(product_id: str | None, product_version: str | None = "") -> str:
    """A candidate's label: its recorded product name and version, never a claim's subject text:
    the product id's words, a vendor repeated at its head once, then the version unless the name holds it ("soniox-soniox-stt-api" is Soniox STT API,
    "railway-railway" at hobby is Railway Hobby). The modes a product offers reach the memo as claims."""
    pid, ver = normalise_identity(product_id, product_version)
    words = pid.split("-")
    words = words[1:] if len(words) > 1 and words[0] == words[1] else words
    name = " ".join(LABEL_WORDS.get(w, w[:1].upper() + w[1:]) for w in words)
    return name if not ver or set(ver.split("-")) <= set(words) else f"{name} {ver.title() if ver.isalpha() else ver}"


def majority_identity(claims: list[dict], label: str = "") -> tuple[str, str]:
    """The product identity a set of cited claims names. One identity (after normalisation) is returned as stored;
    several identities resolve to the one most claims name; a tie goes to the identity whose words share most with the option's label,
    then the first in sorted order. ('', '') when no claim names a product."""
    groups = identity_groups(claims)
    if not groups:
        return "", ""
    words = set(re.findall(r"[a-z0-9]+", (label or "").lower()))
    best = max(sorted(groups), key=lambda k: (len(groups[k]), len(words & set(k[0].split("-"))), [-ord(ch) for ch in k[0]]))
    return groups[best][0]


def _canon(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


CLASS_ALIASES = {"speech_to_text": ("speech-to-text", "speech to text", "stt", "transcription api")}   # the class synonyms of a product class, resolved to one token before grouping


def canonical_product(product_id: str | None) -> str:
    """The canonical product string of a normalised id: a class alias resolved to its class token
    (CLASS_ALIASES; "soniox-stt", "soniox-speech-to-text" and "soniox-transcription-api" are one), a trailing "api" dropped, the vendor
    token repeated at the head of the product dropped ("soniox-soniox-stt-api" is "soniox-stt"; "render-render" is "render" and
    "render-render-workspace" "render-workspace", two products), then every separator dropped. The reason: when the extraction spells one product several
    ways, the identity rule would refuse the evidence recorded under the other spellings."""
    pid = normalise_identity(product_id, "")[0]
    text = f"-{pid}-"
    for cls, aliases in CLASS_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            text = text.replace(f"-{'-'.join(alias.split())}-", f"-{_canon(cls)}-")
    text = re.sub(r"-api-$", "-", text)
    words = [w for w in text.split("-") if w]
    canon = "".join(_canon(w) for w in words)
    if len(words) > 1 and canon[len(words[0]):].startswith(_canon(words[0])):
        canon = canon[len(_canon(words[0])):]
    return canon


def canonical_identity(product_id: str | None, product_version: str | None) -> str:
    """One string per product and version whatever the spelling: the normalised id and version with
    every separator dropped, so "dicta-dicta-lm" 3.0, "dicta-dictalm" "3.0" and "dicta-dictalm-3-0" are one identity, and "kolwrite-kolwrite-base"
    is "kolwrite-kolwrite" at version "base"; the typed aliases are resolved first (canonical_product)."""
    pid, ver = normalise_identity(product_id, product_version)
    return canonical_product(pid) + _canon(ver)


def same_product(option_id: str, option_version: str, claim_id: str | None, claim_version: str | None, option_label: str = "", claim_text: str = "") -> bool:
    """The identity rule: the same product and version after normalisation, compared with every separator dropped
    (two spellings of one name are one product); and a claim about one tier or version of the product supports an option that names no
    version when the option's label names that tier ("KolWrite API (Base model ...)" and a claim about KolWrite at version base). A claim
    about another tier, or an option that names a version the claim does not, never matches."""
    if not option_id:
        return False
    a_pid, a_ver = normalise_identity(option_id, option_version); b_pid, b_ver = normalise_identity(claim_id or "", claim_version or "")
    if (a_pid, a_ver) == (b_pid, b_ver):
        return True
    ca, cb = canonical_product(a_pid), canonical_product(b_pid)                             # the typed aliases, a trailing api and the doubled vendor token resolved
    if ca + _canon(a_ver) == cb + _canon(b_ver):
        return True
    if a_ver and not b_ver and ca == cb and claim_text:                                      # the option names a plan or version the claim's record lacks: the claim's own words must name it ("Railway Hobby plan" against a claim about Railway that says Hobby)
        words = {_canon(t) for t in re.findall(r"[A-Za-z0-9.]+", claim_text)}
        return _canon(a_ver) in words
    if not a_ver and cb.startswith(ca):
        rest = cb[len(ca):] + _canon(b_ver)
        if not rest:
            return True
        tokens = [_canon(t) for t in re.findall(r"[A-Za-z0-9.]+", option_label or "")]
        pairs = {tokens[i] + tokens[i + 1] for i in range(len(tokens) - 1)}
        if rest in tokens or rest in pairs:                                              # a whole token of the label (or two adjacent), never a substring
            return True
    if a_ver and b_ver and ca == cb and _canon(b_ver).startswith(_canon(a_ver)):          # the claim's version is a finer spelling of the option's ("free" and "free-plan", "2.0" and "2.0-chat")
        return True
    return named_in_text(a_pid, a_ver, b_pid, claim_text)


def named_in_text(option_pid: str, option_ver: str, claim_pid: str, claim_text: str) -> bool:
    """The last rule: the same vendor (the first word of both ids) and every word of the option's own identity
    (its product words beyond the vendor and its version) named as a whole word in the claim's text. The extraction spells one product
    many ways ("deepgram-nova-3" beside "deepgram-deepgram-api"; "render-render-workspace" beside "render-render"); the claim's words are
    the record of what it is about. A claim that does not name the product never matches."""
    if not claim_text or not option_pid:
        return False
    a_words = [w for w in re.split(r"[-.]", option_pid.lower()) if w]
    b_words = [w for w in re.split(r"[-.]", (claim_pid or "").lower()) if w]
    if not a_words or not b_words or a_words[0] != b_words[0]:
        return False
    own = [w for w in a_words[1:] if w != a_words[0]]
    ver = _canon(option_ver)                                                                # the version is one whole token ("5.5" is 55), never its digits one by one (otherwise "Claude 3.5 Haiku" matches Claude Haiku 5.5 on the digit 5 present twice)
    if not own and not ver:
        return False
    text_words = {_canon(t) for t in re.findall(r"[A-Za-z0-9]+", claim_text)}
    text_tokens = {_canon(t) for t in re.findall(r"[A-Za-z0-9.]+", claim_text)} | {_canon(t) for t in re.split(r"[-]", normalise_identity(claim_pid, "")[0]) if t}   # the whole tokens of the text (a dotted version as one) and of the claim's identity
    return all(_canon(w) in text_words for w in own) and (not ver or ver in text_tokens)


def names_version(product_id: str | None, product_version: str | None) -> bool:
    """Whether an identity names a version anywhere in its spelling: a recorded product_version
    (a number or a tier, "sonnet-4.5", "hobby"), or a number in the product part after the vendor (the reading of workflow.names_model_version:
    the extractor's "anthropic-claude-sonnet-4-5" at no version is Claude Sonnet 4.5, one canonical identity with "anthropic-claude" at "sonnet-4.5").
    A family ("anthropic-claude", "ai21-jamba") names none."""
    pid, ver = normalise_identity(product_id, product_version)
    return bool(ver) or bool(re.search(r"\d", "-".join(pid.split("-")[1:])))


def counts_for_identity(option_id: str | None, option_version: str | None, claim: dict) -> bool:
    """A claim counts for an option only when it names the
    same product (same_product, the claim's own words included) at the same level: both name a version, or neither does. A family-level claim never
    carries to a versioned option, and a claim about one version (for example Claude Sonnet 4.5) never carries to the family that names no version
    (the option anthropic-claude), so the open decision states the check that settles it. The
    level is read from the whole identity (names_version), so the extractor's spelling with the version inside product_id is at the same level as the
    spelling with it in product_version."""
    if not option_id:
        return False
    if not same_product(option_id, option_version or "", claim.get("product_id"), claim.get("product_version"), "", claim.get("text") or ""):
        return False
    return names_version(option_id, option_version) == names_version(claim.get("product_id"), claim.get("product_version"))


def update_subject(conn: sqlite3.Connection, project_id: str, subject_type: str, subject_id: str) -> dict:
    require_guarded(conn)
    from app.core.sqlguard import derive_step
    with derive_step(conn):
        return _update_subject(conn, project_id, subject_type, subject_id)


def _update_subject(conn: sqlite3.Connection, project_id: str, subject_type: str, subject_id: str) -> dict:
    """Recompute both statuses of one subject from the records and store them. A requirement's claims and sources are
    derived from its current-version support records (never attached by a node); a research question's claims are the
    ones attached by extraction and verification."""
    def body():
        row = _row(conn, project_id, subject_type, subject_id)
        if not row:
            return None
        rv = int((conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [row["requirement_version"]])[0])   # the project's current version, never the row's
        if subject_type == "requirement":
            claim_ids = requirement_records(conn, project_id, subject_id, rv)          # the supported records, counting or withdrawn; a conflict reads from the claim's status; counting support alone decides hard constraints
            linked = []
            for cid in claim_ids:
                r = conn.execute("SELECT c.source_id, s.content_hash FROM claims c JOIN sources s ON s.id=c.source_id WHERE c.id=? AND c.project_id=?", (cid, project_id)).fetchone()
                if r and r[0] and not any(x["source_id"] == r[0] for x in linked):
                    linked.append({"source_id": r[0], "source_version": r[1]})
            new_claims, new_links = json.dumps(claim_ids), json.dumps(sorted(linked, key=lambda x: x["source_id"]))
            if (new_claims, new_links) != (row["derived_claim_ids_json"], row["derived_source_versions_json"]):              # the computed links live in their own derived columns; the run's base links are never touched here
                conn.execute("UPDATE coverage SET derived_claim_ids_json=?, derived_source_versions_json=? WHERE project_id=? AND subject_type=? AND subject_id=?", (new_claims, new_links, project_id, subject_type, subject_id))
            rows = claim_rows(conn, project_id, claim_ids, rv, withdrawn=_withdrawn_rows(conn, project_id, subject_id, rv))
            ev = derive_evidence_status(rows)[0] if rows else "pending"
            pr, _ = derive_processing_status(conn, project_id, json.loads(row["source_versions_json"]), json.loads(row["claim_ids_json"]))   # the processing side of a requirement row reads its base links exactly as a research question's; the evidence side stays with the support records above
            if pr != "in_progress" and older_records(conn, project_id, subject_id, rv):
                pr = "in_progress"                                                          # records at an older requirement version only: re-verification pending
        else:
            claim_ids = json.loads(row["claim_ids_json"])
            ev, _ = derive_evidence_status(claim_rows(conn, project_id, claim_ids, rv))
            pr, _ = derive_processing_status(conn, project_id, json.loads(row["source_versions_json"]), claim_ids)
        if (ev, pr, int(row["requirement_version"])) != (row["evidence_status"], row["processing_status"], rv):   # strict idempotency: no write and no timestamp on a no-op pass
            conn.execute("UPDATE coverage SET evidence_status=?, processing_status=?, requirement_version=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND subject_type=? AND subject_id=?",
                         (ev, pr, rv, project_id, subject_type, subject_id))
        return _row(conn, project_id, subject_type, subject_id)
    return _tx_immediate(conn, body)                                                          # the coverage row, base links, requirement version, support, source-version and batch reads, the derivation and the status write in one BEGIN IMMEDIATE; joins the open transaction of link_sources, record_verification and the reconciliation, opens one for the standalone update_subject and refresh_all


def refresh_all(conn: sqlite3.Connection, project_id: str) -> int:
    require_guarded(conn)
    from app.core.sqlguard import derive_step
    with derive_step(conn):
        rows = conn.execute("SELECT subject_type, subject_id FROM coverage WHERE project_id=?", (project_id,)).fetchall()
        for st, sid in rows:
            _update_subject(conn, project_id, st, sid)
    return len(rows)


def is_covered(row: dict) -> bool:
    return (row["evidence_status"], row["processing_status"]) == COVERED


def covered_subjects(conn: sqlite3.Connection, project_id: str) -> list[str]:
    require_guarded(conn)
    with derived(conn, project_id):
        return [r[0] for r in conn.execute("SELECT subject_id FROM coverage WHERE project_id=? AND evidence_status='supported' AND processing_status='complete' ORDER BY subject_type, subject_id", (project_id,))]


# ---------------------------------------------------------------- invalidation
def _affected(conn, project_id, where_sql, params, set_versions=None):
    """The subjects an invalidation touches (base links re-pointed when a source refreshed); the statuses are derived afterwards."""
    from app.core.sqlguard import reads
    with reads(conn):                                                                      # the match looks at the computed links too (a derived column: the accessor's reads context)
        rows = conn.execute(f"SELECT subject_type, subject_id, source_versions_json FROM coverage WHERE project_id=? AND ({where_sql})", (project_id, *params)).fetchall()
    for st, sid, sv in rows:
        linked = json.loads(sv)                                                            # the base links only; a requirement's computed links are derived again afterwards
        if set_versions and linked:
            for x in linked:
                if x["source_id"] in set_versions:
                    x["source_version"] = set_versions[x["source_id"]]
            conn.execute("UPDATE coverage SET source_versions_json=? WHERE project_id=? AND subject_type=? AND subject_id=?", (json.dumps(linked), project_id, st, sid))
    return [(st, sid) for st, sid, _ in rows]


def _derive_now(conn, project_id, now=None):
    from app.core.reconcile import derive
    flag_derivation(conn, project_id)
    derive(conn, project_id, now=now)


def invalidate_requirement_change(conn: sqlite3.Connection, project_id: str, new_requirement_version: int, reverify: set[str] | list[str] | None = None, changed_requirement_id: str | None = None) -> list[tuple[str, str]]:
    """A requirement version change (base event). Without reverify: every claim verified against an older version awaits
    re-verification (verdict cleared). With reverify: only those
    claims, the change's impact set, await re-verification; every other claim whose verdict was given against the version just left
    is carried to the new version (verified_requirement_version moved, its support rows copied at the new version, the changed
    requirement's rows excepted), so a revision re-verifies the impact set and nothing else. The subjects recorded against the older
    version are returned; the derivation in the same transaction sets them back at the new version (a carried claim counts again)."""
    require_guarded(conn)
    def body():
        affected = _affected(conn, project_id, "requirement_version < ?", (new_requirement_version,))
        conn.execute("UPDATE projects SET requirement_version=? WHERE id=? AND requirement_version<?", (new_requirement_version, project_id, new_requirement_version))
        if reverify is None:
            conn.execute("UPDATE claims SET verdict=NULL, scope_status='unchecked', invalidated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                         "WHERE project_id=? AND verdict IS NOT NULL AND COALESCE(verified_requirement_version, 0) < ?", (project_id, new_requirement_version))   # the version the old verdict was given against stays: re-verification at the new version is pending
        else:
            ids = sorted(set(reverify)); marks = ",".join("?" * len(ids)); previous = int(new_requirement_version) - 1
            if ids:
                conn.execute(f"UPDATE claims SET verdict=NULL, scope_status='unchecked', invalidated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND verdict IS NOT NULL AND id IN ({marks})", (project_id, *ids))
            not_impact = f"AND claim_id NOT IN ({marks})" if ids else ""                        # an empty impact set carries every current verdict (NOT IN an empty list would carry none)
            conn.execute(f"INSERT OR IGNORE INTO claim_requirement_support (project_id, claim_id, requirement_id, requirement_version, verdict, created_at) "
                         f"SELECT project_id, claim_id, requirement_id, ?, verdict, created_at FROM claim_requirement_support WHERE project_id=? AND requirement_version=? AND requirement_id<>? {not_impact} "
                         "AND claim_id IN (SELECT id FROM claims WHERE project_id=? AND verdict IS NOT NULL AND verified_requirement_version=?)",
                         (new_requirement_version, project_id, previous, changed_requirement_id or "", *ids, project_id, previous))   # the carried claims' support rows at the new version (the base columns with the record's creation time; the derivation below sets status and reason)
            conn.execute(f"UPDATE claims SET verified_requirement_version=? WHERE project_id=? AND verdict IS NOT NULL AND verified_requirement_version=? {not_impact.replace('claim_id', 'id')}", (new_requirement_version, project_id, previous, *ids))   # carried: the verdict stands at the new version
        _derive_now(conn, project_id)
        return affected
    return _tx_immediate(conn, body)


def touch_read_date(conn: sqlite3.Connection, project_id: str, source_id: str, claim_ids: list[str] | None = None) -> int:
    """Every claim taken from a page read fresh carries the new READ date: the
    claims of the source at its current version (or the given ids) get fresh_until from the source's retrieved_at and class (the claim's
    kind keeps the 24-hour rule of a price or availability fact). Returns the number of claims touched. Inside the caller's transaction
    or its own."""
    require_guarded(conn)
    from app.core.freshness import fresh_until, parse_ts
    row = conn.execute("SELECT retrieved_at, freshness, content_hash FROM sources WHERE id=? AND project_id=?", (source_id, project_id)).fetchone()
    if not row or not row[0]:
        return 0
    read_at = parse_ts(row[0]) or datetime.now(timezone.utc)
    marks = f"AND id IN ({','.join('?' * len(claim_ids))})" if claim_ids else "AND source_version=?"
    params = tuple(claim_ids) if claim_ids else (row[2],)
    n = 0
    for cid, kind in conn.execute(f"SELECT id, kind FROM claims WHERE project_id=? AND source_id=? {marks}", (project_id, source_id, *params)).fetchall():
        until = fresh_until(row[1] or "technical", read_at, kind=kind) if (row[1] or "technical") in ("volatile", "policy", "technical", "stable") else None
        conn.execute("UPDATE claims SET fresh_until=? WHERE id=? AND project_id=?", (until.isoformat() if until else None, cid, project_id)); n += 1
    return n


def invalidate_source_refresh(conn: sqlite3.Connection, project_id: str, source_id: str, new_version: str, text: str | None = None) -> list[tuple[str, str]]:
    """A source refreshed to a new content hash (base event): the links point at the new version and the derivation in the same
    transaction sets the subjects back. Without the fresh text, the claims of its older versions await re-verification (verdict cleared).
    With the fresh text (store_pages passes it for every fresh read of a page
    under the zero-hour rule, marked for the run's read or not and whatever the copy's status; never for
    another page's refresh): a claim already verified whose quoted sentence is in the
    fresh text byte for byte keeps its verdict and moves to the new version with its range and its READ date; only a claim whose sentence
    changed or vanished loses its verdict (the new sentences are the new version's extraction); a claim still awaiting its verdict stays
    the old version's, as the verification protocol expects."""
    require_guarded(conn)
    def body():
        affected = _affected(conn, project_id, "(source_versions_json LIKE ? OR derived_source_versions_json LIKE ?)", (f'%"{source_id}"%', f'%"{source_id}"%'), set_versions={source_id: new_version})
        kept = []
        if text is not None:
            for cid, quote in conn.execute("SELECT id, quote FROM claims WHERE project_id=? AND source_id=? AND source_version<>? AND verdict IS NOT NULL ORDER BY id", (project_id, source_id, new_version)).fetchall():   # a claim already verified; one still awaiting its verdict stays the old version's, as the verification protocol expects
                if quote and quote in text:                                                     # byte for byte: no normalisation
                    start = text.find(quote)
                    conn.execute("UPDATE claims SET source_version=?, range_start=?, range_end=?, invalidated_at=NULL WHERE id=? AND project_id=?", (new_version, start, start + len(quote), cid, project_id))
                    kept.append(cid)
        conn.execute("UPDATE claims SET verdict=NULL, scope_status='unchecked', verified_requirement_version=NULL, invalidated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                     "WHERE project_id=? AND source_id=? AND source_version<>? AND verdict IS NOT NULL", (project_id, source_id, new_version))
        if kept:
            touch_read_date(conn, project_id, source_id, kept)
        _derive_now(conn, project_id)
        return affected
    return _tx_immediate(conn, body)


def claim_subjects(conn, project_id: str) -> dict[str, set[str]]:
    """claim id -> the normalised decision subjects it belongs to: the claim's own decision_subject and the subjects of the research questions
    whose coverage links name it (the subjects a run decides are matched against both)."""
    require_guarded(conn)
    from app.core import applicability as AP
    out: dict[str, set[str]] = {}
    for cid, subj in conn.execute("SELECT id, decision_subject FROM claims WHERE project_id=?", (project_id,)).fetchall():
        out[cid] = {AP.norm(subj)} if subj else set()
    rq_subject = {r[0]: AP.norm(r[1] or "") for r in conn.execute("SELECT id, decision_subject FROM research_questions WHERE project_id=?", (project_id,)).fetchall()}
    for rq, cj in conn.execute("SELECT subject_id, claim_ids_json FROM coverage WHERE project_id=? AND subject_type='research_question'", (project_id,)).fetchall():
        subj = rq_subject.get(rq)
        if not subj:
            continue
        for cid in json.loads(cj or "[]"):
            out.setdefault(cid, set()).add(subj)
    return out


def _subject_sources(conn, project_id: str, subjects) -> set[str] | None:
    """The sources carrying a claim of the given decision subjects (None: every source)."""
    if subjects is None:
        return None
    from app.core import applicability as AP
    allowed = {AP.norm(s) for s in subjects}
    mine = claim_subjects(conn, project_id)
    return {sid for sid, cid in conn.execute("SELECT DISTINCT source_id, id FROM claims WHERE project_id=? AND source_id IS NOT NULL", (project_id,)).fetchall() if mine.get(cid, set()) & allowed}


def sources_for_fresh_read(conn: sqlite3.Connection, project_id: str, subjects=None) -> list[tuple[str, str]]:
    """The zero-hour pages (max_age_hours 0) marked stale that carry claims of the subjects the run decides (every subject when None),
    as (source id, url), for the run's fresh reads."""
    require_guarded(conn)
    mine = _subject_sources(conn, project_id, subjects)
    rows = conn.execute("SELECT id, url FROM sources WHERE project_id=? AND max_age_hours=0 AND stale_at IS NOT NULL AND url IS NOT NULL ORDER BY id", (project_id,)).fetchall()
    return [(sid, url) for sid, url in rows if mine is None or sid in mine]


def pending_sources_for(conn: sqlite3.Connection, project_id: str, subjects=None, claim_ids=()) -> list[str]:
    """The sources the verification sweep visits: those with a claim at the source's current version and no verdict. With subjects, only
    the claims of the subjects the run decides and the given claim ids (the claims a requirement change binds) count; claims of other subjects
    keep their verdicts, or their wait, until a run decides those subjects."""
    require_guarded(conn)
    from app.core import applicability as AP
    rows = conn.execute("SELECT DISTINCT c.source_id, c.id FROM claims c JOIN sources s ON s.id=c.source_id AND s.content_hash=c.source_version WHERE c.project_id=? AND c.verdict IS NULL AND c.source_id IS NOT NULL ORDER BY c.source_id", (project_id,)).fetchall()
    if subjects is None:
        return sorted({r[0] for r in rows})
    allowed = {AP.norm(s) for s in subjects}; bound = set(claim_ids or ()); mine = claim_subjects(conn, project_id)
    return sorted({sid for sid, cid in rows if (mine.get(cid, set()) & allowed) or cid in bound})


def invalidate_stale(conn: sqlite3.Connection, project_id: str, now: datetime | None = None, subjects=None, mark_fresh: bool = True) -> list[tuple[str, str]]:
    """Sources whose freshness window (max_age_hours since retrieved_at) has passed: their subjects go back to pending and in_progress.
    A zero-hour page (prices and availability, max_age_hours 0) is marked for a
    fresh read (stale_at; the read clears it) and its claims keep their verdicts: the fresh read keeps a verdict whose sentence is byte for
    byte the same and re-verifies only a changed or new sentence (invalidate_source_refresh); clearing every verdict of every
    zero-hour page at every research run would re-verify the whole project each time. With subjects, only the
    pages carrying claims of those subjects are marked or expired; the others keep their state until a run decides their subjects. With
    mark_fresh False no zero-hour page is marked (a research adapter that cannot read fresh)."""
    require_guarded(conn)
    now = now or datetime.now(timezone.utc)
    mine = _subject_sources(conn, project_id, subjects)
    expired, fresh = [], []
    for sid, retrieved, hours in conn.execute("SELECT id, retrieved_at, max_age_hours FROM sources WHERE project_id=? AND max_age_hours IS NOT NULL", (project_id,)).fetchall():
        if mine is not None and sid not in mine:
            continue
        try:
            t = datetime.fromisoformat(retrieved.replace("Z", "+00:00"))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        if hours == 0:
            if mark_fresh:
                fresh.append(sid)
        elif now > t + timedelta(hours=hours):
            expired.append(sid)
    def body():
        affected = []
        for sid in fresh:
            conn.execute("UPDATE sources SET stale_at=COALESCE(stale_at, ?) WHERE id=? AND project_id=?", (now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", sid, project_id))   # marked for the fresh read; the verdicts stand
        for sid in expired:
            conn.execute("UPDATE sources SET stale_at=COALESCE(stale_at, ?) WHERE id=? AND project_id=?", (now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", sid, project_id))   # base event: the stale mark; a fetch clears it
            conn.execute("UPDATE claims SET verdict=NULL, scope_status='unchecked', invalidated_at=? WHERE project_id=? AND source_id=? AND verdict IS NOT NULL",
                         (now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", project_id, sid))   # every invalidation clears the verdict: the claim awaits re-verification
            affected += _affected(conn, project_id, "(source_versions_json LIKE ? OR derived_source_versions_json LIKE ?)", (f'%"{sid}"%', f'%"{sid}"%'))
        if fresh or expired:
            _derive_now(conn, project_id, now=now)
        return sorted(set(affected))
    return _tx_immediate(conn, body)


# ---------------------------------------------------------------- report
def coverage_report(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Every subject that is not covered (evidence_status != supported or processing_status != complete), with the reasons:
    which status fails and why, including needs_review batches and condition extraction left incomplete. A supported subject
    whose processing is still in progress appears with that reason."""
    require_guarded(conn)
    out = []
    with derived(conn, project_id):
        rows_cov = conn.execute("SELECT subject_type, subject_id, evidence_status, processing_status, claim_ids_json, source_versions_json, requirement_version, derived_claim_ids_json, derived_source_versions_json FROM coverage WHERE project_id=? ORDER BY subject_type, subject_id", (project_id,)).fetchall()
    marks = run_report_marks(conn, project_id)
    for r in rows_cov:
        st, sid, ev, pr, cj, sj, rv, dcj, dsj = r
        pcj, psj = cj, sj                                                                   # the processing side reads the base links for every subject type
        if st == "requirement":
            cj = dcj                                                                        # a requirement's evidence claims are the computed ones (the support records)
        unprocessed = marks["unprocessed"].get(sid) or []
        unverified = marks["unverified"].get(sid) or []
        covered = (ev, pr) == COVERED and not unprocessed and not unverified                 # a covered subject still appears while work of this run is left with a reason
        if covered:
            with derived(conn, project_id):
                notes = derive_processing_status(conn, project_id, json.loads(psj), json.loads(pcj))[1]   # a reading that stopped at a limit keeps its partial note, covered as it is (the subject is listed so that the note is reported)
            if not notes:
                continue
            out.append({"subject_type": st, "subject_id": sid, "label": _subject_label(conn, st, sid), "evidence_status": ev, "processing_status": pr, "requirement_version": rv, "claim_ids": json.loads(cj),
                        "reasons": [f"processing_status {pr}: " + "; ".join(notes)], "unprocessed_candidates": [], "unverified_claims": [], "covered": True})
            continue
        claim_ids = json.loads(cj)
        with derived(conn, project_id):
            claims = claim_rows(conn, project_id, claim_ids, rv, withdrawn=_withdrawn_rows(conn, project_id, sid, rv) if st == "requirement" else None)
            pr_why = derive_processing_status(conn, project_id, json.loads(psj), json.loads(pcj))[1] if pr != "complete" else []
        reasons = []
        if ev != "supported":
            why = derive_evidence_status(claims)[1] if claims else (f"no verification record names this requirement at version {rv}" if st == "requirement" else "no claim attached yet")
            reasons.append(f"evidence_status {ev}: {why}")
        if pr != "complete":
            why = list(pr_why)
            if st == "requirement":
                why = why + [f"re-verification at requirement version {rv} pending for {', '.join(x)}" for x in [older_records(conn, project_id, sid, rv)] if x]
            reasons.append(f"processing_status {pr}: " + ("; ".join(why) if why else "extraction of the linked sources is not finished"))
        if ev != "supported" and marks["subject_reasons"].get(sid):
            reasons.append(f"candidates {marks['subject_reasons'][sid]}")                     # candidates_exhausted or scan_limited, only for a still unsupported subject after its extension
        if unverified:
            reasons.append("claims not verified: " + "; ".join(f"{x['claim_id']} ({x['reason']})" for x in unverified))
        if unprocessed:
            reasons.append("candidates not processed: " + "; ".join(f"{x['source_id']} ({x['reason']})" for x in unprocessed))
        out.append({"subject_type": st, "subject_id": sid, "label": _subject_label(conn, st, sid), "evidence_status": ev, "processing_status": pr, "requirement_version": rv, "claim_ids": claim_ids, "reasons": reasons,
                    "unprocessed_candidates": unprocessed, "unverified_claims": unverified})
    return out


def _subject_label(conn: sqlite3.Connection, subject_type: str, subject_id: str) -> str:
    if subject_type == "research_question":
        q = conn.execute("SELECT decision_subject, target_fact FROM research_questions WHERE id=?", (subject_id,)).fetchone()
        if q:
            return f"{subject_id} ({q[0]}: {q[1]})"
    return subject_id


def report_lines(report: list[dict]) -> list[str]:
    return [f"Not covered: {r['label']} [{r['subject_type']}]: " + " | ".join(r["reasons"]) for r in report if not r.get("covered")]   # a covered subject's retained note is no "not covered" line


def supporting_claims(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int, claim_ids) -> list[str]:
    """The claims among claim_ids whose verification record names the requirement at this version with verdict supported."""
    require_guarded(conn)
    ids = list(claim_ids)
    if not ids:
        return []
    with derived(conn, project_id):
        rows = conn.execute(f"SELECT claim_id FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version=? AND verdict='supported' AND status='counts' AND claim_id IN ({','.join('?' * len(ids))}) ORDER BY claim_id",
                            (project_id, requirement_id, requirement_version, *ids)).fetchall()
    return [r[0] for r in rows]


def named_in_quote(conn: sqlite3.Connection, project_id: str, requirement: dict, claim_ids) -> list[str]:
    """The claims among claim_ids (a coverage verdict's, which no support record gates) whose own quote names each language the requirement
    records; all of them when it records none."""
    require_guarded(conn)
    from app.core import evidence
    ids = list(claim_ids)
    if not ids:
        return []
    quotes = dict(conn.execute(f"SELECT id, quote FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(ids))})", (project_id, *ids)).fetchall())
    return [x for x in ids if not evidence.unnamed_languages(quotes.get(x), requirement)]


def plan_unresolved_lines(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """What every stored plan carries in unresolved_checks: the current coverage report, the
    unresolved decisions and every comparison's unresolved findings, whether or not the run stopped early."""
    require_guarded(conn)
    superseded = {r[0] for r in conn.execute("SELECT id FROM requirements WHERE project_id=? AND status='superseded'", (project_id,)).fetchall()}
    rv = int((conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [1])[0])
    judged = lambda r: r.get("subject_type") == "requirement" and bool(set(calculated_requirement(conn, project_id, r.get("subject_id"), rv)) & {"entails", "contradicts"})   # a requirement the calculation judged for an option (a figure within or over it) waits for no verification record: each decision states its option's figure against it and the plan states the total; one no option's cost could be calculated for stays listed
    lines = report_lines([r for r in coverage_report(conn, project_id) if not (r.get("subject_type") == "requirement" and r.get("subject_id") in superseded) and not judged(r)])   # current requirements only
    from app.core.evidence import conflict_lines                                      # conflicting evidence is listed with both sources and the preferred one
    with derived(conn, project_id):
        lines += conflict_lines(conn, project_id)
        lines += [d["line"] for d in unresolved_decisions(conn, project_id)]           # a decision's findings are attached to it once (decisions.unresolved), not repeated here
    return lines


DRAFT_ISSUE, NOT_COMPARED_ISSUE = "Draft decisions after the stop", "Not compared after the stop"
PARTIAL_DRAFT_ISSUE = "Partial draft after the stop"   # the one permitted draft stored as a version without the optional steps
STOP_ISSUE_PREFIXES = (DRAFT_ISSUE, NOT_COMPARED_ISSUE, PARTIAL_DRAFT_ISSUE)


def partial_draft_line(reason: str, skipped: list[str]) -> str:
    """The plan's open issue that marks a version as the partial draft written after a cap or time stop: the one permitted draft,
    stored before and independently of the optional steps it names as skipped. The versions screen, the exports and the Markdown route
    read it; a version carrying it is a draft for the approval (I6) and never a regular version."""
    return f"{PARTIAL_DRAFT_ISSUE} ({reason.replace('_', ' ')}): the one permitted draft, stored without " + ", ".join(skipped) + "; a partial draft, not a regular version."


def stop_issues(reason: str, drafts: list[tuple[str, str]], not_compared: list[str]) -> list[str]:
    """The plan's open issues after a stop: one line for the drafts (subject, choice) and one for the subjects not compared; one line each, so
    the reader lists' deduplication by meaning keeps every name."""
    why = reason.replace("_", " ")
    lines = [f"{DRAFT_ISSUE} ({why}): " + "; ".join(f"{subject} ({choice})" for subject, choice in drafts) + ". These are drafts, not final decisions."] if drafts else []
    return lines + ([f"{NOT_COMPARED_ISSUE} ({why}): " + "; ".join(not_compared) + "."] if not_compared else [])


def unresolved_decisions(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Every unresolved decision with its reason (the derivation's reason, else the comparison's rationale), in the one
    wording the plan's unresolved checks and the consistency sweep share."""
    require_guarded(conn)
    with derived(conn, project_id):
        rows = conn.execute("SELECT id, subject, rationale, derivation_reason FROM decisions WHERE project_id=? AND status='unresolved' ORDER BY id", (project_id,)).fetchall()
    return [{"id": did, "subject": subject, "reason": why or rationale, "line": f"Unresolved decision {did} ({subject}): {why or rationale}"} for did, subject, rationale, why in rows]


def settling_checks(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """The settling check of every unresolved decision, for the plan's unresolved checks."""
    require_guarded(conn)
    with derived(conn, project_id):
        rows = conn.execute("SELECT json_extract(memo_json, '$.settling_check') FROM decisions WHERE project_id=? AND status='unresolved' ORDER BY id", (project_id,)).fetchall()
    return [r[0] for r in rows if r[0]]


def write_report_into_stop(conn: sqlite3.Connection, project_id: str, unresolved_checks: list[str], pause_payload: dict, stop_reason: str) -> list[dict]:
    """A budget or time stop: the coverage report goes into the plan's unresolved_checks and the pause payload. The pause
    event itself is emitted by the caller; this function only prepares both payloads."""
    require_guarded(conn)
    report = coverage_report(conn, project_id)
    for line in report_lines(report):
        if line not in unresolved_checks:
            unresolved_checks.append(line)
    pause_payload["stop_reason"] = stop_reason
    pause_payload["coverage_report"] = report
    pause_payload["not_covered"] = len([r for r in report if not r.get("covered")])         # the covered subjects with a note are not counted as missing
    return report
