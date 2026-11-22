"""The export snapshot of a plan version: the complete record of one version, written once and signed (the contract: schemas/export_models.py).

build() reads the store inside the transaction that creates the plan version and returns the ExportSnapshot of that version:
  the project and plan metadata, the runs that produced it;
  every requirement of the project with its version, the assumptions, the answered questions, the plan body;
  every decision with its canonical product identity, its citations, its requirement-support records, the comparison's options
  as the ledger stored them (the alternatives), the tree nodes and the rejected options;
  the components, tasks and acceptance tests of this version, the trace edges;
  the claims used (every claim referenced anywhere: by a decision, an alternative, a tree node, a coverage line, a derived row)
  in full with their quotes, the sources of those claims without page text;
  the coverage of every subject with both statuses, the derived tables, the label dictionary and the optional sections
  (deployment, code examples) as empty lists.

validate() enforces the rules the tests prove: the contract, closure (every reference resolves to a record inside the snapshot),
isolation (every record belongs to the project), version match (the plan's version and requirement version are the row's and
every record version is at or below it).

signature() is the sha256 of the canonical JSON (sorted keys, no whitespace). A snapshot is written once by Repo.set_snapshot and
never changes; a later change to any record is a new plan version.

render_markdown() renders the Markdown export of a version from the snapshot alone, nothing else is read; a version without a
snapshot ("no snapshot", created before snapshots existed) has none and keeps whatever export it had."""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3

from app.core import docrules
from app.core.coverage import coverage_report, derived
from app.core.discovery import display_label, split_label
from app.core.sqlguard import require_guarded
from schemas.export_models import validate_snapshot

LABELS = {"sourced_fact": "sourced fact", "assumption": "assumption", "calculated_estimate": "calculated estimate", "measurement": "measurement",
          "founder_decision": "founder decision", "planning_proposal": "planning proposal", "illustration": "illustration",
          "tested_code": "tested code", "untested_example": "untested example", "pseudocode": "pseudocode"}
NO_SNAPSHOT = "no snapshot"


class SnapshotError(ValueError):
    """The snapshot cannot be created: a reference that does not resolve, a record of another project, a version mismatch, a shape
    outside the contract. The plan version's transaction rolls back with it."""


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def signature(snapshot: dict) -> str:
    return hashlib.sha256(canonical(snapshot).encode("utf-8")).hexdigest()


def _j(value):
    return json.loads(value) if value else None


def _spend_record(sp: dict) -> dict:
    """A run's spend as the snapshot records it, from the ledger's report (run_spend)."""
    return {"billed": float(sp.get("billed_usd", 0.0) or 0.0), "committed": round(float(sp.get("billed_usd", 0.0) or 0.0) + float(sp.get("estimated_unreported_usd", 0.0) or 0.0) + float(sp.get("held_estimates_usd", 0.0) or 0.0), 6),
            "possible": float(sp.get("possible_charges_usd", 0.0) or 0.0)}


def runs_now(conn: sqlite3.Connection, project_id: str, snapshot: dict) -> list[dict]:
    """The runs a version's snapshot names, each as the store holds it now: its
    status, its stop reason, its start, its end and its spend. The snapshot's own rows are written inside the transaction that stores the
    version, while the run that writes it is still running, and they are signed: they stay as they are. The archive's Runs table is a record
    of the runs, read at the download. A run the store no longer holds keeps its snapshot row."""
    require_guarded(conn)
    from app.core.ledger import run_spend
    out = []
    for r in snapshot.get("run") or []:
        row = conn.execute("SELECT status, stop_reason, started_at, ended_at, kind FROM runs WHERE id=? AND project_id=?", (r["id"], project_id)).fetchone()
        out.append(dict(r) if row is None else {**r, "status": row[0], "stop_reason": row[1], "started_at": row[2], "ended_at": row[3], "kind": row[4], "spend": _spend_record(run_spend(conn, r["id"]))})
    return out


# ---------------------------------------------------------------- the build
def build(conn: sqlite3.Connection, project_id: str, version: int, *, run_id: str, component_ids: list[str], task_ids: list[str], test_ids: list[str],
          spend: dict | None = None, change_summary: str | None = None) -> dict:
    """The ExportSnapshot of plan version `version` of `project_id` as the store holds it now: called inside the transaction that
    stores the version (after the plan_versions row and the version's components, tasks and tests are written) with the ids of the
    records this version created. The result is validated; a SnapshotError fails the version creation."""
    require_guarded(conn)
    r = conn.execute("SELECT version, requirement_version, created_at, change_summary, diff_json FROM plan_versions WHERE project_id=? AND version=?", (project_id, int(version))).fetchone()
    if r is None:
        raise SnapshotError(f"plan version {version} of project {project_id!r} does not exist")
    row = {"version": r[0], "requirement_version": r[1], "created_at": r[2], "change_summary": r[3], "diff_json": r[4]}
    snapshot = _collect(conn, project_id, int(version), row, run_id=run_id, component_ids=list(component_ids), task_ids=list(task_ids), test_ids=list(test_ids), spend=spend or {}, change_summary=change_summary)
    validate(snapshot, project_id=project_id, version=int(version), requirement_version=int(row["requirement_version"]))
    return snapshot


def _collect(conn, pid: str, version: int, row: dict, *, run_id: str, component_ids: list[str], task_ids: list[str], test_ids: list[str], spend: dict, change_summary: str | None) -> dict:
    from app.core.ledger import run_spend
    with derived(conn, pid):                                                                  # decision, claim, support and coverage statuses are derived state: every read of this build sits inside the accessor's context
        proj = conn.execute("SELECT id, title, privacy_mode, created_at FROM projects WHERE id=?", (pid,)).fetchone()
        idea = conn.execute("SELECT data_json FROM run_events WHERE run_id IN (SELECT id FROM runs WHERE project_id=? AND kind='initial') AND event='step' AND data_json LIKE '%\"Run started\"%' ORDER BY id LIMIT 1", (pid,)).fetchone()
        body = _j(row["diff_json"]) or {}
        plan = {"id": f"{pid}.PLAN-v{version}", "version": version, "requirement_version": int(row["requirement_version"]), "created_at": row["created_at"], "change_summary": change_summary if change_summary is not None else row["change_summary"]}
        runs = []
        seen = set(); rid = run_id
        while rid and rid not in seen:
            seen.add(rid)
            r = conn.execute("SELECT id, status, started_at, ended_at, kind, parent_run_id FROM runs WHERE id=? AND project_id=?", (rid,  pid)).fetchone()
            if r is None:
                break
            sp = spend if r[0] == run_id else run_spend(conn, r[0])                          # a parent run's spend from its own ledger, as the current run's is, never a literal zero
            runs.append({"id": r[0], "status": r[1], "started_at": r[2], "ended_at": r[3], "kind": r[4], "parent_run_id": r[5], "spend": _spend_record(sp)})
            rid = r[5]
        requirements = [{"id": r[0], "key": r[1], "value": _j(r[2]), "status": r[3], "source": r[4], "hard": bool(r[5]), "version": int(r[6]), "supersedes": r[7]}
                        for r in conn.execute("SELECT id, key, value_json, status, source, hard, version, supersedes FROM requirements WHERE project_id=? ORDER BY id", (pid,))]
        assumptions = [{"id": r[0], "text": r[1], "impact": r[2], "status": r[3]} for r in conn.execute("SELECT id, text, impact, status FROM assumptions WHERE project_id=? ORDER BY id", (pid,))]
        questions = [{"id": r[0], "text": r[1], "answer": _j(r[2]), "date": None, "kind": r[3], "requirement_key": r[4]}
                     for r in conn.execute("SELECT id, text, answer_json, kind, requirement_key FROM questions WHERE project_id=? AND status='answered' ORDER BY id", (pid,))]
        plan_body = {k: body.get(k, [] if k != "problem" else "") for k in ("problem", "users", "scope_in", "scope_out", "user_flow", "risks", "unresolved_checks")}
        if body.get("user_flow_labels"):                                                     # the key exists only where the version holds labels, so the snapshot of a version without them is unchanged
            plan_body["user_flow_labels"] = list(body["user_flow_labels"])
        edges = [{"from_id": r[0], "to_id": r[1], "kind": r[2]} for r in conn.execute("SELECT from_id, to_id, kind FROM trace_edges WHERE project_id=? ORDER BY kind, from_id, to_id", (pid,))]
        decisions = []
        has_run = "run_id" in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}        # the run that wrote the decision (migration 075)
        has_hard_check = "hard_check_json" in {r[1] for r in conn.execute("PRAGMA table_info(tree_nodes)")}   # what the node's hard check read (migration 078)
        has_pack = "evidence_pack_json" in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        cuts = _CutReader(conn, pid, requirements, int(row["requirement_version"]))                # the exclusions and the check dates of every decision, from the records
        for d in conn.execute(f"SELECT id, subject, choice, rationale, importance, product_id, product_version, status, plan_version, rejected_json, unresolved_json{', run_id' if has_run else ', NULL'} FROM decisions WHERE project_id=? ORDER BY id", (pid,)).fetchall():
            did = d[0]
            cited = sorted(e["from_id"] for e in edges if e["to_id"] == did and e["kind"] == "claim_decision")
            req_ids = sorted(e["from_id"] for e in edges if e["to_id"] == did and e["kind"] == "req_decision")
            support = []
            if cited and req_ids:
                q = f"SELECT claim_id, requirement_id, requirement_version, verdict, status, reason FROM claim_requirement_support WHERE project_id=? AND claim_id IN ({','.join('?' * len(cited))}) AND requirement_id IN ({','.join('?' * len(req_ids))}) ORDER BY requirement_id, claim_id, requirement_version"
                support = [{"claim_id": s[0], "requirement_id": s[1], "requirement_version": int(s[2]), "verdict": s[3], "status": s[4], "reason": s[5]} for s in conn.execute(q, (pid, *cited, *req_ids))]
            alternatives, dropped = _alternatives(conn, pid, did, cited)
            for x in dropped:                                                                  # an id that is no claim of the project on a non-picked option is dropped and recorded, never a refusal of the version; the event joins the version's transaction and rolls back with it
                from app.runner import events as ev
                ev.step(conn, run_id, "plan_generate", "Unresolved claim ids dropped from an alternative", f"{did} {x['option']!r}: {x['count']} id(s) that are no claim of the project: {', '.join(x['unresolved'])}",
                        decision=did, option=x["option"], unresolved=x["unresolved"], count=x["count"])
            hc_col = ", t.hard_check_json" if has_hard_check else ", NULL"
            if d[11]:                                                                            # the decision's own tree: the tree of the run that wrote it (its highest version), and none when that run explored no tree for the subject
                node_rows = conn.execute(f"SELECT t.id, t.parent_id, t.label, t.depth, t.score, t.hard_status, t.pruned, t.reason, t.claim_ids_json{hc_col} FROM tree_nodes t WHERE t.run_id=? AND t.decision_subject=? AND t.version=(SELECT MAX(version) FROM tree_nodes WHERE run_id=? AND decision_subject=?) ORDER BY t.depth, t.id", (d[11], d[1], d[11], d[1])).fetchall()
            else:                                                                                # an older store's decision without a run id: the subject's latest tree version across runs
                node_rows = conn.execute(f"SELECT t.id, t.parent_id, t.label, t.depth, t.score, t.hard_status, t.pruned, t.reason, t.claim_ids_json{hc_col} FROM tree_nodes t JOIN runs r ON r.id=t.run_id WHERE r.project_id=? AND t.decision_subject=? ORDER BY t.version DESC, t.depth, t.id", (pid, d[1])).fetchall()
            nodes = [{"id": n[0], "parent": n[1], "label": n[2], "depth": int(n[3]), "score": n[4], "hard_status": n[5], "pruned": bool(n[6]), "reason": n[7], "claim_ids": [str(c) for c in (_j(n[8]) or [])],
                      "hard_check": [x for x in (_j(n[9]) or []) if isinstance(x, dict)]}        # every reference kept; one that does not resolve fails the build; hard_check: what the node's hard check read
                     for n in node_rows]
            if nodes and not d[11]:
                top = nodes[0]
                nodes = [n for n in nodes if conn.execute("SELECT version FROM tree_nodes WHERE id=?", (n["id"],)).fetchone()[0] == conn.execute("SELECT version FROM tree_nodes WHERE id=?", (top["id"],)).fetchone()[0]]   # one snapshot per decision subject: the latest version
            memo = _j((conn.execute("SELECT memo_json FROM decisions WHERE id=?", (did,)).fetchone() or [None])[0]) if "memo_json" in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")} else None
            memo = memo if isinstance(memo, dict) else None
            from app.core import coverage_step as _CS
            memo = _CS.with_conditional(conn, pid, did, memo)                                    # a conditional recommendation carries its mark (the unproven requirements, the sentence, the confirming check) in the snapshot's memo, whatever wrote the decision
            packs = _j((conn.execute("SELECT evidence_pack_json FROM decisions WHERE id=?", (did,)).fetchone() or [None])[0]) if has_pack else None
            own_version = (d[8] is not None and int(d[8]) == version) or d[7] in ("active", "unresolved")   # the version's own decisions and the live ones carried into it; an earlier version's superseded draft keeps the rows a run recorded and gains none derived
            exclusions = cuts.rows(did, d[1], d[11], nodes, packs if isinstance(packs, list) else None, packs is not None, derive=own_version)   # every cut of this decision's weighing, per stage
            decisions.append({"id": did, "subject": d[1], "choice": d[2], "rationale": d[3], "importance": d[4], "product_id": d[5], "product_version": d[6], "status": d[7], "plan_version": d[8],
                              "claim_ids": cited, "requirement_ids": req_ids, "requirement_support": support, "alternatives": alternatives, "tree_nodes": nodes,
                              "rejected": [{"label": x.get("option", ""), "reason": x.get("reason", "")} for x in (_j(d[9]) or [])], "unresolved": list(_j(d[10]) or []), "memo": memo,
                              "exclusions": exclusions, "check_dates": cuts.check_dates(memo, req_ids, d[5], d[6])})   # the exclusions and, per memo option, the verdict's date and the claims' newest checked_at
        components, comp_key = [], {}
        for c in conn.execute(f"SELECT id, name, responsibility, data_leaves_device, product_id, product_version FROM components WHERE project_id=? AND id IN ({','.join('?' * len(component_ids)) or 'NULL'}) ORDER BY id", (pid, *component_ids)):
            comp_key[c[0]] = c[0]
            components.append({"id": c[0], "key": c[0], "name": c[1], "responsibility": c[2], "data_leaves_device": bool(c[3]), "product_id": c[4], "product_version": c[5]})
        tasks = []
        for t in conn.execute(f"SELECT id, component_id, title, depends_on_json, effort_low_h, effort_high_h FROM tasks WHERE project_id=? AND id IN ({','.join('?' * len(task_ids)) or 'NULL'}) ORDER BY id", (pid, *task_ids)):
            low, high = t[4], t[5]
            text = f"{_num(low)} to {_num(high)} hours" if low is not None and high is not None else ("not estimated" if low is None and high is None else f"{_num(low if low is not None else high)} hours")
            tasks.append({"id": t[0], "key": t[0], "title": t[2], "component_key": comp_key.get(t[1], t[1]), "depends_on": list(_j(t[3]) or []), "effort": {"min": low, "max": high, "unit": "hours", "text": text}})
        tests = [{"id": a[0], "task_id": a[1], "requirement_id": a[2], "criterion": a[3]}
                 for a in conn.execute(f"SELECT id, task_id, requirement_id, criterion FROM acceptance_tests WHERE project_id=? AND id IN ({','.join('?' * len(test_ids)) or 'NULL'}) ORDER BY id", (pid, *test_ids))]
        cov_rows = conn.execute("SELECT subject_type, subject_id, evidence_status, processing_status, claim_ids_json, source_versions_json, requirement_version, derived_claim_ids_json FROM coverage WHERE project_id=? ORDER BY subject_type, subject_id", (pid,)).fetchall()
        report = {r["subject_id"]: r for r in coverage_report(conn, pid)}
        rq_rows = {r[0]: {"id": r[0], "decision_subject": r[1], "target_fact": r[2], "query_text": r[3], "run_id": r[4]} for r in conn.execute("SELECT id, decision_subject, target_fact, query_text, run_id FROM research_questions WHERE project_id=? ORDER BY id", (pid,))}
        req_key = {r["id"]: r["key"] for r in requirements}
        coverage = []
        for st, sid, ev_, pr, cj, sj, rv, dcj in cov_rows:
            ids = list(_j(dcj if st == "requirement" else cj) or [])
            label = req_key.get(sid, sid) if st == "requirement" else (f"{rq_rows[sid]['decision_subject']}: {rq_rows[sid]['target_fact']}" if sid in rq_rows else sid)
            reasons = report[sid]["reasons"] if sid in report else []
            coverage.append({"subject_type": st, "subject_id": sid, "subject": label, "evidence_status": ev_, "processing_status": pr, "reason": " | ".join(reasons), "claim_ids": ids,
                             "source_versions": [{"source_id": x.get("source_id"), "source_version": x.get("source_version")} for x in (_j(sj) or []) if isinstance(x, dict)], "requirement_version": int(rv)})
        # claims used: every claim referenced anywhere (decisions, alternatives, tree nodes, coverage, derived rows); sources used: theirs and the coverage lines'
        used = set()
        for d in decisions:
            used.update(d["claim_ids"]); used.update(s["claim_id"] for s in d["requirement_support"] if s.get("claim_id"))
            for a in d["alternatives"]:
                used.update(a["claim_ids"])
            for n in d["tree_nodes"]:
                used.update(n["claim_ids"])
            for x in d["exclusions"]:                                                          # the record a cut rests on is a record of the snapshot (closure)
                used.update(i for i in x["record_ids"] if _prefix_of(i) == "CLM")
        for c in coverage:
            used.update(c["claim_ids"])
        text_refs = _text_references(plan_body, decisions)                                        # a record named in an unresolved check, a decision's finding or a rejection reason is a reference
        used.update(i for i in text_refs if _prefix_of(i) == "CLM")
        derived_tables = [{"id": r[0], "decision_id": r[7], "title": r[1], "fields": list(_j(r[2]) or []), "units": dict(_j(r[3]) or {}), "labels": dict(_j(r[4]) or {}), "rows": list(_j(r[5]) or []), "claim_ids": [list(x) for x in (_j(r[6]) or [])]}
                          for r in conn.execute(f"SELECT id, title, fields_json, units_json, labels_json, rows_json, claim_ids_json, decision_id FROM derived_tables WHERE project_id=? AND decision_id IN ({','.join('?' * len(decisions)) or 'NULL'}) ORDER BY id", (pid, *[d["id"] for d in decisions]))]   # the cost tables of the version's decisions, computed by app/core/derived.py in the comparison
        for t in derived_tables:
            for cids in t["claim_ids"]:
                used.update(cids)
        claims = [{"id": c[0], "text": c[1], "kind": c[2], "subject": c[3], "attribute": c[4], "value": c[5], "conditions": list(_j(c[6]) or []), "status": c[7], "scope_status": c[8], "quote": c[9],
                   "source_id": c[10], "source_version": c[11], "range": (f"characters {c[12]} to {c[13]}" if c[12] is not None and c[13] is not None else None), "product_id": c[14], "product_version": c[15], "checked_at": c[16], "fresh_until": c[17]}
                  for c in conn.execute(f"SELECT id, text, kind, subject, attribute, value, conditions_json, status, scope_status, quote, source_id, source_version, range_start, range_end, product_id, product_version, checked_at, fresh_until FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(used)) or 'NULL'}) ORDER BY id", (pid, *sorted(used)))]
        support_all = [{"claim_id": s[0], "requirement_id": s[1], "requirement_version": int(s[2]), "verdict": s[3], "status": s[4], "reason": s[5]}
                       for s in conn.execute(f"SELECT claim_id, requirement_id, requirement_version, verdict, status, reason FROM claim_requirement_support WHERE project_id=? AND claim_id IN ({','.join('?' * len(used)) or 'NULL'}) ORDER BY claim_id, requirement_id, requirement_version", (pid, *sorted(used)))]
        src_ids = {c["source_id"] for c in claims if c["source_id"]} | {x["source_id"] for c in coverage for x in c["source_versions"] if x.get("source_id")} | {i for i in text_refs if _prefix_of(i) == "SRC"}
        other_versions = {r[0] for t in ("components", "tasks", "acceptance_tests") for r in conn.execute(f"SELECT id FROM {t} WHERE project_id=?", (pid,))} - set(component_ids) - set(task_ids) - set(test_ids)
        edges = [e for e in edges if e["from_id"] not in other_versions and e["to_id"] not in other_versions]   # the trace edges of this version: an edge to a component, task or test of an earlier version of this project is that version's history; every other endpoint stays and must resolve (a foreign or dangling id fails the build)
        has_found = "found_by_json" in {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
        sources = [{"id": s[0], "url": s[1], "canonical_url": s[2], "title": s[3], "publisher": s[4], "tier": s[5], "published_at": s[6], "retrieved_at": s[7], "freshness_class": s[8], "fetch_status": s[9], "source_version": s[10],
                    **({"found_by": _json_list(s[11])} if has_found and s[11] else {})}
                   for s in conn.execute(f"SELECT id, url, canonical_url, title, publisher, tier, published_at, retrieved_at, freshness, fetch_status, content_hash{', found_by_json' if has_found else ', NULL'} FROM sources WHERE project_id=? AND id IN ({','.join('?' * len(src_ids)) or 'NULL'}) ORDER BY id", (pid, *sorted(src_ids)))]
    idea_text = ""
    if idea:
        idea_text = ""                                                                        # the idea is not stored as a record: the run's start event carries ids only
    extra = _phase9a_records(conn, pid, version, row, set(c["key"] for c in components), run_ids=[r["id"] for r in runs])
    return {"project": {"id": proj[0], "title": proj[1], "idea": idea_text or proj[1], "created_at": proj[3], "privacy_mode": proj[2]},
            "plan": plan, "run": runs, "requirements": requirements, "assumptions": assumptions, "questions": questions, "plan_body": plan_body,
            "decisions": decisions, "components": components, "tasks": tasks, "acceptance_tests": tests, "trace_edges": edges,
            "claims": claims, "sources": sources, "coverage": coverage, "derived": derived_tables, "labels": dict(LABELS),
            "research_questions": [rq_rows[k] for k in sorted(rq_rows)], "claim_requirement_support": support_all,
            "component_relations": extra["component_relations"], "deployment": [], "interfaces": extra["interfaces"], "entities": extra["entities"], "code_examples": [],
            "verification": extra["verification"], "first_week": extra["first_week"], "usage": extra["usage"], "budget": extra["budget"],
            "lead": extra.get("lead"), "editor": extra.get("editor"), "candidates": extra.get("candidates") or [],
            "document_rules": docrules.CURRENT}                                               # the version is composed under the rules it was written under, so an approval keeps covering the documents as approved


def _phase9a_records(conn, pid: str, version: int, row: dict, component_keys: set, run_ids: list[str] | None = None) -> dict:
    """The planner enrichment rows of this version (entities, interfaces, component relations), the verification plan,
    the first week and the budget of plan_versions, and the usage question's four numbers; empty when the store holds none (an older version).
    Also the kept typed candidates of the version's runs (the run and its parents), the universe a reader sentence may name."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = {"entities": [], "interfaces": [], "component_relations": [], "verification": [], "first_week": [], "usage": None, "budget": None, "candidates": []}
    if "candidates" in tables:
        typed = "kind" in {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}          # the identity fields beside the label (migration 074); a store from before the migration reads them empty
        rows_c = conn.execute(f"SELECT subject, label, product_class, run_id{', kind, vendor, product, version, access_route, urls_json' if typed else ''} FROM candidates WHERE project_id=? AND status='kept' ORDER BY subject, label", (pid,)).fetchall()
        mine = [r for r in rows_c if r[3] in set(run_ids or [])] or rows_c                   # the version's runs; every kept row of an older store without a matching run
        seen = set()
        for r in mine:
            vendor_c, product_c, version_c = ((r[5] or ""), (r[6] or ""), (r[7] or "")) if typed else ("", "", "")
            shown = (display_label(vendor_c, product_c, version_c) if (vendor_c or product_c) else display_label(*split_label(r[1])))[:120] or r[1]   # the catalogue's label is composed at the read, from the identity fields, or from the stored label split into vendor and product for a row an older run wrote (migration 074 keeps those labels): no repeated vendor, no quoting character; two rows that compose to one label are one entry
            key = (r[0], shown)
            if key in seen:
                continue
            seen.add(key)
            out["candidates"].append({"id": f"{r[0]}:{shown}", "subject": r[0], "label": shown, "class": r[2], "aliases": [],
                                      "kind": (r[4] or "product") if typed else "product", "vendor": (r[5] or "") if typed else "", "product": (r[6] or "") if typed else "", "version": (r[7] or "") if typed else "",
                                      "access_route": (r[8] or "") if typed else "", "urls": list(_j(r[9]) or []) if typed else []})
    if "entities" in tables:
        out["entities"] = [{"id": r[0], "name": r[1], "fields": list(_j(r[2]) or []), "relations": list(_j(r[3]) or []), "status_label": r[4]} for r in conn.execute("SELECT id, name, fields_json, relations_json, status_label FROM entities WHERE project_id=? AND plan_version=? ORDER BY id", (pid, version))]
    if "interfaces" in tables:
        out["interfaces"] = [{"id": r[0], "name": r[1], "from_key": r[2], "to_key": r[3], "direction": r[4], "kind": r[5], "description": r[6], "steps": list(_j(r[7]) or []), "status_label": r[8]}
                             for r in conn.execute("SELECT id, name, from_key, to_key, direction, kind, description, steps_json, status_label FROM interfaces WHERE project_id=? AND plan_version=? ORDER BY id", (pid, version)) if r[2] in component_keys and r[3] in component_keys]
    if "component_relations" in tables:
        out["component_relations"] = [{"id": r[0], "from_key": r[1], "to_key": r[2], "kind": r[3], "label": r[4], "decision_ids": list(_j(r[5]) or []), "claim_ids": list(_j(r[6]) or []), "status_label": r[7]}
                                      for r in conn.execute("SELECT id, from_key, to_key, kind, label, decision_ids_json, claim_ids_json, status_label FROM component_relations WHERE project_id=? AND plan_version=? ORDER BY id", (pid, version))
                                      if r[1] in component_keys and r[2] in component_keys and (_j(r[5]) or _j(r[6]))]
    if "verification_checks" in tables:
        with_result = "result" in {r[1] for r in conn.execute("PRAGMA table_info(verification_checks)")}   # the pass record of a run of the product on the founder's own input (migration 079)
        out["verification"] = [{"id": r[0], "check": r[1], "how": r[2], "pass": r[3], "cost": r[4], "requirement_id": r[5], "risk": r[6], "result": r[7], "result_at": r[8], "result_product_id": r[9], "result_product_version": r[10]}
                               for r in conn.execute(f"SELECT id, check_text, how, pass_text, cost, requirement_id, risk_text{', result, result_at, result_product_id, result_product_version' if with_result else ', NULL, NULL, NULL, NULL'} FROM verification_checks WHERE project_id=? AND plan_version=? ORDER BY id", (pid, version))]
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plan_versions)")}
    if {"first_week_json", "budget_json"} <= cols:
        r = conn.execute("SELECT first_week_json, budget_json FROM plan_versions WHERE project_id=? AND version=?", (pid, version)).fetchone()
        if r:
            out["first_week"] = list(_j(r[0]) or []); out["budget"] = _j(r[1]) if r[1] else None
    if {"lead_json", "editor_json"} <= cols:                                                 # the lead and the Editor's record of the version
        r = conn.execute("SELECT lead_json, editor_json FROM plan_versions WHERE project_id=? AND version=?", (pid, version)).fetchone()
        if r:
            out["lead"] = _j(r[0]) if r[0] else None; out["editor"] = _j(r[1]) if r[1] else None
    qcols = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
    if "usage_json" in qcols:
        from app.core.derived import USAGE_SCOPE_QUESTION, LENGTH_QUESTION_LIKE
        q = conn.execute("SELECT answer_json, usage_json FROM questions WHERE project_id=? AND kind='usage' AND status='answered' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        stated_row = conn.execute("SELECT answer_json FROM questions WHERE project_id=? AND kind='usage' AND status='answered' AND text <> ? AND text NOT LIKE ? ORDER BY id DESC LIMIT 1", (pid, USAGE_SCOPE_QUESTION, LENGTH_QUESTION_LIKE)).fetchone()   # the founder's figures: not the scope answer, nor the length answer (in any item word)
        if q:
            numbers = _j(q[1]) if q[1] else None
            if numbers is None:
                from app.core.derived import parse_usage_numbers
                numbers = parse_usage_numbers(_j(q[0]))
            if numbers:
                from app.core.budget import usage_from_numbers
                u = usage_from_numbers(numbers)
                stated = _j(stated_row[0]) if stated_row and stated_row[0] else _j(q[0])
                out["usage"] = {**numbers, "hours": u["hours"], "stated": stated if isinstance(stated, str) else None, "assumptions": u["assumptions"]}
    return out


class _CutReader:
    """The exclusions and the check dates of a decision, from the records the build reads: the rows a run recorded
    (candidate_exclusions), the evidence pack's withheld claims, the pruned nodes with their hard check records, and for a decision of an
    older store the eligible set's refusals derived read-only (source export) and the question-status cut of older runs (source history,
    only when no evidence pack was stored). The subject's claims are selected as the comparison selects them (workflow.subject_claims), once per subject."""

    def __init__(self, conn, pid: str, requirements: list[dict], requirement_version: int):
        require_guarded(conn)
        self.conn, self.pid, self.rv = conn, pid, requirement_version
        self.hard = {r["id"]: r for r in requirements if r.get("hard") and r.get("status") != "superseded"}
        self._claims = None; self._rel: dict = {}; self._rq = None

    def _all_claims(self) -> list[dict]:
        if self._claims is None:
            cols = ("id", "text", "kind", "source_id", "status", "scope_status", "conditions_json", "subject", "attribute", "value", "decision_subject", "product_id", "product_version", "checked_at")
            with derived(self.conn, self.pid):                                                  # the claim status is derived state: read inside the accessor's context
                self._claims = [dict(zip(cols, r)) for r in self.conn.execute("SELECT " + ", ".join(cols) + " FROM claims WHERE project_id=? ORDER BY id", (self.pid,)).fetchall()]
        return self._claims

    def _questions(self) -> tuple[dict, dict, dict]:
        if self._rq is None:
            rq_claims: dict = {}
            with derived(self.conn, self.pid):                                                  # the coverage statuses are derived state
                for rq, cj in self.conn.execute("SELECT subject_id, claim_ids_json FROM coverage WHERE project_id=? AND subject_type='research_question'", (self.pid,)).fetchall():
                    for cid in _j(cj) or []:
                        rq_claims.setdefault(cid, set()).add(rq)
                rq_subject = {r[0]: r[1] for r in self.conn.execute("SELECT id, decision_subject FROM research_questions WHERE project_id=?", (self.pid,))}
                rq_status = {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT subject_id, evidence_status, processing_status FROM coverage WHERE project_id=? AND subject_type='research_question'", (self.pid,)).fetchall()}
            self._rq = (rq_claims, rq_subject, rq_status)
        return self._rq

    def rel(self, subject: str) -> list[dict]:
        if subject not in self._rel:
            from app.graph.workflow import subject_claims                                       # lazy: the workflow imports this module
            rq_claims, rq_subject, _ = self._questions()
            subjects = [r[0] for r in self.conn.execute("SELECT DISTINCT subject FROM decisions WHERE project_id=?", (self.pid,))]
            self._rel[subject] = subject_claims(self.conn, self.pid, subject, self._all_claims(), rq_claims, rq_subject, subjects)
        return self._rel[subject]

    def rows(self, did: str, subject: str, run_id: str | None, nodes: list[dict], packs: list | None, has_pack: bool, derive: bool = True) -> list[dict]:
        from app.core import exclusions as EX
        rel = self.rel(subject)
        by_claim = {c["id"]: c for c in rel}
        claim_ids_of = lambda p0, v0: sorted(c["id"] for c in rel if c.get("status") == "supported" and (c.get("product_id") or "", c.get("product_version") or "") == (p0 or "", v0 or ""))   # noqa: E731
        out = EX.recorded_rows(self.conn, self.pid, run_id, subject, did, claim_ids_of)
        if derive and not any(x["stage"] == EX.STAGE_ELIGIBLE for x in out):                    # no run recorded the eligible set's refusals for this decision (an older store): the same rules read again, read-only
            out += EX.derived_rows(self.conn, self.pid, subject, rel)
        out += EX.pack_rows(packs, lambda cid: (by_claim.get(cid) or {}).get("checked_at"))
        out += EX.node_rows(nodes)
        if derive and not has_pack:                                                             # the runs before the evidence pack cut by the question's status: history, so the artifact says what it is; read from the coverage rows as they stand at the build
            rq_claims, rq_subject, rq_status = self._questions()
            out += EX.history_rows(subject, rel, rq_claims, rq_subject, rq_status)
        return out

    def check_dates(self, memo: dict | None, req_ids: list[str], product_id, product_version) -> list[dict]:
        """Per memo option (the recommendation first when it is among them): the newest counting verdict's created_at on the decision's binding hard
        requirements (coverage_step.checked_at) and the identity's newest checked_at among the project's claims; None where the records hold none."""
        from app.core import coverage_step as CS
        hard = [{"id": r} for r in req_ids if r in self.hard]
        options = [o for o in ((memo or {}).get("options") or []) if isinstance(o, dict) and o.get("product_id")]
        if not options and product_id:
            options = [{"option": (memo or {}).get("recommendation", {}).get("option") if isinstance((memo or {}).get("recommendation"), dict) else None, "product_id": product_id, "product_version": product_version or ""}]
        out, seen = [], set()
        for o in options:
            key = (o["product_id"], o.get("product_version") or "")
            if key in seen:
                continue
            seen.add(key)
            newest = self.conn.execute("SELECT MAX(checked_at) FROM claims WHERE project_id=? AND product_id=? AND product_version=?", (self.pid, key[0], key[1])).fetchone()[0]
            out.append({"label": str(o.get("option") or coverage_label(*key)), "product_id": key[0], "product_version": key[1],
                        "verdict_date": CS.checked_at(self.conn, self.pid, hard, self.rv, key[0], key[1]) if hard else None, "claims_checked_at": newest})
        return out


def coverage_label(product_id: str, product_version: str) -> str:
    from app.core.coverage import identity_label
    return identity_label(product_id, product_version)


def _num(x) -> str:
    f = float(x)
    return str(int(f)) if f.is_integer() else str(f)


def _alternatives(conn, pid: str, decision_id: str, cited: list[str]) -> tuple[list[dict], list[dict]]:
    """The comparison's options as the ledger stored the validated ComparisonOut of the step that produced this decision (the
    completed attempt of that step with a stored result): label, strengths, risks, meets_hard_constraints, the claim ids and the
    product identity. The recommended option's claim ids are the node's checked citations (the decision's stored claim edges: the
    comparison node refuses an id that is not a citable claim and records the refusal); every other option's ids are the model's
    as validated, less any id that is no claim of the project (for example devA.CLM-46 written for devA.CLM-046: such an
    id is dropped here, as the memo path drops an uncited sentence, and returned for the caller to record). The recommended
    option's ids are never filtered: one that is no claim of the project still fails the build in validate, the pick's reasons
    must be real. Returns (alternatives, dropped): dropped holds one entry per option with such ids (option, unresolved, count).
    An unresolved decision stored without a send has none."""
    mark = conn.execute("SELECT step_key FROM run_steps WHERE node='compare_options' AND status='completed' AND output_json LIKE ? ORDER BY completed_at DESC LIMIT 1", (f'%"decision": "{decision_id}"%',)).fetchone()
    if not mark:
        return [], []
    res = conn.execute("SELECT result_json FROM usage_ledger WHERE step_key=? AND kind='model' AND state='completed' AND result_json IS NOT NULL ORDER BY attempt DESC LIMIT 1", (mark[0],)).fetchone()
    try:
        out = json.loads(res[0]) if res and res[0] else {}
    except ValueError:
        return [], []
    alts, dropped = [], []
    recommended = out.get("recommended") or ((out.get("recommendation") or {}).get("option") if isinstance(out.get("recommendation"), dict) else None)   # the memo shape names the recommendation as an object
    for o in out.get("options") or []:
        if not isinstance(o, dict) or not o.get("option"):
            continue
        if o["option"] == recommended:
            ids = list(cited) if cited else [str(c) for c in (o.get("claim_ids") or [])]
        else:                                                                                  # a non-picked option keeps the ids that are claims of the project; the rest are dropped and reported
            raw = [str(c) for c in (o.get("claim_ids") or [])]
            known = {r[0] for r in conn.execute(f"SELECT id FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(raw)) or 'NULL'})", (pid, *raw))} if raw else set()
            ids = [c for c in raw if c in known]
            lost = [c for c in dict.fromkeys(raw) if c not in known]
            if lost:
                dropped.append({"option": str(o["option"]), "unresolved": lost, "count": len(lost)})
        from app.core import textlimit as TL
        whole = lambda xs: [s for s in (TL.to_statement_end(str(x), 300)[0] for x in xs) if s]   # each cell to a sentence end or a clause end, else not printed; never to a dangling word
        strengths = whole(o.get("strengths") or []) or whole([o["fit"]] if o.get("fit") else [])   # the ledger holds the provider's answer as cut at the contract's limit, so the cells of the snapshot's alternatives table, which the documents print, are ended by whole()
        risks = whole(o.get("risks") or []) or whole([o["risk"]] if o.get("risk") else [])
        meets = bool(o.get("meets_hard_constraints")) if "meets_hard_constraints" in o else (o.get("verdict") != "rejected")
        alts.append({"label": str(o["option"]), "strengths": strengths, "risks": risks, "meets_hard_constraints": meets,
                     "claim_ids": ids, "product_id": o.get("product_id") or None, "product_version": o.get("product_version") or None})
    return alts, dropped


# ---------------------------------------------------------------- the rules
REF_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]+)\.(REQ|ASM|Q|SRC|CLM|DEC|CMP|TSK|ACC|RQ)-([0-9A-Za-z]+)\b")   # a record id written in text: <project>.<PREFIX>-<n>; the project id is read whole at its own boundary, hyphens included (the Project model allows them), so e3-p01.REQ-003 is e3-p01's record and a foreign id is still refused by the validator


def _json_list(text) -> list:
    """A JSON list column read defensively (sources.found_by_json)."""
    try:
        v = json.loads(text) if isinstance(text, str) else (text or [])
    except ValueError:
        return []
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def _prefix_of(record_id: str) -> str:
    return record_id.split(".", 1)[1].split("-", 1)[0] if "." in record_id else ""


def _text_references(plan_body: dict, decisions: list[dict]) -> list[str]:
    """Every record id written in the plan's unresolved checks, a decision's unresolved findings or a rejection reason:
    the texts the contract names beside the structured references. Returned in first-seen order, each once."""
    texts = list(plan_body.get("unresolved_checks") or [])
    for d in decisions:
        texts += list(d.get("unresolved") or []); texts += [r.get("reason", "") for r in d.get("rejected") or []]
    out: list[str] = []
    for t in texts:
        for m in REF_PATTERN.finditer(str(t)):
            if m.group(0) not in out:
                out.append(m.group(0))
    return out


ID_SECTIONS = {"requirements": "REQ", "assumptions": "ASM", "questions": "Q", "sources": "SRC", "claims": "CLM", "decisions": "DEC", "components": "CMP", "tasks": "TSK", "acceptance_tests": "ACC", "research_questions": "RQ"}


def validate(snapshot: dict, *, project_id: str, version: int, requirement_version: int) -> None:
    """The contract (schemas/export_models.py), closure, isolation and version match. Raises SnapshotError."""
    try:
        validate_snapshot(snapshot)
    except Exception as e:                                                                   # noqa: BLE001 (pydantic's error, reported with the rule's name)
        raise SnapshotError(f"the snapshot does not fit the contract: {str(e)[:400]}") from None
    pid = snapshot["project"]["id"]
    if pid != project_id:
        raise SnapshotError(f"the snapshot names project {pid!r}, the plan version belongs to {project_id!r}")
    plan = snapshot["plan"]
    if plan["version"] != version or plan["requirement_version"] != requirement_version or plan["id"] != f"{pid}.PLAN-v{version}":
        raise SnapshotError(f"version mismatch: the snapshot says plan {plan['id']} version {plan['version']} at requirement version {plan['requirement_version']}, the row says version {version} at requirement version {requirement_version}")

    def own(record_id, where: str) -> None:
        if not isinstance(record_id, str) or not record_id.startswith(f"{pid}.") and not record_id.startswith(f"{pid}-"):
            raise SnapshotError(f"{where}: record {record_id!r} does not belong to project {pid!r}")

    ids: dict[str, set] = {}
    for section, prefix in ID_SECTIONS.items():
        ids[prefix] = set()
        for r in snapshot.get(section, []):
            if not str(r["id"]).startswith(f"{pid}.{prefix}-"):
                raise SnapshotError(f"{section}: record {r['id']!r} is not a {prefix} record of project {pid!r}")
            ids[prefix].add(r["id"])
    for r in snapshot["run"]:
        own(r["id"], "run")
    comp_keys = {c["key"] for c in snapshot["components"]}
    task_keys = {t["key"] for t in snapshot["tasks"]}
    node_ids = {n["id"] for d in snapshot["decisions"] for n in d["tree_nodes"]}
    for n_id in node_ids:
        own(n_id, "tree node")

    def need(kind: str, value, where: str) -> None:
        pool = {"CLM": ids["CLM"], "SRC": ids["SRC"], "REQ": ids["REQ"], "DEC": ids["DEC"], "TSK": ids["TSK"], "ACC": ids["ACC"], "component": comp_keys, "task": task_keys, "node": node_ids}[kind]
        if value not in pool:
            raise SnapshotError(f"{where}: {kind} {value!r} does not resolve to a record of the snapshot")

    def by_prefix(value, where: str) -> None:
        own(value, where)
        prefix = str(value).split(".", 1)[1].split("-", 1)[0] if "." in str(value) else ""
        if prefix not in ids or value not in ids[prefix]:
            raise SnapshotError(f"{where}: {value!r} does not resolve to a record of the snapshot")

    for r in snapshot["requirements"]:
        if r["version"] > requirement_version:                                                 # every requirement, the superseded history included; older history stays valid
            raise SnapshotError(f"version mismatch: requirement {r['id']} carries version {r['version']}, above the plan's requirement version {requirement_version}")
        if r.get("supersedes"):
            need("REQ", r["supersedes"], f"requirement {r['id']}")
    for c in snapshot["claims"]:
        need("SRC", c["source_id"], f"claim {c['id']}")
    for d in snapshot["decisions"]:
        if d.get("plan_version") is not None and d["plan_version"] > version:                   # a decision made for a later plan version is not a record of this one
            raise SnapshotError(f"version mismatch: decision {d['id']} is made for plan version {d['plan_version']}, above this version {version}")
        for cid in d["claim_ids"]:
            need("CLM", cid, f"decision {d['id']}")
        for rid in d["requirement_ids"]:
            need("REQ", rid, f"decision {d['id']}")
        for s in d["requirement_support"]:
            need("REQ", s["requirement_id"], f"decision {d['id']} support")
            if s.get("claim_id"):
                need("CLM", s["claim_id"], f"decision {d['id']} support")
            if s["requirement_version"] > requirement_version:
                raise SnapshotError(f"version mismatch: a support record of decision {d['id']} names requirement version {s['requirement_version']}, above {requirement_version}")
        for a in d["alternatives"]:
            for cid in a["claim_ids"]:
                need("CLM", cid, f"decision {d['id']} alternative {a['label']!r}")
        for n in d["tree_nodes"]:
            for cid in n["claim_ids"]:
                need("CLM", cid, f"decision {d['id']} node {n['id']}")
            if n.get("parent"):
                need("node", n["parent"], f"decision {d['id']} node {n['id']}")
        for x in d.get("exclusions") or []:                                                      # every cut resolves to a record: its own row (an id of the project), a claim or a node of the snapshot
            if x.get("id"):
                own(x["id"], f"decision {d['id']} exclusion")
            if not x.get("record_ids") and not x.get("id"):
                raise SnapshotError(f"decision {d['id']}: the exclusion of {x.get('label') or x.get('product_id')!r} names no record")
            for rid_ in x.get("record_ids") or []:
                if rid_ in node_ids:
                    continue
                by_prefix(rid_, f"decision {d['id']} exclusion {x.get('label') or x.get('product_id')!r}")
    for s in snapshot["claim_requirement_support"]:
        need("CLM", s["claim_id"], "support record"); need("REQ", s["requirement_id"], "support record")
        if s["requirement_version"] > requirement_version:                                      # the top-level support collection under the same upper bound as the decisions'
            raise SnapshotError(f"version mismatch: a support record of {s['claim_id']} names requirement version {s['requirement_version']}, above {requirement_version}")
    for ref in _text_references(snapshot["plan_body"], snapshot["decisions"]):                   # a record id written in an unresolved check, a finding or a rejection reason resolves like a structured reference; a foreign or missing one fails
        by_prefix(ref, f"the record {ref!r} named in an unresolved check, a decision's finding or a rejection reason")
    for t in snapshot["tasks"]:
        need("component", t["component_key"], f"task {t['id']}")
        for dep in t["depends_on"]:
            need("task", dep, f"task {t['id']}")
    for a in snapshot["acceptance_tests"]:
        if a.get("task_id") is not None:
            need("TSK", a["task_id"], f"acceptance test {a['id']}")
        if a.get("requirement_id") is not None:
            need("REQ", a["requirement_id"], f"acceptance test {a['id']}")
    for e in snapshot["trace_edges"]:
        by_prefix(e["from_id"], f"trace edge {e['from_id']} -> {e['to_id']}"); by_prefix(e["to_id"], f"trace edge {e['from_id']} -> {e['to_id']}")
    for cov in snapshot["coverage"]:
        by_prefix(cov["subject_id"], f"coverage subject {cov['subject_id']}")
        for cid in cov["claim_ids"]:
            need("CLM", cid, f"coverage {cov['subject_id']}")
        for sv in cov["source_versions"]:
            if isinstance(sv, dict) and sv.get("source_id"):
                need("SRC", sv["source_id"], f"coverage {cov['subject_id']}")
        if cov.get("requirement_version") and cov["requirement_version"] > requirement_version:
            raise SnapshotError(f"version mismatch: coverage of {cov['subject_id']} is at requirement version {cov['requirement_version']}, above {requirement_version}")
    for d in snapshot["derived"]:
        own(d["id"], "derived table")
        for cids in d["claim_ids"]:
            for cid in cids:
                need("CLM", cid, f"derived {d['id']}")
    for section in ("component_relations", "deployment", "interfaces", "entities", "code_examples"):
        for r in snapshot.get(section, []):
            own(r["id"], section)


# ---------------------------------------------------------------- reading a stored snapshot
def read(conn: sqlite3.Connection, project_id: str, version: int) -> dict | None:
    """The stored snapshot of a version with its signature recomputed: {"snapshot", "snapshot_sig", "row"} or None when the version
    has no snapshot (created before snapshots existed) or does not exist. A stored signature that does not recompute is a SnapshotError."""
    require_guarded(conn)
    row = conn.execute("SELECT snapshot_json, snapshot_sig, approved_by, approved_at, approval_ref, content_sig, requirement_version, created_at, change_summary, export_path FROM plan_versions WHERE project_id=? AND version=?", (project_id, int(version))).fetchone()
    if row is None or row[0] is None or row[1] is None:
        return None
    snapshot = json.loads(row[0])
    if signature(snapshot) != row[1]:
        raise SnapshotError(f"the stored signature of version {version} of {project_id!r} does not recompute from its snapshot")
    return {"snapshot": snapshot, "snapshot_sig": row[1], "row": {"approved_by": row[2], "approved_at": row[3], "approval_ref": row[4], "content_sig": _j(row[5]) if row[5] else None,
                                                                 "requirement_version": row[6], "created_at": row[7], "change_summary": row[8], "export_path": row[9]}}


def list_versions(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Every plan version of the project as a listing shows it: the snapshot signature or "no snapshot" (NULL in both columns, a
    version created before snapshots existed), the approval act when given, the export path when written."""
    require_guarded(conn)
    out = []
    has_editor = "editor_json" in {r[1] for r in conn.execute("PRAGMA table_info(plan_versions)")}
    unfinished = {u["version"]: u for u in unfinished_versions(conn, project_id)}
    for r in conn.execute(f"SELECT version, requirement_version, created_at, change_summary, export_path, snapshot_sig, snapshot_json IS NOT NULL, approved_by, approved_at, approval_ref, diff_json, {'editor_json' if has_editor else 'NULL'} FROM plan_versions WHERE project_id=? ORDER BY version", (project_id,)):
        try:
            partial = (json.loads(r[10] or "{}") or {}).get("partial_draft")
        except ValueError:
            partial = None
        from app.core.editor import warnings_of
        out.append({"version": r[0], "requirement_version": r[1], "created_at": r[2], "change_summary": r[3], "export_path": r[4],
                    "snapshot": r[5] if (r[5] is not None and r[6]) else NO_SNAPSHOT, "approved": ({"by": r[7], "at": r[8], "ref": r[9]} if r[8] else None),
                    "unfinished": unfinished.get(int(r[0])),   # the run's record when the row is a version a run began and did not finish; None for a stored version and for a row created before signed snapshots existed
                    "partial_draft": partial if isinstance(partial, dict) else None,   # {"stop_reason", "skipped"} when the version is the partial draft written after a cap or time stop; None for a regular version
                    "editor_warnings": warnings_of(_j(r[11]) if r[11] else None)})   # the Editor's flags, its completeness line when incomplete and the drops the checks refused, shown where the founder reviews the version
    return out


def next_version(conn: sqlite3.Connection, project_id: str, run_id: str | None = None) -> int:
    """The number of the version a run writes: above every signed version and above every row without a snapshot that another run left
    (the record of a version that run did not finish; it is never deleted, rewritten or numbered again). A row without a snapshot written after
    this run started is this run's own half-written row: the store sweeps it and writes its number again, so it does not count here."""
    require_guarded(conn)
    started = (conn.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone() or [None])[0] if run_id else None
    return int(conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM plan_versions WHERE project_id=? AND (snapshot_sig IS NOT NULL OR (? IS NOT NULL AND created_at < ?))", (project_id, started, started)).fetchone()[0])


def unfinished_versions(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """The version rows of the project that a run began and did not finish: a row without a snapshot written at or after the first signed
    version of the store (signed snapshots existed when it was written, so its missing snapshot is a version not stored, never an older format)
    for which a run is recorded: the run of a decision made for that version (decisions.run_id), else the project's run that was open when the
    row was written. A row without a snapshot older than every signed version of the store was created before signed snapshots existed, and a
    row no run is recorded for is not known to be a run's: neither is listed. Each row carries the run's id, status, stop reason and kind, and
    the latest version that holds a snapshot."""
    require_guarded(conn)
    first = conn.execute("SELECT MIN(created_at) FROM plan_versions WHERE snapshot_sig IS NOT NULL").fetchone()[0]
    if first is None:
        return []
    has_run = "run_id" in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    out = []
    for version, created in conn.execute("SELECT version, created_at FROM plan_versions WHERE project_id=? AND snapshot_sig IS NULL AND created_at >= ? ORDER BY version", (project_id, first)).fetchall():
        rid = (conn.execute("SELECT run_id FROM decisions WHERE project_id=? AND plan_version=? AND run_id IS NOT NULL ORDER BY id DESC LIMIT 1", (project_id, version)).fetchone() or [None])[0] if has_run else None
        if rid is None:
            rid = (conn.execute("SELECT id FROM runs WHERE project_id=? AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?) ORDER BY started_at DESC LIMIT 1", (project_id, created, created)).fetchone() or [None])[0]
        if rid is None:
            continue                                                                         # no run is recorded for the row: nothing says a run began it, so it is not named as unfinished
        run = conn.execute("SELECT status, stop_reason, kind FROM runs WHERE id=?", (rid,)).fetchone()
        standing = conn.execute("SELECT MAX(version) FROM plan_versions WHERE project_id=? AND snapshot_sig IS NOT NULL", (project_id,)).fetchone()[0]
        out.append({"version": int(version), "created_at": created, "run_id": rid, "run_status": run[0] if run else None, "stop_reason": run[1] if run else None, "kind": run[2] if run else None, "standing": standing})
    return out


def preserve_draft(conn: sqlite3.Connection, project_id: str, run_id: str, step_key: str, status: str, reason: str, content: str | None, ledger_entry: str | None = None) -> None:
    """The one permitted draft after a cap or time stop that could not be stored as a version is kept whole with the reason (plan_drafts),
    so no paid work is lost: the plan's JSON when the stage held a plan, else the attempt's answer text from its ledger row (raw_json), else
    nothing but the reason. One row per run and step key; a later attempt of the same key replaces it. Inside the caller's transaction or its own."""
    require_guarded(conn)
    def body():
        conn.execute("INSERT OR REPLACE INTO plan_drafts (project_id, run_id, step_key, ledger_entry, status, reason, content_json) VALUES (?,?,?,?,?,?,?)",
                     (project_id, run_id, step_key, ledger_entry, status[:60], reason[:600], content))
    if conn.in_transaction:
        body()
    else:
        with conn:
            body()


def list_drafts(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """The preserved drafts of the project as a listing shows them (the run, the status, the reason, whether a response is held and its size)."""
    require_guarded(conn)
    return [{"run_id": r[0], "step_key": r[1], "ledger_entry": r[2], "status": r[3], "reason": r[4], "has_content": r[5] is not None, "content_chars": len(r[5]) if r[5] else 0, "created_at": r[6]}
            for r in conn.execute("SELECT run_id, step_key, ledger_entry, status, reason, content_json, created_at FROM plan_drafts WHERE project_id=? ORDER BY created_at, run_id", (project_id,))]


# ---------------------------------------------------------------- the Markdown export, from the snapshot alone
H = {"provenance": "Provenance", "requirements": "Requirements", "decisions": "Decisions", "quality": "Decision quality summary", "alternatives": "Alternatives considered", "rejected": "Rejected options",
     "tree": "Explored options", "not_weighed": "Not weighed", "plan": "Plan", "problem": "The problem", "users": "The users", "scope_in": "In scope", "scope_out": "Out of scope", "flow": "User flow", "risks": "Top risks",
     "gaps": "Unresolved checks", "fit": "Fit matrix", "components": "Components", "tasks": "Tasks", "tests": "Acceptance tests", "coverage": "Coverage report", "derived": "Derived data",
     "claims": "Claims and quotes", "sources": "Sources", "assumptions": "Assumptions", "questions": "Questions and answers", "labels": "Labels used in this document", "runs": "Runs"}


def _cell(x) -> str:
    if x is None:
        return ""
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, (list, tuple)):
        return "; ".join(_cell(v) for v in x)
    return str(x).replace("|", "\\|").replace("\n", " ")


def _table(columns: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    out += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return out + [""]


def render_markdown(snapshot: dict, runs: list[dict] | None = None) -> str:
    """The Markdown export of one plan version from its snapshot and nothing else. Every
    reader-facing word is a snapshot value or a heading of H; the decision quality summary and the fit matrix are computed from the
    snapshot's records (support records, citations, coverage lines, trace edges), never from the store."""
    s = snapshot; p = s["plan"]; body = s["plan_body"]
    reqs = {r["id"]: r for r in s["requirements"]}; claims = {c["id"]: c for c in s["claims"]}; sources = {x["id"]: x for x in s["sources"]}
    cov = {c["subject_id"]: c for c in s["coverage"]}
    edges = s["trace_edges"]
    L = [f"# {s['project']['title']}", "", s["project"]["idea"], "", f"## {H['provenance']}", ""]
    L += _table(["Plan version", "Requirement version", "Created", "Change summary", "Snapshot"], [[p["version"], p["requirement_version"], p["created_at"], p["change_summary"], s.get("_signature", "")]])
    if s["run"]:
        L += [f"### {H['runs']}", ""] + _table(["Run", "Kind", "Status", "Stop reason", "Started", "Ended", "Billed (USD)", "Committed (USD)", "Possible charges (USD)"],   # the runs as given (runs_now at a download), else the snapshot's rows as written with the version
                                               [[r["id"], r.get("kind"), r["status"], r.get("stop_reason"), r["started_at"], r["ended_at"], r["spend"]["billed"], r["spend"]["committed"], r["spend"]["possible"]] for r in (s["run"] if runs is None else runs)])
    L += [f"## {H['requirements']}", ""] + _table(["Id", "Key", "Value", "Status", "Source", "Hard constraint", "Version", "Supersedes"],
                                                  [[r["id"], r["key"], r["value"], r["status"], r["source"], r["hard"], r["version"], r.get("supersedes")] for r in s["requirements"]])
    L += [f"## {H['decisions']}", ""]
    for d in s["decisions"]:
        L += [f"### {d['subject']} ({d['id']})", "", f"Choice: {d['choice']}", "", f"Status: {d['status']}; importance: {d['importance']}; product: {d.get('product_id') or ''} {d.get('product_version') or ''}".rstrip(), "", f"Why: {d['rationale']}", ""]
        hard_reqs = [reqs[r] for r in d["requirement_ids"] if r in reqs and reqs[r]["hard"]]
        unproven = {q.get("id") for q in ((d.get("memo") or {}).get("conditional") or {}).get("requirements") or []}   # the count and the must-haves not yet proven are one verdict, the derived status's (app/core/reconcile.py). A recommendation stands on every hard requirement its record does not name as not yet proven (a requirement judged by calculation, such as the monthly budget, counts by the calculation alone, whatever a support row says); any other decision keeps the count of its support rows, less a requirement its record names
        met = [r for r in hard_reqs if r["id"] not in unproven and (d["status"] == "active" or any(x["requirement_id"] == r["id"] and x["verdict"] == "supported" and (x.get("status") in (None, "counts")) and x["requirement_version"] == p["requirement_version"] for x in d["requirement_support"]))]
        cited = [claims[c] for c in d["claim_ids"] if c in claims]
        findings = [u for u in body["unresolved_checks"] if d["id"] in u] + list(d.get("unresolved") or [])
        prices = [c for c in cited if c["kind"] == "price"]
        L += [f"#### {H['quality']}", ""] + _table(["Hard constraints met", "Claims cited (supported)", "Claims cited (partial)", "Open findings", "Price freshness", "Status"],
                                                  [[f"{len(met)} of {len(hard_reqs)}" + (": " + "; ".join(r["key"] for r in met) if met else ""), sum(1 for c in cited if c["status"] == "supported"), sum(1 for c in cited if c["status"] == "partial"),
                                                    len(findings), "; ".join(f"{c['id']} fresh until {c.get('fresh_until') or 'not recorded'}" for c in prices) or "no price cited", d["status"]]])
        if findings:
            L += ["Open findings:", ""] + [f"- {u}" for u in findings] + [""]
        if cited:
            L += ["Claims this decision relies on:", ""] + [f"- {c['id']} ({c['status']}, {c['scope_status']}): {c['text']}" for c in cited] + [""]
        if d["alternatives"]:
            L += [f"#### {H['alternatives']}", ""] + _table(["Option", "Strengths", "Risks", "Meets hard constraints", "Claims"], [[a["label"], a["strengths"], a["risks"], a["meets_hard_constraints"], a["claim_ids"]] for a in d["alternatives"]])
        if d["tree_nodes"]:
            L += [f"#### {H['tree']}", ""] + _table(["Option", "Depth", "Score", "Hard constraint status", "Pruned", "Reason"], [[n["label"], n["depth"], n["score"], n["hard_status"], n["pruned"], n.get("reason")] for n in d["tree_nodes"]])
        if d["rejected"]:
            L += [f"#### {H['rejected']}", ""] + _table(["Option", "Reason"], [[r["label"], r["reason"]] for r in d["rejected"]])
        if d.get("exclusions"):                                                                   # every cut of the weighing, with its stage, rule, reason, check date and record
            L += [f"#### {H['not_weighed']}", ""] + _table(["Option", "Stage", "Rule", "Reason", "Checked on", "Record", "Source"], [[x["label"] or x["product_id"], x["stage"], x["rule"], x["reason"], (x.get("checked_at") or "not recorded")[:10], x.get("id") or x["record_ids"], x["source"]] for x in d["exclusions"]])
    L += [f"## {H['plan']}", "", f"### {H['problem']}", "", body["problem"], "", f"### {H['users']}", "", _cell(body["users"]), ""]
    for key in ("scope_in", "scope_out", "flow", "risks", "gaps"):
        items = body.get({"flow": "user_flow", "gaps": "unresolved_checks"}.get(key, key), [])
        L += [f"### {H[key]}", ""] + ([f"- {x}" for x in items] if items else ["(none)"]) + [""]
    # the fit matrix: requirement -> decisions -> tasks -> acceptance tests -> evidence status, from the trace edges and the coverage lines
    comp_of_dec = {}
    for e in edges:
        if e["kind"] == "decision_component":
            comp_of_dec.setdefault(e["from_id"], []).append(e["to_id"])
    tasks_of_comp = {}
    for e in edges:
        if e["kind"] == "component_task":
            tasks_of_comp.setdefault(e["from_id"], []).append(e["to_id"])
    version_tasks = {t["id"] for t in s["tasks"]}
    rows = []
    for r in s["requirements"]:
        if r["status"] == "superseded":
            continue
        decs = sorted({e["to_id"] for e in edges if e["kind"] == "req_decision" and e["from_id"] == r["id"]})
        tasks = sorted({t for d in decs for c in comp_of_dec.get(d, []) for t in tasks_of_comp.get(c, []) if t in version_tasks})
        tests = sorted({a["id"] for a in s["acceptance_tests"] if a.get("requirement_id") == r["id"]})
        c = cov.get(r["id"])
        rows.append([r["id"], r["key"], decs, tasks, tests, f"{c['evidence_status']} / {c['processing_status']}" if c else "no coverage line"])
    L += [f"## {H['fit']}", ""] + _table(["Requirement", "Key", "Decisions", "Tasks", "Acceptance tests", "Evidence status / processing status"], rows)
    L += [f"## {H['components']}", ""] + (_table(["Id", "Name", "Responsibility", "Product"], [[c["id"], c["name"], c["responsibility"], f"{c.get('product_id') or ''} {c.get('product_version') or ''}".strip()] for c in s["components"]]) if s["components"] else ["(none)", ""])
    L += [f"## {H['tasks']}", ""] + (_table(["Id", "Task", "Component", "Depends on", "Effort"], [[t["id"], t["title"], t["component_key"], t["depends_on"], t["effort"]["text"]] for t in s["tasks"]]) if s["tasks"] else ["(none)", ""])
    L += [f"## {H['tests']}", ""] + (_table(["Id", "Task", "Requirement", "Criterion"], [[a["id"], a.get("task_id"), a.get("requirement_id"), a["criterion"]] for a in s["acceptance_tests"]]) if s["acceptance_tests"] else ["(none)", ""])
    L += [f"## {H['coverage']}", ""] + (_table(["Subject", "Type", "Evidence status", "Processing status", "Reason", "Claims"], [[c["subject"], c["subject_type"], c["evidence_status"], c["processing_status"], c["reason"], c["claim_ids"]] for c in s["coverage"]]) if s["coverage"] else ["(none)", ""])
    if s["derived"]:
        L += [f"## {H['derived']}", ""]
        for d in s["derived"]:
            L += [f"### {d['title']} ({d['id']})", ""] + _table([d["labels"].get(f, f) for f in d["fields"]] + ["Claims"], [list(r) + [c] for r, c in zip(d["rows"], d["claim_ids"])])
    L += [f"## {H['claims']}", ""]
    if s["claims"]:
        L += _table(["Id", "Claim", "Kind", "Status", "Scope status", "Conditions", "Source", "Checked", "Fresh until"], [[c["id"], c["text"], c["kind"], c["status"], c["scope_status"], c["conditions"], c["source_id"], c.get("checked_at"), c.get("fresh_until")] for c in s["claims"]])
        for c in s["claims"]:
            if c.get("quote"):
                src = sources.get(c["source_id"], {})
                L += [f"> {c['quote']}", "", f"({c['id']}, {src.get('title') or c['source_id']}, version {c.get('source_version') or ''}, {c.get('range') or ''})".replace(", )", ")"), ""]
    else:
        L += ["(none)", ""]
    L += [f"## {H['sources']}", ""] + (_table(["Id", "Title", "Publisher", "URL", "Published", "Retrieved", "Freshness class", "Fetch status", "Version"], [[x["id"], x.get("title"), x.get("publisher"), x["url"], x.get("published_at"), x["retrieved_at"], x["freshness_class"], x["fetch_status"], x["source_version"]] for x in s["sources"]]) if s["sources"] else ["(none)", ""])
    L += [f"## {H['assumptions']}", ""] + ([f"- {a['id']} ({a['impact']}, {a['status']}): {a['text']}" for a in s["assumptions"]] or ["(none)"]) + [""]
    if s["questions"]:
        L += [f"## {H['questions']}", ""] + _table(["Id", "Kind", "Question", "Answer"], [[q["id"], q.get("kind"), q["text"], q.get("answer")] for q in s["questions"]])
    L += [f"## {H['labels']}", ""] + [f"- {s['labels'][k]}" for k in sorted(s["labels"])] + [""]
    return "\n".join(L)


def write_markdown(conn: sqlite3.Connection, project_id: str, version: int, exports_root: pathlib.Path | str) -> pathlib.Path:
    """Render the version's Markdown export from its stored snapshot into <exports_root>/<project_id>/plan-v<version>.md and record
    the path on the row (export_path). A version without a snapshot is refused: its existing export, if any, stays as it is."""
    require_guarded(conn)
    stored = read(conn, project_id, version)
    if stored is None:
        raise SnapshotError(f"version {version} of {project_id!r} has {NO_SNAPSHOT}: no Markdown export is rendered for it")
    snap = dict(stored["snapshot"]); snap["_signature"] = stored["snapshot_sig"]
    text = render_markdown(snap)
    out = pathlib.Path(exports_root) / project_id / f"plan-v{int(version)}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    from app.repo import Repo
    Repo(conn).set_export_path(project_id, int(version), str(out))
    return out
