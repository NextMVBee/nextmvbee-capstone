"""A none verdict does not prune: the hard check of the tree (the bounded exploration of a decision's alternatives, app/core/tree.py) prunes a branch on a contradicts verdict (the proven
violation) and on the budget arithmetic only; a none verdict leaves the branch unresolved, and unresolved branches survive, are
scored and can be selected (otherwise a run can prune every alternative of a decision on a none verdict and end with no branch
selected, no memo, no settling check). A tree that selects no branch never leaves the decision without a memo: the comparison decides on the
packs as in the simple configuration (the comparison without the tree). Offline, through the Runner on the pipeline fake (tests/pipeline_fake.py; the coverage step on, the verdict under the scenario's
control). No network, no keys.

    tp1  three candidates, every coverage verdict none on the binding hard requirements: no branch pruned, every evaluated node scored, one
         branch selected, the decision unresolved with its memo stored, the state not_proven and the settling check first in the memo's open
         list; the branch event shows no pruned node and the reasoning record says 0 pruned
    tp2  one candidate's verdict contradicts, the others none: that branch alone is pruned, its reason names the requirement and quotes the
         contradicting claim by id; the others are unresolved and scored; a branch is selected and the memo stored
    tp3  a candidate whose cited price exceeds the hard budget at the founder's usage is pruned by the arithmetic (the reason the arithmetic
         gives), the other candidate, none on every verdict, is unresolved, scored and selected; the memo stored with its settling check
    tp4  the tree's generator proposes nothing: no node, no evaluation send, the tree's outcome recorded (the branch event with no node and the
         step that says the tree selected no branch), the comparison's memo send under the subject's simple key, the memo stored with its
         settling check
    tp5  every alternative pruned (one on the budget, one on contradicts): no evaluation send over an empty branch list, the comparison decides
         on the packs, the pick rejected on the proven violation with the claim quoted and the memo stored
    tp6  the tie-break (tree.explore, a unit check): two leaves with equal rubric scores, one whose hard constraints pass and one unresolved: the
         proven branch is selected whatever the node order; a better-scoring unresolved branch still wins (unresolved branches can be selected)
    tp7  the language-naming rule inside the tree's hard check: two identities, the dearer one pruned by the
         budget arithmetic, the other with entails on both binding requirements but a languages quote that names no language: that node's
         hard status is unresolved (not pass, not fail), it is not pruned, its variants are expanded and scored, it is selected and the
         selection is recorded as conditional (the tree's reasoning record, the memo record); the gate then refuses activation for lack of language-naming support with the state not_proven
         and the reason the records name (the quote does not name Hebrew, English), the settling check first in the memo's open list
    tp8  the control of tp7: the same run with the quote that names Hebrew and English: the node passes, is selected without the conditional
         mark, and the decision is active on it with the languages claim as counting support

Run one scenario with its name as the argument, or all of them without one."""
import copy, json, os, pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("NMB_TEST_FIXTURES", "1")

results = []
def check(name, cond, detail=None):
    results.append((name, bool(cond))); assert cond, (name if detail is None else f"{name} [{detail}]")
import background_threads; background_threads.install()

import pipeline_fake as PF
from app.core.sqlguard import inspect as insp
from app.runner import events as ev

SE = copy.deepcopy(PF.S); SE["extraction"]["coverage_step"] = True
NO_BRANCH = "Tree selected no branch"
CHECK_START = "The check that settles this: does "


class TwinScript(PF.Script):
    """The pipeline fake's Script with two more identities (versions v2 and v3, no price claim, so the budget arithmetic stays out of it) and
    the coverage verdict under the scenario's control per product version (verdict_for: version -> verdict; the default verdict otherwise)."""
    def __init__(self, twins=True, verdict_for=None):
        super().__init__(questions=False, subjects=("transcription",))
        self.entail_none_for = ("languages", "price"); self.coverage_verdict = "none"
        self.twins, self.verdict_for = twins, verdict_for or {}
    def __call__(self, body):
        s = PF.schema_of(body)
        if s == "CoverageOut" and self.verdict_for:
            u = PF.all_user_text(body)
            product = u[u.index("Product: ") + len("Product: "): u.index("\n", u.index("Product: "))]
            listed = [cid for cid, _ in re.findall(r"^Claim (\S+): (.*)$", u, flags=re.M)]
            for ver, verdict in self.verdict_for.items():
                if f"'product_version': '{ver}'" in product or f'"product_version": "{ver}"' in product or f" {ver}" in product:
                    return PF.J({"verdict": verdict, "claim_ids_used": listed[:20] if verdict == "contradicts" else [], "note": f"scripted {verdict} for version {ver}"})
        res = super().__call__(body)
        if self.twins and s in ("EvidenceExtractionOut", "EvidenceExtractionOutV2"):
            data = json.loads(res["content"]); extra = []
            for c in data["claims"]:
                if c["kind"] != "price" and "second" not in c["attribute"] and "third" not in c["attribute"]:
                    extra.append(dict(c, attribute=c["attribute"] + "_second", subject="Speech API A v2", text=c["text"].replace("v3", "v2") + " (second)", product_version="v2"))
                    extra.append(dict(c, attribute=c["attribute"] + "_third", subject="Speech API A v3", text=c["text"] + " (third)", product_version="v3"))
            data["claims"] += extra
            return PF.J(data)
        return res


def run(name, script, settings=None):
    r, stub = PF.runner_for("tp_" + name, script, settings=settings or SE)
    res = r.start("P1", PF.IDEA)
    for _ in range(4):
        if res["status"] != "awaiting_user":
            break
        res = r.resume(res["run_id"], PF.answers_for(res))
    c = r.conn; rid = res["run_id"]
    nodes = [dict(zip(("id", "depth", "label", "hard_status", "score", "pruned", "reason", "selected", "hard_check"), x[:8] + (json.loads(x[8] or "[]"),))) for x in c.execute("SELECT id, depth, label, hard_status, score, pruned, reason, selected, hard_check_json FROM tree_nodes WHERE run_id=? ORDER BY id", (rid,))]   # hard_check_json: what the node's hard check read
    decisions = [{"id": d[0], "subject": d[1], "status": d[2], "choice": d[3], "rationale": d[4], "memo": json.loads(d[5]) if d[5] else None, "rejected": json.loads(d[6] or "[]")}
                 for d in insp(c).execute("SELECT id, subject, status, choice, rationale, memo_json, rejected_json FROM decisions ORDER BY id")]
    events = ev.events(c, rid)
    branch = [d for _, e, d in events if e == "branch"]
    steps = [d for _, e, d in events if e == "step"]
    tree_keys = [k[0].split(":", 4)[4] for k in c.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? ORDER BY planned_at, attempt", (rid, f"{rid}:tree:%"))]
    cmp_keys = [k[0].split(":", 2)[2] for k in c.execute("SELECT DISTINCT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? ORDER BY step_key", (rid, f"{rid}:compare_options:%"))]
    records = [tuple(x) for x in c.execute("SELECT action, observation, change FROM reasoning_records WHERE run_id=? AND action='bounded tree (T25)'", (rid,))]
    schemas = [PF.schema_of(b) for b in stub.bodies]
    return {"res": res, "conn": c, "rid": rid, "nodes": nodes, "decisions": decisions, "branch": branch, "steps": steps, "tree_keys": tree_keys, "cmp_keys": cmp_keys, "records": records, "schemas": schemas}


def memo_facts(out):
    d = out["decisions"][-1] if out["decisions"] else {"status": None, "memo": None, "rationale": "", "rejected": []}
    memo = d["memo"] or {}
    return d, memo


# ---------------------------------------------------------------- tp1: three candidates, every verdict none: nothing pruned, a branch selected, the memo with its settling check
def tp1():
    out = run("all_none", TwinScript())
    d, memo = memo_facts(out)
    depth1 = [n for n in out["nodes"] if n["depth"] == 1]; evaluated = [n for n in out["nodes"] if not n["pruned"] and (n["depth"] == 2 or not any(m["depth"] == 2 for m in out["nodes"]))]
    detail = {"status": out["res"]["status"], "depth1": [(n["label"], n["hard_status"], n["pruned"], n["score"]) for n in depth1], "selected": [n["id"] for n in out["nodes"] if n["selected"]], "decision": (d["status"], d["rationale"][:120]), "memo_keys": sorted(memo)[:8], "records": out["records"]}
    check("tp1 three candidates, every coverage verdict none on the binding hard requirements: three depth-1 nodes, every node unresolved, none pruned, every evaluated node scored, exactly one branch selected",
          out["res"]["status"] == "completed" and len(depth1) == 3 and all(n["hard_status"] == "unresolved" for n in out["nodes"]) and not any(n["pruned"] for n in out["nodes"])
          and evaluated and all(n["score"] is not None for n in evaluated) and sum(1 for n in out["nodes"] if n["selected"]) == 1, detail)
    check("tp1 the selected branch carries its unproven status into the memo (Phase 19 package 19.1, D-A408: a conditional recommendation): the decision is active on the selected branch with its memo stored, the state not_proven, the rationale opens Recommended, not yet proven and says not proven, and the confirming check stands in the memo's open list after the conditional sentence",
          d["status"] == "active" and d["memo"] is not None and memo.get("evidence_state") == "not_proven" and d["rationale"].startswith("Recommended, not yet proven on ") and "not proven" in d["rationale"]
          and str(memo.get("settling_check", "")).startswith("The check that would confirm it: ") and (memo.get("open") or [None, None])[:2] == [(memo.get("conditional") or {}).get("line"), memo.get("settling_check")], {"decision": (d["status"], d["rationale"][:160]), "check": memo.get("settling_check"), "open": (memo.get("open") or [])[:2], "state": memo.get("evidence_state")})
    check("tp1 the branch event shows no pruned node and a selection, the reasoning record says 0 pruned and names the selection, and the tree's memo send ran under the subject's memo key",
          len(out["branch"]) == 1 and len(out["branch"][0]["nodes"]) == len(out["nodes"]) and not any(n["pruned"] for n in out["branch"][0]["nodes"]) and out["branch"][0]["selected"] is not None
          and any(", 0 pruned, " in r[1] and r[2].startswith("selected A") for r in out["records"]) and any(k.endswith(":memo") for k in out["cmp_keys"]) and not any(s.get("label") == NO_BRANCH for s in out["steps"]),
          {"branch": out["branch"][:1], "records": out["records"], "cmp_keys": out["cmp_keys"]})


# ---------------------------------------------------------------- tp2: one candidate contradicts, the others none: that branch alone pruned with the claim quoted
def tp2():
    out = run("one_contradicts", TwinScript(verdict_for={"2": "contradicts"}))
    d, memo = memo_facts(out)
    depth1 = [n for n in out["nodes"] if n["depth"] == 1]; pruned = [n for n in out["nodes"] if n["pruned"]]
    cids = [x[0] for x in out["conn"].execute("SELECT id FROM claims WHERE project_id='P1' AND product_version='2'")]
    quotes = [x[0] for x in out["conn"].execute("SELECT DISTINCT quote FROM claims WHERE project_id='P1' AND product_version='2'")]
    detail = {"depth1": [(n["label"], n["hard_status"], n["pruned"], n["score"], (n["reason"] or "")[:200]) for n in depth1], "pruned": [(n["label"], n["reason"]) for n in pruned], "selected": [n["id"] for n in out["nodes"] if n["selected"]]}
    check("tp2 one candidate's verdict contradicts, the others none: exactly one depth-1 node is pruned (version 2, hard status fail), its reason names the violated requirement and quotes the contradicting claim by id; no variant of it is explored",
          len(depth1) == 3 and len(pruned) == 1 and pruned[0]["depth"] == 1 and pruned[0]["hard_status"] == "fail" and " 2" in pruned[0]["label"] and str(pruned[0]["reason"]).startswith("confirmed hard-constraint failure: ")
          and ("languages" in pruned[0]["reason"] or "budget" in pruned[0]["reason"]) and any(cid in pruned[0]["reason"] for cid in cids) and any(q and q[:60] in pruned[0]["reason"] for q in quotes)
          and not any(n["depth"] == 2 and n["id"].rsplit(":", 1)[-1].startswith(pruned[0]["id"].rsplit(":", 1)[-1] + ".") for n in out["nodes"]), detail)
    check("tp2 the other two candidates are unresolved and scored, one branch is selected and the decision, a conditional recommendation on the selected branch (D-A408), has its memo with the confirming check",
          sum(1 for n in depth1 if n["hard_status"] == "unresolved" and not n["pruned"]) == 2 and all(n["score"] is not None for n in out["nodes"] if not n["pruned"] and n["depth"] == 2)
          and sum(1 for n in out["nodes"] if n["selected"]) == 1 and d["status"] == "active" and d["memo"] is not None and (memo.get("conditional") or {}).get("requirements") and str(memo.get("settling_check", "")).startswith("The check that would confirm it: "), dict(detail, decision=(d["status"], d["rationale"][:160]), check=memo.get("settling_check")))

    hc = pruned[0]["hard_check"]; hard_ids = {x[0] for x in out["conn"].execute("SELECT id FROM requirements WHERE project_id='P1' AND hard=1")}
    check("tp2 Phase 18 package 18.3 (D-A383): the pruned node stores what its hard check read behind its hard_status: the requirement, the verdict contradicts, the verdict's id and date and the claim ids the verdict rests on; an unpruned node whose verdicts are none keeps its records too",
          len(hc) >= 1 and all(x["requirement_id"] in hard_ids and x["verdict_id"] and x["checked_at"] and isinstance(x["claim_ids"], list) for x in hc) and any(x["verdict"] == "contradicts" and x["claim_ids"] for x in hc)
          and all(n["hard_check"] and all(x["verdict"] == "none" or (x["requirement_id"] == "P1.REQ-001" and x["verdict"] == "entails") for x in n["hard_check"]) for n in depth1 if not n["pruned"]), {"pruned": hc, "others": [n["hard_check"] for n in depth1 if not n["pruned"]]})


# ---------------------------------------------------------------- tp3: a cited price above the budget prunes by arithmetic; the none candidate survives and is selected
def tp3():
    sc = TwinScript(twins=False); sc.second_product = True
    out = run("budget_prunes", sc)
    d, memo = memo_facts(out)
    depth1 = [n for n in out["nodes"] if n["depth"] == 1]; pruned = [n for n in out["nodes"] if n["pruned"]]; sel = [n for n in out["nodes"] if n["selected"]]
    detail = {"depth1": [(n["label"], n["hard_status"], n["pruned"], n["score"], n["reason"]) for n in depth1], "selected": [(n["label"], n["hard_status"]) for n in sel], "decision": (d["status"], d["rationale"][:160])}
    check("tp3 two candidates, every verdict none, the dearer version's cited price above the hard budget at the founder's usage: that branch alone is pruned by the arithmetic (hard status fail, the arithmetic's reason naming the budget requirement), the other is unresolved, scored and selected",
          len(depth1) == 2 and len(pruned) == 1 and pruned[0]["depth"] == 1 and " 2" in pruned[0]["label"] and pruned[0]["hard_status"] == "fail" and pruned[0]["reason"].startswith("confirmed hard-constraint failure: budget monthly usd (")
          and sum(1 for n in depth1 if n["hard_status"] == "unresolved" and not n["pruned"]) == 1 and len(sel) == 1 and sel[0]["hard_status"] == "unresolved" and sel[0]["score"] is not None, detail)
    price2 = out["conn"].execute("SELECT id, value, checked_at FROM claims WHERE project_id='P1' AND kind='price' AND product_version='2'").fetchone()
    hc = pruned[0]["hard_check"]
    check("tp3 round 2, SF-4 (D-A347): the node the budget arithmetic pruned keeps a hard check record as a verdict-pruned node does: the budget requirement, the price claim it read with that claim's checked_at, the identity, the monthly figure and the ceiling, the basis arithmetic; its reason names the requirement, quotes the claim by id and value and states the figure against the ceiling",
          price2 is not None and len(hc) == 1 and hc[0]["requirement_id"] == "P1.REQ-001" and hc[0]["claim_ids"] == [price2[0]] and hc[0]["checked_at"] == price2[2] and hc[0]["product_id"] == "orion-compute" and hc[0]["product_version"] == "2"
          and hc[0]["verdict"] == "above budget" and hc[0]["basis"] == "arithmetic" and hc[0]["monthly_usd"] > hc[0]["ceiling_usd"] == 50.0
          and price2[0] in pruned[0]["reason"] and price2[1] in pruned[0]["reason"] and "above the ceiling of 50.00 USD" in pruned[0]["reason"], (hc, pruned[0]["reason"], price2))
    check("tp3 the decision is a conditional recommendation (D-A408) with its memo stored, the state not_proven and the confirming check in the open list after the conditional sentence",
          d["status"] == "active" and d["memo"] is not None and memo.get("evidence_state") == "not_proven" and str(memo.get("settling_check", "")).startswith("The check that would confirm it: ") and (memo.get("open") or [None, None])[:2] == [(memo.get("conditional") or {}).get("line"), memo.get("settling_check")],
          {"decision": (d["status"], d["rationale"][:160]), "check": memo.get("settling_check"), "state": memo.get("evidence_state")})


# ---------------------------------------------------------------- tp4: the generator proposes nothing: the comparison decides on the packs, the memo with its settling check
def tp4():
    sc = TwinScript(twins=False); sc.second_product = True; sc.tree_fail = True
    out = run("no_alternatives", sc)
    d, memo = memo_facts(out)
    simple_keys = [k for k in out["cmp_keys"] if not k.endswith(":memo") and not k.endswith(":final")]
    detail = {"status": out["res"]["status"], "nodes": out["nodes"], "branch": out["branch"], "steps": [s.get("label") for s in out["steps"] if s.get("stage") == "compare_options"], "tree_keys": out["tree_keys"], "cmp_keys": out["cmp_keys"], "decision": (d["status"], d["rationale"][:160]), "memo": sorted(memo)[:6]}
    check("tp4 the tree's generator proposes nothing: no tree node, no evaluation send (the ledger holds the alternatives send alone), the branch event with no node and no selection, and the step that says the tree selected no branch",
          out["res"]["status"] == "completed" and out["nodes"] == [] and len(out["branch"]) == 1 and out["branch"][0]["nodes"] == [] and out["branch"][0]["selected"] is None
          and out["tree_keys"] and all(k.endswith(":alternatives") for k in out["tree_keys"]) and not any(":evaluate" in k for k in out["tree_keys"]) and any(s.get("label") == NO_BRANCH for s in out["steps"]), detail)
    check("tp4 the comparison decides on the packs as in the simple configuration: one ComparisonOutV2 send under the subject's simple key (no memo key), the decision a conditional recommendation (D-A408) with its memo stored, the state not_proven and the confirming check in the open list after the conditional sentence",
          len(simple_keys) == 1 and not any(k.endswith(":memo") for k in out["cmp_keys"]) and out["schemas"].count("ComparisonOutV2") == 1 and d["status"] == "active" and d["memo"] is not None
          and memo.get("evidence_state") == "not_proven" and str(memo.get("settling_check", "")).startswith("The check that would confirm it: ") and (memo.get("open") or [None, None])[1] == memo.get("settling_check"), detail)


# ---------------------------------------------------------------- tp5: every alternative pruned (budget and contradicts): no empty evaluation send, the comparison decides, the pick rejected with the quote
def tp5():
    sc = TwinScript(twins=False); sc.second_product = True; sc.coverage_verdict = "contradicts"
    out = run("all_pruned", sc)
    d, memo = memo_facts(out)
    depth1 = [n for n in out["nodes"] if n["depth"] == 1]
    detail = {"depth1": [(n["label"], n["hard_status"], n["pruned"], (n["reason"] or "")[:160]) for n in depth1], "tree_keys": out["tree_keys"], "cmp_keys": out["cmp_keys"], "steps": [s.get("label") for s in out["steps"] if s.get("stage") == "compare_options"], "decision": (d["status"], d["rationale"][:200]), "state": memo.get("evidence_state")}
    check("tp5 every alternative pruned, the dearer version on the budget arithmetic and the other on a contradicts verdict with the claim quoted: no evaluation send over an empty branch list (the ledger holds the alternatives send alone), the tree's outcome recorded as the step that says it selected no branch",
          len(depth1) == 2 and all(n["pruned"] and n["hard_status"] == "fail" for n in depth1) and any(str(n["reason"]).startswith("confirmed hard-constraint failure: ") and "P1.CLM-" in str(n["reason"]) for n in depth1)
          and out["tree_keys"] and all(k.endswith(":alternatives") for k in out["tree_keys"]) and any(s.get("label") == NO_BRANCH for s in out["steps"]), detail)
    check("tp5 the comparison decides on the packs: one send under the simple key, the pick rejected on the proven violation (the rationale quotes the claim), the state proven_violation and the memo stored",
          sum(1 for k in out["cmp_keys"] if not k.endswith(":memo") and not k.endswith(":final")) == 1 and d["status"] == "unresolved" and d["memo"] is not None and memo.get("evidence_state") == "proven_violation"
          and d["rationale"].startswith("Rejected on evidence:") and "P1.CLM-" in d["rationale"], detail)


# ---------------------------------------------------------------- tp6: at equal score the proven branch outranks the unresolved one (a unit check on tree.explore)
def tp6():
    from app.core import tree
    def alts(subject): return [{"label": "Proven", "claim_ids": ["c1"]}, {"label": "Unproven", "claim_ids": ["c2"]}]
    def variants(label): return [{"label": f"{label} / streaming", "claim_ids": []}]
    def evaluate(nodes): return {x.id: {"fit": 9, "evidence": 7, "cost": 3, "risk": 6, "claim_ids": []} for x in nodes}
    def evaluate_better_unproven(nodes): return {x.id: {"fit": 9 if "Unproven" in x.label else 5, "evidence": 7, "cost": 3, "risk": 6, "claim_ids": []} for x in nodes}
    hard = lambda node: "pass" if "Proven" in node.label and "Unproven" not in node.label else "unresolved"
    r1 = tree.explore("s", alts, variants, evaluate, hard)
    r2 = tree.explore("s", lambda s0: list(reversed(alts(s0))), variants, evaluate, hard)
    r3 = tree.explore("s", alts, variants, evaluate_better_unproven, hard)
    sel = lambda r: next(n for n in r["nodes"] if n.selected)
    detail = {"equal": (sel(r1).label, sel(r1).hard_status, r1["selected_unresolved"]), "equal reversed": (sel(r2).label, sel(r2).hard_status), "better unproven": (sel(r3).label, sel(r3).hard_status, r3["selected_unresolved"]), "scores": sorted({n.score for n in r1["nodes"] if n.score is not None})}
    check("tp6 two leaves with equal rubric scores, one proven (pass) and one unresolved: the proven branch is selected whatever the alternatives' order (before D-A227's tie-break the larger node id won), and the selection is not conditional",
          sel(r1).hard_status == "pass" and sel(r2).hard_status == "pass" and not r1["selected_unresolved"] and not r2["selected_unresolved"] and len(detail["scores"]) == 1, detail)
    check("tp6 a better-scoring unresolved branch still wins: unresolved branches can be selected, and the selection is conditional",
          sel(r3).label.startswith("Unproven") and sel(r3).hard_status == "unresolved" and r3["selected_unresolved"], detail)


# ---------------------------------------------------------------- tp7 and tp8: the language-naming rule inside the tree's hard check
GAP_QUOTE = "Languages: 42 languages"            # the page's sentence cut before the names (a verbatim substring, so the exact-quote rule holds; the claim's text still names Hebrew)
GAP_REASON = "the quote does not name Hebrew, English"   # the reconciliation's reason on the verifier's record


class GapScript(TwinScript):
    """tp3's two identities (the dearer second version, pruned by the budget arithmetic), the coverage verdict by the claims' words (entails on both
    binding requirements for the surviving identity) and the verifier's records on; with gap set, every languages claim's quote is the page's
    sentence cut before the names, so the stored entails rests on a claim whose quote names no language."""
    def __init__(self, gap):
        super().__init__(twins=False); self.second_product = True; self.coverage_verdict = None; self.entail_none_for = (); self.gap = gap
    def __call__(self, body):
        res = super().__call__(body)
        if self.gap and PF.schema_of(body) in ("EvidenceExtractionOut", "EvidenceExtractionOutV2") and "content" in res:
            data = json.loads(res["content"])
            for c in data["claims"]:
                if c["attribute"] == "languages":
                    c["quote"] = GAP_QUOTE
            return PF.J(data)
        return res


def gap_facts(out):
    """The stored rows behind the surviving node: the binding requirements, the coverage verdicts of the v1 identity, the languages support records with
    their reason, the languages quotes, the step's recorded support per requirement and the decision event."""
    from app.core import coverage_step as CS
    c = out["conn"]; rid = out["rid"]; d, memo = memo_facts(out)
    hard = [{"id": r[0], "key": r[1], "value": json.loads(r[2]), "hard": True, "applies_to": None} for r in insp(c).execute("SELECT id, key, value_json FROM requirements WHERE project_id='P1' AND hard=1 AND status='confirmed' ORDER BY id")]
    lang = next((h for h in hard if h["key"] == "languages"), {})
    rv = max(int(r[0]) for r in c.execute("SELECT version FROM requirements WHERE project_id='P1'"))
    verdicts = {r[0]: r[1] for r in insp(c).execute("SELECT requirement_id, verdict FROM coverage_checks WHERE project_id='P1' AND product_id='orion-compute' AND product_version IN ('1', 'v1')")}   # the store records the version as 1
    support = [tuple(x) for x in insp(c).execute("SELECT claim_id, status, reason FROM claim_requirement_support WHERE project_id='P1' AND requirement_id=? AND verdict='supported' ORDER BY claim_id", (lang.get("id"),))]
    quotes = sorted({r[0] for r in insp(c).execute("SELECT quote FROM claims WHERE project_id='P1' AND attribute='languages'")})
    step = json.loads((c.execute("SELECT output_json FROM run_steps WHERE run_id=? AND node='compare_options' AND output_json LIKE ? ORDER BY completed_at DESC LIMIT 1", (rid, f'%"decision": "{d["id"]}"%')).fetchone() or ["{}"])[0]) if d.get("id") else {}
    events = [e for _, name, e in ev.events(c, rid) if name == "decision" and e.get("decision_id") == d.get("id")]
    unresolved = json.loads((c.execute("SELECT unresolved_json FROM decisions WHERE id=?", (d["id"],)).fetchone() or ["[]"])[0] or "[]") if d.get("id") else []
    tree_memo = [tuple(x) for x in c.execute("SELECT action, observation, change FROM reasoning_records WHERE run_id=? AND action=?", (rid, "the tree's memo"))]
    return {"tree_memo": tree_memo, "hard": hard, "lang": lang, "rv": rv, "status": CS.hard_status(c, "P1", hard, rv, "orion-compute", "v1") if hard else None, "verdicts": verdicts, "support": support, "quotes": quotes, "step": step, "events": events, "unresolved": unresolved, "d": d, "memo": memo}


def tp7():
    out = run("language_gap", GapScript(gap=True))
    g = gap_facts(out); d, memo = g["d"], g["memo"]
    depth1 = [n for n in out["nodes"] if n["depth"] == 1]; pruned = [n for n in out["nodes"] if n["pruned"]]; sel = [n for n in out["nodes"] if n["selected"]]
    node = next((n for n in depth1 if not n["pruned"]), None) or {}
    variants = [n for n in out["nodes"] if n["depth"] == 2 and n["id"].rsplit(":", 1)[-1].startswith(node.get("id", "?").rsplit(":", 1)[-1] + ".")]
    detail = {"status": out["res"]["status"], "depth1": [(n["label"], n["hard_status"], n["pruned"], n["score"], (n["reason"] or "")[:80]) for n in depth1], "variants": [(n["label"], n["hard_status"], n["pruned"], n["score"]) for n in variants], "selected": [(n["label"], n["hard_status"]) for n in sel],
              "verdicts": g["verdicts"], "support": g["support"], "quotes": g["quotes"], "hard_status": g["status"]}
    check("tp7 the surviving identity's stored verdicts read entails on both binding requirements (hard_status pass, nothing partial or none), its languages quote names no language, and the verifier's record on that claim stands withdrawn with the reason the records name (D-A155)",
          out["res"]["status"] == "completed" and g["lang"] and set(g["verdicts"]) == {h["id"] for h in g["hard"]} and all(v == "entails" for v in g["verdicts"].values()) and g["status"] == "pass" and g["quotes"] == [GAP_QUOTE]
          and g["support"] and all(st == "withdrawn" and why == GAP_REASON for _, st, why in g["support"]), detail)
    check("tp7 inside the tree's hard check the language rule turns that node's pass into unresolved: its hard status is unresolved (not pass, not fail), it is not pruned (the dearer version alone is, by the budget arithmetic), its variants are expanded and scored, and it is selected",
          len(depth1) == 2 and len(pruned) == 1 and pruned[0]["depth"] == 1 and pruned[0]["hard_status"] == "fail" and pruned[0]["reason"].startswith("confirmed hard-constraint failure: budget monthly usd (") and node and node["hard_status"] == "unresolved" and not node["pruned"]
          and variants and all(n["hard_status"] == "unresolved" and not n["pruned"] and n["score"] is not None for n in variants) and len(sel) == 1 and sel[0]["hard_status"] == "unresolved" and sel[0]["id"] in {n["id"] for n in variants} | {node["id"]}, detail)
    short = lambda nid: nid.rsplit(":", 1)[-1]                                              # the branch event names the nodes by their short id (for example node A1.2, a variant of alternative A1); the rows by the run-scoped id
    check("tp7 the selection is recorded as conditional: the tree's reasoning record names the selection with the conditional mark, the memo record's observation says the selected branch's hard constraints are unresolved, the branch event shows the node unpruned and the selection, and the decision's findings carry the withdrawn claim with the records' reason",
          any(r[2].endswith("(hard constraints unresolved: conditional)") for r in out["records"]) and any(r[0] == "the tree's memo" and "hard constraints unresolved" in r[1] for r in g["tree_memo"])
          and len(out["branch"]) == 1 and out["branch"][0]["selected"] == short(sel[0]["id"]) and any(n["id"] == short(node["id"]) and not n["pruned"] for n in out["branch"][0]["nodes"])
          and any(" does not count for languages " in u and GAP_REASON in u for u in g["unresolved"]), {"records": out["records"], "tree_memo": g["tree_memo"], "branch": out["branch"][:1], "unresolved": g["unresolved"]})
    check("tp7 the gate then finds no language-naming support (Phase 19 package 19.1, D-A408: a conditional recommendation, never a proven pass): the decision and its event read active, the memo is kept with the state not_proven and the conditional record naming Languages, the rationale opens Recommended, not yet proven on Languages and names the withdrawn claim with the records' reason, the step's record holds no support for languages and the budget's claim, and the confirming check stands in the memo's open list naming the requirement",
          d["status"] == "active" and d["choice"] not in ("unresolved", "", None) and g["events"] and g["events"][-1].get("status") == "active" and d["memo"] is not None and memo.get("evidence_state") == "not_proven"
          and d["rationale"].startswith("Recommended, not yet proven on Languages: he, en") and GAP_REASON in d["rationale"] and any(cid in d["rationale"] for cid, _, _ in g["support"]) and "Rejected" not in d["rationale"] and [q["key"] for q in (memo.get("conditional") or {}).get("requirements") or []] == ["languages"]
          and g["step"].get("evidence_by_requirement", {}).get(g["lang"]["id"]) == [] and all(v for k, v in g["step"].get("evidence_by_requirement", {}).items() if k != g["lang"]["id"])
          and str(memo.get("settling_check", "")).startswith("The check that would confirm it: ") and "Languages: he, en" in memo["settling_check"] and (memo.get("open") or [None, None])[1] == memo["settling_check"],
          {"decision": (d["status"], d["choice"], d["rationale"][:300]), "state": memo.get("evidence_state"), "events": g["events"], "step": g["step"].get("evidence_by_requirement"), "check": memo.get("settling_check"), "open": (memo.get("open") or [])[:2]})


def tp8():
    out = run("language_named", GapScript(gap=False))
    g = gap_facts(out); d, memo = g["d"], g["memo"]
    depth1 = [n for n in out["nodes"] if n["depth"] == 1]; sel = [n for n in out["nodes"] if n["selected"]]
    node = next((n for n in depth1 if not n["pruned"]), None) or {}
    detail = {"status": out["res"]["status"], "depth1": [(n["label"], n["hard_status"], n["pruned"], n["score"]) for n in depth1], "selected": [(n["label"], n["hard_status"]) for n in sel], "support": g["support"], "quotes": g["quotes"], "records": out["records"]}
    check("tp8 the control: the same run with the languages quote that names Hebrew and English: the verifier's record counts, the surviving node passes the tree's hard check, is selected, and the selection carries no conditional mark",
          out["res"]["status"] == "completed" and g["quotes"] == [PF.FACT_LANGS] and g["support"] and all(st == "counts" for _, st, _ in g["support"]) and node and node["hard_status"] == "pass" and not node["pruned"]
          and len(sel) == 1 and sel[0]["hard_status"] == "pass" and not any("conditional" in r[2] for r in out["records"]) and not any("conditional" in u for u in g["unresolved"]), detail)
    check("tp8 the decision is active on it: the stored row and the event read active, the choice is the candidate's label, the step's record names the languages claim as the requirement's support, and no settling check is stated",
          d["status"] == "active" and d["choice"] not in ("unresolved", "", None) and g["events"] and g["events"][-1].get("status") == "active" and g["step"].get("evidence_by_requirement", {}).get(g["lang"]["id"]) and not memo.get("settling_check"),
          {"decision": (d["status"], d["choice"], d["rationale"][:200]), "events": g["events"], "step": g["step"].get("evidence_by_requirement"), "check": memo.get("settling_check")})


SCENARIOS = {"tp1": tp1, "tp2": tp2, "tp3": tp3, "tp4": tp4, "tp5": tp5, "tp6": tp6, "tp7": tp7, "tp8": tp8}

if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        SCENARIOS[name]()
    print(f"test_tree_prune: {sum(1 for _, ok in results if ok)} of {len(results)} checks passed")
