"""Evidence packs with the contradiction rule: what each comparison is shown per candidate, what the cap leaves out, and how a contradiction
must be addressed and never cited. Offline, through the Runner on the pipeline fake (tests/pipeline_fake.py); one unit block on synthetic
inputs for the ranking.

    ep1  the pack's shape and ranking on synthetic inputs: tiers (hard-requirement claims, contradictions on questions and conflicts, verifier-
         confirmed support, the rest by recency), ties by claim id, the duplicate rule, the product fields of every line, the conditions verbatim,
         the gaps
    ep2  through a run: one pack per eligible candidate, every claim line with product_id and product_version in the stored pack and in the v7
         prompt, the memo's line "no claim omitted", the step event with the included ids, the pack stored with the decision, the v7 key
    ep3  a binding cap: the omitted ids with the reason cap in the trace and the memo's one line; the prompt shows the included claims only
    ep4  the reserved slots: a conflict's two claims under a cap of 2 are included before every supporting claim and named in must_address;
         without a binding hard requirement the comparison is sent
    ep5  insufficient_capacity: the same conflict with the hard requirement binding: no comparison send, the decision open with the reason
         pack_capacity in the memo, the rationale and the plan's unresolved checks; a run under a larger cap decides it
    ep6  the panel (three models that read a draft comparison) and the tree (the bounded exploration of a decision's alternatives) see only the pack: every claim id the tree's prompts carry is inside the packs, the panel's prompt names none
    ep7  the reuse key carries the prompt version (v7); a result stored under the v6 key is not the v7 key's; v6 is byte for byte the file it was
    ep8  zero "no options" cases when the budget stops the run at the first final comparison: no memo is left without options
    ep9  scripts/eval/pack_stats.py on a copy of a fake store: exit 0, one row per subject, the totals line
    ep10 the pack boundary on the comparison's answer: a claim the cap omitted, planted in every field of the answer on every send (the draft,
         the tree's memo, the final), reaches no stored memo, no citation edge, no panel payload and no tree prompt
    ep11 a material contradiction still reaches must_address under the citation rule, and is never cited
    ep12 the subject join across plan versions: two runs on one store, the planner of the second naming the subject anew with the old name as
         its product class (as in the devA evaluation project); the version 2 decision's pack holds the claims attached to the predecessor's
         questions beside its own, the hard-requirement lines first with their support records, the decision active; without the join the pack
         would hold the version 2 claim alone and the decision would stay unresolved with the settling check naming the requirements
    ep13 cross-version attribution: the joined claims' rows and the version 1 questions' attachments are unchanged by the second run (question
         ids, version, source, verdict); every joined pack line names the subject and the plan version it came from and the predecessor's
         questions attaching it, the own line names its own question; the prompt's lines carry no origin field; the helper's guards: a live
         subject named as another's class shares no claim, a claim of a different decision's subject is not joined, the predecessor itself
         joins nothing
    ep14 the two measurement scripts make the same selection as the comparison (scripts/eval/pack_stats.py, scripts/eval/retrieval_diagnosis.py
         on a copy of the two-version store): the version 2 subject's row counts the joined claims and the diagnosis's pack stage finds a
         predecessor claim in the version 2 subject's pack
    ep15 the extractor fallback is the subject's own name only: a claim the extractor tagged with the predecessor's name that no question
         attaches, or that a foreign subject's question alone attaches, is not selected for the renamed subject; the own extractor-only claim
         still is; every selected claim that is not the subject's own carries an origin; a subject whose class is only a class (no decision, no
         question carries it) joins nothing, on a copy of the two-version store
    ep16 one claim cannot contradict itself, on records of the devA evaluation project (tests/fixtures/deva_contradictions.json): a settled
         conflict's loser is omitted as superseded, never a one-claim contradiction; a conflict item of must_address comes only from an open
         conflict record and names at least two claims and its key
    ep17 a conflict across decision subjects: through the real selection, loader and pack on a store built from the devA Fly.io record and a
         synthetic second page whose claim another subject attaches, the one item names both claims and both values and both are shown as
         contradicting lines, neither citable

Run one scenario with its name as the argument, or all of them without one."""
import copy, hashlib, json, pathlib, re, shutil, subprocess, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

results = []
def check(name, cond, detail=None):
    results.append((name, bool(cond))); assert cond, (name if detail is None else f"{name} [{detail}]")
import background_threads; background_threads.install()

import pipeline_fake as PF
from app.core import coverage, coverage_step as CS, derived, evidence_pack as EP
from app.core.ledger import BudgetLedger, BudgetExceeded
from app.core.sqlguard import inspect as insp, open_connection
from app.core import snapshot as SNAP
from app.graph import prompts
CV = f"v{prompts.VERSIONS['compare_options']}"   # the comparison's prompt version as the keys name it
from app.graph import workflow as WF

V6_SHA = "bd8c1ea0a948639f54f2245dc95a9b54dc1c3f0c975e46fdc467790f3341c371"   # the SHA-256 of prompts/runtime/compare_options.v6.md, which must stay byte for byte as it is
SE = copy.deepcopy(PF.S); SE["extraction"]["coverage_step"] = True                                   # the coverage step on
SEP = copy.deepcopy(SE); SEP["discovery"].update({"panel": True, "simulate_panel": True, "panel_cap_usd": 0.05})   # the simulated panel and its follow-up


def caps(settings, claims=None, tokens=None):
    s = copy.deepcopy(settings); s["evidence_pack"] = {}
    if claims is not None:
        s["evidence_pack"]["max_claims_per_pack"] = claims
    if tokens is not None:
        s["evidence_pack"]["max_tokens_per_pack"] = tokens
    return s


def drive(runner, res):
    for _ in range(4):
        if res["status"] != "awaiting_user":
            break
        res = runner.resume(res["run_id"], PF.answers_for(res))
    return res


def packs_shown(text):
    """The evidence packs of one comparison prompt (the v7 user turn)."""
    line = next((l for l in text.splitlines() if l.startswith("Evidence packs")), None)
    return json.loads(line[line.index(": ") + 2:]) if line else None


def run(name, script, settings, research=None, ledger_cls=None, soft=False):
    r, stub = PF.runner_for("ep_" + name, script, settings=settings, research=research, ledger_cls=ledger_cls)
    if soft:
        with r.conn:
            r.conn.execute("UPDATE questions SET answer_json='\"no\"' WHERE kind='hardness'")       # the founder states that no requirement is hard
    res = drive(r, r.start("P1", PF.IDEA))
    try:
        c = r.conn; rid = res["run_id"]
        decisions = [{"id": d[0], "subject": d[1], "status": d[2], "proposed": d[3], "choice": d[4], "rationale": d[5], "memo": json.loads(d[6]) if d[6] else None, "packs": json.loads(d[7]) if d[7] else None}
                     for d in insp(c).execute("SELECT id, subject, status, proposed_status, choice, rationale, memo_json, evidence_pack_json FROM decisions ORDER BY id")]
        bodies = {s: [PF.all_user_text(b) for b in stub.bodies if PF.schema_of(b) == s] for s in ("ComparisonOutV2", "AlternativesOut", "VariantsOut", "BranchEvaluationOut", "PanelOut")}
        keys = [k[0] for k in c.execute("SELECT DISTINCT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE '%compare_options%' ORDER BY step_key", (rid,))]
        events = [json.loads(e[0]) for e in c.execute("SELECT data_json FROM run_events WHERE run_id=? AND event='step' ORDER BY id", (rid,)) if '"Evidence packs"' in e[0]]
        records = [tuple(x) for x in c.execute("SELECT purpose, action, observation, change FROM reasoning_records WHERE run_id=? AND action LIKE '%evidence pack%' ORDER BY id", (rid,))]
        stored = SNAP.read(c, "P1", 1) if res["status"] in ("completed", "partial") else None
        checks = ((stored or {}).get("snapshot") or {}).get("plan_body", {}).get("unresolved_checks", []) if stored else []
        return {"res": res, "decisions": decisions, "bodies": bodies, "keys": keys, "events": events, "records": records, "checks": checks, "conn_path": str(PF.tmp / ("ep_" + name) / "app.db")}
    finally:
        r.close(); stub.srv.shutdown(); stub.srv.server_close()


# ---------------------------------------------------------------- ep1: the shape and the ranking on synthetic inputs
def ep1():
    hard = [{"id": "P1.REQ-001", "key": "budget_monthly_usd", "value": "50"}, {"id": "P1.REQ-002", "key": "languages", "value": "he, en"}]
    mk = lambda i, attr, value, status="supported", src="S1", read="2026-10-0%dT00:00:00Z" % 1: {"id": f"P1.CLM-{i:03d}", "text": f"claim {i} {attr} {value}", "kind": "capability", "source_id": src, "quote": None, "status": status, "scope_status": "in_scope",
                                                                                                "conditions_json": json.dumps(["as of 2026-10", "EU region"]), "subject": "Orion", "attribute": attr, "value": value, "decision_subject": "transcription", "product_id": "orion-compute", "product_version": "1", "source_version": "h"}
    claims = {c["id"]: c for c in [mk(1, "price", "$1"), mk(2, "languages", "42 languages"), mk(3, "latency", "fast", read="2026-10-03T00:00:00Z"), mk(4, "latency", "fast", src="S2"), mk(5, "region", "EU", status="conflicting"), mk(6, "region", "US", status="conflicting", src="S2"),
                                   mk(7, "uptime", "99%", status="contradicted"), mk(8, "sdk", "python", read="2026-10-09T00:00:00Z"), mk(9, "sdk2", "go", status="contradicted"), mk(10, "price", "$1", src="S3")]}
    inputs = EP.PackInputs(subject="transcription", hard=hard, rq_ids=["P1.RQ-1", "P1.RQ-2", "P1.RQ-3"], citable={"P1.CLM-001", "P1.CLM-002", "P1.CLM-003", "P1.CLM-004", "P1.CLM-008", "P1.CLM-010"}, by_id=claims,
                           support={"P1.CLM-001": {"P1.REQ-001"}, "P1.CLM-002": {"P1.REQ-002"}, "P1.CLM-010": {"P1.REQ-001"}}, records={"P1.CLM-001": {"P1.REQ-001"}, "P1.CLM-002": {"P1.REQ-002"}, "P1.CLM-007": {"P1.REQ-002"}, "P1.CLM-010": {"P1.REQ-001"}},
                           questions={"P1.CLM-001": {"P1.RQ-1"}, "P1.CLM-003": {"P1.RQ-2"}, "P1.CLM-004": {"P1.RQ-2"}, "P1.CLM-005": {"P1.RQ-1"}, "P1.CLM-006": {"P1.RQ-1"}, "P1.CLM-010": {"P1.RQ-1"}},
                           conflicts=[{"key": "orion-compute@1:region", "claim_ids": ["P1.CLM-005", "P1.CLM-006"], "attribute": "region", "values": ["EU", "US"], "product_id": "orion-compute", "product_version": "1"}],
                           superseded={}, recency={"P1.CLM-003": "2026-10-03T00:00:00Z", "P1.CLM-008": "2026-10-09T00:00:00Z", "P1.CLM-009": "2026-10-01T00:00:00Z"})
    full = EP.build_pack(inputs, "orion-compute", "1", "Orion Compute 1", {"max_claims_per_pack": 100, "max_tokens_per_pack": 100000})
    order = [c["claim_id"] for c in full["claims"]]
    roles = {c["claim_id"]: c.get("contradicts") for c in full["claims"]}
    check("ep1 the ranking: hard-requirement claims first (support records and the verifier's denial of a hard-requirement claim, by id), then the contradictions on questions and the open conflict, then verifier-confirmed support by id, then the rest by recency (newest first), ties by id; the duplicate of a kept claim (same attribute, value and conditions, its roles a subset) is omitted as duplicate and named on the kept line",
          order == ["P1.CLM-001", "P1.CLM-002", "P1.CLM-007", "P1.CLM-005", "P1.CLM-006", "P1.CLM-003", "P1.CLM-008", "P1.CLM-009"] and roles["P1.CLM-007"] == "verifier" and roles["P1.CLM-005"].startswith("conflict:")
          and [o for o in full["omitted"] if o["reason"] == "duplicate"] == [{"claim_id": "P1.CLM-004", "reason": "duplicate"}, {"claim_id": "P1.CLM-010", "reason": "duplicate"}] and next(c for c in full["claims"] if c["claim_id"] == "P1.CLM-003")["duplicates"] == ["P1.CLM-004"]
          and next(c for c in full["claims"] if c["claim_id"] == "P1.CLM-001")["duplicates"] == ["P1.CLM-010"], {"order": order, "omitted": full["omitted"], "roles": roles})
    check("ep1 D-A181 on every pack line: claim id, source id, product id and version, the status and the conditions copied verbatim; the material contradictions in must_address (the verifier's denial about a hard requirement, the open conflict with its values); the gaps name the question with no claim and no hard requirement (both have claims)",
          all(c["claim_id"] and c["source_id"] and c["product_id"] == "orion-compute" and c["product_version"] == "1" and c["conditions"] == ["as of 2026-10", "EU region"] and c["status"] for c in full["claims"])
          and {m["kind"] for m in full["must_address"]} == {"verifier", "conflict"} and any(m["kind"] == "conflict" and m["values"] == ["EU", "US"] and m["claim_ids"] == ["P1.CLM-005", "P1.CLM-006"] for m in full["must_address"])
          and sum(1 for m in full["must_address"] if m["kind"] == "verifier") == 1 and full["gaps"] == {"hard_requirements": [], "research_questions": ["P1.RQ-3"]} and not full["insufficient_capacity"], full)
    capped = EP.build_pack(inputs, "orion-compute", "1", "Orion Compute 1", {"max_claims_per_pack": 5, "max_tokens_per_pack": 100000})
    ids5 = [c["claim_id"] for c in capped["claims"]]
    check("ep1 a cap of 5 over three material contradictions: the contradictions take their reserved slots, one supporting claim per hard requirement follows, nothing else fits, and the omitted list names every other claim with the reason cap (the two duplicates keep their own reason)",
          set(ids5) == {"P1.CLM-007", "P1.CLM-005", "P1.CLM-006", "P1.CLM-001", "P1.CLM-002"} and len(ids5) == 5 and not capped["insufficient_capacity"]
          and {o["claim_id"] for o in capped["omitted"] if o["reason"] == "cap"} == {"P1.CLM-003", "P1.CLM-008", "P1.CLM-009"} and capped["counts"]["omitted"] == 5, capped)
    short = EP.build_pack(inputs, "orion-compute", "1", "Orion Compute 1", {"max_claims_per_pack": 4, "max_tokens_per_pack": 100000})
    check("ep1 a cap of 4: the reserved slots leave room for one hard requirement's support and not the other's, so the pack is insufficient_capacity and did_not_fit names the requirement and the claim that would have filled it; the contradictions are still all there",
          short["insufficient_capacity"] and short["did_not_fit"] == [{"requirement_id": "P1.REQ-002", "claim_ids": ["P1.CLM-002"]}] and {"P1.CLM-007", "P1.CLM-005", "P1.CLM-006"} <= {c["claim_id"] for c in short["claims"]}, short)
    tok = EP.build_pack(inputs, "orion-compute", "1", "Orion Compute 1", {"max_claims_per_pack": 100, "max_tokens_per_pack": 1})
    check("ep1 the token cap binds like the count cap: with one token no supporting claim fits beside the reserved slots, the pack is insufficient_capacity and the tokens are the budget estimator's figure",
          tok["insufficient_capacity"] and len(tok["did_not_fit"]) == 2 and tok["tokens"] == EP.tokens_of(EP.prompt_view(tok)) and tok["tokens"] > 1, tok)
    line = EP.pack_line([capped, full])
    check("ep1 the memo's one line counts the cap's omissions across the packs and names the ids; with none it reads 'no claim omitted'", line == "3 claims omitted by the cap: P1.CLM-003, P1.CLM-008, P1.CLM-009" and EP.pack_line([full]) == "no claim omitted", line)


# ---------------------------------------------------------------- ep2 and ep3: the shape through a run, and a binding cap
def ep2():
    base = run("base", PF.Script(questions=False), SE)
    d = base["decisions"][-1]; shown = [packs_shown(t) for t in base["bodies"]["ComparisonOutV2"]]
    pack = (d["packs"] or [None])[0]
    check("ep2 through a run: the decision is active, one pack for the one eligible candidate is stored with it, every line of the stored pack and of the prompt's packs carries claim id, source id, product id and version (D-A181), the memo's line reads 'no claim omitted', and the pack's supporting lines name the hard requirements their records count for",
          base["res"]["status"] == "completed" and d["status"] == "active" and d["packs"] and len(d["packs"]) == 1 and pack["product_id"] == "orion-compute" and pack["product_version"] == "1" and len(pack["claims"]) == 3
          and all(c["claim_id"] and c["source_id"] and c["product_id"] == "orion-compute" and c["product_version"] == "1" for c in pack["claims"])
          and shown and all(s and all(c["product_id"] == "orion-compute" and c["product_version"] == "1" for p in s for c in p["claims"]) for s in shown)
          and d["memo"]["pack_line"] == "no claim omitted" and any("P1.REQ-" in r for c in pack["claims"] for r in c.get("supports", [])) and not pack["omitted"] and not pack["insufficient_capacity"], {"d": {k: v for k, v in d.items() if k != "packs"}, "pack": pack, "shown": shown})
    ev = base["events"]
    check("ep2 the trace: one step event per decision, label Evidence packs, whose data lists the included claim ids per pack and an empty omitted list; one reasoning record names the step; the comparison's step key carries the prompt version",
          len(ev) == 1 and ev[0]["packs"][0]["included"] == [c["claim_id"] for c in pack["claims"]] and ev[0]["packs"][0]["omitted"] == [] and len(base["records"]) == 1
          and base["keys"] and all(f":compare_options:w0:b0:{CV}:a-x:d" in k for k in base["keys"]), {"events": ev, "records": base["records"], "keys": base["keys"]})
    check("ep2 the v7 prompt (v8 since Phase 17 package 17.3 carries the same packs and rules): the comparison is told the contradiction rule and receives the packs; v6 is unchanged byte for byte", prompts.VERSIONS["compare_options"] >= 7 and "must_address" in prompts.template("compare_options")["system"] and "{evidence_packs}" in prompts.template("compare_options")["user"]
          and hashlib.sha256((ROOT / "prompts/runtime/compare_options.v6.md").read_bytes()).hexdigest() == V6_SHA)


def ep3():
    capped = run("cap", PF.Script(questions=False), caps(SE, claims=2))
    d = capped["decisions"][-1]; pack = (d["packs"] or [None])[0]; shown = packs_shown(capped["bodies"]["ComparisonOutV2"][0])
    omitted = [o for o in pack["omitted"] if o["reason"] == "cap"]
    check("ep3 a binding cap of 2 over three supporting claims: two included (the hard-requirement claims, by id), one omitted with the reason cap, named in the trace event and in the memo's one line; the prompt shows the two included claims only; the decision still decides",
          len(pack["claims"]) == 2 and len(omitted) == 1 and capped["events"][0]["packs"][0]["omitted"] == omitted and d["memo"]["pack_line"] == f"1 claim omitted by the cap: {omitted[0]['claim_id']}"
          and [c["claim_id"] for c in shown[0]["claims"]] == [c["claim_id"] for c in pack["claims"]] and shown[0]["omitted_count"] == 1 and omitted[0]["claim_id"] not in json.dumps(shown)
          and d["status"] in ("active", "unresolved") and capped["res"]["status"] == "completed", {"pack": pack, "memo": d["memo"], "event": capped["events"]})


# ---------------------------------------------------------------- ep4 and ep5: the reserved slots and insufficient_capacity, on a subject with a conflict
class _Atlas(PF.Script):
    """The summary subject's one product, Atlas Text 4, with its two token prices and its languages, and one page stating two values of another attribute
    (languages_region EU and US): an open tie, so two conflicting claims about the candidate."""
    def __init__(self, version="4"):
        super().__init__(questions=False, subjects=("summary",))
        self.vendor, self.product, self.version, self.subject = "Atlas", "Text", version, "Atlas Text"
        pid, ver = coverage.product_identity(self.vendor, self.product, version, self.subject); self.select = (pid, ver, "Atlas Text")
        self.facts = [("input_token_price", "$0.15 per 1M tokens", f"Atlas Text version {version} input token price is $0.15 per 1M tokens."), ("output_token_price", "$0.60 per 1M tokens", f"Atlas Text version {version} output token price is $0.60 per 1M tokens."),
                      ("languages", "42 languages including Hebrew and English", f"Atlas Text version {version} languages is 42 languages including Hebrew and English."),
                      ("languages_region", "EU", f"Atlas Text version {version} languages region is EU."), ("languages_region", "US", f"Atlas Text version {version} languages region is US.")]
    def __call__(self, body):
        schema = PF.schema_of(body)
        if schema in ("EvidenceExtractionOut", "EvidenceExtractionOutV2"):
            u = PF.user_text(body); text = u[u.index(">", u.index("<untrusted_source")) + 1:u.rindex("</untrusted_source>")]
            m = re.search(r"do not repeat: (.*)\n", u); recorded = m.group(1).lower() if m else ""; helper = u.replace("| summary:", "| transcription:"); claims = []
            for attr, value, fact in self.facts:
                if fact not in text or (attr in recorded and attr != "languages_region"):
                    continue
                ids = PF.subjects_for_claim(helper, attr, fact)
                if ids:
                    cl = {"text": fact, "kind": "price" if "price" in attr else "capability", "source_id": "x", "quote": fact, "decision_subject": "summary", "subject": self.subject, "attribute": attr, "value": value, "conditions": [], "more_conditions": False, "vendor": self.vendor, "product": self.product, "product_version": self.version}
                    claims.append(dict(cl, subject_ids=ids) if schema.endswith("V2") else cl)
            return PF.J({"claims": claims, "injection_suspected": False, "more_claims": False})
        if schema in ("VerificationOut", "VerificationOutV2"):
            u = PF.all_user_text(body); hard = json.loads(u[u.index("Hard requirements: ") + len("Hard requirements: "):u.index("\n", u.index("Hard requirements: "))]); by_key = {h["key"]: h["id"] for h in hard}; verdicts = []
            for cid, kind, text in PF.claims_listed(u):
                entailed = ([by_key["budget_monthly_usd"]] if ("token price" in text or "token_price" in text) and "budget_monthly_usd" in by_key else []) + ([by_key["languages"]] if "languages is" in text and "languages" in by_key else [])
                verdicts.append({"claim_id": cid, "verdict": "supported", "quote": next((f for _, _, f in self.facts if f in text), None), "reason": "the synthetic source states this", "scope_matches_claim": True, "conditions_found": [], "version_or_date": "2026-10-30", "requirements_entailed": entailed})
            return PF.J({"verdicts": verdicts})
        result = super().__call__(body)
        if schema in ("ResearchPlanOut", "ResearchPlanOutV2") and "content" in result:
            data = json.loads(result["content"])
            for i, q in enumerate(data.get("queries", [])):
                q["decision_subject"] = "summary"; q["target_fact"] = "input and output token price" if i == 0 else "languages"
            for row in data.get("applicability", []):
                row["subjects"] = ["summary"]
            return PF.J(data)
        if schema == "ComparisonOutV2" and "content" in result:
            data = json.loads(result["content"]); data["decision_subject"] = "summary"
            for o in data["options"]:
                o["price"] = "recorded token tariffs"; o["monthly_cost"] = ""
            return PF.J(data)
        return result


class _AtlasResearch(PF.Sim):
    def __init__(self, script):
        super().__init__(); self.script = script
    def page_text(self):
        return "Synthetic Atlas Text pricing documentation, 30 October 2026. " + " ".join(f for _, _, f in self.script.facts)


def ep4():
    sc = _Atlas(); soft = run("reserved", sc, caps(SE, claims=2), research=_AtlasResearch(sc), soft=True)
    d = soft["decisions"][-1]; pack = (d["packs"] or [None])[0]
    contra = [c for c in pack["claims"] if c.get("contradicts")]
    check("ep4 the reserved slots under a cap of 2 with no binding hard requirement: the open tie's two claims (the preferred one keeps its supported status, the other reads conflicting; neither is citable) are both in the pack with their contradicts field and named in must_address with their two values, every supporting claim is omitted with the reason cap, the pack is not insufficient, and the comparison is sent with the pack",
          pack and len(pack["claims"]) == 2 and len(contra) == 2 and all(c["contradicts"].startswith("conflict:") and c["status"] in ("supported", "conflicting") for c in contra) and {c["status"] for c in contra} == {"supported", "conflicting"} and len(pack["must_address"]) == 1 and sorted(pack["must_address"][0]["values"]) == ["EU", "US"]
          and sum(1 for o in pack["omitted"] if o["reason"] == "cap") == 3 and not pack["insufficient_capacity"] and len(soft["bodies"]["ComparisonOutV2"]) >= 1 and soft["res"]["status"] == "completed"
          and "resolve" in prompts.template("compare_options")["system"].lower(), {"pack": pack, "bodies": len(soft["bodies"]["ComparisonOutV2"]), "res": soft["res"]})


def ep5():
    sc = _Atlas(); short = run("insufficient", sc, caps(SE, claims=2), research=_AtlasResearch(sc))
    d = next((x for x in short["decisions"] if x["subject"] == "summary"), None); pack = ((d or {}).get("packs") or [None])[0]
    check("ep5 insufficient_capacity: the tie's two reserved slots leave no room for one supporting claim of the binding hard requirements, so no comparison is sent for the subject, the decision stays open with the reason pack_capacity in its rationale and its memo, the did_not_fit list names the requirements, the pack is stored, and the plan's unresolved checks surface it like any other unresolved draft",
          short["res"]["status"] == "completed" and d and d["status"] == "unresolved" and "pack_capacity" in d["rationale"] and d["memo"]["pack_capacity"] and d["memo"]["pack_capacity"][0]["did_not_fit"] and "pack_capacity" in " ".join(d["memo"]["open"])
          and pack and pack["insufficient_capacity"] and len(pack["did_not_fit"]) == 2 and short["bodies"]["ComparisonOutV2"] == [] and short["events"] and short["events"][0]["packs"][0]["insufficient_capacity"]
          and any(d["id"] in c and "pack_capacity" in c for c in short["checks"]), {"d": {k: v for k, v in (d or {}).items() if k != "packs"}, "pack": pack, "checks": short["checks"], "res": short["res"]})
    sc2 = _Atlas(); wide = run("insufficient_wide", sc2, caps(SE, claims=4), research=_AtlasResearch(sc2))
    d2 = next((x for x in wide["decisions"] if x["subject"] == "summary"), None); pack2 = ((d2 or {}).get("packs") or [None])[0]
    check("ep5 a run under a larger cap (4) rebuilds the pack: the two reserved slots, one supporting claim per hard requirement, the fifth claim omitted by the cap, the comparison sent and the subject decided",
          d2 and pack2 and not pack2["insufficient_capacity"] and len(pack2["claims"]) == 4 and sum(1 for o in pack2["omitted"] if o["reason"] == "cap") == 1 and len(wide["bodies"]["ComparisonOutV2"]) >= 1 and "pack_capacity" not in (d2["rationale"] or ""), {"d": {k: v for k, v in (d2 or {}).items() if k != "packs"}, "pack": pack2})


# ---------------------------------------------------------------- ep6: the panel and the tree see only the pack
def ep6():
    sc = PF.Script(questions=False, subjects=("transcription",)); sc.second_product = True
    tree = run("tree_panel2", sc, caps(SEP, claims=2))
    pack_ids = {c["claim_id"] for d in tree["decisions"] for p in (d["packs"] or []) for c in p["claims"]}
    support_ids = {c["claim_id"] for d in tree["decisions"] for p in (d["packs"] or []) for c in p["claims"] if not c.get("contradicts")}
    tree_ids = {cid for s in ("AlternativesOut", "VariantsOut", "BranchEvaluationOut") for t in tree["bodies"][s] for cid in re.findall(r"P1\.CLM-\d+", t)}
    panel_ids = {cid for t in tree["bodies"]["PanelOut"] for cid in re.findall(r"P1\.CLM-\d+", t)}
    all_claims = {r[0] for r in open_connection(tree["conn_path"]).execute("SELECT id FROM claims")}
    check("ep6 the tree and the panel consume the pack: with two identities and a cap of 2 the tree ran (its three prompts were sent), every claim id its prompts carry is inside the packs, claims outside the packs exist and reach none of them, the panel's prompt carries no claim id, and the tree's memo send carries the v7 key",
          tree["bodies"]["AlternativesOut"] and tree["bodies"]["BranchEvaluationOut"] and tree_ids and tree_ids <= pack_ids and all_claims - pack_ids and not (panel_ids - pack_ids) and tree["bodies"]["PanelOut"]
          and any(k.endswith(f":{CV}:a-x:d1:memo") for k in tree["keys"]) and len({p["product_id"] + p["product_version"] for d in tree["decisions"] for p in (d["packs"] or [])}) >= 2,
          {"tree_ids": sorted(tree_ids), "pack_ids": sorted(pack_ids), "outside": sorted(all_claims - pack_ids), "panel_ids": sorted(panel_ids), "keys": tree["keys"], "sent": {k: len(v) for k, v in tree["bodies"].items()}})


# ---------------------------------------------------------------- ep7: the reuse key names the prompt version
def ep7():
    base = run("reuse", PF.Script(questions=False), SE)
    c = open_connection(base["conn_path"]); rid = base["res"]["run_id"]
    v7 = next(k for k in base["keys"] if k.endswith(f":{CV}:a-x:d1")); v6 = v7.replace(f":{CV}:a-x:d1", ":d1")
    with c:
        c.execute("INSERT INTO usage_ledger (id, run_id, step_key, kind, attempt, reserved_usd, reported_usd, state, result_json) VALUES (?,?,?,?,1,0.01,0.01,'completed',?)", (f"{v6}:model:1", rid, v6, "model", json.dumps({"decision_subject": "transcription", "recommendation": None, "runner_up": None, "why": [], "options": [], "change_mind": [], "open": []})))
    led = BudgetLedger(c, rid, 10.0)
    seeded = led.completed_result(v6, "model"); own = led.completed_result(v7, "model")
    check("ep7 the reuse key: the comparison's send sits under <run>:compare_options:<worker>:<branch>:v7:a-x:d1 (the prompt version and the model identity, D-A192); a completed result stored under the v6-shaped key is found by that key only and is never the v7 key's replay, whose own stored answer is the memo the v7 send returned (options present)",
          seeded is not None and own is not None and '"options": []' in str(seeded) and '"options": []' not in str(own) and prompts.VERSIONS["compare_options"] >= 7, {"v6": v6, "v7": v7, "seeded": str(seeded)[:80], "own": str(own)[:80]})
    c.close()


# ---------------------------------------------------------------- ep8: zero "no options" cases when the budget stops the run at the first final comparison
def _stop_ledger(at):
    class _Stopping(BudgetLedger):
        def claim_dispatch(self, step_key, kind, estimate_usd, **kw):
            if at in step_key:
                raise BudgetExceeded("forced: reserving would pass the cap minus the completion reserve")
            return super().claim_dispatch(step_key, kind, estimate_usd, **kw)
    return _Stopping


def ep8():
    stopped = run("no_options", PF.Script(questions=False, subjects=("transcription", "summary")), SEP, ledger_cls=_stop_ledger(f":{CV}:a-x:d1:final"))
    memos = [d["memo"] for d in stopped["decisions"] if d["memo"] is not None and d["memo"].get("draft_stop")]
    compared = [d for d in stopped["decisions"] if d["packs"] is not None]                                     # a decision a comparison was built for (the no-evidence branch of a subject without a claim builds none)
    no_options = [d["id"] for d in compared if d["memo"] is None or (not d["memo"].get("options") and not d["memo"].get("pack_capacity"))]
    placeholders = sum(1 for t in stopped["bodies"]["ComparisonOutV2"] if "no candidate with an explicit product identity" in t)
    check("ep8 zero 'no options' cases on the D-A178 shape: the run stopped by the budget at the first final comparison carries its drafts, each marked; every decision a comparison was built for holds a memo with options, and no comparison was sent with the placeholder candidate; the subject without a claim takes the no-evidence branch (no pack, no comparison)",
          stopped["res"]["status"] == "partial" and stopped["res"]["stop_reason"] == "budget_reserve_reached" and memos and compared and no_options == [] and placeholders == 0 and all(d["packs"] and d["memo"] and d["memo"].get("options") for d in compared)
          and [d["subject"] for d in stopped["decisions"] if d["packs"] is None] == ["summary"],
          {"res": stopped["res"], "no_options": no_options, "decisions": [(d["id"], d["subject"], d["status"], bool(d["memo"] and d["memo"].get("options")), d["packs"] is not None) for d in stopped["decisions"]]})


# ---------------------------------------------------------------- ep9: the measurement script on a copy of a fake store
def ep9():
    base = run("stats", PF.Script(questions=False), SE)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nmb-ep9-")); dst = tmp / "copy.db"
    src = open_connection(base["conn_path"]); copy_ = open_connection(str(dst))
    try:
        src.backup(copy_)
    finally:
        src.close(); copy_.close()
    before = hashlib.sha256(dst.read_bytes()).hexdigest()
    p = subprocess.run([sys.executable, str(ROOT / "scripts/eval/pack_stats.py"), "--db", str(dst), "--project", "P1", "--out", str(tmp / "stats.json")], capture_output=True, text=True, timeout=600)
    out = json.loads((tmp / "stats.json").read_text()) if (tmp / "stats.json").exists() else {}
    row = next((r for r in out.get("subjects", []) if r["subject"] == "transcription"), None)
    check("ep9 scripts/eval/pack_stats.py on a copy of a fake store: exit 0, one row per decision subject with candidates, considered, included, omitted, material contradictions, insufficient_capacity and the tokens before (v6 lines) and after (v7 packs), a totals line, the copy's bytes unchanged",
          p.returncode == 0 and row and row["candidates"] == 1 and row["included"] == 3 and row["omitted"] == 0 and row["insufficient_capacity"] == 0 and row["tokens_before"] > 0 and row["tokens_after"] > 0 and "totals" in out and "pack_stats: subjects 1" in p.stdout and hashlib.sha256(dst.read_bytes()).hexdigest() == before,
          {"rc": p.returncode, "stdout": p.stdout[-800:], "stderr": p.stderr[-800:], "row": row})
    live = subprocess.run([sys.executable, str(ROOT / "scripts/eval/pack_stats.py"), "--db", "NextMVBee-atelier/data/app.db", "--out", str(tmp / "no.json")], capture_output=True, text=True, timeout=120)
    check("ep9 the live store is refused before anything opens (exit 3, no output file)", live.returncode == 3 and not (tmp / "no.json").exists() and "refused" in live.stdout, {"rc": live.returncode, "stdout": live.stdout})
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- ep10 and ep11: the pack boundary on the comparison's answer
MARK = "AUDIT_OMITTED_CLAIM"


class _OutOfPack(PF.Script):
    """The comparison's answer cites a verified claim the cap left out of the packs: the omitted
    claim's id and text are planted in every field of the answer that could carry it (a why sentence, the picked option's claim_ids, a non-picked
    option's claim_ids, open, change_mind), on every comparison send (the draft, the tree's memo, the final)."""
    def __init__(self, **kw):
        super().__init__(questions=False, **kw); self.verified = {}; self.injected = []

    def verify_one(self, cid, kind, text, by_key):
        self.verified[cid] = text
        return super().verify_one(cid, kind, text, by_key)

    def __call__(self, body):
        result = super().__call__(body)
        if PF.schema_of(body) == "ComparisonOutV2" and "content" in result:
            u = next((m["content"] for m in body["messages"] if m["role"] == "user" and "Candidates: " in str(m.get("content"))), PF.user_text(body))
            shown = {c["claim_id"] for c in PF.claims_shown(u)}
            outside = sorted(set(self.verified) - shown)
            if outside:
                cid = outside[0]; marker = f"{MARK} {cid}: {self.verified[cid]}"[:300]
                v = json.loads(result["content"])
                v["why"].append({"text": marker, "claim_ids": [cid]})
                for o in v["options"]:
                    o["claim_ids"] = list(o["claim_ids"]) + [cid]
                v["open"].append(marker); v["change_mind"].append(marker)
                self.injected.append({"id": cid, "shown": sorted(shown)})
                return PF.J(v)
        return result


def _leaks(result, planted):
    """Every place an omitted claim could survive: the stored memos (the draft left stale and the final), the decisions' citation edges, the panel's
    request payloads, the tree's prompts and the later comparison prompts."""
    leaks = []
    for d in result["decisions"]:
        if d["packs"] is None or not d["memo"]:
            continue
        inside = {c["claim_id"] for p in d["packs"] for c in p["claims"]}
        leaks += [(d["id"], "why", w) for w in d["memo"].get("why", []) if MARK in w["text"] or set(w["claim_ids"]) - inside]
        leaks += [(d["id"], "option", o["option"], sorted(set(o["claim_ids"]) - inside)) for o in d["memo"].get("options", []) if set(o["claim_ids"]) - inside]
    c = open_connection(result["conn_path"])
    try:
        edges = {r[0] for r in c.execute("SELECT from_id FROM trace_edges WHERE kind='claim_decision'")}
    finally:
        c.close()
    leaks += [("edge", cid) for cid in sorted(edges & planted)]
    for kind in ("PanelOut", "AlternativesOut", "VariantsOut", "BranchEvaluationOut", "ComparisonOutV2"):
        leaks += [(kind, "prompt", n) for n, t in enumerate(result["bodies"][kind]) if MARK in t or any(cid in t for cid in planted)]
    return leaks


def ep10():
    sc = _OutOfPack(); plain = run("boundary", sc, caps(SEP, claims=2))
    planted = {x["id"] for x in sc.injected}; leaks = _leaks(plain, planted)
    memos = [d for d in plain["decisions"] if d["packs"] is not None and d["memo"] and d["memo"].get("options")]
    check("ep10 the pack boundary on the comparison's answer (the Codex 10.2 round 1 probe): under a cap of 2 with the panel and its follow-up, the answer of every comparison send plants a claim the cap omitted in a why sentence, every option's claim_ids, open and change_mind; after the run no stored memo (the draft left stale, the final) carries the claim's id or text in why or options, no decision cites it, the panel's request payload carries neither its id nor its text, and no later comparison prompt carries it; the memo keeps the sentence citing the pack's claims and counts the dropped one",
          sc.injected and len(plain["bodies"]["ComparisonOutV2"]) >= 2 and plain["bodies"]["PanelOut"] and leaks == [] and memos
          and all(d["memo"]["why"] and d["memo"]["dropped_sentences"] >= 1 for d in memos) and any(d["memo"].get("final") for d in memos) and plain["res"]["status"] == "completed",
          {"injected": sc.injected, "leaks": leaks[:6], "sent": {k: len(v) for k, v in plain["bodies"].items()}, "memos": [(d["id"], d["status"], d["memo"].get("final"), d["memo"].get("dropped_sentences"), [w["claim_ids"] for w in d["memo"]["why"]]) for d in memos], "res": plain["res"]})
    sc2 = _OutOfPack(subjects=("transcription",)); sc2.second_product = True
    tree = run("boundary_tree", sc2, caps(SEP, claims=2))
    planted2 = {x["id"] for x in sc2.injected}; leaks2 = _leaks(tree, planted2)
    check("ep10 the same through the tree: with two identities the tree ran and its memo send planted the omitted claim too; the tree's three prompts carry no planted id, the panel's payload neither, and no stored memo or citation edge holds it",
          sc2.injected and tree["bodies"]["AlternativesOut"] and tree["bodies"]["BranchEvaluationOut"] and any(k.endswith(f":{CV}:a-x:d1:memo") for k in tree["keys"]) and tree["bodies"]["PanelOut"] and leaks2 == [] and tree["res"]["status"] == "completed",
          {"injected": sc2.injected, "leaks": leaks2[:6], "keys": tree["keys"], "sent": {k: len(v) for k, v in tree["bodies"].items()}, "res": tree["res"]})


def ep11():
    sc = _Atlas(); soft = run("must_address", sc, caps(SE, claims=2), research=_AtlasResearch(sc), soft=True)
    d = soft["decisions"][-1]; pack = (d["packs"] or [None])[0]; shown = packs_shown(soft["bodies"]["ComparisonOutV2"][0]) if soft["bodies"]["ComparisonOutV2"] else None
    tie = sorted({cid for m in (pack or {}).get("must_address", []) for cid in m["claim_ids"]})
    c = open_connection(soft["conn_path"])
    try:
        edges = {r[0] for r in c.execute("SELECT from_id FROM trace_edges WHERE kind='claim_decision'")}
    finally:
        c.close()
    check("ep11 a material contradiction still reaches must_address under the citation rule: the open tie's two claims take the reserved slots of the cap-2 pack, must_address names them with the two values in the stored pack and in the comparison's prompt, and neither is cited (not in a why sentence's claim_ids, not a citation edge): the contradiction is addressed, never cited",
          pack and len(tie) == 2 and shown and shown[0]["must_address"] and sorted({cid for m in shown[0]["must_address"] for cid in m["claim_ids"]}) == tie and any(sorted(m.get("values") or []) == ["EU", "US"] for m in shown[0]["must_address"])
          and {c0["claim_id"] for c0 in shown[0]["claims"]} == set(tie) and d["memo"] and not any(set(w["claim_ids"]) & set(tie) for w in d["memo"].get("why", [])) and not (edges & set(tie)) and soft["res"]["status"] == "completed",
          {"tie": tie, "must_address": (pack or {}).get("must_address"), "shown": shown, "memo_why": (d["memo"] or {}).get("why"), "edges": sorted(edges)})


# ---------------------------------------------------------------- ep12 to ep14: the subject join across plan versions
PAGE2_URL = "https://docs.speech-a.example/security"
PAGE2_TEXT = "Speech API A v3 security documentation (September 2026). " + PF.filler(6500, 7) + " " + PF.FACT_LOCAL + " " + PF.filler(300, 901)
V2_QUERY = {"text": "on-device processing and network access", "target_fact": "on-device processing and network access", "freshness": "technical", "language": "en"}   # no word of the pricing page (its filler says documentation, capability, figure), so that page is outside the second plan's candidate lists
CLAIM_COLS = "id, source_id, quote, verdict, status, scope_status, decision_subject, product_id, product_version, verified_requirement_version, provenance_json"


class _Renamed(PF.Script):
    """The planner of the first run names the subject speech_to_text; the planner of the second run names it transcription with the product class
    speech_to_text (as in the devA evaluation project, where llm_model became itinerary_language_model and the candidates rows carried the old name
    as the class). The stub's subject words follow the subject in force: the plan's queries, the extractor's decision_subject and the memo's."""
    def __init__(self):
        super().__init__(questions=False, subjects=("speech_to_text",))

    def class_of(self, subject):
        return "speech_to_text"

    def applicability_for(self, reqs_u):
        return [(rid, list(self.subjects)) for rid, _ in super().applicability_for(reqs_u)]

    def __call__(self, body):
        result = super().__call__(body); s = PF.schema_of(body)
        if "content" not in result or s not in ("ResearchPlanOutV2", "EvidenceExtractionOutV2", "ComparisonOutV2"):
            return result
        v = json.loads(result["content"]); sub = self.subjects[0]
        if s == "ResearchPlanOutV2":
            v["queries"] = [dict(V2_QUERY, decision_subject=sub)] if sub == "transcription" else [dict(q, decision_subject=sub) for q in v["queries"]]
        elif s == "EvidenceExtractionOutV2":
            for c in v["claims"]:
                c["decision_subject"] = sub
        else:
            v["decision_subject"] = sub
        return PF.J(v)


class _TwoPages(PF.Sim):
    """The first run's searches return the pricing page, the second run's the security page, whose one fact (on-device processing) shares no
    searchable word with the pricing page; a read by URL returns that page's text (the zero-hour re-read of the pricing page finds it unchanged)."""
    def __init__(self):
        super().__init__(); self.phase = 1

    def text_of(self, url):
        return PAGE2_TEXT if url == PAGE2_URL else PF.PAGE_TEXT

    def search(self, q, f, n):
        self.calls.append(("search", q)); url = PAGE2_URL if self.phase == 2 else PF.PAGE_URL
        return [{"url": url, "title": "Speech API A", "text": self.text_of(url), "cache_status": "livecrawl"}]

    def contents(self, urls, f):
        self.calls.append(("contents", urls[0])); return [{"url": urls[0], "title": "Speech API A", "text": self.text_of(urls[0]), "cache_status": "livecrawl"}]


_TWO = [None]


def two_versions():
    """Two runs on one store, once per process: run 1 decides speech_to_text from the pricing page (three claims of orion-compute 1, the budget and
    the languages records counting); run 2 plans the subject transcription (class speech_to_text) with one question the security page alone
    answers, so its question attaches the on-device claim only. The rows of the first run are read before the second starts."""
    if _TWO[0] is not None:
        return _TWO[0]
    sc = _Renamed(); sim = _TwoPages()
    r, stub = PF.runner_for("ep_versions", sc, settings=SE, research=sim)
    try:
        c = r.conn
        def rows():
            ic = insp(c)
            return {"claims": {x[0]: tuple(x) for x in ic.execute(f"SELECT {CLAIM_COLS} FROM claims ORDER BY id")},
                    "coverage": {x[0]: json.loads(x[1]) for x in ic.execute("SELECT subject_id, claim_ids_json FROM coverage WHERE subject_type='research_question' ORDER BY subject_id")},
                    "support": sorted(tuple(x) for x in ic.execute("SELECT claim_id, requirement_id, verdict, status, requirement_version FROM claim_requirement_support"))}
        res1 = drive(r, r.start("P1", PF.IDEA)); rows1 = rows()
        sc.subjects = ["transcription"]; sim.phase = 2
        res2 = drive(r, r.start("P1", PF.IDEA)); rows2 = rows()
        ic = insp(c)
        decisions = {d[0]: {"id": d[0], "subject": d[1], "plan_version": d[2], "status": d[3], "choice": d[4], "rationale": d[5], "memo": json.loads(d[6]) if d[6] else None, "packs": json.loads(d[7]) if d[7] else None}
                     for d in ic.execute("SELECT id, subject, plan_version, status, choice, rationale, memo_json, evidence_pack_json FROM decisions ORDER BY id")}
        questions = {q[0]: {"subject": q[1], "run_id": q[2]} for q in ic.execute("SELECT id, decision_subject, run_id FROM research_questions ORDER BY id")}
        candidates = [tuple(x) for x in ic.execute("SELECT run_id, subject, subject_class, status FROM candidates ORDER BY run_id, subject, label")]
        shown = [packs_shown(PF.all_user_text(b)) for b in stub.bodies if PF.schema_of(b) == "ComparisonOutV2"]
        _TWO[0] = {"res1": res1, "res2": res2, "rows1": rows1, "rows2": rows2, "decisions": decisions, "questions": questions, "candidates": candidates, "shown": shown, "conn_path": str(PF.tmp / "ep_versions" / "app.db")}
    finally:
        r.close(); stub.srv.shutdown(); stub.srv.server_close()
    return _TWO[0]


def ep12():
    t = two_versions(); d1, d2 = t["decisions"].get("P1.DEC-001"), t["decisions"].get("P1.DEC-002")
    v1_qs = sorted(q for q, x in t["questions"].items() if x["subject"] == "speech_to_text"); v2_qs = sorted(q for q, x in t["questions"].items() if x["subject"] == "transcription")
    check("ep12 the premise, the devA shape: two completed runs and two plan versions; the version 1 subject speech_to_text with three questions of run 1, the version 2 subject transcription with one question of run 2 whose coverage row attaches the on-device claim alone; the transcription candidates carry the class speech_to_text; the three claims of the pricing page are attached to the version 1 questions only, two of them with counting support records for the two hard requirements",
          t["res1"]["status"] == "completed" and t["res2"]["status"] == "completed" and d1 and d2 and (d1["subject"], d1["plan_version"]) == ("speech_to_text", 1) and (d2["subject"], d2["plan_version"]) == ("transcription", 2)
          and len(v1_qs) == 3 and len(v2_qs) == 1 and t["rows2"]["coverage"][v2_qs[0]] == ["P1.CLM-004"] and all(c[1] == "transcription" and c[2] == "speech_to_text" and c[3] == "kept" for c in t["candidates"] if c[0] == "P1-R2")
          and all({"P1.CLM-001", "P1.CLM-002", "P1.CLM-003"} <= set(t["rows2"]["coverage"][q]) for q in v1_qs) and {("P1.CLM-001", "P1.REQ-001", "supported", "counts", 1), ("P1.CLM-003", "P1.REQ-002", "supported", "counts", 1)} <= set(t["rows2"]["support"]),
          {"res": (t["res1"]["status"], t["res2"]["status"]), "questions": t["questions"], "coverage": t["rows2"]["coverage"], "candidates": t["candidates"], "support": t["rows2"]["support"]})
    pack = next((p for p in (d2["packs"] or []) if p["product_id"] == "orion-compute"), None); lines = [l["claim_id"] for l in (pack or {}).get("claims", [])]
    by = {l["claim_id"]: l for l in (pack or {}).get("claims", [])}
    c = open_connection(t["conn_path"])
    try:
        hs = CS.hard_status(c, "P1", [{"id": "P1.REQ-001"}, {"id": "P1.REQ-002"}], 1, "orion-compute", "1")
    finally:
        c.close()
    check("ep12 the join (D-A253): the version 2 decision's pack of orion-compute 1 holds the predecessor's three claims beside its own, the two hard-requirement lines first by id with their support records (the price for the budget, the languages claim for languages), then the speaker-labels line and the on-device line; the hard status reads pass from the predecessor's records; the decision is active with the pick and the memo's open list carries no settling check; the comparison's prompt showed the four lines (on the base: the on-device line alone, unresolved, the settling check naming both requirements)",
          pack and lines == ["P1.CLM-001", "P1.CLM-003", "P1.CLM-002", "P1.CLM-004"] and by["P1.CLM-001"].get("supports") == ["P1.REQ-001"] and by["P1.CLM-003"].get("supports") == ["P1.REQ-002"] and not pack["omitted"] and not pack["insufficient_capacity"]
          and hs == "pass" and d2["status"] == "active" and d2["choice"] == "Orion Compute 1" and not any("settles this" in o for o in (d2["memo"] or {}).get("open", []))
          and t["shown"] and t["shown"][-1] and sorted(c0["claim_id"] for c0 in t["shown"][-1][0]["claims"]) == ["P1.CLM-001", "P1.CLM-002", "P1.CLM-003", "P1.CLM-004"],
          {"lines": lines, "supports": {k: v.get("supports") for k, v in by.items()}, "hard_status": hs, "status": d2["status"], "choice": d2["choice"], "open": (d2["memo"] or {}).get("open"), "shown": t["shown"][-1] if t["shown"] else None})
    check("ep12 the version 1 decision is untouched: its pack holds its three claims and nothing of the second run", d1["status"] == "active" and [l["claim_id"] for l in d1["packs"][0]["claims"]] == ["P1.CLM-001", "P1.CLM-003", "P1.CLM-002"], d1["packs"])


def ep13():
    t = two_versions(); d2 = t["decisions"]["P1.DEC-002"]
    v1_qs = sorted(q for q, x in t["questions"].items() if x["subject"] == "speech_to_text"); v2_qs = sorted(q for q, x in t["questions"].items() if x["subject"] == "transcription")
    old = ("P1.CLM-001", "P1.CLM-002", "P1.CLM-003")
    check("ep13 attribution kept: the second run changed no row of the three joined claims (source, quote, verdict, status, scope, decision_subject speech_to_text, identity, verified requirement version, provenance) and every version 1 question still attaches the claims it attached after the first run (the join reads the coverage rows, it rewrites none); the support records of the first run stand",
          all(t["rows1"]["claims"][cid] == t["rows2"]["claims"][cid] for cid in old) and all(t["rows2"]["claims"][cid][6] == "speech_to_text" for cid in old)
          and all(set(t["rows1"]["coverage"][q]) <= set(t["rows2"]["coverage"][q]) for q in v1_qs) and set(t["rows1"]["support"]) <= set(t["rows2"]["support"]),
          {"before": t["rows1"]["claims"], "after": {k: v for k, v in t["rows2"]["claims"].items() if k in old}, "coverage": (t["rows1"]["coverage"], t["rows2"]["coverage"])})
    pack = next(p for p in d2["packs"] if p["product_id"] == "orion-compute"); by = {l["claim_id"]: l for l in pack["claims"]}
    origin = {cid: by[cid].get("from") for cid in old}
    check("ep13 every joined pack line names where it came from: the predecessor subject speech_to_text, its plan version 1 and the version 1 questions attaching the claim; it carries no question of the version 2 decision; the decision's own line names its own question and no origin",
          all(o and o["subject"] == "speech_to_text" and o["plan_version"] == 1 and o["questions"] and set(o["questions"]) <= set(v1_qs) and set(o["questions"]) == {q for q in v1_qs if cid in t["rows2"]["coverage"][q]} for cid, o in origin.items())
          and all("questions" not in by[cid] for cid in old) and by["P1.CLM-004"].get("questions") == v2_qs and "from" not in by["P1.CLM-004"], {"origin": origin, "own": by["P1.CLM-004"]})
    shown = t["shown"][-1][0]["claims"]
    check("ep13 the prompt's lines carry no origin field and no question ids (the stored pack keeps both)", all("from" not in l and "questions" not in l for l in shown) and len(shown) == 4, shown)
    c = open_connection(t["conn_path"])
    try:
        ic = insp(c)
        claims = [dict(zip(CLAIM_COLS.replace(" ", "").split(","), x)) for x in ic.execute(f"SELECT {CLAIM_COLS} FROM claims ORDER BY id")]
        rq_claims = {}
        for rq, cj in ic.execute("SELECT subject_id, claim_ids_json FROM coverage WHERE subject_type='research_question'"):
            for cid in json.loads(cj):
                rq_claims.setdefault(cid, set()).add(rq)
        rq_subject = {q: x["subject"] for q, x in t["questions"].items()}
        joined = {x["id"] for x in WF.subject_claims(c, "P1", "transcription", claims, rq_claims, rq_subject, ["transcription"])}
        live = {x["id"] for x in WF.subject_claims(c, "P1", "transcription", claims, rq_claims, rq_subject, ["transcription", "speech_to_text"])}
        foreign = [dict(claims[0], id="P1.CLM-900", decision_subject="email")]
        rqc = dict(rq_claims, **{"P1.CLM-900": {"P1.RQ-email"}}); rqs = dict(rq_subject, **{"P1.RQ-email": "email"})
        other = {x["id"] for x in WF.subject_claims(c, "P1", "transcription", claims + foreign, rqc, rqs, ["transcription", "email"])}
        pred = {x["id"] for x in WF.subject_claims(c, "P1", "speech_to_text", claims, rq_claims, rq_subject, ["transcription"])}
        own_old = {x["id"] for x in claims if x["status"] != "unverified" and (any(coverage._norm(rq_subject.get(rq, "")) == "speech_to_text" for rq in rq_claims.get(x["id"], ())) or coverage._norm(x["decision_subject"] or "") == "speech_to_text")}
        where = WF.predecessor_of(c, "P1", "transcription", ["transcription"]), WF.predecessor_of(c, "P1", "transcription", ["transcription", "speech_to_text"]), WF.predecessor_of(c, "P1", "speech_to_text", ["transcription"])
    finally:
        c.close()
    check("ep13 the helper's guards: the version 2 subject reads the predecessor's claims beside its own; with speech_to_text a live subject of the plan (a current subject named as the class) it reads its own claim alone; a claim attached to a question of a different decision's subject (email) is never joined; the predecessor itself (its class its own name) reads exactly what the old rule read; predecessor_of names speech_to_text at plan version 1 and nothing when the name is live or is the subject's own",
          joined == {"P1.CLM-001", "P1.CLM-002", "P1.CLM-003", "P1.CLM-004"} and live == {"P1.CLM-004"} and "P1.CLM-900" not in other and other == joined and pred == own_old and {"P1.CLM-001", "P1.CLM-002", "P1.CLM-003"} <= pred
          and where[0] == {"subject": "speech_to_text", "plan_version": 1} and where[1] is None and where[2] is None, {"joined": sorted(joined), "live": sorted(live), "other": sorted(other), "pred": sorted(pred), "own_old": sorted(own_old), "where": where})


def ep14():
    t = two_versions()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nmb-ep14-")); dst = tmp / "copy.db"
    src = open_connection(t["conn_path"]); copy_ = open_connection(str(dst))
    try:
        src.backup(copy_)
    finally:
        src.close(); copy_.close()
    before = hashlib.sha256(dst.read_bytes()).hexdigest()
    p = subprocess.run([sys.executable, str(ROOT / "scripts/eval/pack_stats.py"), "--db", str(dst), "--project", "P1", "--out", str(tmp / "stats.json")], capture_output=True, text=True, timeout=600)
    out = json.loads((tmp / "stats.json").read_text()) if (tmp / "stats.json").exists() else {}
    rows = {r["subject"]: r for r in out.get("subjects", [])}
    own_v1 = {c for q, x in t["questions"].items() if x["subject"] == "speech_to_text" for c in t["rows2"]["coverage"][q]}   # what the version 1 questions attach (the second run's page reached them too)
    check("ep14 scripts/eval/pack_stats.py on a copy of the two-version store: the version 2 subject's row counts the three joined claims beside its own (included 4), the version 1 subject's row exactly what its own questions attach (it joins nothing), exit 0, the copy unchanged",
          p.returncode == 0 and rows.get("transcription", {}).get("included") == 4 and rows.get("speech_to_text", {}).get("candidates") == 1 and rows["speech_to_text"]["included"] == len(own_v1)
          and hashlib.sha256(dst.read_bytes()).hexdigest() == before, {"rc": p.returncode, "rows": rows, "own_v1": sorted(own_v1), "stderr": p.stderr[-600:]})
    gold = {"model_version": 2, "meta": {"project_id": "P1"},
            "decisions": [{"id": "P1.DEC-002", "subject": "transcription", "recommendation": "Orion Compute 1", "why": [{"text": "languages", "claims": ["P1.CLM-003"]}], "alternatives": []}],
            "evidence": {"claims": {"P1.CLM-003": {"id": "P1.CLM-003", "text": "languages", "status": "supported", "quotes": [{"text": PF.FACT_LANGS, "source": "P1.SRC-001"}]}}, "sources": {"P1.SRC-001": {"id": "P1.SRC-001", "url": PF.PAGE_URL}}}}
    (tmp / "gold.json").write_text(json.dumps(gold), encoding="utf-8")
    q = subprocess.run([sys.executable, str(ROOT / "scripts/eval/retrieval_diagnosis.py"), "--db", str(dst), "--project", "P1", "--gold", str(tmp / "gold.json"), "--out", str(tmp / "diag")], capture_output=True, text=True, timeout=600)
    funnel = json.loads((tmp / "diag" / "funnel.json").read_text()) if (tmp / "diag" / "funnel.json").exists() else {}
    item = next((r for r in funnel.get("items", []) if r["claim_id"] == "P1.CLM-003"), None); pk = (item or {}).get("stages", {}).get("pack") or {}
    check("ep14 scripts/eval/retrieval_diagnosis.py on the same copy: the languages claim of the version 1 question is cited by id and found in the version 2 subject's pack of Orion Compute 1 as a supporting line (on the base: 'not among the subject's verified claims'), and the version 2 decision names it",
          q.returncode == 0 and item and item["stages"]["cited"]["ok"] and pk.get("ok") and pk.get("role") == "supporting" and pk.get("candidate") == "Orion Compute 1" and item["stages"]["decision"]["ok"] and hashlib.sha256(dst.read_bytes()).hexdigest() == before,
          {"rc": q.returncode, "item": item, "stderr": q.stderr[-600:]})
    shutil.rmtree(tmp, ignore_errors=True)


def ep15():
    t = two_versions()
    c = open_connection(t["conn_path"])
    try:
        ic = insp(c)
        claims = [dict(zip(CLAIM_COLS.replace(" ", "").split(","), x)) for x in ic.execute(f"SELECT {CLAIM_COLS} FROM claims ORDER BY id")]
        rq_claims = {}
        for rq, cj in ic.execute("SELECT subject_id, claim_ids_json FROM coverage WHERE subject_type='research_question'"):
            for cid in json.loads(cj):
                rq_claims.setdefault(cid, set()).add(rq)
        rq_subject = {q: x["subject"] for q, x in t["questions"].items()}
        real = {x["id"] for x in claims}
        planted = [dict(claims[0], id="P1.CLM-910", decision_subject="speech_to_text"),     # the predecessor's name from the extractor, no question attaches it
                   dict(claims[0], id="P1.CLM-911", decision_subject="speech_to_text"),     # the predecessor's name from the extractor, a foreign subject's question alone attaches it
                   dict(claims[0], id="P1.CLM-912", decision_subject="transcription")]      # the subject's own name from the extractor, no question: the fallback still selects it
        rqc = dict(rq_claims, **{"P1.CLM-911": {"P1.RQ-email"}}); rqs = dict(rq_subject, **{"P1.RQ-email": "email"})
        current = ["transcription", "email"]
        selected = WF.subject_claims(c, "P1", "transcription", claims + planted, rqc, rqs, current); sel = {x["id"] for x in selected}
        pred = WF.predecessor_of(c, "P1", "transcription", current)
        inputs = EP.load_inputs(c, "P1", "transcription", [], 1, selected, set(), rqc, rqs, predecessor=pred)
        own_direct = {x["id"] for x in claims + planted if coverage._norm(x["decision_subject"] or "") == "transcription" or any(coverage._norm(rqs.get(rq, "")) == "transcription" for rq in rqc.get(x["id"], ()))}
    finally:
        c.close()
    check("ep15 the extractor fallback is the subject's own name only (S1): a claim tagged speech_to_text by the extractor that no question attaches is not selected for transcription, nor one a foreign subject's question alone attaches; a claim tagged transcription with no question still is; the four claims of the store are selected as before",
          "P1.CLM-910" not in sel and "P1.CLM-911" not in sel and "P1.CLM-912" in sel and sel & real == {"P1.CLM-001", "P1.CLM-002", "P1.CLM-003", "P1.CLM-004"}, {"selected": sorted(sel), "pred": pred})
    check("ep15 every selected claim that is not the subject's own (not its question's, not its extractor tag) is a predecessor-question claim with an origin, so every joined stored line carries from",
          sel - own_direct and (sel - own_direct) <= set(inputs.origin) and all(inputs.origin[cid]["questions"] for cid in sel - own_direct), {"joined": sorted(sel - own_direct), "origin": inputs.origin})
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nmb-ep15-")); dst = tmp / "copy.db"
    src = open_connection(t["conn_path"]); copy_ = open_connection(str(dst))
    try:
        src.backup(copy_)
    finally:
        src.close(); copy_.close()
    c = open_connection(str(dst))
    try:
        c.execute("INSERT INTO candidates (project_id, run_id, subject, label, product_class, subject_class, status) VALUES (?,?,?,?,?,?,?)", ("P1", "P1-R2", "dictation", "Orion", "generic_class", "generic_class", "kept")); c.commit()
        where = WF.predecessor_of(c, "P1", "dictation", ["dictation"])
        cls_only = {x["id"] for x in WF.subject_claims(c, "P1", "dictation", claims + [dict(claims[0], id="P1.CLM-913", decision_subject="generic_class")], rq_claims, rq_subject, ["dictation"])}
    finally:
        c.close()
    shutil.rmtree(tmp, ignore_errors=True)
    check("ep15 a class that is only a class (no decision and no question carries generic_class) joins nothing: predecessor_of names it with no plan version and subject_claims selects no claim for dictation, not the claim the extractor tagged generic_class",
          where == {"subject": "generic_class", "plan_version": None} and cls_only == set(), {"where": where, "selected": sorted(cls_only)})


# ---------------------------------------------------------------- ep16: one claim cannot contradict itself, on the devA records
def ep16():
    """devA.Q-013 and the Vercel item of devA.Q-014: the loser of a settled conflict (status conflicting, in the superseded map, no open conflict record)
    is omitted as superseded. build_pack reads the superseded map before the status, so the loser never becomes a one-claim 'contradiction' with an
    empty conflict_key and its own value as the only value. The records are copied from the devA evaluation project (tests/fixtures/deva_contradictions.json)."""
    D = json.loads((ROOT / "tests/fixtures/deva_contradictions.json").read_text(encoding="utf-8"))
    by_all = {c["id"]: c for c in D["claims"]}
    settled = [cf for cf in D["conflicts"] if cf["status"] == "settled"]
    caps0 = dict(EP.EVIDENCE_PACK_DEFAULTS)
    budget = [{"id": "devA.REQ-002", "key": "budget_monthly_usd", "value": "50"}]

    def inputs_for(subject, nums, conflicts=(), superseded=True, questions=None):
        """The pack inputs as load_inputs builds them from these rows: the supported claims outside an open conflict citable, a settled conflict's losers in the superseded map (none when superseded is False), the open conflicts given."""
        by_id = {f"devA.CLM-{n}": by_all[f"devA.CLM-{n}"] for n in nums}
        in_open = {x for k in conflicts for x in k["claim_ids"]}
        sup = {x: f"superseded by {cf['preferred_claim_id']} ({cf['reason']})" for cf in settled for x in cf["claim_ids"] if x in by_id and x != cf["preferred_claim_id"]} if superseded else {}
        return EP.PackInputs(subject=subject, hard=list(budget), rq_ids=[], citable={i for i, c in by_id.items() if c["status"] == "supported" and i not in in_open}, by_id=by_id, support={}, records={}, questions=questions or {}, conflicts=list(conflicts), superseded=sup, recency={})
    lake = EP.build_pack(inputs_for("itinerary_storage", (1018, 962)), "databricks-lakebase", "", "Databricks Lakebase", caps0)
    neon = EP.build_pack(inputs_for("itinerary_storage", ("047", "090", 691, 803, 851)), "neon-neon", "", "Neon", caps0)
    supa = EP.build_pack(inputs_for("itinerary_storage", (165, 369, 909)), "supabase-supabase", "", "Supabase", caps0)
    verc = EP.build_pack(inputs_for("web_hosting", (268, 306, 646)), "vercel-vercel", "", "Vercel", caps0)
    packs = {"lake": lake, "neon": neon, "supa": supa, "verc": verc}
    check("ep16 devA.Q-013 and the Vercel item of devA.Q-014 (D-A269): a settled conflict's loser is omitted as superseded and is never a one-claim contradiction: Lakebase's CLM-1018, Neon's CLM-803 and CLM-851 (with CLM-047 and CLM-090), Supabase's CLM-909 (with CLM-369) and Vercel's CLM-268 and CLM-646 leave must_address empty and the contradictions count 0, and the preferred claim is the one line of each pack",
          all(p["must_address"] == [] and p["counts"]["contradictions"] == 0 for p in packs.values())
          and [c["claim_id"] for c in lake["claims"]] == ["devA.CLM-962"] and {"claim_id": "devA.CLM-1018", "reason": "superseded"} in lake["omitted"]
          and [c["claim_id"] for c in neon["claims"]] == ["devA.CLM-691"] and {o["claim_id"] for o in neon["omitted"] if o["reason"] == "superseded"} == {"devA.CLM-047", "devA.CLM-090", "devA.CLM-803", "devA.CLM-851"}
          and [c["claim_id"] for c in supa["claims"]] == ["devA.CLM-165"] and {o["claim_id"] for o in supa["omitted"] if o["reason"] == "superseded"} == {"devA.CLM-369", "devA.CLM-909"}
          and [c["claim_id"] for c in verc["claims"]] == ["devA.CLM-306"] and {o["claim_id"] for o in verc["omitted"] if o["reason"] == "superseded"} == {"devA.CLM-268", "devA.CLM-646"},
          {k: {"must_address": p["must_address"], "omitted": p["omitted"], "claims": [c["claim_id"] for c in p["claims"]]} for k, p in packs.items()})
    fly_open = {"key": "fly-io-fly-io@:invoice waiver threshold [billing]", "claim_ids": ["devA.CLM-1104", "devA.CLM-1124"], "attribute": "invoice_waiver_threshold", "values": ["under $5", "under $5 waived"], "product_id": "fly-io-fly-io", "product_version": ""}
    with_record = EP.build_pack(inputs_for("web_hosting", (1104, 1124), conflicts=[fly_open]), "fly-io-fly-io", "", "Fly Io Fly Io", caps0)
    stray = EP.build_pack(inputs_for("web_hosting", (1104, 1124), superseded=False, questions={"devA.CLM-1124": {"devA.RQ-009"}}), "fly-io-fly-io", "", "Fly Io Fly Io", caps0)
    check("ep16 a conflict item of must_address comes only from an open conflict record and names at least two claims and its key: with the stored open record the Fly.io pair is one item naming both claims and the key (the devA-R3 shape, before D-A270 removes that conflict at its source); a claim whose status is conflicting with no open record and no superseded entry is shown as a contradicting line (material on its question) and raises no item",
          [(m["kind"], m["conflict_key"], m["claim_ids"]) for m in with_record["must_address"]] == [("conflict", fly_open["key"], ["devA.CLM-1104", "devA.CLM-1124"])] and with_record["counts"]["contradictions"] == 2
          and stray["must_address"] == [] and [c["claim_id"] for c in stray["claims"] if c.get("contradicts") == "conflict"] == ["devA.CLM-1124"] and stray["counts"]["contradictions"] == 1
          and all(len(m["claim_ids"]) >= 2 and m["conflict_key"] for p in list(packs.values()) + [with_record, stray] for m in p["must_address"] if m["kind"] == "conflict"),
          {"with_record": with_record["must_address"], "stray": (stray["must_address"], [(c["claim_id"], c.get("contradicts")) for c in stray["claims"]])})


# ---------------------------------------------------------------- ep17: a conflict across decision subjects, on the devA records
def ep17():
    """An open conflict whose other member another subject's questions attach: the subject's own selection holds one member only, so the loader
    reads the other from the store, and the conflict item names both claims and both values with both shown. The real selection
    (workflow.subject_claims, citable_of), loader and pack, inside the derived-read context, on a store built from the devA Fly.io record and a synthetic
    second page of equal standing (a controlled input, not a quote of a page)."""
    from app.core import reconcile
    from app.repo import Repo, connect, migrate
    from schemas import models as M
    D = json.loads((ROOT / "tests/fixtures/deva_contradictions.json").read_text(encoding="utf-8"))
    a = next(c for c in D["claims"] if c["id"] == "devA.CLM-1104"); src = next(s for s in D["sources"] if s["id"] == a["source_id"])
    b = dict(a, id="devA.CLM-9999", source_id="devA.SRC-9999", source_version="sha256:" + "9" * 64, decision_subject="itinerary_storage", value="under $6 waived", text="Fly.io waives invoices under $6.", quote="Invoices under $6 are waived.")
    src2 = dict(src, id=b["source_id"], url=src["url"].rstrip("/") + "/second-page", content_hash=b["source_version"])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ep17-")); c = connect(tmp / "ep17.db")
    try:
        migrate(c); Repo(c).create_project(M.Project(id="devA", owner_id="U1", title="t"))
        with c:
            for s in (src, src2):
                c.execute("INSERT INTO sources (id, project_id, url, canonical_url, title, tier, retrieved_at, published_at, content_hash, freshness, cache_status) VALUES (?, 'devA', ?, ?, 't', ?, ?, ?, ?, ?, ?)",
                          (s["id"], s["url"], s["url"], s["tier"], s["retrieved_at"], s["published_at"], s["content_hash"], s["freshness"], s["cache_status"]))
            for x in (a, b):
                c.execute("INSERT INTO claims (id, project_id, text, kind, source_id, quote, scope_status, source_version, subject, attribute, value, product_id, product_version, verdict, checked_at, verified_requirement_version, decision_subject, conditions_json) "
                          "VALUES (?, 'devA', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-11-07T05:00:00Z', 1, ?, ?)",
                          (x["id"], x["text"], x["kind"], x["source_id"], x["quote"], x["scope_status"], x["source_version"], x["subject"], x["attribute"], x["value"], x["product_id"], x["product_version"], x["verdict"], x["decision_subject"], x["conditions_json"]))
        reconcile.reconcile_project(c, "devA")
        ic = insp(c)
        stored = [tuple(r) for r in ic.execute("SELECT conflict_key, claim_ids_json, status FROM claim_conflicts WHERE project_id='devA'")]
        cur = ic.execute("SELECT * FROM claims WHERE project_id='devA' ORDER BY id"); names = [d[0] for d in cur.description]; claims = [dict(zip(names, r)) for r in cur.fetchall()]
        with coverage.derived(c, "devA"):
            selected = WF.subject_claims(c, "devA", "web_hosting", claims, {}, {}, ["web_hosting", "itinerary_storage"])
            citable = WF.citable_of(c, "devA", selected, 1)
            inputs = EP.load_inputs(c, "devA", "web_hosting", [], 1, selected, citable, {}, {})
            pack = EP.build_pack(inputs, "fly-io-fly-io", "", "Fly.io", dict(EP.EVIDENCE_PACK_DEFAULTS))
    finally:
        c.close(); shutil.rmtree(tmp, ignore_errors=True)
    items = pack["must_address"]; lines = {l["claim_id"]: l.get("contradicts") for l in pack["claims"]}
    check("ep17 a conflict across decision subjects (SF-3, D-A269 amended): web_hosting selects devA.CLM-1104 alone, the stored open conflict names it with devA.CLM-9999 of itinerary_storage; the loader reads the other member from the store, so the one conflict item names both claims and both values and both are shown as contradicting lines under the conflict's key, neither citable",
          len(stored) == 1 and stored[0][2] == "open" and [x["id"] for x in selected] == ["devA.CLM-1104"] and len(items) == 1 and items[0]["kind"] == "conflict" and items[0]["conflict_key"] == stored[0][0]
          and items[0]["claim_ids"] == ["devA.CLM-1104", "devA.CLM-9999"] and items[0]["values"] == ["under $5 waived", "under $6 waived"] and inputs.conflicts[0]["values"] == ["under $5 waived", "under $6 waived"]
          and lines == {"devA.CLM-1104": f"conflict:{stored[0][0]}", "devA.CLM-9999": f"conflict:{stored[0][0]}"} and pack["counts"]["contradictions"] == 2 and not citable,
          {"stored": stored, "selected": [x["id"] for x in selected], "loader_conflicts": inputs.conflicts, "must_address": items, "lines": lines, "citable": sorted(citable)})


SCENARIOS = {"ep1": ep1, "ep2": ep2, "ep3": ep3, "ep4": ep4, "ep5": ep5, "ep6": ep6, "ep7": ep7, "ep8": ep8, "ep9": ep9, "ep10": ep10, "ep11": ep11, "ep12": ep12, "ep13": ep13, "ep14": ep14, "ep15": ep15, "ep16": ep16, "ep17": ep17}
for name in (sys.argv[1:] or list(SCENARIOS)):
    SCENARIOS[name]()
print("\n".join(f"PASS  {n}" for n, ok in results if ok)); print(f"{sum(ok for _, ok in results)}/{len(results)} checks passed")
background_threads.finish()
