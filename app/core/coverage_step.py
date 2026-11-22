"""The coverage step: one model call judges whether a product meets a hard requirement, from the product's verified claims.

What runs: after verification and before the comparison, for each decision subject, each product identity among the subject's
citable claims and each confirmed hard requirement that binds the subject (app/core/applicability), one model call (the coverage
prompt, CoverageOut) over the product's supported, in-scope claims with their verbatim quotes returns entails, partial, none or
contradicts with the claim ids it used. The strongest routed model answers (the gateway's model override). Checks run over
current requirements only.

What is stored: the verdicts are stored in coverage_checks per requirement version and product; counts_for_hard_constraints is
derived from them in code (entails at the current requirement version), never set by hand.

The three evidence states:
  contradicts is a proven violation under the disqualification rule: a candidate is disqualified only by a cited claim whose
  statement is explicit, current, from an authoritative source and about exactly the product and the requirement's conditions
  (a contradicts verdict that cites no claim is stored as none). The candidate is rejected with the claim quoted (violations,
  rejection_line).
  none is no supporting evidence, not proven and not disproven, so the decision is unresolved with its settling check
  (hard_status unproven). Nothing converts a stored none.
  sources that disagree are an open conflict shown before any decision (app/graph/workflow.py reads them).

When a stored verdict counts:
  only under the prompt version it was judged with (prompt_version; a row of an older prompt reads as unchecked and is owed again);
  only under the model identity it was judged with (model_id, the coverage stage's route the workflow sets for the run in
  progress through use_model; a row of another model, or one without a model record, reads as unchecked and is owed again);
  outside a run no model is in use and a stored row is read as it stands (a read is not a reuse);
  only while the claims it judged are unchanged: judged_json holds them, by id and source version, and any change is a fresh verdict.

Replay: the step key <run>:coverage:<worker>:<branch>:v<prompt>:<model>:d<n>:<subject>:<product>:<requirement>:<evidence> replays
a stored answer on resume, over the same claims, prompt version and model only.

Tested offline in tests/test_reuse_keys.py and tests/test_evidence_packs.py."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from app.core import applicability as AP
from app.core import wording as W
from app.core.sqlguard import require_guarded

VERDICTS = ("entails", "partial", "none", "contradicts")   # contradicts is a proven violation; none stays "not proven"
RANK = {"contradicts": 3, "entails": 2, "partial": 1, "none": 0}   # among spelling variants of one identity a proven violation outranks every other verdict


def prompt_version() -> int:
    """The coverage prompt's version in use (app/graph/prompts.py); the version a stored verdict must carry to count."""
    from app.graph import prompts
    return int(prompts.VERSIONS["coverage"])


MODEL_IN_USE: str | None = None   # the coverage stage's model identity of the run in progress (app/graph/workflow.py build_workflow sets it); None outside a run


def use_model(model: str | None) -> None:
    """The workflow names the coverage stage's route for the run in progress; None (no run) reads stored rows as they stand."""
    global MODEL_IN_USE
    MODEL_IN_USE = str(model) if model else None


def model_in_use() -> str | None:
    return MODEL_IN_USE


def counts(row_prompt_version, row_model_id) -> bool:
    """Whether a stored verdict counts now: under the prompt version in use and, inside a run, under the run's coverage model;
    a row without either record never counts inside a run."""
    m = model_in_use()
    return row_prompt_version == prompt_version() and (m is None or row_model_id == m)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-") or "x"


def product_claims(claims: list[dict], product_id: str, product_version: str) -> list[dict]:
    """The supported, in-scope (or conditional) claims of one product identity among the given claims."""
    return [c for c in claims if (c.get("product_id") or "") == product_id and (c.get("product_version") or "") == (product_version or "")
            and c.get("status") == "supported" and c.get("scope_status") in ("in_scope", "conditional")]


def judged_json(conn: sqlite3.Connection, project_id: str, product_id: str, product_version: str) -> str:
    """What a verdict on this product judges, as it is stored: the product's supported, in-scope claims now, by id and source version."""
    require_guarded(conn)
    from app.core.coverage import judged_claims
    return json.dumps(judged_claims(conn, project_id, product_id, product_version))


def record(conn: sqlite3.Connection, project_id: str, run_id: str | None, requirement_id: str, requirement_version: int, subject: str, product_id: str, product_version: str, verdict: str, claim_ids: list[str], note: str) -> None:
    """One verdict stored (replaces the row of the same requirement, version and product), with the claims it judged. The caller owns the transaction."""
    require_guarded(conn)
    if verdict not in VERDICTS:
        raise ValueError(f"unknown coverage verdict {verdict!r}")
    note_text = (note or "")[:1500 if is_calculated(note) else 300]; ids_json = json.dumps(sorted(set(claim_ids)))
    conn.execute("INSERT INTO coverage_checks (project_id, run_id, requirement_id, requirement_version, subject, product_id, product_version, verdict, claim_ids_json, note, judged_json, prompt_version, model_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(project_id, requirement_id, requirement_version, product_id, product_version) DO UPDATE SET run_id=excluded.run_id, subject=excluded.subject, verdict=excluded.verdict, claim_ids_json=excluded.claim_ids_json, note=excluded.note, judged_json=excluded.judged_json, prompt_version=excluded.prompt_version, model_id=excluded.model_id, created_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                 (project_id, run_id, requirement_id, int(requirement_version), subject, product_id, product_version or "", verdict, ids_json, note_text, judged_json(conn, project_id, product_id, product_version), prompt_version(), model_in_use()))   # the model identity the verdict was judged under


def is_calculated(note) -> bool:
    """Whether a stored verdict was computed by code (the budget by calculation): its note opens with the calculation's mark."""
    from app.core.budget import CALCULATED
    return str(note or "").startswith(CALCULATED)


def calculation_of(note) -> str:
    """The calculation's sentence of a computed verdict's note, without the mark."""
    from app.core.budget import CALCULATED
    return str(note or "")[len(CALCULATED):] if is_calculated(note) else ""


def _verdict_row(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int, product_id: str, product_version: str) -> tuple | None:
    """The coverage_checks row verdict_of reads: (verdict, claim_ids_json, note, product_id, product_version, created_at) of the exact spelling when its
    record counts, else the best counting row of the same product; None when none counts."""
    r = conn.execute("SELECT verdict, claim_ids_json, note, judged_json, prompt_version, model_id, product_id, product_version, created_at FROM coverage_checks WHERE project_id=? AND requirement_id=? AND requirement_version=? AND product_id=? AND product_version=?",
                     (project_id, requirement_id, int(requirement_version), product_id, product_version or "")).fetchone()
    if r is not None:
        return (r[0], r[1], r[2], r[6], r[7], r[8]) if r[3] == judged_json(conn, project_id, product_id, product_version) and counts(r[4], r[5]) else None   # a verdict counts only while the claims it judged are unchanged, only under the prompt version in use and, inside a run, only under the run's coverage model; a row without these records never counts inside a run
    if product_id:                                                                         # another spelling of the same identity (the claims named it two ways) carries the verdict; the best of them, a proven violation first
        from app.core.coverage import same_product
        rows = [x for x in conn.execute("SELECT verdict, claim_ids_json, note, product_id, product_version, judged_json, prompt_version, model_id, created_at FROM coverage_checks WHERE project_id=? AND requirement_id=? AND requirement_version=?",
                                        (project_id, requirement_id, int(requirement_version))).fetchall() if same_product(product_id, product_version or "", x[3], x[4]) and x[5] == judged_json(conn, project_id, x[3], x[4]) and counts(x[6], x[7])]
        best = max(rows, key=lambda x: RANK.get(x[0], -1)) if rows else None
        return (best[0], best[1], best[2], best[3], best[4], best[8]) if best else None
    return None


def stored_verdict(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int, product_id: str, product_version: str) -> tuple | None:
    """The counting row of exactly this identity as (verdict, claim ids, note), or None when no row of it counts now (none stored, the
    judged claims changed, another prompt version or model): what a recomputed calculation is compared with before it writes."""
    require_guarded(conn)
    r = conn.execute("SELECT verdict, claim_ids_json, note, judged_json, prompt_version, model_id FROM coverage_checks WHERE project_id=? AND requirement_id=? AND requirement_version=? AND product_id=? AND product_version=?",
                     (project_id, requirement_id, int(requirement_version), product_id, product_version or "")).fetchone()
    if r is None or r[3] != judged_json(conn, project_id, product_id, product_version) or not counts(r[4], r[5]):
        return None
    return (r[0], json.loads(r[1] or "[]"), r[2] or "")


def verdict_of(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int, product_id: str, product_version: str) -> dict | None:
    require_guarded(conn)
    r = _verdict_row(conn, project_id, requirement_id, requirement_version, product_id, product_version)
    return {"verdict": r[0], "claim_ids": json.loads(r[1] or "[]"), "note": r[2] or ""} if r else None


def verdict_id(project_id: str, requirement_id: str, requirement_version: int, product_id: str, product_version: str) -> str:
    """The id of a coverage_checks row: its primary key written as one string (the table has no id column)."""
    return f"{project_id}:{requirement_id}:{int(requirement_version)}:{product_id}:{product_version or ''}"


def hard_records(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, product_id: str, product_version: str) -> list[dict]:
    """What a hard check read, per binding hard requirement: the verdict, the id and date of the row it came from and the
    claim ids it rests on (verdict_of's reading); a requirement without a counting row is listed with verdict unchecked and no row. The tree node keeps
    the list behind its hard_status (tree_nodes.hard_check_json), so a later run's replacement of the row never changes what the node read."""
    require_guarded(conn)
    out = []
    for h in hard_binding:
        r = _verdict_row(conn, project_id, h["id"], requirement_version, product_id, product_version)
        if r is None:
            out.append({"requirement_id": h["id"], "verdict": "unchecked", "verdict_id": None, "checked_at": None, "claim_ids": [], "product_id": product_id, "product_version": product_version or ""})
        else:
            out.append({"requirement_id": h["id"], "verdict": r[0], "verdict_id": verdict_id(project_id, h["id"], requirement_version, r[3], r[4]), "checked_at": r[5], "claim_ids": json.loads(r[1] or "[]"), "product_id": product_id, "product_version": product_version or ""})   # the identity the check was for (the node's), so a reader of the node needs no other record
    return out


def checked_at(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, product_id: str, product_version: str) -> str | None:
    """When the identity's hard-requirement verdicts were written: the newest created_at among the rows that count
    (verdict_of's reading: the exact spelling or the same product, the judged claims unchanged, the prompt version and the model in use), or None."""
    require_guarded(conn)
    from app.core.coverage import same_product
    dates = []
    for h in hard_binding:
        for r in conn.execute("SELECT product_id, product_version, judged_json, prompt_version, model_id, created_at FROM coverage_checks WHERE project_id=? AND requirement_id=? AND requirement_version=?", (project_id, h["id"], int(requirement_version))).fetchall():
            if ((r[0] == product_id and (r[1] or "") == (product_version or "")) or same_product(product_id, product_version or "", r[0], r[1])) and r[2] == judged_json(conn, project_id, r[0], r[1]) and counts(r[3], r[4]) and r[5]:
                dates.append(str(r[5]))
    return max(dates) if dates else None


def entails(conn: sqlite3.Connection, project_id: str, requirement_id: str, requirement_version: int, product_id: str, product_version: str) -> bool:
    """counts_for_hard_constraints, derived: the stored verdict is entails at this requirement version."""
    require_guarded(conn)
    v = verdict_of(conn, project_id, requirement_id, requirement_version, product_id, product_version)
    return bool(v and v["verdict"] == "entails")


HARD_LEVELS = {"pass": 0, "unresolved": 1, "unproven": 2}


def hard_status(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, product_id: str, product_version: str) -> str:
    """pass when every binding hard requirement is entailed for the product; fail when one is contradicts (a proven violation); unproven when
    one is none (no supporting evidence: not proven, not disproven; the candidate is refused and the tree prunes it; the words say not proven);
    unresolved otherwise (partial or unchecked)."""
    require_guarded(conn)
    if not product_id:
        return "unresolved"
    worst = "pass"
    for h in hard_binding:
        v = verdict_of(conn, project_id, h["id"], requirement_version, product_id, product_version)
        if v is not None and v["verdict"] == "contradicts":
            return "fail"
        state = "pass" if v is not None and v["verdict"] == "entails" else ("unproven" if v is not None and v["verdict"] == "none" else "unresolved")
        if HARD_LEVELS[state] > HARD_LEVELS[worst]:
            worst = state
    return worst


def violations(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, product_id: str, product_version: str) -> list[dict]:
    """The binding hard requirements whose stored verdict is contradicts, each with the claim ids the verdict rests on and its note."""
    require_guarded(conn)
    out = []
    for h in hard_binding:
        v = verdict_of(conn, project_id, h["id"], requirement_version, product_id, product_version)
        if v is not None and v["verdict"] == "contradicts":
            out.append({"requirement": h, "claim_ids": list(v["claim_ids"]), "note": v["note"]})
    return out


def unproven(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, product_id: str, product_version: str) -> list[dict]:
    """The binding hard requirements whose stored verdict is none for the product (no supporting evidence)."""
    require_guarded(conn)
    return [h for h in hard_binding if (verdict_of(conn, project_id, h["id"], requirement_version, product_id, product_version) or {}).get("verdict") == "none"]


REJECTED = "Rejected on evidence: "


def violation_quote(conn: sqlite3.Connection, project_id: str, violation: dict) -> str:
    """The source and the exact quote of the contradicting claim behind one violation, by id: '<source> states "<quote>" (<claim id>)';
    the rejection line and the tree's prune reason both read it."""
    require_guarded(conn)
    from app.core.evidence import claims_with_sources
    if is_calculated(violation.get("note")):                                              # a budget violation is shown with its arithmetic, as an evidence violation with its quote
        return calculation_of(violation["note"]).rstrip(".")
    ids = [x for x in violation["claim_ids"] if x]
    cid = ids[0] if ids else None
    src = {c["id"]: c for c in claims_with_sources(conn, project_id, ids)}.get(cid) or {}
    quote = (conn.execute("SELECT COALESCE(quote, text) FROM claims WHERE project_id=? AND id=?", (project_id, cid)).fetchone() or [""])[0] if cid else ""
    where = src.get("url") or src.get("source_id") or "the source"
    return f"{where} states \"{str(quote or '')[:160]}\" ({cid or 'no claim'})"


def rejection_line(conn: sqlite3.Connection, project_id: str, option: str, violations: list[dict]) -> str:
    """The founder's sentence for a proven violation, from recorded fields: the option, the requirement and its value, the source and
    the exact quote of the contradicting claim, by id; one sentence per violated requirement."""
    require_guarded(conn)
    parts = []
    for v in violations:
        h = v["requirement"]
        key = str(h.get("key") or "").replace("_", " "); value = str(h.get("value") or "")[:60]
        if is_calculated(v.get("note")):                                                  # the arithmetic, in the founder's words, where the quote stands for an evidence violation
            parts.append(f"{W.requirement_words(h)} is not met: {calculation_of(v['note'])}")
            continue
        parts.append(f"{option} does not meet the hard requirement {key} ({value}): {violation_quote(conn, project_id, v)}.")
    return REJECTED + " ".join(parts)


def unproven_line(option: str, requirements: list[dict]) -> str:
    """The founder's sentence when the evidence states nothing about the requirement: not proven, not disproven; what settles it."""
    keys = ", ".join(str(h.get("key") or "").replace("_", " ") for h in requirements) or "the hard requirements"
    return f"No verified evidence shows that {option} meets the requirement {keys} (not proven, not disproven); a vendor page stating it would settle the decision."


CONDITIONAL = "Recommended, not yet proven on "


def conditional_line(option: str, requirements: list[dict], reasons=()) -> str:
    """The founder's sentence of a conditional recommendation: the option is recommended and one or more must-haves are not yet proven
    for it, each named by its label and value; nothing is relaxed in silence. reasons: for a requirement judged by calculation, why
    no figure stands (the calculation's own sentence)."""
    return (f"{CONDITIONAL}{'; '.join(W.requirement_words(h) for h in requirements)}: {option} is the recommended option, and no verified evidence shows yet that it meets {'this must-have' if len(requirements) == 1 else 'these must-haves'} (not proven, not disproven)."
            + "".join(f" {r}" for r in reasons if r))


def calculation_reasons(conn: sqlite3.Connection, project_id: str, option: dict, requirements: list[dict], requirement_version: int) -> list[str]:
    """For the requirements judged by calculation among the given ones, the calculation's sentence for the option (why no figure stands)."""
    require_guarded(conn)
    out = []
    for h in requirements:
        v = verdict_of(conn, project_id, h["id"], requirement_version, option.get("product_id") or "", option.get("product_version") or "") if option.get("product_id") else None
        if v is not None and is_calculated(v.get("note")):
            out.append(calculation_of(v["note"]))
    return out


def confirming_check(conn: sqlite3.Connection, project_id: str, option: dict, requirements: list[dict], requirement_version: int) -> str:
    """The check that would confirm a conditional recommendation, from recorded fields: the option, each unproven requirement by its
    label and value, and what confirms it (a supported, in-scope source statement; for a requirement judged by calculation, the
    figure that is missing, in the calculation's own words)."""
    require_guarded(conn)
    who = str(option.get("option") or "").strip()
    stated, missing = [], []
    for h in requirements:
        v = verdict_of(conn, project_id, h["id"], requirement_version, option.get("product_id") or "", option.get("product_version") or "") if option.get("product_id") else None
        if v is not None and is_calculated(v.get("note")):
            missing.append(calculation_of(v["note"]).rstrip("."))
        else:
            stated.append(W.requirement_words(h))
    parts = []
    if stated:
        parts.append(f"does {who} meet the requirement ({'; '.join(stated)})? A supported, in-scope source statement confirms it, and evidence against it rejects {who}")
    if missing:
        parts.append("; ".join(missing) + ": a sourced price in a unit your usage figures state, or the missing usage figure, lets the cost be calculated")
    return "The check that would confirm it: " + "; ".join(parts) + "."


def conditional_record(conn: sqlite3.Connection, project_id: str, decision_id: str) -> dict | None:
    """The conditional mark of a decision, read from the derived status rule's reason (app/core/reconcile.py: an active decision whose hard
    requirement has no counting support): the requirements it is not yet proven on (id, key, label, value), the founder's sentence and the check
    that would confirm it. The comparison's own record (memo_json.conditional) when it names the same requirements; otherwise composed here from
    the stored fields (a decision the reconciliation made conditional after the comparison). None for any other decision."""
    require_guarded(conn)
    from app.core import coverage, reconcile
    cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}                      # a store of an earlier schema holds a decision without these columns: it is no conditional recommendation
    if not {"derivation_reason", "memo_json", "product_id", "product_version"} <= cols:
        return None
    with coverage.derived(conn, project_id):
        row = conn.execute("SELECT status, derivation_reason, choice, memo_json, product_id, product_version FROM decisions WHERE id=? AND project_id=?", (decision_id, project_id)).fetchone()
    ids = reconcile.conditional_requirements(row[1]) if row and row[0] == "active" else []
    if not ids:
        return None
    stored = (json.loads(row[3]) if row[3] else {}).get("conditional") or {}
    if stored and sorted(q.get("id") for q in stored.get("requirements") or []) == sorted(ids):
        return stored
    reqs = [{"id": r[0], "key": r[1], "value": json.loads(r[2]) if r[2] else None} for r in conn.execute(f"SELECT id, key, value_json FROM requirements WHERE project_id=? AND id IN ({','.join('?' * len(ids))}) ORDER BY id", (project_id, *ids)).fetchall()]
    rv = int((conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [1])[0])
    option = {"option": row[2], "product_id": row[4] or "", "product_version": row[5] or ""}
    return {"requirements": [{"id": h["id"], "key": h["key"], "label": W.label_of(h["key"]), "value": W.value_words(h["value"])} for h in reqs], "line": conditional_line(row[2], reqs, calculation_reasons(conn, project_id, option, reqs, rv)), "check": confirming_check(conn, project_id, option, reqs, rv)}


def with_conditional(conn: sqlite3.Connection, project_id: str, decision_id: str, memo: dict | None) -> dict | None:
    """A decision's memo as every founder surface reads it: with the conditional record (and the state not_proven) when the decision is
    a conditional recommendation, unchanged otherwise. The mark is never left to a surface to remember."""
    require_guarded(conn)
    rec = conditional_record(conn, project_id, decision_id)
    if rec is None:
        return memo
    return {**(memo or {}), "conditional": rec, "evidence_state": "not_proven", "settling_check": (memo or {}).get("settling_check") or rec["check"]}


def conditional_lines(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """For the plan's checks: the sentence and the confirming check of every conditional recommendation, so the plan prints each
    unproven must-have with the recommendation it rides on."""
    require_guarded(conn)
    from app.core import coverage, reconcile
    with coverage.derived(conn, project_id):
        ids = [r[0] for r in conn.execute("SELECT id FROM decisions WHERE project_id=? AND status='active' AND derivation_reason LIKE ? ORDER BY id", (project_id, reconcile.CONDITIONAL + "%")).fetchall()]
    out = []
    for did in ids:
        rec = conditional_record(conn, project_id, did)
        if rec:
            out += [rec["line"], rec["check"]]
    return out


def verdicts_for(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, product_id: str, product_version: str) -> dict:
    """{requirement id: verdict or 'unchecked'} for the comparison's prompt input and the memo."""
    require_guarded(conn)
    out = {}
    for h in hard_binding:
        v = verdict_of(conn, project_id, h["id"], requirement_version, product_id, product_version)
        out[h["id"]] = v["verdict"] if v else "unchecked"
    return out


def tie_unresolved(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, claims: list[dict]) -> bool:
    """The claims split evenly between identities whose verdicts differ: neither the label nor the sort order settles which one the
    branch is, so the branch is unresolved, never pruned."""
    require_guarded(conn)
    from app.core.coverage import tied_identities
    return len({hard_status(conn, project_id, hard_binding, requirement_version, pid, ver) for pid, ver in tied_identities(claims)}) > 1


def counting_support(conn: sqlite3.Connection, project_id: str, requirement: dict, requirement_version: int, product_id: str, product_version: str, claim_ids) -> list[str]:
    """The one test the decision gate and the settling check share: the verifier's records among the cited claims, else the coverage step's entails,
    and then only the claims it used whose quote names the requirement's languages. A claim counts for the identity it names and at its
    level: a claim about one version never counts for an option that names no version, and a family
    claim never counts for a versioned option."""
    require_guarded(conn)
    from app.core.coverage import counts_for_identity, named_in_quote, supporting_claims
    v0 = verdict_of(conn, project_id, requirement["id"], requirement_version, product_id, product_version) if product_id else None
    if v0 is not None and is_calculated(v0.get("note")):                                    # a requirement judged by calculation counts by the calculation alone (its price claims), never by a sentence in a source
        return list(v0["claim_ids"]) if v0["verdict"] == "entails" else []
    ids = [x for x in claim_ids if x]
    if product_id and ids:
        rows = {r[0]: {"product_id": r[1], "product_version": r[2] or "", "text": r[3] or ""} for r in conn.execute(f"SELECT id, product_id, product_version, text FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(ids))})", (project_id, *ids)).fetchall()}
        ids = [x for x in ids if x in rows and counts_for_identity(product_id, product_version, rows[x])]
    return supporting_claims(conn, project_id, requirement["id"], requirement_version, ids) or (named_in_quote(conn, project_id, requirement, (verdict_of(conn, project_id, requirement["id"], requirement_version, product_id, product_version) or {}).get("claim_ids") or []) if entails(conn, project_id, requirement["id"], requirement_version, product_id, product_version) else [])


OPTION_ORDER = ("recommended", "runner-up", "bake-off", "watch", "later", "not now", "rejected")   # the memo's verdicts, leading first


def settling_check(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, options: list[dict]) -> str | None:
    """The concrete check an unresolved decision states when a binding hard requirement lacks counting support for its two leading
    options, in code from recorded fields (the requirement and its value, the options, the result that settles it); None when no requirement lacks it."""
    require_guarded(conn)
    leading = sorted([o for o in options if o.get("option") and not str(o.get("risk") or "").startswith(REJECTED)], key=lambda o: OPTION_ORDER.index(o.get("verdict")) if o.get("verdict") in OPTION_ORDER else len(OPTION_ORDER))[:2]   # a candidate rejected on evidence is no leading option; the rejection says what settled it
    lacking = [h for h in hard_binding if not any(counting_support(conn, project_id, h, requirement_version, o.get("product_id") or "", o.get("product_version") or "", list(o.get("claim_ids") or [])) for o in leading)]   # the gate's own test (counting_support), so a check is stated whenever the gate refuses
    if not leading or not lacking:
        return None
    return settling_sentence(leading, lacking)


def settling_sentence(leading: list[dict], lacking: list[dict]) -> str:
    """The settling sentence from its parts, each whole: the requirement's key and value as recorded (never cut at a length:
    a value cut on a space before the separator prints a broken sentence, for example "the traveller's text are not ; budget monthly usd"), an empty part
    dropped with its separator, the leading options named whole."""
    parts = []
    for h in lacking:
        key = str(h.get("key") or "").replace("_", " ").strip()
        value = "" if h.get("value") is None else str(h.get("value")).strip()
        part = f"{key}: {value}" if key and value else (key or value)
        if part:
            parts.append(part)
    wanted = "; ".join(parts)
    who = " or ".join(str(o.get("option") or "").strip() for o in leading if str(o.get("option") or "").strip())
    return f"The check that settles this: does {who} meet the requirement ({wanted})? A supported, in-scope source statement for one of them settles it for that option; if neither has one, the decision stays open."


def gave_up_check(conn: sqlite3.Connection, project_id: str, hard_binding: list[dict], requirement_version: int, leading: list[dict]) -> str | None:
    """The settling check of a comparison that gave up, from the subject's leading identities: the requirement they lack (settling_check's sentence),
    else, none lacking or none binding the subject (for example a hosting subject that no hard requirement binds), the check that ties the evidence to them; None without an identity."""
    require_guarded(conn)
    check = settling_check(conn, project_id, hard_binding, requirement_version, leading)
    if check or not leading:
        return check
    who = " or ".join(str(o.get("option") or "").strip() for o in leading if str(o.get("option") or "").strip())   # the labels whole
    return f"The check that settles this: can the verified claims be tied to {who}, by product and plan, with its price? A supported, in-scope source statement for one of them settles it for that option; if neither has one, the decision stays open."


def plan(subject: str, products: list[tuple[str, str]], hard_requirements: list[dict], requirement_version: int, conn, project_id: str) -> list[dict]:
    """The calls the step owes for a subject: one per product and binding hard requirement without a current verdict at this version (a verdict
    over claims that changed is owed again; the key names the claims, so a stored answer replays over the same claims only)."""
    require_guarded(conn)
    todo = []
    for pid, pver in products:
        if not pid:
            continue
        for h in AP.binding(hard_requirements, subject):
            if verdict_of(conn, project_id, h["id"], requirement_version, pid, pver) is None:
                evidence = hashlib.sha256(judged_json(conn, project_id, pid, pver).encode("utf-8")).hexdigest()[:12]
                todo.append({"subject": subject, "product_id": pid, "product_version": pver, "requirement": h, "key_part": f"{_slug(subject)}:{_slug(pid + ' ' + (pver or ''))}:{_slug(h['id'])}:{evidence}"})
    return todo


def prompt_values(check: dict, claims: list[dict], label: str) -> dict:
    """The coverage prompt's inputs for one check: the requirement, the subject, the product and the product's supported in-scope claims with
    their exact quotes inside untrusted tags (never page text beyond the stored quotes)."""
    h = check["requirement"]
    lines = []
    for c in claims:
        conds = "; ".join(c.get("conditions") or []) or "none"
        lines.append(f"Claim {c['id']}: {c['text']}\n<untrusted_source id=\"{c.get('source_id') or ''}\">{c.get('quote') or ''}</untrusted_source>\nConditions: {conds}")
    return {"requirement": {"id": h["id"], "key": h.get("key"), "value": h.get("value")}, "subject": check["subject"],
            "product": {"product_id": check["product_id"], "product_version": check["product_version"], "label": label}, "claims": "\n".join(lines) or "none"}


def apply(conn: sqlite3.Connection, project_id: str, run_id: str | None, check: dict, requirement_version: int, out, listed_ids: set[str]) -> dict:
    """Store one CoverageOut: only listed claim ids are kept; the verdict and the note recorded. The caller owns the transaction."""
    require_guarded(conn)
    used = [x for x in (out.claim_ids_used or []) if x in listed_ids]
    verdict = out.verdict if out.verdict in VERDICTS else "none"
    if verdict == "entails" and not used:                                   # an entailment rests on cited claims; none cited is partial at most
        verdict = "partial"
    if verdict == "contradicts" and not used:                               # a violation rests on a cited claim; none cited proves nothing (not proven)
        verdict = "none"
    record(conn, project_id, run_id, check["requirement"]["id"], requirement_version, check["subject"], check["product_id"], check["product_version"], verdict, used, out.note or "")
    return {"verdict": verdict, "claim_ids": used}
