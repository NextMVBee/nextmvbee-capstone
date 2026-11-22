"""The single writer of derived evidence state. From the persisted base records only (claims with
the verifier's verdict and scope, source versions, cache status and stale marks, requirement versions, conditions, the
verification's requirement entailments, the comparisons' proposed outcomes and cited claims) `derive()` recomputes, inside
the caller's BEGIN IMMEDIATE transaction, idempotently and writing only values that changed:

  claim eligibility (rule a)           -> claims.status (the verdict, unverified while awaiting one, conflicting while in an open conflict)
  the claim-kind rule (rule b)         -> claim_requirement_support.status counts | withdrawn with its reason
  conflicts and their lifecycle (c)    -> claim_conflicts rows opened, kept, reopened or resolved with a reason and a timestamp, never deleted
  decision status                      -> from the comparison's proposed_status:
                                          active while no hard requirement is violated for the decision's product (a contradicts verdict, the
                                          budget by calculation among them), no hard requirement's support was withdrawn for sources that
                                          disagree and no cited claim is in an open conflict; a hard requirement without counting support
                                          among the cited claims is permitted only where the comparison recorded the recommendation as not
                                          yet proven on it (memo_json.conditional: the evidence was silent): the decision is then a
                                          conditional recommendation, active, its derivation_reason naming those requirements; support that
                                          was recorded and does not count now leaves the decision unresolved with derivation_reason
  coverage                             -> evidence_status and processing_status of every subject

Nothing else in the application writes those columns: base writers (a verdict, a new claim, an invalidation, a comparison)
write their base columns, set projects.derivation_pending in the same transaction and either call derive() inside it or
leave the flag for the next reconciliation; readers of support and decision status treat a flagged project as not yet
derived. History is append-only (rule d): claim events, trace edges and run records are never edited; the reconciliation
writes its own rows into reconciliation_log (what changed, why, rules_version). `reconcile_project()` wraps derive() in
its own BEGIN IMMEDIATE; startup reconciles every project whose rules_version is older or whose flag is set."""
import json
import sqlite3
from datetime import datetime, timezone
from app.core.sqlguard import require_guarded

from app.core import coverage, evidence

RULES_VERSION = 7                                                                      # a hard requirement on which the evidence is silent leaves an active proposal active, marked conditional
CONDITIONAL = "conditional"                                                             # the opening word of decisions.derivation_reason on a conditional recommendation; conditional_requirements reads the ids


def _now(now=None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _log(conn, project_id, run_id, kind, subject_id, detail, report):
    conn.execute("INSERT INTO reconciliation_log (project_id, run_id, rules_version, kind, subject_id, detail) VALUES (?,?,?,?,?,?)", (project_id, run_id, RULES_VERSION, kind, subject_id, detail[:600]))
    report["log"].append({"kind": kind, "subject_id": subject_id, "detail": detail[:300]})


def base_claims(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Claims joined with their source's current version and provenance (base columns and the derived status for the report)."""
    require_guarded(conn)
    rows = conn.execute("SELECT c.id, c.status, c.verdict, c.scope_status, c.subject, c.attribute, c.value, c.kind, c.source_id, c.source_version, c.verified_requirement_version, c.product_id, c.product_version, c.conditions_json, c.quote, c.text, "
                        "s.content_hash, s.tier, s.published_at, s.retrieved_at, s.url, s.cache_status, s.freshness FROM claims c LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=? ORDER BY c.id", (project_id,)).fetchall()
    cols = ("id", "status", "verdict", "scope_status", "subject", "attribute", "value", "kind", "source_id", "source_version", "verified_requirement_version", "product_id", "product_version", "conditions_json", "quote", "text",
            "current_version", "tier", "published_at", "retrieved_at", "url", "cache_status", "freshness")                 # the text is read by evidence.tier_of for the conflict key
    return [dict(zip(cols, r)) for r in rows]                                                # a NULL verdict means unverified at the current rules version; no inference from the derived status


def migrate_legacy_base(conn: sqlite3.Connection, project_id: str, run_id: str | None = None) -> dict:
    """The one-time migration of base records written before their columns existed (never part of derive()):
    claims.verdict from the status of a verified legacy row, a support record's verdict 'conflicting' (an earlier
    rule) back to the verifier's 'supported', decisions.proposed_status from the stored status. Runs inside the caller's
    transaction, only while the project's rules_version is older than RULES_VERSION. Logged per row."""
    require_guarded(conn)
    from app.core.sqlguard import derive_step
    report = {"claims": 0, "support": 0, "decisions": 0}
    with derive_step(conn):
        return _migrate_legacy_base(conn, project_id, run_id, report)


def _migrate_legacy_base(conn, project_id, run_id, report):
    for cid, status, scope in conn.execute("SELECT id, status, scope_status FROM claims WHERE project_id=? AND verdict IS NULL AND status<>'unverified' AND COALESCE(scope_status,'unchecked')<>'unchecked'", (project_id,)).fetchall():
        v = "supported" if status == "conflicting" else status
        conn.execute("UPDATE claims SET verdict=? WHERE id=? AND project_id=?", (v, cid, project_id)); report["claims"] += 1
        conn.execute("INSERT INTO reconciliation_log (project_id, run_id, rules_version, kind, subject_id, detail) VALUES (?,?,?,?,?,?)", (project_id, run_id, RULES_VERSION, "migration", cid, f"verdict column backfilled from status {status}"))
    for cid, req, rv in conn.execute("SELECT claim_id, requirement_id, requirement_version FROM claim_requirement_support WHERE project_id=? AND verdict='conflicting'", (project_id,)).fetchall():
        conn.execute("UPDATE claim_requirement_support SET verdict='supported' WHERE project_id=? AND claim_id=? AND requirement_id=? AND requirement_version=?", (project_id, cid, req, rv)); report["support"] += 1
        conn.execute("INSERT INTO reconciliation_log (project_id, run_id, rules_version, kind, subject_id, detail) VALUES (?,?,?,?,?,?)", (project_id, run_id, RULES_VERSION, "migration", f"{cid}->{req}", "support verdict 'conflicting' (pre-round-1 rule) restored to the verifier's 'supported'"))
    for did, status in conn.execute("SELECT id, status FROM decisions WHERE project_id=? AND proposed_status IS NULL", (project_id,)).fetchall():
        conn.execute("UPDATE decisions SET proposed_status=? WHERE id=? AND project_id=?", (status, did, project_id)); report["decisions"] += 1
        conn.execute("INSERT INTO reconciliation_log (project_id, run_id, rules_version, kind, subject_id, detail) VALUES (?,?,?,?,?,?)", (project_id, run_id, RULES_VERSION, "migration", did, f"proposed_status backfilled from status {status}"))
    return report


def derive(conn: sqlite3.Connection, project_id: str, run_id: str | None = None, now=None) -> dict:
    """Recompute the derived state of one project inside the caller's open transaction. Returns what changed; a pass that
    finds nothing to change performs no write at all (strict idempotency). Opens the derive-step context of the execution
    boundary (app/core/sqlguard.py): the only context in which derived columns may be written."""
    require_guarded(conn)
    if not conn.in_transaction:
        raise RuntimeError("derive() runs inside the caller's BEGIN IMMEDIATE transaction; use reconcile_project() otherwise")
    from app.core.sqlguard import derive_step
    require_guarded(conn)
    migration_gate(conn, project_id, run_id)                                               # the older-version migration runs before the first derivation in both transaction paths
    with derive_step(conn):
        return _derive(conn, project_id, run_id, now)


def migration_gate(conn: sqlite3.Connection, project_id: str, run_id: str | None = None) -> bool:
    """While the project's rules_version is older than RULES_VERSION, run the one-time base migration first (inside the
    caller's transaction); the version itself is bumped by the derivation that follows. Historical support is never lost."""
    require_guarded(conn)
    prev = conn.execute("SELECT rules_version FROM projects WHERE id=?", (project_id,)).fetchone()
    if prev and int(prev[0] or 0) < RULES_VERSION:
        migrate_legacy_base(conn, project_id, run_id=run_id)
        return True
    return False


def _derive(conn: sqlite3.Connection, project_id: str, run_id: str | None, now) -> dict:
    report = {"project_id": project_id, "rules_version": RULES_VERSION, "claims_status_changed": [], "support_withdrawn": [], "support_restored": [], "conflicts_opened": [], "conflicts_resolved": [],
              "conflicts_reopened": [], "decisions_changed": [], "coverage_changed": [], "log": [], "changed": False}
    prow = conn.execute("SELECT requirement_version, rules_version, derivation_pending FROM projects WHERE id=?", (project_id,)).fetchone()
    if not prow:
        return report
    rv, prev_rules, pending = int(prow[0] or 1), int(prow[1] or 0), int(prow[2] or 0)
    ts = _now(now)
    claims = base_claims(conn, project_id)                                                    # derive() writes derived columns only: legacy base rows are migrated by migrate_legacy_base()
    # rule a: eligibility; rule b: the claim-kind rule
    elig, settles = {}, {}
    for c in claims:
        ok, why = evidence.eligibility(c["verdict"], c["scope_status"], c["source_version"] == c["current_version"] if c["source_id"] else False, c["verified_requirement_version"] == rv)
        elig[c["id"]] = (ok, why)
        settles[c["id"]] = evidence.settles_alone(c["kind"], c["cache_status"], c["published_at"], c["tier"])
    # rule c: conflicts among eligible claims only; lifecycle of stored conflicts
    fresh = {cf["key"]: cf for cf in evidence.detect_conflicts([dict(c, status=c["verdict"] or "unverified") for c in claims if elig[c["id"]][0]], records=claims)}   # detection sees the verdict, never the status it is about to derive
    stored = {r[0]: dict(zip(("key", "claim_ids", "status", "preferred"), (r[0], json.loads(r[1]), r[2], r[3]))) for r in conn.execute("SELECT conflict_key, claim_ids_json, status, preferred_claim_id FROM claim_conflicts WHERE project_id=?", (project_id,)).fetchall()}
    for key, cf in fresh.items():
        st = stored.get(key)
        want = "settled" if cf.get("settled") else "open"                                       # a conflict the rule settles (a winner, the losers superseded) is 'settled'; a tie stays 'open'
        if st is None:
            conn.execute("INSERT INTO claim_conflicts (project_id, conflict_key, claim_ids_json, preferred_claim_id, reason, run_id, status, rules_version) VALUES (?,?,?,?,?,?,?,?)",
                         (project_id, key, json.dumps(cf["claim_ids"]), cf["preferred_claim_id"], cf["reason"], run_id, want, RULES_VERSION))
            report["conflicts_opened"].append(cf); _log(conn, project_id, run_id, "conflict", key, f"{'settled' if want == 'settled' else 'opened'}: {' vs '.join(cf['values'])}; preferred {cf['preferred_claim_id']} ({cf['reason']})", report)
        elif st["status"] == "resolved":
            conn.execute("UPDATE claim_conflicts SET status=?, resolved_at=NULL, resolution_reason=NULL, claim_ids_json=?, preferred_claim_id=?, reason=?, rules_version=? WHERE project_id=? AND conflict_key=?",
                         (want, json.dumps(cf["claim_ids"]), cf["preferred_claim_id"], cf["reason"], RULES_VERSION, project_id, key))
            report["conflicts_reopened"].append(cf); _log(conn, project_id, run_id, "conflict", key, f"reopened as {want}: detected again among eligible claims", report)
        elif st["claim_ids"] != cf["claim_ids"] or st["preferred"] != cf["preferred_claim_id"] or st["status"] != want:
            conn.execute("UPDATE claim_conflicts SET status=?, claim_ids_json=?, preferred_claim_id=?, reason=?, rules_version=? WHERE project_id=? AND conflict_key=?",
                         (want, json.dumps(cf["claim_ids"]), cf["preferred_claim_id"], cf["reason"], RULES_VERSION, project_id, key))
            _log(conn, project_id, run_id, "conflict", key, f"members, preference or settlement updated: {cf['claim_ids']} preferred {cf['preferred_claim_id']} ({want}: {cf['reason']})", report)
    for key, st in stored.items():
        if key not in fresh and st["status"] in ("open", "settled"):
            why = "; ".join(f"{cid}: {elig[cid][1]}" for cid in st["claim_ids"] if cid in elig and not elig[cid][0]) or "the eligible claims no longer state different values"
            conn.execute("UPDATE claim_conflicts SET status='resolved', resolved_at=?, resolution_reason=?, rules_version=? WHERE project_id=? AND conflict_key=?", (ts, f"no longer detected: {why}"[:400], RULES_VERSION, project_id, key))
            report["conflicts_resolved"].append({"key": key, "reason": why}); _log(conn, project_id, run_id, "conflict", key, f"resolved: no longer detected: {why}", report)
    in_conflict = {cid: key for key, cf in fresh.items() for cid in cf["claim_ids"] if cid != cf["preferred_claim_id"]}   # a conflict with a preferred claim leaves the preferred claim eligible (the others are conflicting); in a settled conflict they are superseded, recorded as such (claim_conflicts, the coverage rows' reason) and kept out of every count
    # derived claim status
    for c in claims:
        new_status = "conflicting" if c["id"] in in_conflict else (c["verdict"] or "unverified")
        if new_status != c["status"]:
            conn.execute("UPDATE claims SET status=? WHERE id=? AND project_id=?", (new_status, c["id"], project_id))
            report["claims_status_changed"].append({"claim_id": c["id"], "from": c["status"], "to": new_status}); _log(conn, project_id, run_id, "claim_status", c["id"], f"{c['status']} -> {new_status}", report)
    # support records at the current requirement version
    by_id = {c["id"]: c for c in claims}
    requirements = {r[0]: {"key": r[1], "value": r[2]} for r in conn.execute("SELECT id, key, value_json FROM requirements WHERE project_id=?", (project_id,)).fetchall()}
    unnamed = lambda claim_id, requirement_id: evidence.unnamed_languages(by_id[claim_id].get("quote"), requirements.get(requirement_id) or {})   # the quote itself names each language the requirement records, or the support is withdrawn
    for cid, req, verdict, status, reason in conn.execute("SELECT claim_id, requirement_id, verdict, status, reason FROM claim_requirement_support WHERE project_id=? AND requirement_version=? ORDER BY claim_id, requirement_id", (project_id, rv)).fetchall():
        base_verdict = "supported" if verdict == "conflicting" else verdict          # rows marked conflicting by the earlier rule were supported verdicts
        c = by_id.get(cid)
        if base_verdict != "supported":
            new, why = "withdrawn", f"verdict {base_verdict}"
        elif c is None:
            new, why = "withdrawn", "claim no longer exists"
        elif not elig[cid][0]:
            new, why = "withdrawn", elig[cid][1]
        elif cid in in_conflict:
            new, why = "withdrawn", f"conflicting evidence ({in_conflict[cid]})"
        elif not settles[cid][0]:
            new, why = "withdrawn", settles[cid][1]
        elif unnamed(cid, req):
            new, why = "withdrawn", "the quote does not name " + ", ".join(unnamed(cid, req))
        elif status == "withdrawn" and str(reason or "").startswith(("conflicting evidence", "restored support refused")) and not evidence.applies_to_requirement(c, requirements.get(req) or {}):   # a record restored after a conflict counts only where the claim's own attribute or quote states the requirement's terms
            new, why = "withdrawn", f"restored support refused: the claim's attribute ({c.get('attribute') or 'none'}) and quote do not state {' and '.join(evidence.requirement_terms(requirements.get(req) or {}))} (D-A378)"
        else:
            new, why = "counts", None
        if (new, why) != (status, reason):                                                   # derived columns only; the base verdict is never rewritten here
            conn.execute("UPDATE claim_requirement_support SET status=?, reason=? WHERE project_id=? AND claim_id=? AND requirement_id=? AND requirement_version=?", (new, why, project_id, cid, req, rv))
            if new == "withdrawn" and status != "withdrawn":
                report["support_withdrawn"].append({"claim_id": cid, "requirement_id": req, "kind": c["kind"] if c else None, "reason": why})
            elif new == "counts" and status != "counts":
                report["support_restored"].append({"claim_id": cid, "requirement_id": req})
            _log(conn, project_id, run_id, "support", f"{cid}->{req}", f"{status} -> {new}" + (f": {why}" if why else ""), report)
    # decisions: derived from the comparison's proposal and the counting support of the cited claims
    for did, status, proposed, reason_now in conn.execute("SELECT id, status, proposed_status, derivation_reason FROM decisions WHERE project_id=? ORDER BY id", (project_id,)).fetchall():
        if proposed is None:                                                                  # a legacy row read as its status; migrate_legacy_base() writes the column once
            proposed = status
        problems, unproven = [], []
        cited = [r[0] for r in conn.execute("SELECT from_id FROM trace_edges WHERE project_id=? AND to_id=? AND kind='claim_decision' ORDER BY from_id", (project_id, did)).fetchall()]
        if proposed == "active":                                                               # hard requirements are checked unconditionally; an empty citation list is no support
            reqs = [r[0] for r in conn.execute("SELECT from_id FROM trace_edges WHERE project_id=? AND to_id=? AND kind='req_decision' ORDER BY from_id", (project_id, did)).fetchall()]
            hard = [r for r in reqs if (conn.execute("SELECT hard FROM requirements WHERE id=? AND project_id=?", (r, project_id)).fetchone() or [0])[0]]
            lacking, detail, unproven = [], [], []
            prod = conn.execute("SELECT product_id, product_version FROM decisions WHERE id=? AND project_id=?", (did, project_id)).fetchone() or (None, None)
            allowed = _recorded_conditional(conn, project_id, did)                               # the requirements the comparison itself recorded the recommendation as not yet proven on
            for r in hard:
                usable = lambda ids: [x for x in ids if x in cited and x in elig and elig[x][0] and x not in in_conflict and settles[x][0] and not unnamed(x, r)]
                checked = _checked(conn, project_id, r, rv, prod[0], prod[1])                    # the coverage step's row for the decision's product: (verdict, claim ids, note) or None
                if checked is not None and _is_calculated(checked[2]):                           # a requirement judged by calculation counts by the calculation alone, through the price claims it used while the decision cites them
                    counting = usable(checked[1]) if checked[0] == "entails" else []
                else:
                    counting = _counting(conn, project_id, r, rv, cited) or usable(_entailed(conn, project_id, r, rv, prod[0], prod[1]))   # the coverage step's entails counts as support through the claims it used, while those claims are cited by the decision, eligible, unconflicted and settling (an empty citation list stays no support)
                if counting:
                    continue
                withdrawn = [f"{x[0]} ({x[1]})" for x in conn.execute(f"SELECT claim_id, reason FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version=? AND status='withdrawn' AND claim_id IN ({','.join('?' * len(cited)) or 'NULL'})", (project_id, r, rv, *cited)).fetchall()] if cited else []
                why = f"{r}: " + (", ".join(withdrawn) if withdrawn else ("no cited claim" if not cited else f"no verification record names it at requirement version {rv} for the cited claims"))
                disagree = sorted(x[0] for x in conn.execute("SELECT s.claim_id, c.product_id, c.product_version FROM claim_requirement_support s JOIN claims c ON c.id=s.claim_id AND c.project_id=s.project_id WHERE s.project_id=? AND s.requirement_id=? AND s.requirement_version=? AND s.status='withdrawn' AND s.reason LIKE 'conflicting evidence%'",
                                                             (project_id, r, rv)).fetchall() if prod[0] and coverage.same_product(prod[0], prod[1] or "", x[1], x[2]))
                if checked is not None and checked[0] == "contradicts":                          # a proven violation, by a quote or by the calculation: never active
                    lacking.append(r); detail.append(f"{r}: a proven violation for {prod[0]} {prod[1] or ''}".rstrip() + (" by calculation" if _is_calculated(checked[2]) else ""))
                elif disagree or any("conflicting evidence" in w for w in withdrawn):          # the sources disagree on the requirement's attribute: the decision stays open
                    lacking.append(r); detail.append(why if withdrawn else f"{r}: sources disagree ({', '.join(disagree)})")
                elif r not in allowed or not any(x in elig and elig[x][0] and x not in in_conflict for x in cited):   # support that was recorded and is not counted now (a stale or unconfirmed copy, a changed requirement, a withdrawn record) leaves the decision unresolved with the reason, and a decision that rests on no eligible cited claim is no recommendation to mark
                    lacking.append(r); detail.append(why)
                else:                                                                            # the comparison found the evidence silent on this requirement and recorded the recommendation as not yet proven on it (memo_json.conditional, the base record): recommended, not yet proven
                    unproven.append(r)
            if lacking:
                problems.append(f"no counting support for {', '.join(lacking)} ({'; '.join(detail)})")
            for cid in cited:
                if cid in in_conflict:
                    problems.append(f"cites {cid}, in open conflict {in_conflict[cid]}")
        new_status = proposed if proposed in ("active", "unresolved", "stale", "rejected") else "unresolved"
        new_reason = None
        if proposed == "active" and problems:
            new_status, new_reason = "unresolved", (f"reconciled (rules v{RULES_VERSION}): " + "; ".join(problems))[:600]
        elif proposed == "active" and unproven:                                                  # the conditional recommendation: active, the unproven requirements named in the derived reason
            new_reason = f"{CONDITIONAL} (rules v{RULES_VERSION}): recommended and not yet proven on {', '.join(unproven)}"[:600]
        if (new_status, new_reason) != (status, reason_now):
            conn.execute("UPDATE decisions SET status=?, derivation_reason=? WHERE id=? AND project_id=?", (new_status, new_reason, did, project_id))
            report["decisions_changed"].append({"decision_id": did, "from": status, "to": new_status, "reason": new_reason}); _log(conn, project_id, run_id, "decision", did, f"{status} -> {new_status}" + (f": {new_reason}" if new_reason else ""), report)
    # coverage, in the same transaction
    before = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute("SELECT subject_type, subject_id, evidence_status, processing_status FROM coverage WHERE project_id=?", (project_id,)).fetchall()}
    coverage.refresh_all(conn, project_id)
    after = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute("SELECT subject_type, subject_id, evidence_status, processing_status FROM coverage WHERE project_id=?", (project_id,)).fetchall()}
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            report["coverage_changed"].append({"subject": k[1], "before": before.get(k), "after": after.get(k)}); _log(conn, project_id, run_id, "coverage", k[1], f"{before.get(k)} -> {after.get(k)}", report)
    report["changed"] = any(report[k] for k in ("claims_status_changed", "support_withdrawn", "support_restored", "conflicts_opened", "conflicts_resolved", "conflicts_reopened", "decisions_changed", "coverage_changed"))
    pending_now = int((conn.execute("SELECT derivation_pending FROM projects WHERE id=?", (project_id,)).fetchone() or [0])[0] or 0)   # a base-column backfill above fires the triggers
    if prev_rules != RULES_VERSION or pending_now:
        conn.execute("UPDATE projects SET rules_version=?, derivation_pending=0 WHERE id=?", (RULES_VERSION, project_id))
        _log(conn, project_id, run_id, "project", project_id, f"rules v{prev_rules} -> v{RULES_VERSION}; pending flag {'cleared' if (pending or pending_now) else 'not set'}; changed={report['changed']}", report)
    return report


def _counting(conn, project_id, requirement_id, rv, cited) -> list[str]:
    """Counting support among the cited claims (read directly: the flag is being cleared in this very transaction)."""
    ids = list(cited)
    if not ids:
        return []
    return [r[0] for r in conn.execute(f"SELECT claim_id FROM claim_requirement_support WHERE project_id=? AND requirement_id=? AND requirement_version=? AND verdict='supported' AND status='counts' AND claim_id IN ({','.join('?' * len(ids))}) ORDER BY claim_id",
                                       (project_id, requirement_id, rv, *ids)).fetchall()]


def _checked(conn, project_id, requirement_id, rv, product_id, product_version) -> tuple | None:
    """The coverage step's row for a product and a requirement at this version, read directly: (verdict, claim ids, note), or None."""
    if not product_id:
        return None
    r = conn.execute("SELECT verdict, claim_ids_json, note FROM coverage_checks WHERE project_id=? AND requirement_id=? AND requirement_version=? AND product_id=? AND product_version=?",
                     (project_id, requirement_id, rv, product_id, product_version or "")).fetchone()
    return (r[0], json.loads(r[1] or "[]"), r[2] or "") if r else None


def _recorded_conditional(conn, project_id, decision_id) -> set:
    """The requirement ids of the conditional record the comparison wrote on the decision's memo (memo_json.conditional.requirements), the
    base record of a conditional recommendation; empty for a decision without one, or a store whose decisions hold no memo."""
    if "memo_json" not in {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}:
        return set()
    row = conn.execute("SELECT memo_json FROM decisions WHERE id=? AND project_id=?", (decision_id, project_id)).fetchone()
    try:
        memo = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return set()
    cond = memo.get("conditional") if isinstance(memo, dict) else None
    return {str(q.get("id")) for q in (cond or {}).get("requirements") or [] if isinstance(q, dict) and q.get("id")} if isinstance(cond, dict) else set()


def _is_calculated(note) -> bool:
    from app.core.budget import CALCULATED
    return str(note or "").startswith(CALCULATED)


def conditional_requirements(derivation_reason) -> list[str]:
    """The requirement ids a conditional recommendation is not yet proven on, read from its derived reason; [] for any other decision."""
    text = str(derivation_reason or "")
    if not text.startswith(CONDITIONAL) or " not yet proven on " not in text:
        return []
    return [x.strip() for x in text.split(" not yet proven on ", 1)[1].split(",") if x.strip()]


def _entailed(conn, project_id, requirement_id, rv, product_id, product_version) -> list[str]:
    """The claim ids of the coverage step's entails verdict for the decision's product at this requirement version (read directly, as _counting)."""
    if not product_id:
        return []
    r = conn.execute("SELECT claim_ids_json FROM coverage_checks WHERE project_id=? AND requirement_id=? AND requirement_version=? AND product_id=? AND product_version=? AND verdict='entails'",
                     (project_id, requirement_id, rv, product_id, product_version or "")).fetchone()
    return (json.loads(r[0]) or ["coverage"]) if r else []


def reconcile_project(conn: sqlite3.Connection, project_id: str, run_id: str | None = None, now=None) -> dict:
    """derive() in its own BEGIN IMMEDIATE transaction."""
    require_guarded(conn)
    if conn.in_transaction:
        raise RuntimeError("reconcile_project opens its own BEGIN IMMEDIATE transaction; inside one call derive()")
    conn.execute("BEGIN IMMEDIATE")
    try:
        report = derive(conn, project_id, run_id=run_id, now=now)                          # derive() runs the migration gate first
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK"); raise
    return report


def reconcile_outdated(conn: sqlite3.Connection, now=None) -> list[dict]:
    """Startup: every project whose recorded rules_version is older than RULES_VERSION or whose derivation_pending flag is set."""
    require_guarded(conn)
    ids = [r[0] for r in conn.execute("SELECT id FROM projects WHERE COALESCE(rules_version, 0) < ? OR COALESCE(derivation_pending, 0) = 1 ORDER BY id", (RULES_VERSION,)).fetchall()]
    return [reconcile_project(conn, pid, now=now) for pid in ids]


def summary(report: dict) -> str:
    """One line per report for logs and the CLI (ids and counts only)."""
    return (f"{report['project_id']}: rules v{report['rules_version']}; decisions changed {len(report['decisions_changed'])}, support withdrawn {len(report['support_withdrawn'])}, restored {len(report['support_restored'])}, "
            f"conflicts opened {len(report['conflicts_opened'])} resolved {len(report['conflicts_resolved'])} reopened {len(report['conflicts_reopened'])}, claim status changed {len(report['claims_status_changed'])}, coverage rows changed {len(report['coverage_changed'])}")
