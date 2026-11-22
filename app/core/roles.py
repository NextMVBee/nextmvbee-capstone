"""Specialised roles under one application-controlled coordinator. Roles are prompt, contract, tools and memory scope,
instantiated only when needed. Research workers run in parallel (at most three) on independent tasks and return
proposed evidence records; the coordinator validates and merges them, preserves provenance and exposes conflicts."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from app.core.sqlguard import require_guarded

ROLES = {
    "Clarifier": dict(objective="turn an idea into confirmed requirements and at most three blocking questions", inputs="idea text, answers", outputs="ClarifyOut", tools="none", memory="requirements, questions", done="ready_for_research or questions pending", failure="failure shape; founder decides"),
    "Researcher": dict(objective="find current sources for one decision subject", inputs="decision subject, target facts", outputs="EvidenceProposal list", tools="search, read, ask_user", memory="read: requirements, source library; write: proposals only", done="target facts covered or limits reached", failure="partial proposals with the reason"),
    "Verifier": dict(objective="check each high-impact claim against stored text, scope and conditions", inputs="claim, passage", outputs="VerificationOut", tools="re_fetch", memory="read: sources, claims; write: verdicts", done="every high-impact claim has a verdict", failure="insufficient verdict, never a guess"),
    "Comparer": dict(objective="explore bounded alternatives for one decision", inputs="requirements, supported claims", outputs="tree with a selected branch", tools="none", memory="read: claims, requirements; write: tree nodes", done="selection or partial with reason", failure="partial tree, unresolved constraints visible"),
    "Planner": dict(objective="assemble the traceable plan", inputs="decisions, claims, requirements", outputs="PlanOut", tools="none", memory="read all; write: plan version", done="trace validator passes", failure="failure shape; plan not published"),
    "Reviewer": dict(objective="check the whole plan for consistency after any change", inputs="plan, requirements", outputs="issues list", tools="none", memory="read all; write: flags", done="no open issues or issues listed", failure="issues listed for the founder"),
}


def researcher_contract():
    """The Researcher worker's structured output (prompts/runtime/researcher_worker.v1.md): the proposals, or the failure shape."""
    from pydantic import Field
    from schemas.models import Strict, EvidenceProposal

    class ResearcherOut(Strict):
        proposals: list[EvidenceProposal] = Field(default_factory=list, max_length=20)
        failure_reason: str | None = Field(default=None, max_length=300)
    return ResearcherOut


@dataclass
class WorkerResult:
    worker: str
    task_id: str
    status: str
    proposals: list = field(default_factory=list)
    error: str | None = None


MAX_WORKERS = 3   # settings [workers] max_research_workers may lower this; never raised at call sites
WORKER_TOOLS = frozenset({"search", "read"})      # the tools a Researcher worker may execute; ask_user is the coordinator's alone



def run_workers(tasks: list[dict], worker_fn, max_workers: int = MAX_WORKERS) -> list[WorkerResult]:
    """Run independent research tasks in parallel. Each task gets a unique worker id (so step keys never collide);
    at most min(max_workers, MAX_WORKERS) run at once. A failing worker never stops the others."""
    max_workers = max(1, min(max_workers, MAX_WORKERS))
    def one(i, task):
        wid = f"w{i + 1}-{task['task_id']}"
        try:
            return WorkerResult(wid, task["task_id"], "completed", list(worker_fn(wid, task)))
        except Exception as e:
            return WorkerResult(wid, task["task_id"], "failed", [], str(e)[:200])
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(lambda it: one(*it), enumerate(tasks)))


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _product(p: dict) -> tuple[str, str]:
    from app.core.coverage import product_identity
    return product_identity(p.get("vendor"), p.get("product"), p.get("product_version"), p.get("subject"))


def claim_key(p: dict) -> tuple:
    """Claim identity: the normalised product id and version (claims about different products or
    versions never merge), subject, attribute, value, conditions and the claim text, plus the passage it cites.
    Two different atomic claims backed by one passage are two claims; the passage is deduplicated separately."""
    pid, ver = _product(p)
    return (pid, ver, _norm(p.get("subject", "")), _norm(p.get("attribute", "")), _norm(p.get("value", "")), tuple(sorted(_norm(c) for c in p.get("conditions", []))),
            _norm(p.get("claim_text", "")), _norm(p["source_url"]), _norm(p["quote"]))


def extraction_identity(c: dict) -> str:
    """Claim identity for extraction merges: the atomic triple subject, attribute, value when the model
    filled it, otherwise the claim text and its quote. Conditions are deliberately not part of this identity: the same
    claim found at the end of one batch without its qualification and at the start of the next with it is one claim whose
    conditions accumulate (condition continuation adds to the same record the same way). claim_key() above stays the
    coordinator's full identity for worker proposals."""
    pid, ver = _product(c)                                                   # the product id and version are part of every identity
    head = f"{pid}@{ver}:"
    triple = (_norm(c.get("subject", "") or ""), _norm(c.get("attribute", "") or ""), _norm(c.get("value", "") or ""))
    if all(triple):
        return head + "sav:" + "|".join(triple)
    return head + "tq:" + _norm(c.get("text", "") or c.get("claim_text", "") or "") + "|" + _norm(c.get("quote", "") or "")


def merge_proposals(confirmed_ids: set[str], results: list[WorkerResult]) -> dict:
    """Coordinator merge: validate every proposal, deduplicate claims by claim identity (passages separately), expose
    conflicts, keep provenance, refuse writes to confirmed records. Order-independent: sorted by a deterministic key."""
    from pydantic import ValidationError
    from schemas.models import EvidenceProposal
    accepted, dupes, conflicts, rejected, malformed, seen, by_attr, passages = [], [], [], [], [], {}, {}, {}
    raw = [(r, p) for r in results if r.status == "completed" for p in r.proposals]
    proposals = []
    for r, p in raw:
        try:
            proposals.append(EvidenceProposal.model_validate(p).model_dump())
        except (ValidationError, TypeError) as e:
            malformed.append({"worker": r.worker, "task_id": r.task_id, "error": str(e)[:160]})
    proposals.sort(key=lambda p: (claim_key(p), p["worker"], p["task_id"]))
    for p in proposals:
        if any(w in confirmed_ids for w in p.get("writes", [])):
            rejected.append({"proposal": p, "reason": "would overwrite a confirmed requirement or approved decision"}); continue
        key = claim_key(p); pkey = (_norm(p["source_url"]), _norm(p["quote"]))
        passages.setdefault(pkey, []).append(p["worker"])
        if key in seen:
            seen[key]["provenance"].append(p["worker"]); dupes.append(p); continue
        rec = dict(p, provenance=[p["worker"]], passage_workers=passages[pkey]); seen[key] = rec; accepted.append(rec)
        attr = (p.get("subject", ""), p.get("attribute", ""))
        if attr != ("", "") and attr in by_attr and _norm(by_attr[attr]["value"]) != _norm(p.get("value", "")):
            conflicts.append({"attribute": attr, "values": [by_attr[attr]["value"], p.get("value", "")], "sources": [by_attr[attr]["source_url"], p["source_url"]]})
        by_attr.setdefault(attr, rec)
    failed = [r for r in results if r.status == "failed"]
    accepted.sort(key=claim_key)
    return {"accepted": accepted, "duplicates": len(dupes), "passages": len(passages), "conflicts": conflicts, "rejected": rejected, "malformed": malformed,
            "failed_workers": [(r.worker, r.task_id, r.error) for r in failed]}


def persist_results(conn, run_id: str, results: list[WorkerResult]) -> None:
    require_guarded(conn)
    with conn:
        for r in results:
            conn.execute("INSERT OR REPLACE INTO worker_results (id, run_id, worker, task_id, status, proposals_json, error) VALUES (?,?,?,?,?,?,?)",
                         (f"{run_id}:{r.task_id}:{r.worker}", run_id, r.worker, r.task_id, r.status, json.dumps(r.proposals), r.error))
