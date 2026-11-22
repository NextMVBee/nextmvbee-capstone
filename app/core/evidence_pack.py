"""Evidence packs. One pack per decision subject and eligible candidate identity, built by code from
the stored claims, verdicts, support records and conflicts, never by a model: the typed, capped and recorded evidence the comparison, the
panel (three models that read a draft comparison) and the tree (the bounded exploration of a decision's alternatives, app/core/tree.py) consume.

A claim belongs to a candidate when its recorded identity is the candidate's canonical identity (coverage.canonical_identity). Roles:
supporting (citable), contradicting (the verifier denied it, or it is in an open conflict), superseded (the loser of a settled
conflict, omitted with the reason). Every line keeps claim_id, source_id, product_id and product_version, the status and the
conditions copied verbatim. Ranking when a cap binds: hard-requirement claims, then contradictions on the decision's questions and open
conflicts, then verifier-confirmed support, then the rest by recency; ties by claim id. A material contradiction takes a reserved
slot whatever the cap and is named in must_address; when the slots left cannot hold one supporting claim for every binding hard requirement
that has one, the pack is insufficient_capacity and the decision stays open (reason pack_capacity). The token count is the budget
reservation's estimator (app.core.ledger.estimate_tokens)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from app.core import coverage, evidence
from app.core.ledger import estimate_tokens
from app.core.sqlguard import require_guarded

EVIDENCE_PACK_DEFAULTS = {"max_claims_per_pack": 40, "max_tokens_per_pack": 4000}   # the defaults were chosen from a measurement of stored packs (scripts/eval/pack_stats.py)
REASONS = ("cap", "duplicate", "superseded")
_CHUNK = 500


def caps_of(settings: dict | None) -> dict:
    """The two caps from settings [evidence_pack], the module defaults for a key the file does not set."""
    cfg = (settings or {}).get("evidence_pack") or {}
    return {k: max(0, int(cfg.get(k, v))) for k, v in EVIDENCE_PACK_DEFAULTS.items()}


def tokens_of(obj) -> int:
    """The budget reservation's estimate of one JSON value sent as one message."""
    return estimate_tokens([{"role": "user", "content": json.dumps(obj, ensure_ascii=False, default=str)}])


@dataclass
class PackInputs:
    """What one subject's packs are built from, loaded once per subject (load_inputs) and read by build_pack without the store."""
    subject: str
    hard: list                      # the binding hard requirements (id, key, value)
    rq_ids: list                    # the decision's research question ids
    citable: set                    # the claim ids the comparison may cite
    by_id: dict                     # the subject's verified claims (status not unverified) by id; an unverified claim of the decision's questions is in withheld
    support: dict                   # claim id -> binding hard requirement ids with a counting supported record at the current requirement version
    records: dict                   # claim id -> binding hard requirement ids with any verifier record at that version
    questions: dict                 # claim id -> the decision's research question ids whose coverage row attaches it
    conflicts: list                 # the open conflicts touching these claims: key, claim_ids, attribute, values, product_id, product_version
    superseded: dict = field(default_factory=dict)   # claim id -> reason: the loser of a settled conflict
    stale: dict = field(default_factory=dict)        # claim id -> why a supported, in-scope claim is not valid at the current versions (coverage.claim_rows' stale_reason)
    recency: dict = field(default_factory=dict)      # claim id -> the source's retrieved_at
    origin: dict = field(default_factory=dict)       # claim id -> {"subject", "plan_version", "questions"} for a claim joined from the subject's predecessor name (attached to its questions, not to the decision's own)
    withheld: dict = field(default_factory=dict)     # claim id -> the claim, for a claim of the decision's questions the selection withheld for its unverified status (workflow.subject_claims); listed in its identity's omitted list, never a line


def _chunks(ids: list):
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def load_inputs(conn, project_id: str, subject: str, hard: list, requirement_version: int, rel: list, citable: set, rq_claims: dict, rq_subject: dict, predecessor: dict | None = None) -> PackInputs:
    """The subject's inputs from the store: the support records at this requirement version for the binding hard requirements, the decision's
    questions per claim, the open conflicts and the settled conflicts' losers, the sources' read dates. Reads derived state inside the accessor.
    An open conflict's member outside rel (a claim of the same product that another subject's questions attach) is read
    from the store into by_id so that the pack shows both claims and the item names both values.
    predecessor (workflow.predecessor_of): the subject's earlier name and its plan version when a later plan renamed it; a claim of rel
    attached to that name's questions and to none of the decision's own is recorded in origin, and its pack line names where it came from."""
    require_guarded(conn)
    by_id = {c["id"]: c for c in rel}; ids = sorted(by_id)
    hard_ids = {h["id"] for h in hard}
    support: dict = {}; records: dict = {}; recency: dict = {}; conflicts: list = []; superseded: dict = {}
    mine_rqs = sorted(rq for rq, subj in rq_subject.items() if coverage._norm(subj or "") == coverage._norm(subject))
    mine = set(mine_rqs)
    questions = {cid: {rq for rq in rqs if rq in mine} for cid, rqs in rq_claims.items() if cid in by_id}
    origin: dict = {}
    if predecessor and predecessor.get("subject"):
        theirs = {rq for rq, subj in rq_subject.items() if coverage._norm(subj or "") == coverage._norm(predecessor["subject"])}
        for cid, rqs in rq_claims.items():
            if cid in by_id and not questions.get(cid) and rqs & theirs:
                origin[cid] = {"subject": predecessor["subject"], "plan_version": predecessor.get("plan_version"), "questions": sorted(rqs & theirs)}
    stale: dict = {}
    withheld_ids = sorted(cid for cid, rqs in rq_claims.items() if cid not in by_id and set(rqs) & mine)   # the decision's questions' claims the selection withheld (status unverified), read from the store for the omitted list; neither citable nor a line
    withheld = {c["id"]: c for c in (evidence.claims_with_sources(conn, project_id, withheld_ids) if withheld_ids else []) if c.get("status") == "unverified"}
    with coverage.derived(conn, project_id):
        for row in coverage.claim_rows(conn, project_id, ids, requirement_version):             # the currency of every claim of the selection, for the omitted list's reason
            if not row["valid"]:
                stale[row["id"]] = row["stale_reason"] or "not valid at the current requirement and source versions"
        for part in _chunks(ids):
            marks = ",".join("?" * len(part))
            if hard_ids:
                for cid, req, verdict, status in conn.execute(f"SELECT claim_id, requirement_id, verdict, status FROM claim_requirement_support WHERE project_id=? AND requirement_version=? AND claim_id IN ({marks})", (project_id, int(requirement_version), *part)).fetchall():
                    if req not in hard_ids:
                        continue
                    records.setdefault(cid, set()).add(req)
                    if verdict == "supported" and status == "counts":
                        support.setdefault(cid, set()).add(req)
            for cid, read in conn.execute(f"SELECT c.id, s.retrieved_at FROM claims c LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=? AND c.id IN ({marks})", (project_id, *part)).fetchall():
                recency[cid] = read or ""
        for key, cj, status, pref, reason in conn.execute("SELECT conflict_key, claim_ids_json, status, preferred_claim_id, reason FROM claim_conflicts WHERE project_id=? AND status IN ('open', 'settled') ORDER BY conflict_key", (project_id,)).fetchall():
            all_ids = json.loads(cj or "[]")
            members = [x for x in all_ids if x in by_id]
            if not members:
                continue
            if status == "settled":
                for x in members:
                    if x != pref:
                        superseded[x] = f"superseded by {pref} ({reason})"
                continue
            missing = [x for x in all_ids if x not in by_id]
            if missing:                                                                       # an open conflict's member that another subject's questions attach is read from the store and shown, so the item names both claims and both values
                for c in evidence.claims_with_sources(conn, project_id, missing):
                    by_id[c["id"]] = c; recency[c["id"]] = c.get("retrieved_at") or ""
                members = [x for x in all_ids if x in by_id]
            first = by_id[members[0]]
            conflicts.append({"key": key, "claim_ids": sorted(all_ids), "attribute": first.get("attribute") or "", "values": sorted({str(by_id[x].get("value") or "") for x in members}),
                              "product_id": first.get("product_id") or "", "product_version": first.get("product_version") or ""})
    return PackInputs(subject=subject, hard=list(hard), rq_ids=mine_rqs, citable=set(citable), by_id=by_id, support=support, records=records, questions=questions, conflicts=conflicts, superseded=superseded, recency=recency, origin=origin, stale=stale, withheld=withheld)


def _ts(x) -> float:
    try:
        return datetime.fromisoformat(str(x or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _line(c: dict, role: str, inputs: PackInputs, contradicts: str | None) -> dict:
    try:
        conditions = json.loads(c.get("conditions_json") or "[]")
    except (TypeError, ValueError):
        conditions = []
    line = {"claim_id": c["id"], "text": c.get("text") or "", "source_id": c.get("source_id") or "", "product_id": c.get("product_id") or "", "product_version": c.get("product_version") or "",
            "status": c.get("status") or "", "scope_status": c.get("scope_status") or "", "kind": c.get("kind") or "", "conditions": conditions if isinstance(conditions, list) else [conditions]}
    sup = sorted(inputs.support.get(c["id"], ())) if role == "supporting" else []
    qs = sorted(inputs.questions.get(c["id"], ()))
    if sup:
        line["supports"] = sup
    if qs:
        line["questions"] = qs
    if c["id"] in inputs.origin:                                                              # a claim joined from the subject's predecessor name names where it came from
        line["from"] = dict(inputs.origin[c["id"]])
    if contradicts:
        line["contradicts"] = contradicts
    return line


def build_pack(inputs: PackInputs, product_id: str, product_version: str, label: str, caps: dict) -> dict:
    """One candidate's pack from the subject's inputs under the caps. Pure: no store, deterministic for the same inputs."""
    max_claims, max_tokens = int(caps.get("max_claims_per_pack", EVIDENCE_PACK_DEFAULTS["max_claims_per_pack"])), int(caps.get("max_tokens_per_pack", EVIDENCE_PACK_DEFAULTS["max_tokens_per_pack"]))
    canon = coverage.canonical_identity(product_id, product_version or "")
    hard_ids = {h["id"] for h in inputs.hard}
    conflict_of = {}
    for k in inputs.conflicts:
        for x in k["claim_ids"]:
            conflict_of.setdefault(x, k)
    mine = [c for c in inputs.by_id.values() if c.get("product_id") and coverage.canonical_identity(c["product_id"], c.get("product_version") or "") == canon]
    entries = []; omitted = []; not_evidence = 0
    for c in sorted(mine, key=lambda c0: c0["id"]):
        cid = c["id"]
        if cid in inputs.citable:
            role, contradicts, material = "supporting", None, False
        elif cid in inputs.superseded:                                                     # a settled conflict's loser is superseded (load_inputs), read before its status: never a one-claim contradiction
            omitted.append({"claim_id": cid, "reason": "superseded"}); continue
        elif c.get("status") in ("contradicted", "conflicting") or cid in conflict_of:
            role = "contradicting"
            contradicts = "verifier" if c.get("status") == "contradicted" else (f"conflict:{conflict_of[cid]['key']}" if cid in conflict_of else "conflict")
            material = bool(inputs.records.get(cid, set()) & hard_ids) or bool(inputs.questions.get(cid)) or cid in conflict_of   # a conflicting status alone, without an open record, makes no material contradiction
        else:                                                                               # a claim of the identity the gate withholds is listed with the reason of its own: its status, its scope or its currency (never its question's status)
            not_evidence += 1
            if c.get("status") != "supported":
                omitted.append({"claim_id": cid, "reason": "status", "detail": c.get("status") or "unverified"})
            elif c.get("scope_status") not in ("in_scope", "conditional"):
                omitted.append({"claim_id": cid, "reason": "scope", "detail": c.get("scope_status") or "unchecked"})
            else:
                omitted.append({"claim_id": cid, "reason": "currency", "detail": inputs.stale.get(cid) or "not valid at the current requirement and source versions"})
            continue
        if role == "supporting" and inputs.support.get(cid, set()) & hard_ids:
            tier = 0
        elif role == "contradicting" and inputs.records.get(cid, set()) & hard_ids:
            tier = 0
        elif role == "contradicting" and material:
            tier = 1
        elif role == "supporting":
            tier = 2
        else:
            tier = 3
        key = (tier, cid) if tier < 3 else (tier, -_ts(inputs.recency.get(cid)), cid)
        entries.append({"key": key, "claim": c, "role": role, "contradicts": contradicts, "material": material, "line": _line(c, role, inputs, contradicts)})
    for cid, c in sorted(inputs.withheld.items()):                                             # a claim of this identity the selection withheld for its unverified status is accounted with that reason (never a line, never citable)
        if c.get("product_id") and coverage.canonical_identity(c["product_id"], c.get("product_version") or "") == canon:
            not_evidence += 1; omitted.append({"claim_id": cid, "reason": "status", "detail": c.get("status") or "unverified"})
    entries.sort(key=lambda e: e["key"])
    kept_by_fact: dict = {}; ranked = []                                                       # the duplicate rule: same attribute, value and conditions as an earlier-ranked supporting line whose roles cover it
    for e in entries:
        if e["role"] != "supporting":
            ranked.append(e); continue
        c = e["claim"]; fact = (coverage._norm(c.get("attribute") or ""), evidence.normalise_value(c.get("value")), evidence.conditions_of(c))
        kept = kept_by_fact.get(fact) if fact[0] and fact[1] else None
        if kept is not None and set(e["line"].get("supports", ())) <= set(kept["line"].get("supports", ())) and set(e["line"].get("questions", ())) <= set(kept["line"].get("questions", ())):
            omitted.append({"claim_id": c["id"], "reason": "duplicate"}); kept["line"].setdefault("duplicates", []).append(c["id"]); continue
        if fact[0] and fact[1]:
            kept_by_fact.setdefault(fact, e)
        ranked.append(e)
    included: list = []; count = 0; tokens = 0; chosen = set()

    def fits(e) -> bool:
        return count < max_claims and tokens + e["tokens"] <= max_tokens

    def take(e):
        nonlocal count, tokens
        included.append(e["line"]); chosen.add(e["claim"]["id"]); count += 1; tokens += e["tokens"]
    for e in ranked:
        e["tokens"] = tokens_of(e["line"])
    for e in ranked:                                                                           # the reserved slots of the material contradictions, whatever the cap
        if e["role"] == "contradicting" and e["material"]:
            take(e)
    did_not_fit = []
    for h in inputs.hard:                                                                      # one supporting claim per binding hard requirement that has one, the best ranked
        cands = [e for e in ranked if e["role"] == "supporting" and h["id"] in e["line"].get("supports", ())]
        if not cands or any(e["claim"]["id"] in chosen for e in cands):
            continue
        best = cands[0]
        if fits(best):
            take(best)
        else:
            did_not_fit.append({"requirement_id": h["id"], "claim_ids": [best["claim"]["id"]]})
    for e in ranked:
        cid = e["claim"]["id"]
        if cid in chosen:
            continue
        if fits(e):
            take(e)
        else:
            omitted.append({"claim_id": cid, "reason": "cap"})
    omitted.sort(key=lambda o: (o["reason"], o["claim_id"]))
    included = [e["line"] for e in ranked if e["claim"]["id"] in chosen]                      # the pack reads in rank order whatever the order the slots were taken in
    must_address = []
    for e in ranked:
        if e["role"] != "contradicting" or not e["material"]:
            continue
        cid = e["claim"]["id"]
        if e["contradicts"].startswith("conflict"):
            k = conflict_of.get(cid)
            if k is None or any(m.get("conflict_key") == k["key"] for m in must_address):   # a conflict item comes from an open conflict record only, so it names at least two claims and its key
                continue
            must_address.append({"kind": "conflict", "conflict_key": k["key"], "claim_ids": [x for x in k["claim_ids"] if x in inputs.by_id], "about": k.get("attribute") or e["claim"].get("attribute") or "", "values": k.get("values") or [str(e["claim"].get("value") or "")],
                                 "note": "two sources, or one source twice, give different values for this attribute of this product version; both claims are shown, neither is support"})
        else:
            about = sorted(inputs.records.get(cid, set()) & hard_ids) + sorted(inputs.questions.get(cid, ()))
            must_address.append({"kind": "verifier", "claim_ids": [cid], "about": ", ".join(about), "note": "the verifier found that the source does not state this claim; it is no support and the memo says what it means for the option"})
    present = {x for e in ranked for x in e["line"].get("supports", ())} | {x for e in ranked for x in inputs.records.get(e["claim"]["id"], ())}
    asked = {x for e in ranked for x in e["line"].get("questions", ())}
    pack = {"subject": inputs.subject, "product_id": product_id, "product_version": product_version or "", "label": label, "caps": {"max_claims_per_pack": max_claims, "max_tokens_per_pack": max_tokens},
            "claims": included, "must_address": must_address, "gaps": {"hard_requirements": [h["id"] for h in inputs.hard if h["id"] not in present], "research_questions": [rq for rq in inputs.rq_ids if rq not in asked]},
            "omitted": omitted, "insufficient_capacity": bool(did_not_fit), "did_not_fit": did_not_fit,
            "counts": {"considered": len(entries) + sum(1 for o in omitted if o["reason"] == "superseded"), "included": len(included), "omitted": len(omitted), "contradictions": sum(1 for e in entries if e["role"] == "contradicting" and e["material"]), "not_evidence": not_evidence}}
    reads = sorted(x for x in (inputs.recency.get(c["claim_id"]) for c in included) if x)
    pack["read_dates"] = {"newest": reads[-1] if reads else None, "oldest": reads[0] if reads else None}   # when the pages behind the pack's lines were read, for the memo's dates
    pack["tokens"] = tokens_of(prompt_view(pack))
    return pack


def pool_identities(identities: dict, excluded: dict, rel: list) -> tuple[dict, dict]:
    """The identities the packs are built for. The eligible set (eligible_candidates) when one of its
    identities carries a supported claim; otherwise the eligible set and, beside it, the identities the subject's supported claims name that the
    pool rules excluded (a model family with no version; no input and output token tariff of its own, or two models; a label the
    planner dropped as another class), one per canonical identity, labelled as any candidate (coverage.identity_label), so the supported
    claims reach the comparison as packs (otherwise a subject whose supported claims all sit on refused identities gets no pack). Returns (pool, the excluded
    identities in the pool with their reasons); no excluded identity activates (eligible_pick) and the memo's open list names each one."""
    supported = {coverage.canonical_identity(c.get("product_id") or "", c.get("product_version") or "") for c in rel or [] if c.get("status") == "supported" and c.get("product_id")}
    seen = {coverage.canonical_identity(*k) for k in identities}
    if seen & supported:
        return dict(identities), {}
    pool = dict(identities); used: dict = {}
    for k in sorted(excluded or {}):
        ck = coverage.canonical_identity(*k)
        if ck in seen or ck not in supported:
            continue
        seen.add(ck); pool[k] = coverage.identity_label(*k); used[k] = excluded[k]
    return pool, used


def build_packs(inputs: PackInputs, identities: dict, caps: dict) -> list[dict]:
    """One pack per identity of the pool ({(product_id, product_version): label}: the eligible set, and the excluded identities beside it when
    no eligible identity carries a supported claim, pool_identities), in identity order."""
    return [build_pack(inputs, k[0], k[1], label, caps) for k, label in sorted(identities.items())]


PROMPT_LINE_DROPS = ("questions", "kind", "from")   # the prompt's lines leave out the question ids, the kind and the origin (the stored pack keeps them): the memo never uses the question ids, and they cost many tokens; the origin is a record for the reader, not an input of the comparison


def prompt_view(pack: dict) -> dict:
    """What the comparison, the panel and the tree see: the included lines (without the question ids and the kind), must_address, the hard-requirement
    gaps by id, the question gaps as a count, and how many claims the cap left out. The stored pack keeps the full lists and fields."""
    gaps = pack.get("gaps") or {}
    return {"product_id": pack["product_id"], "product_version": pack["product_version"], "label": pack["label"], "claims": [{k: v for k, v in c.items() if k not in PROMPT_LINE_DROPS} for c in pack["claims"]], "must_address": pack["must_address"],
            "gaps": {"hard_requirements": list(gaps.get("hard_requirements") or []), "research_questions": len(gaps.get("research_questions") or [])}, "omitted_count": sum(1 for o in pack.get("omitted") or [] if o["reason"] == "cap")}


def claim_lines(packs: list[dict]) -> list[dict]:
    """Every included line of the packs as the prompt view carries them, in pack order (the tree's claims)."""
    return [c for p in packs for c in prompt_view(p)["claims"]]


def option_support(packs: list[dict], product_id: str | None, product_version: str | None, subject_wide: set) -> set:
    """The claim ids a memo option may cite: the
    supporting lines of the pack whose identity is the option's (coverage.canonical_identity, so a spelling of one identity finds its pack), the
    subject-wide supporting set only when the option names no identity. A subject-wide filter alone would let a claim of one model version
    pass under another version's option, because it is a supporting line of another identity's pack."""
    if not product_id:
        return set(subject_wide)
    canon = coverage.canonical_identity(product_id, product_version or "")
    return {c["claim_id"] for p in packs if coverage.canonical_identity(p["product_id"], p.get("product_version") or "") == canon for c in prompt_view(p)["claims"] if not c.get("contradicts")}


def pack_line(packs: list[dict]) -> str:
    """The memo's one line: the cap's omissions across the packs, by id, or 'no claim omitted'."""
    ids = sorted({o["claim_id"] for p in packs for o in (p.get("omitted") or []) if o["reason"] == "cap"})
    if not ids:
        return "no claim omitted"
    return f"{len(ids)} claim{'s' if len(ids) != 1 else ''} omitted by the cap: {', '.join(ids)}"


def event_packs(packs: list[dict]) -> list[dict]:
    """The trace's record per pack: the included ids, the omitted ids with their reason, the material contradictions, the flag, the tokens."""
    return [{"product_id": p["product_id"], "product_version": p["product_version"], "label": p["label"], "included": [c["claim_id"] for c in p["claims"]], "omitted": list(p.get("omitted") or []),
             "must_address": len(p.get("must_address") or []), "insufficient_capacity": bool(p.get("insufficient_capacity")), "did_not_fit": list(p.get("did_not_fit") or []), "tokens": p.get("tokens", 0)} for p in packs]


def summary(packs: list[dict]) -> dict:
    """The counts over a subject's packs: candidates, considered, included, omitted by reason, material contradictions, insufficient packs, tokens."""
    out = {"candidates": len(packs), "considered": 0, "included": 0, "omitted": 0, "omitted_cap": 0, "omitted_duplicate": 0, "omitted_superseded": 0, "omitted_status": 0, "omitted_scope": 0, "omitted_currency": 0, "contradictions": 0, "insufficient_capacity": 0, "not_evidence": 0, "tokens": 0}   # status, scope and currency: the three reasons of a withheld claim
    for p in packs:
        c = p["counts"]
        out["considered"] += c["considered"]; out["included"] += c["included"]; out["omitted"] += c["omitted"]; out["contradictions"] += c["contradictions"]; out["not_evidence"] += c["not_evidence"]
        out["insufficient_capacity"] += int(bool(p["insufficient_capacity"])); out["tokens"] += p.get("tokens", 0)
        for o in p.get("omitted") or []:
            out[f"omitted_{o['reason']}"] = out.get(f"omitted_{o['reason']}", 0) + 1
    return out
