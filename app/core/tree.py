"""Bounded alternative exploration (Tree of Thought) for one decision: expand up to 3 alternatives, then up to 3
implementation variants each, no further depth. At most 7 model calls including retries; a partial result is produced
when the limit is reached. Branches are judged by deterministic hard-constraint checks and an evidence-grounded rubric.
Pruned branches keep their scores, claims and reasons; an unverified constraint stays unresolved, never a rejection."""
import json
from dataclasses import dataclass, field
from app.core.sqlguard import require_guarded

FACET_KINDS = ("route", "approach")                                      # a variant of these kinds is a facet of its parent identity, never a scored sibling

RUBRIC_WEIGHTS = {"fit": 0.35, "evidence": 0.25, "cost": 0.15, "risk": 0.15, "currency": 0.10}   # the fifth term, currency, is computed by code; fit, evidence and cost keep 0.75 of the weight, so the newest model need not win
MODEL_TERMS = ("fit", "evidence", "cost", "risk")                                              # the terms the model scores (tree_evaluate.v2); the currency is never asked of it
CURRENCY_BOTH, CURRENCY_ONE, CURRENCY_NONE = 10, 5, 0


class CallLimit(Exception):
    pass


@dataclass
class Node:
    id: str
    label: str
    depth: int
    parent_id: str | None = None
    hard_status: str = "unchecked"
    score: float | None = None
    claim_ids: list = field(default_factory=list)
    pruned: bool = False
    reason: str | None = None
    selected: bool = False
    facets: list = field(default_factory=list)                           # the route and approach variants folded into this node ({label, kind, access_route, claim_ids})
    product_id: str = ""                                                 # the identity the resolution gave the node from its cited claims (or the declared pair the claims know); its evidence may grow, this does not; "" when the claims named none (the workflow's own rule names it then)
    product_version: str = ""
    currency: int = 0                                                    # the currency term code computed for the node (0, 5 or 10), carried into the branch event; never persisted as a score of the model
    hard_check: list = field(default_factory=list)                       # what the hard check read behind hard_status (coverage_step.hard_records), persisted with the node


@dataclass
class Limits:
    max_alternatives: int = 3
    max_variants: int = 3
    max_depth: int = 2
    max_model_calls: int = 7


class CallBudget:
    def __init__(self, limit: int):
        self.limit, self.used = limit, 0

    def take(self, n: int = 1):
        if self.used + n > self.limit:
            raise CallLimit(f"model-call limit of {self.limit} reached")
        self.used += n


def rubric_score(ev: dict, currency: int = 0) -> float:
    """The rubric over the model's four terms and the currency code computed: a currency the model may have written into its answer is never read."""
    return round(sum(RUBRIC_WEIGHTS[k] * float(ev[k]) for k in MODEL_TERMS) + RUBRIC_WEIGHTS["currency"] * float(currency or 0), 3)


def _own_page(product_id: str, url: str, tier=None) -> bool:
    """The page is the identity's own by the records' rule alone (evidence.standing): a tier-1
    source, or a host that is exactly the vendor's identity word under one label or a subdomain of it (anthropic.com, platform.openai.com); never
    because a word of the identity is in the host (compute-reviews.invalid is not example-compute's, medium.com is not mistral-medium's)."""
    from app.core import evidence as EV
    return EV.standing({"url": url, "product_id": product_id, "tier": tier if tier is not None else 3})[0] == 0


def currency_score(product_id: str, product_version: str, newest_source: dict | None, today=None) -> int:
    """The currency term of a branch, by code from the records: 10 when the identity names a version
    (coverage.names_version) and its newest supported claim's source is the vendor's own page (_own_page: evidence.standing, a
    tier-1 page or the vendor's host) read within the freshness window of its class and the claim's kind (freshness.fresh_until against today), 5 when
    one of the two holds, 0 when neither. newest_source: {url, retrieved_at, tier, freshness, kind} of the newest read among the node's citable
    claims, or None."""
    from datetime import datetime, timezone
    from app.core import coverage as CV, discovery as DS, freshness as FR
    versioned = bool(product_id) and CV.names_version(product_id, product_version or "")
    fresh = False
    if newest_source and newest_source.get("url"):
        own = _own_page(product_id or "", newest_source["url"], newest_source.get("tier"))         # the vendor's own page by the records' rule (a tier-1 source, the vendor's host or its subdomain)
        read = FR.parse_ts(newest_source.get("retrieved_at"))
        if own and read is not None:
            cls = newest_source.get("freshness") if newest_source.get("freshness") in FR.POLICY else "technical"
            until = FR.fresh_until(cls, read, newest_source.get("kind"))
            now = FR.parse_ts(today) or datetime.now(timezone.utc)
            fresh = until is not None and until >= now
    return CURRENCY_BOTH if versioned and fresh else (CURRENCY_ONE if versioned or fresh else CURRENCY_NONE)


def _vendor_of(product_id: str) -> str:
    return (product_id or "").split("-", 1)[0]


def _known_identities(claims: list[dict]) -> tuple[dict, dict]:
    """The identities the claims passed to the model name: the canonical key -> (product_id, product_version) as stored, and the vendor's
    versioned identities (the vendor is the first token of the normalised id), the one most claims name first."""
    from app.core import coverage as CV
    known, counts = {}, {}
    for c in claims:
        if c.get("product_id"):
            key = CV.canonical_identity(c["product_id"], c.get("product_version") or "")
            known.setdefault(key, (c["product_id"], c.get("product_version") or "")); counts[key] = counts.get(key, 0) + 1
    versioned_by_vendor: dict[str, list[tuple[str, str]]] = {}
    for key, (pid0, ver0) in sorted(known.items(), key=lambda kv: (-counts[kv[0]], kv[1])):
        if ver0:
            versioned_by_vendor.setdefault(_vendor_of(CV.normalise_identity(pid0, "")[0]), []).append((pid0, ver0))
    return known, versioned_by_vendor


def _identity_of(entry: dict, by_id: dict, known: dict) -> tuple[str, str, bool]:
    """The identity of a proposed node is the one most of its cited claims name (app.core.coverage.majority_identity,
    the identity the workflow later gives the node); a node citing no identity takes the (product_id, product_version) it declares when the claims
    know that pair, and otherwise the slug of its label (so a family label with its claim ids filtered away is still judged as the family). The
    third value says whether the claims name the identity (the majority or the known pair); the slug judges the family rule and names
    no node."""
    from app.core import coverage as CV
    pid0, ver0 = CV.majority_identity([by_id[x] for x in entry.get("claim_ids") or [] if x in by_id], entry.get("label") or "")
    if pid0:
        return pid0, ver0, True
    given = CV.canonical_identity(entry.get("product_id") or "", entry.get("product_version") or "") if entry.get("product_id") else None
    if given in known:
        return (*known[given], True)
    return CV.normalise_identity(entry.get("label") or "", "")[0], "", False


def _family_reason(pid0: str, ver0: str, versioned_by_vendor: dict) -> str | None:
    """An identity that names no version while a versioned identity of the same vendor is among the claims is a family, refused with the
    reason naming both; None when the identity may be a node."""
    from app.core import coverage as CV
    if pid0 and not CV.normalise_identity(pid0, ver0)[1]:
        others = versioned_by_vendor.get(_vendor_of(CV.normalise_identity(pid0, "")[0]), [])
        if others:
            named = list(dict.fromkeys(f"{p0} {v0}".strip() for p0, v0 in others))
            return f"{pid0} names no version while the claims name {', '.join(named[:3])}{' and others' if len(named) > 3 else ''} of the same vendor (D-A301): the family is a label, never a node"
    return None


def resolve_alternatives(alts: list[dict], claims: list[dict]) -> tuple[list[dict], list[dict]]:
    """The identity of every proposed alternative, from the claims passed to the model, and the family rule: an
    alternative whose identity names no version while a versioned identity of the same vendor is among the claims is refused with the reason
    naming both (the family stays a label, never a node: for example the roots anthropic-claude, openai-gpt-4o and ai21-labs-jamba beside
    anthropic-claude-haiku 5.5, openai-gpt-4o-mini 2024.07.18 and ai21-jamba-large 1.7). The cited claims decide the
    identity (_identity_of); the declared pair stands only for an alternative citing no identity, so the identity judged here is the identity
    the node gets. Returns (kept, refused); a kept alternative carries its resolved identity (the identity the claims name; an
    alternative they name none for carries none, and tree.explore gives the node what it carries)."""
    by_id = {c.get("claim_id") or c.get("id"): c for c in claims}
    known, versioned_by_vendor = _known_identities(claims)
    kept, refused = [], []
    for a in alts:
        pid0, ver0, named = _identity_of(a, by_id, known)
        why = _family_reason(pid0, ver0, versioned_by_vendor)
        if why:
            refused.append(dict(a, product_id=pid0, product_version="", reason=why))
            continue
        kept.append(dict(a, product_id=pid0 if named else "", product_version=ver0 if named else ""))
    return kept, refused


def resolve_variants(variants: list[dict], claims: list[dict]) -> tuple[list[dict], list[dict]]:
    """The family rule binds the variants as it binds the alternatives: a variant of kind product is judged by
    the identity its cited claims name (or its declared pair, or its label's slug when it cites no identity) and refused when that identity names
    no version while a versioned identity of the same vendor is among the claims; a variant of kind route or approach is a facet of its parent
    (tree.explore folds it in) and passes unjudged. Returns (kept, refused); a kept product variant carries its resolved identity (the
    identity the claims name, none otherwise)."""
    by_id = {c.get("claim_id") or c.get("id"): c for c in claims}
    known, versioned_by_vendor = _known_identities(claims)
    kept, refused = [], []
    for v in variants:
        if (v.get("kind") or "product") in FACET_KINDS:
            kept.append(v)
            continue
        pid0, ver0, named = _identity_of(v, by_id, known)
        why = _family_reason(pid0, ver0, versioned_by_vendor)
        if why:
            refused.append(dict(v, product_id=pid0, product_version="", reason=why))
            continue
        kept.append(dict(v, product_id=pid0 if named else "", product_version=ver0 if named else ""))
    return kept, refused


def validate_result(result: dict, claims_exist=None) -> list[str]:
    """Checks before a tree may drive a decision: rubric scores in range, at most one selection, cited claims exist,
    hard-constraint states known, calls within the limit."""
    issues = []
    sel = [n for n in result["nodes"] if n.selected]
    if len(sel) > 1:
        issues.append("more than one selected node")
    for n in result["nodes"]:
        if n.score is not None and not (0 <= n.score <= 10):
            issues.append(f"{n.id}: score out of range")
        if n.hard_status not in ("pass", "fail", "unresolved", "unchecked"):
            issues.append(f"{n.id}: unknown hard-constraint state")
        if claims_exist is not None:
            unknown = [c for c in n.claim_ids if not claims_exist(c)]
            if unknown:
                issues.append(f"{n.id}: unknown claims {unknown}")
    if result["calls_used"] > result.get("limit", result["calls_used"]):
        issues.append("model-call limit exceeded")
    return issues


def explore(subject: str, expand_alternatives, expand_variants, evaluate, hard_check, limits: Limits = Limits(), retries: int = 0, fail_reason=None, currency=None) -> dict:
    """expand_* and evaluate are model-backed callables (each counts one model call; batched evaluation is one call);
    hard_check is deterministic code returning pass, fail or unresolved. retries counts repair calls already spent. fail_reason, when given, names
    why a failed node is pruned (no supporting evidence reads as not proven, never as a confirmed failure); None keeps the confirmed wording.
    currency is deterministic code returning the node's currency term (0, 5 or 10; currency_score) read before the scoring; None scores every node at 0."""
    reason_of = lambda node: (fail_reason(node) if fail_reason else None) or "confirmed hard-constraint failure"
    budget = CallBudget(limits.max_model_calls); budget.used = retries
    nodes: list[Node] = []; partial, reason = False, None
    try:
        budget.take(1)
        alts = expand_alternatives(subject)[: limits.max_alternatives]
        for i, a in enumerate(alts):
            n = Node(id=f"A{i+1}", label=a["label"], depth=1, claim_ids=list(a.get("claim_ids", [])), product_id=a.get("product_id") or "", product_version=a.get("product_version") or "")   # the resolved identity is the node's; the facets' and the evaluation's claims join its evidence below and never change it
            n.hard_status = hard_check(n)
            if n.hard_status == "fail":
                n.pruned, n.reason = True, reason_of(n)
            nodes.append(n)
        for n in [x for x in nodes if not x.pruned and limits.max_depth >= 2]:
            try:
                budget.take(1)
            except CallLimit as e:
                partial, reason = True, str(e); break
            j = 0
            for v in expand_variants(n.label)[: limits.max_variants]:
                if (v.get("kind") or "product") in FACET_KINDS:                           # a route or an approach is a facet of the parent identity: its claims join the parent's, it is never a scored sibling
                    n.facets.append({"label": v["label"], "kind": v["kind"], "access_route": v.get("access_route") or "", "claim_ids": list(v.get("claim_ids", []))})
                    n.claim_ids = sorted(set(n.claim_ids) | set(v.get("claim_ids", [])))
                    continue
                j += 1
                c = Node(id=f"{n.id}.{j}", label=v["label"], depth=2, parent_id=n.id, claim_ids=list(v.get("claim_ids", [])), product_id=v.get("product_id") or "", product_version=v.get("product_version") or "")
                c.hard_status = hard_check(c)
                if c.hard_status == "fail":
                    c.pruned, c.reason = True, reason_of(c)
                nodes.append(c)
            if n.facets and not n.pruned:
                n.hard_status = hard_check(n)                                              # the parent's claim set grew: its hard check reads the new set before the scoring
                if n.hard_status == "fail":
                    n.pruned, n.reason = True, reason_of(n)
        live = [x for x in nodes if not x.pruned]
        evals = {}
        if live:                                                         # no branch to score, no evaluation send (a call over an empty branch list buys nothing)
            budget.take(1)                                               # one batched evaluation call
            evals = evaluate(live)
        for x in live:
            ev = evals.get(x.id)
            if ev:
                x.claim_ids = sorted(set(x.claim_ids) | set(ev.get("claim_ids", [])))
                x.currency = int(currency(x)) if currency else 0                          # the currency term by code over the node's final claim set, never the model's number
                x.score = rubric_score(ev, x.currency)
                x.hard_status = hard_check(x)                            # the final claim set is rechecked by code before the selection
                if x.hard_status == "fail":
                    x.pruned, x.reason = True, reason_of(x) + " (evidence added at evaluation)"   # the scored trace is kept
    except CallLimit as e:
        partial, reason = True, str(e)
    leaves = [x for x in nodes if not x.pruned and x.score is not None and (x.depth == 2 or not any(c.parent_id == x.id for c in nodes))]
    chosen = max(leaves, key=lambda x: (x.score, x.hard_status == "pass", -len(x.id), x.id), default=None) if leaves else None   # at equal score a branch whose hard constraints are proven outranks an unresolved one
    if chosen:
        chosen.selected = True
    return {"subject": subject, "nodes": nodes, "selected_id": chosen.id if chosen else None, "limit": limits.max_model_calls,
            "selected_unresolved": bool(chosen and chosen.hard_status == "unresolved"), "partial": partial, "reason": reason, "calls_used": budget.used}


def persist(conn, run_id: str, result: dict) -> int:
    """Replace the whole snapshot for this run and decision subject in one transaction (a new version), so an obsolete
    winner never survives beside a new one. Parent ids are stored fully qualified."""
    require_guarded(conn)
    issues = validate_result(result)
    if issues:
        raise ValueError("; ".join(issues))
    q = lambda nid: f"{run_id}:{result['subject']}:{nid}"
    with conn:
        prev = conn.execute("SELECT COALESCE(MAX(version),0) FROM tree_nodes WHERE run_id=? AND decision_subject=?", (run_id, result["subject"])).fetchone()[0]
        conn.execute("DELETE FROM tree_nodes WHERE run_id=? AND decision_subject=?", (run_id, result["subject"]))
        for n in result["nodes"]:
            conn.execute("INSERT INTO tree_nodes (id, run_id, decision_subject, parent_id, depth, label, hard_status, score, claim_ids_json, pruned, reason, selected, version, facets_json, hard_check_json) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (q(n.id), run_id, result["subject"], q(n.parent_id) if n.parent_id else None, n.depth, n.label, n.hard_status,
                                                                n.score, json.dumps(n.claim_ids), int(n.pruned), n.reason, int(n.selected), prev + 1, json.dumps(getattr(n, "facets", []) or []), json.dumps(getattr(n, "hard_check", []) or [])))   # the facets folded into the node, and what its hard check read
    return prev + 1
