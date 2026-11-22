"""Targeted invalidation after a requirement change (preview, unlinked decisions, consistency sweep).
Tested offline (tests/test_workflow.py). apply_change is the one path that changes a requirement
(the founder's new value supersedes the row at the next requirement version, the dependent records go stale, the coverage is
invalidated at the new version); a revision run (app/graph/workflow.py, kind revision) re-decides the stale decisions afterwards."""
import json
import sqlite3
from app.core.ids import prefix_of
from app.core.sqlguard import require_guarded

STALE_TARGETS = {
    "DEC": ("decisions", "stale"),
    "CMP": ("components", "stale"),
    "TSK": ("tasks", "stale"),
    "ACC": ("acceptance_tests", "stale"),
}


def downstream(conn: sqlite3.Connection, project_id: str, start_id: str) -> set[str]:
    """All records reachable from start_id along trace_edges (direct and indirect)."""
    require_guarded(conn)
    seen: set[str] = set()
    frontier = [start_id]
    while frontier:
        node = frontier.pop()
        for (to_id,) in conn.execute("SELECT to_id FROM trace_edges WHERE project_id = ? AND from_id = ?", (project_id, node)):
            if to_id not in seen:
                seen.add(to_id)
                frontier.append(to_id)
    return seen


def unlinked_decisions(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Active decisions with no requirement edge. A missing link must never make a decision look unaffected."""
    require_guarded(conn)
    from app.core.coverage import derived
    require_guarded(conn)
    with derived(conn, project_id):                                                          # decision status is derived state: read inside the accessor's context
        return [r[0] for r in conn.execute(
            "SELECT d.id FROM decisions d WHERE d.project_id=? AND d.status='active' AND NOT EXISTS "
            "(SELECT 1 FROM trace_edges e WHERE e.project_id=d.project_id AND e.to_id=d.id AND e.kind='req_decision') ORDER BY d.id", (project_id,))]


def preview_impact(conn: sqlite3.Connection, project_id: str, changed_requirement_id: str) -> dict:
    """Read-only: what a change would touch. Shown to the founder before anything runs."""
    require_guarded(conn)
    direct = sorted(r[0] for r in conn.execute(
        "SELECT to_id FROM trace_edges WHERE project_id=? AND from_id=? AND kind='req_decision'", (project_id, changed_requirement_id)))
    reach = downstream(conn, project_id, changed_requirement_id)
    unlinked = [d for d in unlinked_decisions(conn, project_id) if d not in reach]
    for d in unlinked:
        reach |= {d} | downstream(conn, project_id, d)
    indirect = sorted(x for x in reach if x not in direct)
    return {"direct": direct, "indirect": indirect, "unlinked_rechecked": unlinked,
            "decisions": sorted(x for x in reach if prefix_of(x) == "DEC")}


def invalidate(conn: sqlite3.Connection, project_id: str, changed_requirement_id: str, include_unlinked: bool = True) -> dict[str, list[str]]:
    """Mark dependent decisions, components, tasks and tests stale in one transaction; unlinked decisions are re-checked too."""
    require_guarded(conn)
    affected = {prefix: [] for prefix in STALE_TARGETS}
    items = downstream(conn, project_id, changed_requirement_id)
    if include_unlinked:
        for d in unlinked_decisions(conn, project_id):
            items |= {d} | downstream(conn, project_id, d)
    def body():
        for item in sorted(items):
            prefix = prefix_of(item)
            if prefix in STALE_TARGETS:
                table, status = STALE_TARGETS[prefix]
                if table == "decisions":                                                       # a decision's status is derived; the stale proposal is the base record
                    conn.execute("UPDATE decisions SET proposed_status=? WHERE id=? AND project_id=?", (status, item, project_id))
                else:
                    conn.execute(f"UPDATE {table} SET status = ? WHERE id = ? AND project_id = ?", (status, item, project_id))
                affected[prefix].append(item)
        if affected["DEC"]:
            from app.core.reconcile import derive
            conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (project_id,))
            derive(conn, project_id)
    if conn.in_transaction:                                                                    # inside a caller's transaction (apply_change under the ask node) the writes join it; otherwise one transaction of its own
        body()
    else:
        with conn:
            body()
    return affected


def blocking_issues(issues: list[dict]) -> list[dict]:
    """The sweep's inconsistencies that keep a plan from being stored; unresolved decisions are reported, not blocking."""
    return [i for i in issues if i.get("kind") != "unresolved"]


def consistency_sweep(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Whole-plan check after any revision. Deterministic; the model review of flagged items comes after."""
    require_guarded(conn)
    from app.core.coverage import derived, unresolved_decisions
    issues = []
    with derived(conn, project_id):                                                          # the sweep reads derived state inside the accessor's context
        for (d,) in conn.execute(
                "SELECT DISTINCT e.to_id FROM trace_edges e JOIN requirements r ON r.id=e.from_id JOIN decisions d ON d.id=e.to_id "
                "WHERE e.project_id=? AND e.kind='req_decision' AND r.status='superseded' AND COALESCE(d.proposed_status, d.status)='active'", (project_id,)):   # the comparison still proposes it (base); a stale or rejected proposal is out
            issues.append({"id": d, "issue": "active decision still cites a superseded requirement"})
    for d in unresolved_decisions(conn, project_id):                                         # every unresolved decision with its reason, whatever the other filters say
        issues.append({"id": d["id"], "issue": "decision unresolved: " + d["reason"], "line": d["line"], "kind": "unresolved"})   # informational: carried in the plan's unresolved checks, never a plan inconsistency
    for d in unlinked_decisions(conn, project_id):
        issues.append({"id": d, "issue": "decision has no requirement link"})
    for (a,) in conn.execute("SELECT a.id FROM acceptance_tests a JOIN requirements r ON r.id=a.requirement_id "
                             "WHERE a.project_id=? AND r.status='superseded' AND a.status='current'", (project_id,)):
        issues.append({"id": a, "issue": "acceptance test checks a superseded requirement"})
    for (t,) in conn.execute("SELECT t.id FROM tasks t JOIN components c ON c.id=t.component_id WHERE t.project_id=? AND c.status='stale' AND t.status='current'", (project_id,)):
        issues.append({"id": t, "issue": "current task belongs to a stale component"})
    return issues


class ChangeRefused(ValueError):
    """The requirement cannot be changed through this path: it does not exist, belongs to another project or is superseded."""


def candidate_identities(conn: sqlite3.Connection, project_id: str, decision_ids: list[str]) -> dict[str, set[str]]:
    """The candidates of the re-opened decisions, by subject (normalised): the product identities of each decision's evidence packs
    (one pack per eligible candidate), of its memo's options and of the decision itself. A subject whose decisions record no
    identity maps to an empty set, which widens to every claim of the subject."""
    require_guarded(conn)
    from app.core import applicability as AP
    out: dict[str, set[str]] = {}
    if not decision_ids:
        return out
    marks = ",".join("?" * len(decision_ids))
    for subject, product_id, memo_json, packs_json in conn.execute(f"SELECT subject, product_id, memo_json, evidence_pack_json FROM decisions WHERE project_id=? AND id IN ({marks})", (project_id, *decision_ids)).fetchall():
        ids = out.setdefault(AP.norm(subject), set())
        if product_id:
            ids.add(product_id)
        for o in ((json.loads(memo_json) if memo_json else {}) or {}).get("options") or []:
            if o.get("product_id"):
                ids.add(o["product_id"])
        for pk in (json.loads(packs_json) if packs_json else []) or []:
            if isinstance(pk, dict) and pk.get("product_id"):
                ids.add(pk["product_id"])
    return out


def apply_change(conn: sqlite3.Connection, project_id: str, requirement_id: str, new_value, hard: bool | None = None) -> dict:
    """The founder's change of one requirement's value (the path every change takes, a confirmed
    requirement included). In one transaction: the preview is computed first (preview_impact, read-only), the current row is
    superseded by a new row of the same key with the founder's value exactly as given (never widened or narrowed), status
    confirmed, source user, the hard flag carried, at requirement version n+1; projects.requirement_version
    moves to n+1; every claim verified against an older version awaits re-verification (coverage.invalidate_requirement_change);
    the dependent decisions, components, tasks and tests and the unlinked decisions go stale (invalidate). Returns the preview,
    the old and new ids and values, the new version and the stale set for the revision run: the change's own stale decisions
    together with the project's decisions still marked stale from an earlier revision (a failed revision strands none).
    hard: the changed requirement's hardness as the founder states it (None carries the
    superseded row's flag); a hard changed requirement re-opens every current decision it binds (its map; every decision
    of the project when applies_to is empty), beside the decisions its recorded edges reach. The re-verification set of a
    non-budget requirement is bounded to the claims it binds on the candidates of the re-opened decisions (candidate_identities), with
    the remainder an earlier sweep left pending on those candidates, and the claims the re-opened decisions cite are listed first
    (reverify_cited) so the sweep decides those decisions first."""
    require_guarded(conn)
    from app.core import applicability as AP, coverage
    from app.repo import Repo
    from schemas import models as M
    has_map = "applies_to" in {r[1] for r in conn.execute("PRAGMA table_info(requirements)")}                                   # the map column (migration 056); a suite's fixture schema may lack it
    row = conn.execute(f"SELECT id, key, value_json, status, hard, version{', applies_to' if has_map else ''} FROM requirements WHERE id=? AND project_id=?", (requirement_id, project_id)).fetchone()
    if row is None:
        raise ChangeRefused(f"requirement {requirement_id!r} does not exist in project {project_id!r}")
    if row[3] == "superseded":
        raise ChangeRefused(f"requirement {requirement_id!r} is superseded; change the current row of key {row[1]!r}")
    preview = preview_impact(conn, project_id, requirement_id)
    applies_to = AP.applies_to_of({"applies_to": row[6]}) if has_map else None                                                  # the superseded row's map travels to the new row: the change applies where the old row applied
    req_map = {"key": row[1], "applies_to": applies_to}
    repo = Repo(conn)
    repeated = json.loads(row[2]) == new_value                                                   # a change to the value the current row already carries (the retry after a failed revision run) writes no row and bumps no version; the run re-decides what the earlier one left
    hard_flag = bool(row[4]) if hard is None else bool(hard)                                      # the founder's stated hardness, else the superseded row's flag
    with repo._tx():
        current_version = int(conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone()[0])
        new_version = current_version if repeated else current_version + 1
        new_id = requirement_id if repeated else repo.next_id(project_id, "REQ")
        if not repeated:
          repo.upsert_requirement(M.Requirement(id=new_id, project_id=project_id, key=row[1], value=new_value, status="confirmed", source="user", hard=hard_flag, version=new_version, supersedes=requirement_id, applies_to=applies_to))
          conn.execute("UPDATE requirements SET status='superseded' WHERE id=? AND project_id=?", (requirement_id, project_id))
        elif hard is not None and bool(row[4]) != hard_flag:
          conn.execute("UPDATE requirements SET hard=? WHERE id=? AND project_id=?", (1 if hard_flag else 0, requirement_id, project_id))   # a repeated change states the hardness of the row it reuses
        current = {r[0] for r in conn.execute("SELECT d.id FROM decisions d WHERE d.project_id=? AND NOT EXISTS (SELECT 1 FROM decisions n WHERE n.project_id=d.project_id AND n.subject=d.subject "
                                               "AND (n.plan_version > d.plan_version OR (n.plan_version = d.plan_version AND CAST(substr(n.id, instr(n.id, '.DEC-') + 5) AS INTEGER) > CAST(substr(d.id, instr(d.id, '.DEC-') + 5) AS INTEGER))))", (project_id,))}   # the revision re-decides a subject's current decision only (the latest by plan version, then the numeric suffix of the id: as text, P1.DEC-999 sorts after P1.DEC-1000); the earlier generations the edges reach are history, stale as they were, and re-deciding them would multiply the revision's work
        subject_of = {r[0]: r[1] for r in conn.execute("SELECT id, subject FROM decisions WHERE project_id=?", (project_id,)).fetchall()}
        bound = sorted(d for d in current if hard_flag and AP.binds(req_map, subject_of.get(d) or "")) if hard_flag else []   # a hard requirement re-opens every current decision it binds, not only those its recorded edges reach
        impact = sorted(set(preview["decisions"]) | set(bound))                                       # the impact set's claims are the claims the impact decisions cite and the claims whose support record names the changed requirement; every other verdict is carried
        cited = {r[0] for r in conn.execute(f"SELECT from_id FROM trace_edges WHERE project_id=? AND kind='claim_decision' AND to_id IN ({','.join('?' * len(impact)) or 'NULL'})", (project_id, *impact))}
        reverify = set(cited)
        reverify |= {r[0] for r in conn.execute("SELECT DISTINCT claim_id FROM claim_requirement_support WHERE project_id=? AND requirement_id=?", (project_id, requirement_id))}
        candidates: dict[str, set[str]] = {}
        if not AP.is_budget(req_map):                                                                 # the supported capability claims the changed requirement binds on the candidates of the re-opened decisions (the subjects the map names; every subject when it binds all), so no claim's version advances without a verdict at that version; a budget is arithmetic over the cited price claims and widens nothing
            rq_subject = {r[0]: r[1] for r in conn.execute("SELECT id, decision_subject FROM research_questions WHERE project_id=?", (project_id,))}
            bound_rqs = {rq for rq, subj in rq_subject.items() if AP.binds(req_map, subj or "")}
            via_rq = {cid for rq, cj in conn.execute("SELECT subject_id, claim_ids_json FROM coverage WHERE project_id=? AND subject_type='research_question'", (project_id,)) if rq in bound_rqs for cid in json.loads(cj or "[]")}
            candidates = candidate_identities(conn, project_id, [d for d in impact if d in current])
            def on_candidate(subj: str, product_id: str | None) -> bool:
                ids = candidates.get(AP.norm(subj or ""))
                return not ids or (product_id or "") in ids                                            # a subject whose re-opened decisions record no identity keeps the whole set
            reverify |= {cid for cid, subj, prod in conn.execute("SELECT id, decision_subject, product_id FROM claims WHERE project_id=? AND kind='capability' AND verdict='supported'", (project_id,)) if (cid in via_rq or AP.binds(req_map, subj or "")) and on_candidate(subj, prod)}
            reverify |= {cid for cid, subj, prod in conn.execute("SELECT id, decision_subject, product_id FROM claims WHERE project_id=? AND verdict IS NULL AND invalidated_at IS NOT NULL", (project_id,)) if (cid in via_rq or AP.binds(req_map, subj or "")) and on_candidate(subj, prod)}   # the remainder an earlier sweep left pending on these candidates, whatever the claims' kind, continues here instead of starting over
        if repeated:                                                                                  # a repeated change never clears a verdict already given at the current requirement version (the earlier run's re-verification stands); only the claims still awaiting it are in the set
            at_current = {r[0] for r in conn.execute("SELECT id FROM claims WHERE project_id=? AND verdict IS NOT NULL AND verified_requirement_version=?", (project_id, new_version))}
            reverify = {c for c in reverify if c not in at_current}
        coverage.invalidate_requirement_change(conn, project_id, new_version, reverify=reverify, changed_requirement_id=requirement_id)
        coverage.ensure_subject(conn, project_id, "requirement", new_id, new_version)
        affected = invalidate(conn, project_id, requirement_id)
        affected["DEC"] = [d for d in affected["DEC"] if d in current]
        for d in bound:                                                                               # the bound decisions go stale like the reached ones (the proposal is the base record; the derivation follows)
            if d not in affected["DEC"]:
                conn.execute("UPDATE decisions SET proposed_status='stale' WHERE id=? AND project_id=?", (d, project_id)); affected["DEC"].append(d)
        if bound:
            from app.core.reconcile import derive
            conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (project_id,))
            derive(conn, project_id)
        carried = [r[0] for r in conn.execute("SELECT s.id FROM decisions s WHERE s.project_id=? AND s.proposed_status='stale' AND NOT EXISTS "
                                               "(SELECT 1 FROM decisions n WHERE n.project_id=s.project_id AND n.subject=s.subject AND n.plan_version>s.plan_version AND COALESCE(n.proposed_status,'active')<>'stale') ORDER BY s.id", (project_id,))
                   if r[0] not in affected["DEC"]]   # a decision left stale by an earlier revision that no later decision of its subject re-decided (a failed revision's) stays in the authoritative set
        affected["DEC"] = sorted((set(affected["DEC"]) | set(carried)) & current)
    return {"preview": preview, "requirement_key": row[1], "old_id": requirement_id, "new_id": new_id, "old_value": json.loads(row[2]), "new_value": new_value,
            "requirement_version": new_version, "hard": hard_flag, "hard_stated": hard is not None, "applies_to": applies_to, "bound": bound, "stale": affected, "reverify": sorted(reverify),
            "reverify_cited": sorted(c for c in cited if c in reverify), "candidates": {k: sorted(v) for k, v in candidates.items()}, "repeated": repeated}
