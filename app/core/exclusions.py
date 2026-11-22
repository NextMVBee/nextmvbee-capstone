"""The record of every cut: each product identity a decision's weighing left out, with the rule, the reason and the date of the check.

A decision's weighing leaves identities out at several stages:
  the eligible set refuses an identity by rule (no model version, no token tariff of its own or two names, a class the planner,
  the research planning call of the workflow, dropped);
  the tree (the bounded exploration of a decision's alternatives, app/core/tree.py) refuses a family slug beside a versioned
  identity of its vendor and prunes a node on a proven violation;
  the evidence pack (app/core/evidence_pack.py) withholds a claim for its own status, scope or currency or leaves one out under its cap.
A reader must be able to trace every cut per identity, with its rule and the date of the check.

What is stored: record() persists the eligible set's and the tree's refusals one row per identity (candidate_exclusions, id
<project>.EXC-<n>), the tree node keeps what its hard check read (tree_nodes.hard_check_json) and the snapshot gathers, per
decision, one row per cut of every stage (recorded_rows, pack_rows, node_rows), each naming the identity, the stage, the rule,
the reason, the check's date and the record it rests on (the row's own id, the claim or the node).

Older stores: for a decision of an older store no run recorded rows for, derived_rows applies the eligible set's rules read-only
over the same records (source "export"), and history_rows names the identities that an earlier gate cut by the status of their
research question (no evidence pack stored; source "history").

Reading the rows: the documents (built by the document renderer, not part of this repository) print the rows in the indexed
evidence trail and the consequence in the body. A rule is named by the id its reason carries in parentheses (RULE_RE) or by
a pack's omitted reason; READER_WORDS gives each rule its reader phrase."""
from __future__ import annotations

import json
import re
import sqlite3

from app.core.sqlguard import require_guarded

STAGE_ELIGIBLE, STAGE_TREE, STAGE_PACK, STAGE_GATE = "eligible set", "tree", "evidence pack", "citable gate"
PACK_REASONS = ("status", "scope", "currency", "cap")           # the pack's omitted reasons that are cuts of the weighing; a duplicate or superseded line is covered by another line and is no cut
HISTORY_RULE = "question status"                                 # the gate of earlier runs: a question's conflicting status withheld every claim attached only to it
PRUNE_RULE = "D-A190"                                            # a node pruned on a proven violation (its reason starts with "confirmed hard-constraint failure", node_rows)
RULE_RE = re.compile(r"\((D-A\d+)\)")
RECORD_IDS_CAP = 12                                              # the claim ids a derived row names (sorted); record_count keeps the whole count
READER_WORDS = {"D-A143": "no input and output token price of its own was on record", "D-A133": "its name gives no model version", "D-A41": "the planner dropped it as a product of another class",
                "D-A301": "it was a product family named without its version", "D-A96": "the comparison's pick was outside the eligible set", PRUNE_RULE: "its own evidence contradicts a hard requirement",
                "status": "a claim of it was not supported by its source", "scope": "a claim of it fell outside the requirement's scope", "currency": "a claim of it was no longer valid at the current versions",
                "cap": "the evidence pack's size cap left a claim of it out", HISTORY_RULE: "its evidence hung on a research question whose sources disagreed at the time"}


STAGE_ORDER = {STAGE_ELIGIBLE: 0, STAGE_TREE: 1, STAGE_GATE: 2}   # the stages that cut an identity, in the order they cut; the evidence pack's omission is a cut of a claim, never of an identity
REASON_WORDS = (("names more than one model or version", "its name covers more than one model or version"), ("names no model version", READER_WORDS["D-A133"]),
                ("dropped under this subject by the planner", READER_WORDS["D-A41"]))   # a rule with several reasons (one rule refuses a missing tariff and a name covering two models) is read by the reason's own words first
CLAIM_WORDS = {"status": "its source did not support it", "scope": "it fell outside the requirement's scope", "currency": "it was no longer valid at the current versions", "cap": "the evidence pack's size cap left it out"}   # the pack's omitted reasons, said of the claim


def rule_of(reason: str | None) -> str:
    """The rule id a reason names in parentheses (RULE_RE), or ''."""
    m = RULE_RE.search(str(reason or ""))
    return m.group(1) if m else ""


def reader_words(rule: str, reason: str = "") -> str:
    """The reader phrase of a cut, for the body's consequence sentence: the reason's own words first (REASON_WORDS), then the rule's; a rule
    without one is named by its id."""
    for prefix, words in REASON_WORDS:
        if str(reason or "").startswith(prefix):
            return words
    return READER_WORDS.get(rule) or (f"rule {rule}" if rule else "a rule of the research")


def identity_cuts(rows: list[dict] | None) -> dict:
    """The surfaced alternatives among the rows: one entry per canonical identity (the label when a row names no identity) over the stages
    that cut identities (STAGE_ORDER), each with the rule and the reason of the stage that cut it first; a pack's claim omission makes no entry."""
    from app.core import coverage
    out: dict = {}
    for r in rows or []:
        if r.get("stage") not in STAGE_ORDER:
            continue
        key = coverage.canonical_identity(r.get("product_id") or "", r.get("product_version") or "") if r.get("product_id") else (r.get("label") or "")
        rank = (STAGE_ORDER[r["stage"]], r.get("rule") or "")
        if key not in out or rank < out[key][0]:
            out[key] = (rank, r.get("rule") or "", r.get("reason") or "")
    return out


def not_weighed_count(rows: list[dict] | None) -> int:
    """The number the body's sentence and the appendix index state: the surfaced alternatives, identity_cuts."""
    return len(identity_cuts(rows))


def record(conn: sqlite3.Connection, project_id: str, run_id: str, subject: str, decision_id: str | None, refused: dict, labels: dict | None = None, stage: str = STAGE_ELIGIBLE, rule: str | None = None, checked_at: str | None = None) -> list[str]:
    """Persist the refusals of one stage for one run and subject: one row per identity ((product_id, product_version) -> reason), the rule read from
    the reason unless given, the label coverage.identity_label unless given, the check's date now unless given. A second record of the same
    (run, subject, decision, identity, stage) replaces the row and keeps its id (the decision is part of the key, so the final's
    record never takes the draft's row; the tree's rows, recorded before the decision exists, match on decision_id IS NULL). Inside the caller's
    transaction or its own. Returns the ids in key order."""
    require_guarded(conn)
    from app.core import coverage
    from app.core.ids import next_number
    ids: list[str] = []

    def body():
        for key in sorted(refused or {}):
            pid0, ver0 = key[0] or "", (key[1] if len(key) > 1 else "") or ""
            if not pid0:
                continue
            reason = str(refused[key] or "")
            label = (labels or {}).get(key) or coverage.identity_label(pid0, ver0)
            row = conn.execute("SELECT id FROM candidate_exclusions WHERE project_id=? AND run_id=? AND subject=? AND decision_id IS ? AND product_id=? AND product_version=? AND stage=?", (project_id, run_id, subject, decision_id, pid0, ver0, stage)).fetchone()   # the row of this decision (IS: it matches the tree's NULL too)
            xid = row[0] if row else f"{project_id}.EXC-{next_number(conn, 'candidate_exclusions', project_id, 'EXC'):03d}"
            conn.execute("INSERT OR REPLACE INTO candidate_exclusions (id, project_id, run_id, subject, decision_id, product_id, product_version, label, stage, rule, reason, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ','now')))",
                         (xid, project_id, run_id, subject, decision_id, pid0, ver0, str(label)[:200], stage, rule if rule is not None else rule_of(reason), reason[:600], checked_at))
            ids.append(xid)
    if conn.in_transaction:
        body()
    else:
        with conn:
            body()
    return ids


def _row(product_id: str, product_version: str, label: str, stage: str, rule: str, reason: str, checked_at, record_ids: list[str], source: str, xid: str | None = None, record_count: int | None = None) -> dict:
    return {"id": xid, "product_id": product_id or "", "product_version": product_version or "", "label": label or "", "stage": stage, "rule": rule or "", "reason": reason or "", "checked_at": checked_at,
            "record_ids": list(record_ids), "record_count": record_count if record_count is not None else len(record_ids), "source": source}


def recorded_rows(conn: sqlite3.Connection, project_id: str, run_id: str | None, subject: str, decision_id: str, claim_ids_of) -> list[dict]:
    """The rows a run recorded for the decision: those naming it, and those of its run and subject recorded before the decision existed (the
    tree's refusals). claim_ids_of(product_id, product_version) -> the identity's supported claim ids, the record the rule read beside the row's own id."""
    require_guarded(conn)
    if "candidate_exclusions" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return []
    if run_id:
        rows = conn.execute("SELECT id, product_id, product_version, label, stage, rule, reason, checked_at FROM candidate_exclusions WHERE project_id=? AND (decision_id=? OR (decision_id IS NULL AND run_id=? AND subject=?)) ORDER BY id", (project_id, decision_id, run_id, subject)).fetchall()
    else:
        rows = conn.execute("SELECT id, product_id, product_version, label, stage, rule, reason, checked_at FROM candidate_exclusions WHERE project_id=? AND decision_id=? ORDER BY id", (project_id, decision_id)).fetchall()
    out = []
    for r in rows:
        ids = list(claim_ids_of(r[1], r[2]) or [])
        out.append(_row(r[1], r[2], r[3], r[4], r[5], r[6], r[7], ids[:RECORD_IDS_CAP], "run", xid=r[0], record_count=len(ids)))
    return out


def pack_rows(packs: list | None, checked_at_of) -> list[dict]:
    """The claims the evidence packs withheld or left out (the omitted list, each claim with its own reason): one row per claim, the
    identity the pack's, the rule the omitted reason, the record the claim. checked_at_of(claim_id) -> the claim's checked_at."""
    out = []
    for p in packs or []:
        if not isinstance(p, dict):
            continue
        for o in p.get("omitted") or []:
            reason = str(o.get("reason") or "")
            if reason not in PACK_REASONS:
                continue
            cid = str(o.get("claim_id") or "")
            detail = str(o.get("detail") or "")
            text = {"status": f"claim {cid} withheld: its status is {detail or 'not supported'}", "scope": f"claim {cid} withheld: its scope is {detail or 'outside the requirement'}",
                    "currency": f"claim {cid} withheld: {detail or 'not valid at the current versions'}", "cap": f"claim {cid} left out by the pack's cap"}[reason]
            out.append(_row(p.get("product_id") or "", p.get("product_version") or "", p.get("label") or "", STAGE_PACK, reason, text, checked_at_of(cid), [cid] if cid else [], "run"))
    return out


def node_rows(nodes: list[dict]) -> list[dict]:
    """The pruned nodes of the decision's tree: the rule the node's reason names (PRUNE_RULE for a proven violation), the check's date the newest
    of its hard check records, the record the node."""
    out = []
    for n in nodes or []:
        if not n.get("pruned"):
            continue
        reason = str(n.get("reason") or "pruned")
        rule = rule_of(reason) or (PRUNE_RULE if reason.startswith("confirmed hard-constraint failure") else "tree")
        hcs = [h for h in (n.get("hard_check") or []) if isinstance(h, dict)]
        dates = [str(h.get("checked_at")) for h in hcs if h.get("checked_at")]
        pid0, ver0 = n.get("product_id") or "", n.get("product_version") or ""
        if not pid0 and hcs:                                                        # the identity the node's hard check was for: the record names it (hard_records), or its verdict id ends with it
            pid0, ver0 = hcs[0].get("product_id") or "", hcs[0].get("product_version") or ""
            if not pid0 and hcs[0].get("verdict_id") and str(hcs[0]["verdict_id"]).count(":") >= 4:
                pid0, ver0 = str(hcs[0]["verdict_id"]).rsplit(":", 2)[1:]
        out.append(_row(pid0, ver0, n.get("label") or "", STAGE_TREE, rule, reason, max(dates) if dates else None, [n["id"]], "run"))
    return out


def derived_rows(conn: sqlite3.Connection, project_id: str, subject: str, rel: list[dict]) -> list[dict]:
    """For a decision no run recorded eligible-set rows for (an older store): the eligible set's refusals read again, read-only, over the subject's
    claims (app.graph.workflow.eligible_candidates: the same rules), one row per refused identity, the record the identity's
    supported claims (RECORD_IDS_CAP ids, the count whole), the check's date the newest checked_at among them; source export."""
    require_guarded(conn)
    from app.graph.workflow import eligible_candidates                 # lazy: the workflow imports this module's neighbours
    return _export_rows(eligible_candidates(conn, project_id, subject, rel).get("refused") or {}, rel)


def derived_rows_from_snapshot(subject: str, snapshot: dict, rel: list[dict]) -> list[dict]:
    """For a decision of a version stored before the cuts were recorded (no exclusions in
    its snapshot), the eligible set's refusals derived read-only from the version's own records, the one view the documents read: the subject's
    claims as the version holds them (rel) and whether the subject is a model class by the version's catalogue rows (candidates.class), through
    the same rule code the run uses (workflow.refuse_identities). The planner's dropped labels are not in a snapshot, so the dropped-class rule is
    not derived here. One row per refused identity with a supported claim, source export, as derived_rows."""
    from app.core import coverage
    from app.graph.workflow import is_model_class, refuse_identities   # lazy: the workflow imports this module's neighbours
    norm = coverage._norm
    model_subject = any(is_model_class(x.get("class")) for x in snapshot.get("candidates") or [] if isinstance(x, dict) and norm(str(x.get("subject") or "")) == norm(subject))
    _ident, refused = refuse_identities(rel, model_subject)
    return _export_rows(refused, rel)


def _export_rows(refused: dict, rel: list[dict]) -> list[dict]:
    """One row per refused identity ((product_id, product_version) -> reason) that has a supported claim among rel: the record the identity's supported
    claims (RECORD_IDS_CAP ids, the count whole), the check's date the newest checked_at among them; source export. A refused identity without a
    supported claim is no surfaced alternative (nothing a reader could have weighed)."""
    from app.core import coverage
    out = []
    for key in sorted(refused or {}):
        reason = str(refused[key])
        mine = sorted(c["id"] for c in rel if c.get("status") == "supported" and (c.get("product_id") or "", c.get("product_version") or "") == (key[0], key[1] or ""))
        if not mine:
            continue
        dates = [str(c.get("checked_at")) for c in rel if c["id"] in set(mine) and c.get("checked_at")]
        out.append(_row(key[0], key[1] or "", coverage.identity_label(key[0], key[1] or ""), STAGE_ELIGIBLE, rule_of(reason), reason, max(dates) if dates else None, mine[:RECORD_IDS_CAP], "export", record_count=len(mine)))
    return collapse(out)


def history_rows(subject: str, rel: list[dict], rq_claims: dict, rq_subject: dict, rq_status: dict) -> list[dict]:
    """The cut of earlier runs, whose gate went by question status, for a decision stored without an evidence pack: an identity whose supported claims are attached to
    no research question of the subject that reads supported and to at least one that reads conflicting was not citable under that gate;
    one row per identity naming the questions, the record the claims, source history."""
    from app.core import coverage
    norm = coverage._norm
    by_identity: dict = {}
    for c in rel:
        if c.get("status") != "supported" or not c.get("product_id"):
            continue
        by_identity.setdefault((c["product_id"], c.get("product_version") or ""), []).append(c)
    out = []
    for key in sorted(by_identity):
        mine = by_identity[key]
        supported_q, conflicting_q = set(), set()
        for c in mine:
            for rq in rq_claims.get(c["id"], ()):
                if norm(rq_subject.get(rq, "")) != norm(subject):
                    continue
                st = (rq_status.get(rq) or ("", ""))[0]
                (supported_q if st == "supported" else conflicting_q if st == "conflicting" else set()).add(rq)
        if supported_q or not conflicting_q:
            continue
        qs = sorted(conflicting_q)
        reason = (f"every supported claim of this identity was attached only to {len(qs)} research question{'s' if len(qs) != 1 else ''} which read conflicting at the time ({', '.join(qs)}): "
                  "under the run's code before D-A151 none was citable (the cut by question status; no evidence pack was stored)")   # the count stands in the sentence and the ids sit in parentheses, where the documents write each question's own words (document_model.reader_words_for)
        ids = sorted(c["id"] for c in mine)
        dates = [str(c.get("checked_at")) for c in mine if c.get("checked_at")]
        out.append(_row(key[0], key[1], coverage.identity_label(key[0], key[1]), STAGE_GATE, HISTORY_RULE, reason, max(dates) if dates else None, ids[:RECORD_IDS_CAP], "history", record_count=len(ids)))
    return collapse(out)


def collapse(rows: list[dict]) -> list[dict]:
    """One row per canonical identity, stage and rule (two spellings of one identity are one identity): the first row's label and reason, the
    newest check date, the record ids merged (RECORD_IDS_CAP kept, the count summed), the first row's id and source; the input order kept."""
    from app.core import coverage
    out: list[dict] = []; index: dict = {}
    for r in rows or []:
        key = (coverage.canonical_identity(r.get("product_id") or "", r.get("product_version") or "") if r.get("product_id") else (r.get("label") or ""), r.get("stage"), r.get("rule"), r.get("reason") if r.get("stage") == STAGE_PACK else "")
        if key in index:
            k = index[key]
            k["checked_at"] = max(x for x in (k.get("checked_at"), r.get("checked_at")) if x) if (k.get("checked_at") or r.get("checked_at")) else None
            merged = list(dict.fromkeys(list(k["record_ids"]) + list(r.get("record_ids") or [])))
            k["record_ids"] = merged[:RECORD_IDS_CAP]; k["record_count"] = (k.get("record_count") or 0) + (r.get("record_count") if r.get("record_count") is not None else len(r.get("record_ids") or []))
            continue
        index[key] = dict(r, record_ids=list(r.get("record_ids") or []), record_count=r.get("record_count") if r.get("record_count") is not None else len(r.get("record_ids") or []))
        out.append(index[key])
    return out


def rows_from_snapshot(decision: dict, snapshot: dict) -> list[dict]:
    """The Not weighed rows of a decision for the documents: the snapshot's own exclusions when the version carries them;
    for an older version, what its records still show, in one view: the eligible set's refusals derived from the version's own
    claims and catalogue rows (derived_rows_from_snapshot, source export), the pruned nodes and the question-status cut of its coverage rows
    (history_rows over the version's coverage lines, which hold the questions' statuses and claim ids as they were when the version was written)."""
    if "exclusions" in decision:
        return collapse(decision.get("exclusions") or [])
    claims = {c["id"]: c for c in snapshot.get("claims") or []}
    rq_claims: dict = {}; rq_status: dict = {}; rq_subject: dict = {}
    for cov in snapshot.get("coverage") or []:
        if cov.get("subject_type") != "research_question":
            continue
        rq_status[cov["subject_id"]] = (cov.get("evidence_status"), cov.get("processing_status"))
        rq_subject[cov["subject_id"]] = str(cov.get("subject") or "").split(":", 1)[0]
        for cid in cov.get("claim_ids") or []:
            rq_claims.setdefault(cid, set()).add(cov["subject_id"])
    for q in snapshot.get("research_questions") or []:
        rq_subject[q["id"]] = q.get("decision_subject") or rq_subject.get(q["id"], "")
    from app.core import coverage
    norm = coverage._norm
    mine = {cid for cid, qs in rq_claims.items() if any(norm(rq_subject.get(q, "")) == norm(decision.get("subject") or "") for q in qs)}
    rel = [c for cid, c in claims.items() if cid in mine]
    subject = decision.get("subject") or ""
    return collapse(derived_rows_from_snapshot(subject, snapshot, rel) + node_rows(decision.get("tree_nodes") or []) + history_rows(subject, rel, rq_claims, rq_subject, rq_status))


def consequence(rows: list[dict], appendix_entry: str, appendix_name: str = "Appendix A") -> str:
    """The body's one or two code-composed sentences: how many surfaced alternatives (canonical identities, identity_cuts) were not
    weighed and why, in the reader's words of the reason that cut each first; the claims the evidence packs left out stated apart, as claims; the
    second sentence naming the document's own appendix (appendix_name) and the entry that holds the detailed table; '' when nothing was cut."""
    idents = identity_cuts(rows)
    by_rule: dict = {}
    for _rank, rule, reason in idents.values():
        w = reader_words(rule, reason); by_rule[w] = by_rule.get(w, 0) + 1
    by_claim: dict = {}; j = 0
    for r in rows or []:
        if r.get("stage") == STAGE_PACK:
            k = r.get("record_count") if r.get("record_count") is not None else (len(r.get("record_ids") or []) or 1)
            w = CLAIM_WORDS.get(r.get("rule") or "") or reader_words(r.get("rule") or "", r.get("reason") or ""); by_claim[w] = by_claim.get(w, 0) + k; j += k
    if not idents and not j:
        return ""
    order = lambda d: "; ".join(f"{k} because {w}" for w, k in sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))   # noqa: E731
    n = len(idents)
    claims_part = f"{j} claim{'s' if j != 1 else ''} {'were' if j != 1 else 'was'} left out of {'the evidence packs' if j != 1 else 'an evidence pack'}: " + order(by_claim) if j else ""
    if n:
        first = f"{n} surfaced alternative{'s' if n != 1 else ''} {'were' if n != 1 else 'was'} not weighed for this decision: " + order(by_rule) + (f"; and {claims_part}" if j else "") + "."
    else:
        first = claims_part + "."
    second = f"{appendix_name} lists each under {appendix_entry}, with its stage, its rule and the date of the check."
    return first + " " + second
