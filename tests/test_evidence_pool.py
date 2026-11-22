"""Empty evidence packs beside supported claims: when no eligible identity of a subject carries a supported
claim, the identities the subject's supported claims name that the pool rules excluded (a model family with no version; no input and
output token tariff of its own, or two models; a label the planner dropped as another class) are built into packs beside the
eligible set, each marked as excluded with its reason, so the supported claims reach the comparison as packs and the memo's open list says which
product was excluded and why instead of "no options" (for example, a model subject with "0 pack(s), 0 claim(s) included" beside 123 supported
claims on 74 typed identities, every one refused by a pool rule, and every configuration abstaining with "no supported rationale").
The gates and the citation rules are unchanged: no excluded identity activates. Offline, through the Runner
on the pipeline fake (tests/pipeline_fake.py). No network, no keys.

    ex1  a model-class subject whose eligible set is empty and whose supported claims name three products (two with an input tariff alone, one
         family with no version): one pack per product stored with the decision and listed in the Evidence packs step, the comparison's prompt
         carries the three packs and the three candidates marked excluded with the reason, the memo's options name them; the decision unresolved
    ex2  the open list: one line per pool rule naming the products it excluded with its reason (one for the tariffs, one for the family,
         the family's identity without a version), the candidates' excluded marks carrying the same reasons; the control: a comparison that
         recommends an excluded product is refused (it is not among the subject's eligible typed candidates) and the identity never activates
    ex3  a subject with an eligible identity is unchanged: the packs are the eligible set's alone (the one pack of the pipeline fake's transcription
         subject as ep2 of tests/test_evidence_packs.py records it; a model subject with one eligible identity beside three excluded ones gets the
         eligible pack only), no candidate carries the excluded mark and no open line names an excluded product
    ex4  two products the planner dropped under the model subject as two different classes
         carry two different reasons; the memo's duplicate rule keeps both code-composed lines (compared by their text, not by token overlap),
         so the stored memo names each product with its own reason after the plan stage's duplicate pass
    ex5  six models with an input tariff alone and labels of 37 to 40 characters: the open line names all six whole, in
         order, and carries the whole reason with its identifier; the line is never cut (its bound is the six names and the reason)

Run one scenario with its name as the argument, or all of them without one."""
import copy, json, os, pathlib, re, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("NMB_TEST_FIXTURES", "1")

results = []
def check(name, cond, detail=None):
    results.append((name, bool(cond))); assert cond, (name if detail is None else f"{name} [{detail}]")
import background_threads; background_threads.install()

import pipeline_fake as PF
from app.core import coverage
from app.core.sqlguard import inspect as insp

SE = copy.deepcopy(PF.S); SE["extraction"]["coverage_step"] = True                                   # the coverage step on
NOT_AN_OPTION = re.compile(r"^(?P<who>.+?) (is not an option|are not options) of this decision: (?P<why>.+); a source stating what is missing would make (it|one of them) one\.$")
DEVB_OPEN = "Which language models to consider. No named product was found to compare."            # the open line of a comparison that found no named product (as in the memo of devB.DEC-004)
PRODUCTS = [("Atlas", "Text", "4", "Atlas Text 4"), ("Boreal", "Chat", "2", "Boreal Chat 2"), ("Cirrus", "Chat", "", "Cirrus Chat")]   # two models with an input tariff alone, one family with no version
ELIGIBLE = ("Delta", "Chat", "1", "Delta Chat 1")                                                     # a model with both tariffs on record: an eligible identity
DROPPED = [("Atlas", "Speech", "4", "Atlas Speech 4"), ("Boreal", "Image", "2", "Boreal Image 2")]  # two products the planner drops under the model subject, each as another class: two different reasons
DROPPED_CLASS = {"Atlas Speech 4": "speech_to_text", "Boreal Image 2": "text_to_image"}
LONG = [("Atlas", "Enterprise Multilingual Chat", "4.1", "Atlas Enterprise Multilingual Chat 4.1"), ("Boreal", "Enterprise Instruction Model", "2.0", "Boreal Enterprise Instruction Model 2.0"),
        ("Cirrus", "Multilingual Reasoning Model", "3.0", "Cirrus Multilingual Reasoning Model 3.0"), ("Delta", "Enterprise Multimodal Model", "4.0", "Delta Enterprise Multimodal Model 4.0"),
        ("Epsilon", "Long Context Reasoning Model", "2.0", "Epsilon Long Context Reasoning Model 2.0"), ("Fjord", "Enterprise Language Model Pro", "1.0", "Fjord Enterprise Language Model Pro 1.0")]   # six models with an input tariff alone, labels of 37 to 40 characters
DROPPED_WHY = "dropped under this subject by the planner: class {} is not the subject's llm_model (D-A41)"
TARIFF_WHY = "no input and output token prices of its own on record (D-A143)"


class PoolScript(PF.Script):
    """A model-class subject (summary, class llm_model) whose extraction writes supported claims on products the pool rules exclude; eligible adds
    one product with both tariffs; pick False answers the comparison in devB's shape (no recommendation, the listed candidates as options, the
    DEVB_OPEN line), pick True recommends the first listed candidate citing its own lines."""
    def __init__(self, eligible=False, pick=False, products=None, classes=None):
        super().__init__(questions=False, subjects=("summary",))
        self.products = list(products or PRODUCTS) + ([ELIGIBLE] if eligible else []); self.pick = pick; self.classes = classes
        self.facts = []
        for vendor, product, version, label in self.products:
            self.facts.append((vendor, product, version, label, "input_token_price", "$0.15 per 1M tokens", f"{label} input token price is $0.15 per 1M tokens."))
            if (vendor, product, version, label) == ELIGIBLE:
                self.facts.append((vendor, product, version, label, "output_token_price", "$0.60 per 1M tokens", f"{label} output token price is $0.60 per 1M tokens."))
            self.facts.append((vendor, product, version, label, "languages", "42 languages including Hebrew and English", f"{label} languages is 42 languages including Hebrew and English."))
    def page(self):
        return "Synthetic language model pricing documentation, 4 November 2026. " + " ".join(f for *_, f in self.facts)
    def candidates_for(self, subject):
        """With classes, the planner names every product by label with the class given (two labels of other classes, which the planner drops under
        the model subject; or the six long labels as models); otherwise the fake's three generic candidates."""
        return [(label, self.classes.get(label, "llm_model"), None) for *_, label in self.products] if self.classes is not None else super().candidates_for(subject)
    def __call__(self, body):
        schema = PF.schema_of(body)
        if schema in ("EvidenceExtractionOut", "EvidenceExtractionOutV2"):
            u = PF.user_text(body); text = u[u.index(">", u.index("<untrusted_source")) + 1:u.rindex("</untrusted_source>")]
            m = re.search(r"do not repeat: (.*)\n", u); recorded = m.group(1).lower() if m else ""; helper = u.replace("| summary:", "| transcription:"); claims = []
            for vendor, product, version, label, attr, value, fact in self.facts:
                if fact not in text or fact.lower() in recorded:
                    continue
                ids = PF.subjects_for_claim(helper, attr, fact)
                if ids:
                    claims.append({"text": fact, "kind": "price" if "price" in attr else "capability", "source_id": "x", "quote": fact, "decision_subject": "summary", "subject": label, "attribute": attr, "value": value, "conditions": [], "more_conditions": False,
                                   "vendor": vendor, "product": product, "product_version": version, "subject_ids": ids})
            return PF.J({"claims": claims, "injection_suspected": False, "more_claims": False})
        if schema in ("VerificationOut", "VerificationOutV2"):
            u = PF.all_user_text(body); hard = json.loads(u[u.index("Hard requirements: ") + len("Hard requirements: "):u.index("\n", u.index("Hard requirements: "))]); by_key = {h["key"]: h["id"] for h in hard}; verdicts = []
            for cid, kind, text in PF.claims_listed(u):
                entailed = ([by_key["budget_monthly_usd"]] if "token price" in text and "budget_monthly_usd" in by_key else []) + ([by_key["languages"]] if "languages is" in text and "languages" in by_key else [])
                verdicts.append({"claim_id": cid, "verdict": "supported", "quote": next((f for *_, f in self.facts if f in text), None), "reason": "the synthetic source states this", "scope_matches_claim": True, "conditions_found": [], "version_or_date": "2026-11-04", "requirements_entailed": entailed})
            return PF.J({"verdicts": verdicts})
        if schema == "ComparisonOutV2":
            u = next((m["content"] for m in body["messages"] if m["role"] == "user" and "Candidates: " in str(m.get("content"))), PF.user_text(body))
            cands = json.loads(u[u.index("Candidates: ") + len("Candidates: "): u.index("\nFounder's usage:")])
            lines = PF.claims_shown(u)
            if not self.pick:
                options = [{"option": c["label"], "product_id": c["product_id"], "product_version": c["product_version"], "fit": "", "price": "", "monthly_cost": "", "risk": "no current version or tariff on record", "verdict": "not now", "claim_ids": []} for c in cands if c["product_id"]]
                return PF.J({"decision_subject": "summary", "recommendation": None, "runner_up": None, "why": [], "options": options, "change_mind": ["A named model, plan or API, with a source showing it handles Hebrew well"], "open": [DEVB_OPEN]})
            sel = cands[0]; mine = [c["claim_id"] for c in lines if (c["product_id"], c["product_version"]) == (sel["product_id"], sel["product_version"]) and not c.get("contradicts")]
            options = [{"option": sel["label"], "product_id": sel["product_id"], "product_version": sel["product_version"], "fit": "supports Hebrew", "price": "recorded token tariffs", "monthly_cost": "", "risk": "", "verdict": "recommended", "claim_ids": mine[:12]}]
            options += [{"option": c["label"], "product_id": c["product_id"], "product_version": c["product_version"], "fit": "", "price": "", "monthly_cost": "", "risk": "dearer", "verdict": "runner-up" if k == 0 else "not now", "claim_ids": []} for k, c in enumerate(cands[1:5])]
            return PF.J({"decision_subject": "summary", "recommendation": {"option": sel["label"], "product_id": sel["product_id"], "product_version": sel["product_version"]}, "runner_up": None,
                         "why": [{"text": "the candidate with supported claims for Hebrew", "claim_ids": mine[:10]}], "options": options, "change_mind": [], "open": ["Hebrew accuracy unverified in production"]})
        result = super().__call__(body)
        if schema in ("ResearchPlanOut", "ResearchPlanOutV2") and "content" in result:
            data = json.loads(result["content"])
            for i, q in enumerate(data.get("queries", [])):
                q["decision_subject"] = "summary"; q["target_fact"] = "input and output token price" if i == 0 else "languages"
            for row in data.get("applicability", []):
                row["subjects"] = ["summary"]
            return PF.J(data)
        return result


class PoolResearch(PF.Sim):
    def __init__(self, script):
        super().__init__(); self.script = script
    def page_text(self):
        return self.script.page()


def packs_shown(text):
    """The evidence packs of one comparison prompt (the v7 user turn)."""
    line = next((l for l in text.splitlines() if l.startswith("Evidence packs")), None)
    return json.loads(line[line.index(": ") + 2:]) if line else None


def candidates_shown(text):
    return json.loads(text[text.index("Candidates: ") + len("Candidates: "): text.index("\nFounder's usage:")]) if "Candidates: " in text else None


def run(name, script, settings=None, research=None):
    r, stub = PF.runner_for("ex_" + name, script, settings=settings or SE, research=research)
    res = r.start("P1", PF.IDEA)
    for _ in range(4):
        if res["status"] != "awaiting_user":
            break
        res = r.resume(res["run_id"], PF.answers_for(res))
    try:
        c = r.conn; rid = res["run_id"]
        decisions = [{"id": d[0], "subject": d[1], "status": d[2], "choice": d[3], "rationale": d[4], "memo": json.loads(d[5]) if d[5] else None, "packs": json.loads(d[6]) if d[6] else None, "product_id": d[7]}
                     for d in insp(c).execute("SELECT id, subject, status, choice, rationale, memo_json, evidence_pack_json, product_id FROM decisions ORDER BY id")]
        bodies = [PF.all_user_text(b) for b in stub.bodies if PF.schema_of(b) == "ComparisonOutV2"]
        events = [json.loads(e[0]) for e in c.execute("SELECT data_json FROM run_events WHERE run_id=? AND event='step' ORDER BY id", (rid,)) if '"Evidence packs"' in e[0]]
        records = [tuple(x) for x in c.execute("SELECT purpose, action, observation, change FROM reasoning_records WHERE run_id=? AND action LIKE '%evidence pack%' ORDER BY id", (rid,))]
        claims = [tuple(x) for x in insp(c).execute("SELECT id, product_id, product_version, kind, status FROM claims WHERE project_id='P1' ORDER BY id")]
        exclusions = [tuple(x) for x in c.execute("SELECT id, decision_id, product_id, product_version, stage, rule, reason FROM candidate_exclusions WHERE run_id=? ORDER BY id", (rid,))]   # the exclusions recorded for the run
        snap_row = c.execute("SELECT snapshot_json FROM plan_versions WHERE project_id='P1' ORDER BY version DESC LIMIT 1").fetchone()
        snap_ex = {d["id"]: d.get("exclusions") for d in (json.loads(snap_row[0])["decisions"] if snap_row and snap_row[0] else [])}
        return {"res": res, "decisions": decisions, "bodies": bodies, "events": events, "records": records, "claims": claims, "exclusions": exclusions, "snapshot_exclusions": snap_ex}
    finally:
        r.close(); stub.srv.shutdown(); stub.srv.server_close()


def open_lines(memo):
    """The code-composed open lines naming the excluded products: [(the products named, the reason)]."""
    return [(m.group("who"), m.group("why")) for m in (NOT_AN_OPTION.match(x) for x in (memo or {}).get("open") or []) if m]


def _pool_run(name, **kw):
    sc = PoolScript(**kw)
    return run(name, sc, research=PoolResearch(sc))


# ---------------------------------------------------------------- ex1: an empty eligible set with supported claims on three products: packs built, carried, named
def ex1():
    out = _pool_run("empty_eligible")
    d = out["decisions"][-1] if out["decisions"] else {"status": None, "memo": None, "packs": None, "rationale": ""}
    packs = d["packs"] or []; ids = [(p["product_id"], p["product_version"]) for p in packs]; shown = [packs_shown(t) for t in out["bodies"]]; cands = [candidates_shown(t) for t in out["bodies"]]
    supported = {(x[1], x[2]) for x in out["claims"] if x[4] == "supported" and x[1]}
    detail = {"status": out["res"]["status"], "decision": (d["status"], (d.get("rationale") or "")[:120]), "pack_ids": ids, "included": [[c["claim_id"] for c in p["claims"]] for p in packs], "supported_identities": sorted(supported),
              "events": [e.get("detail") for e in out["events"]], "records": out["records"], "shown": [[(p["product_id"], p["product_version"], len(p["claims"])) for p in s] for s in shown if s], "cands": cands}
    check("ex1 a model-class subject whose eligible set is empty (two models with an input tariff alone, one family with no version) and whose supported claims name three typed identities: the run completes, the decision is unresolved, and one pack per identity the supported claims name is stored with it, each holding that identity's supported lines (before D-A231: 0 packs, 0 claims included, the devB shape)",
          out["res"]["status"] == "completed" and d["status"] == "unresolved" and len(supported) == 3 and sorted(ids) == sorted(supported) and all(len(p["claims"]) == 2 and all(c["product_id"] == p["product_id"] and c["product_version"] == p["product_version"] for c in p["claims"]) for p in packs), detail)
    check("ex1 the Evidence packs step and the reasoning record count the three packs and their six included claims, and the comparison's prompt carries the three packs and three candidates, each marked excluded with the pool rule's reason (the candidates' JSON is data: the v7 prompt text is unchanged)",
          len(out["events"]) == 1 and out["events"][0]["detail"].startswith("summary: 3 pack(s), 6 claim(s) included") and sorted((p["product_id"], p["product_version"]) for p in out["events"][0]["packs"]) == sorted(supported)
          and len(out["records"]) == 1 and "3 pack(s); 6 claim(s) included" in out["records"][0][2]
          and len(out["bodies"]) == 1 and shown[0] and sorted((p["product_id"], p["product_version"]) for p in shown[0]) == sorted(supported) and all(len(p["claims"]) == 2 for p in shown[0])
          and cands[0] and sorted((c["product_id"], c["product_version"]) for c in cands[0]) == sorted(supported) and all(c.get("excluded") for c in cands[0]), detail)
    memo = d["memo"] or {}
    check("ex1 the memo names the three products: the comparison's options rows (the model's) carry them and the memo keeps the model's own open line beside the code's; nothing else of the memo shape changes (pack_line 'no claim omitted', coverage keyed by the three labels)",
          sorted((o["product_id"], o["product_version"]) for o in memo.get("options") or []) == sorted(supported) and DEVB_OPEN in (memo.get("open") or []) and memo.get("pack_line") == "no claim omitted"
          and sorted(memo.get("coverage") or {}) == sorted(p["label"] for p in packs), {"options": memo.get("options"), "open": memo.get("open"), "coverage": memo.get("coverage")})


    rows18 = [x for x in out["exclusions"] if x[1] == d.get("id")]
    snap18 = (out["snapshot_exclusions"] or {}).get(d.get("id")) or []
    check("ex1 Phase 18 package 18.3 (D-A383): every cut becomes a record: the three identities the eligible set refused are persisted for the run, the subject and the decision, one row per identity with the rule id (D-A143 for the two models with an input tariff alone, D-A133 for the family) and the reason, under project-scoped ids; the version's snapshot carries them on the decision with source run",
          len(rows18) == 3 and sorted(x[5] for x in rows18) == ["D-A133", "D-A143", "D-A143"] and all(x[4] == "eligible set" and x[0].startswith("P1.EXC-") and x[6] for x in rows18)
          and sorted((x["product_id"], x["rule"], x["source"]) for x in snap18 if x["stage"] == "eligible set") == sorted((x[2], x[5], "run") for x in rows18) and all(x["id"] and x["record_ids"] and x["checked_at"] for x in snap18 if x["stage"] == "eligible set"), (rows18, snap18))


# ---------------------------------------------------------------- ex2: the open list names the excluded products with the reason; an excluded pick never activates
def ex2():
    out = _pool_run("open_lines")
    d = out["decisions"][-1] if out["decisions"] else {"status": None, "memo": None, "packs": None}
    lines = open_lines(d["memo"]); packs = d["packs"] or []
    cands = [candidates_shown(t) for t in out["bodies"]]; marks = {c["label"]: c.get("excluded") for c in (cands[0] or []) if c.get("product_id")} if cands else {}
    detail = {"open": (d["memo"] or {}).get("open"), "lines": lines, "marks": marks, "packs": [(p["product_id"], p["product_version"], p["label"]) for p in packs]}
    check("ex2 the memo's open list says which product the pool rule excluded and why: one line per reason naming the products by label (the memo's duplicate rule reads two lines that differ by a name alone as one): the two models with an input tariff alone under D-A143, the family with no version under D-A133; the candidates' excluded marks carry the same reasons (before D-A231 the open list said no named product was found and named none)",
          sorted(lines) == [("Atlas Text 4, Boreal Chat 2", "no input and output token prices of its own on record (D-A143)"), ("Cirrus Chat", "names no model version (D-A133)")]
          and marks == {"Atlas Text 4": "no input and output token prices of its own on record (D-A143)", "Boreal Chat 2": "no input and output token prices of its own on record (D-A143)", "Cirrus Chat": "names no model version (D-A133)"}, detail)
    fam = next((p for p in packs if p["product_id"] == "cirrus-chat"), None)
    check("ex2 the family claim with no version is an identity without a version, marked so: its pack carries product_version '' and its lines the empty version, its candidate's excluded reason is D-A133's, and its open line names it by its product name alone (D-A154 unchanged: nothing versioned counts it)",
          fam is not None and fam["product_version"] == "" and fam["label"] == "Cirrus Chat" and all(c["product_version"] == "" for c in fam["claims"]) and ("Cirrus Chat", "names no model version (D-A133)") in lines, {"family": fam, "lines": lines})
    ctl = _pool_run("excluded_pick", pick=True)
    dc = ctl["decisions"][-1] if ctl["decisions"] else {"status": None, "rationale": "", "product_id": None, "memo": None, "choice": None}
    check("ex2 control: a comparison that recommends an excluded product (the first listed, Atlas Text 4, citing its own pack lines) is refused with D-A96's reason (not among the subject's eligible typed candidates, none) and never activates: the decision unresolved with the choice unresolved (the pick's identity is recorded with the unresolved draft as before, D-A44); the open lines still name the three excluded products under their two reasons",
          ctl["res"]["status"] == "completed" and dc["status"] == "unresolved" and dc["choice"] == "unresolved" and "not among the subject's eligible typed candidates (none)" in (dc["rationale"] or "")
          and sorted(open_lines(dc["memo"])) == sorted(lines), {"decision": (dc["status"], dc["choice"], (dc["rationale"] or "")[:200], dc["product_id"]), "open": (dc["memo"] or {}).get("open")})


# ---------------------------------------------------------------- ex3: a subject with an eligible identity is unchanged
def ex3():
    base = run("eligible_base", PF.Script(questions=False))
    d = base["decisions"][-1]; packs = d["packs"] or []; cands = [candidates_shown(t) for t in base["bodies"]]
    check("ex3 the pipeline fake's transcription subject (one eligible identity, ep2's shape): the decision active, one pack for orion-compute 1 with its three claims, no candidate marked excluded, no open line naming an excluded product (the base's packs, unchanged)",
          base["res"]["status"] == "completed" and d["status"] == "active" and [(p["product_id"], p["product_version"], len(p["claims"])) for p in packs] == [("orion-compute", "1", 3)]
          and cands and all(not c.get("excluded") for s in cands if s for c in s) and not open_lines(d["memo"]) and base["events"] and base["events"][0]["detail"].startswith("transcription: 1 pack(s), 3 claim(s) included"),
          {"status": base["res"]["status"], "decision": (d["status"], d["choice"]), "packs": [(p["product_id"], p["product_version"], len(p["claims"])) for p in packs], "cands": cands, "open": (d["memo"] or {}).get("open")})
    one = _pool_run("eligible_one", eligible=True, pick=True)
    d1 = one["decisions"][-1]; packs1 = d1["packs"] or []; cands1 = [candidates_shown(t) for t in one["bodies"]]; shown1 = [packs_shown(t) for t in one["bodies"]]
    supported = {(x[1], x[2]) for x in one["claims"] if x[4] == "supported" and x[1]}
    check("ex3 a model subject with one eligible identity (both tariffs on record) beside three excluded ones: the pool is the eligible set alone: one pack (delta-chat 1, its three claims) stored, shown and counted, one candidate without the excluded mark, no open line naming an excluded product, the decision active on the eligible candidate",
          one["res"]["status"] == "completed" and len(supported) == 4 and [(p["product_id"], p["product_version"], len(p["claims"])) for p in packs1] == [("delta-chat", "1", 3)]
          and shown1 and [(p["product_id"], p["product_version"]) for p in shown1[0]] == [("delta-chat", "1")] and [{k0: v0 for k0, v0 in c0.items() if k0 not in ("checked", "budget_calculation")} for c0 in cands1[0]] == [{"product_id": "delta-chat", "product_version": "1", "label": "Delta Chat 1", "coverage": cands1[0][0].get("coverage", {})}] and set(cands1[0][0].get("checked") or {}) == {"verdict_date", "newest_read", "oldest_read"}
          and "0.07 USD a month" in cands1[0][0].get("budget_calculation", "") and "CLM-" not in cands1[0][0].get("budget_calculation", "")   # the candidate carries its calculated budget sentence, without ids
          and not open_lines(d1["memo"]) and d1["status"] == "active" and d1["product_id"] == "delta-chat" and one["events"][0]["detail"].startswith("summary: 1 pack(s), 3 claim(s) included"),
          {"status": one["res"]["status"], "decision": (d1["status"], d1["choice"], (d1["rationale"] or "")[:160]), "packs": [(p["product_id"], p["product_version"], len(p["claims"])) for p in packs1], "cands": cands1, "open": (d1["memo"] or {}).get("open"), "supported": sorted(supported)})


# ---------------------------------------------------------------- ex4: two different exclusion reasons survive the memo's duplicate rule
def ex4():
    from app.core import editor as ED
    why = {lab: DROPPED_WHY.format(cls) for lab, cls in DROPPED_CLASS.items()}
    composed = [f"{lab} is not an option of this decision: {w}; a source stating what is missing would make it one." for lab, w in why.items()]
    check("ex4 the memo's duplicate rule (C6, app.core.editor.dedupe_by_meaning, run over every stored memo at the plan stage and over the reader lists of the export) keeps two code-composed exclusion lines that differ in their reason (the planner's class of each product) and still folds the same line repeated (before D-A232 its token-overlap rule read the second line as a repeat of the first, the shared wording outweighing the two class words, and dropped it)",
          ED.dedupe_by_meaning(composed) == composed and ED.dedupe_by_meaning([composed[0], composed[0]]) == [composed[0]], {"kept": ED.dedupe_by_meaning(composed)})
    out = _pool_run("dropped_two", products=DROPPED, classes=DROPPED_CLASS)
    d = out["decisions"][-1] if out["decisions"] else {"status": None, "memo": None, "packs": None}
    lines = open_lines(d["memo"]); packs = d["packs"] or []
    cands = [candidates_shown(t) for t in out["bodies"]]; marks = {c["label"]: c.get("excluded") for c in (cands[0] or []) if c.get("product_id")} if cands else {}
    detail = {"status": out["res"]["status"], "open": (d["memo"] or {}).get("open"), "lines": lines, "marks": marks, "packs": [(p["product_id"], p["product_version"], p["label"]) for p in packs]}
    check("ex4 two products the planner dropped under the model subject as two different classes (the audit's SF-1 fixture): both reach the comparison as packs marked excluded with the planner's reason, and the stored memo's open list names each with its own reason after the plan stage's duplicate pass (before D-A232 Boreal Image 2's line was lost: the product named in the settling check alone, its text_to_image reason nowhere in the memo)",
          out["res"]["status"] == "completed" and d["status"] == "unresolved" and sorted(p["label"] for p in packs) == sorted(why) and marks == why and sorted(lines) == sorted(why.items()), detail)


# ---------------------------------------------------------------- ex5: the six names and the reason, whole; no character cut
def ex5():
    labels = [lab for *_, lab in LONG]
    out = _pool_run("long_labels", products=LONG, classes={lab: "llm_model" for lab in labels})
    d = out["decisions"][-1] if out["decisions"] else {"status": None, "memo": None, "packs": None}
    lines = open_lines(d["memo"]); cands = [candidates_shown(t) for t in out["bodies"]]; marks = {c["label"]: c.get("excluded") for c in (cands[0] or []) if c.get("product_id")} if cands else {}
    raw = [x for x in (d["memo"] or {}).get("open") or [] if " are not options of this decision: " in x]
    who, why = lines[0] if lines else ("", "")
    m = re.search(r" and (\d+) more$", who); named = (who[:m.start()] if m else who).split(", ") if who else []; rest = int(m.group(1)) if m else 0
    detail = {"status": out["res"]["status"], "open": (d["memo"] or {}).get("open"), "raw": raw, "raw_lengths": [len(x) for x in raw], "lines": lines, "named": named, "rest": rest, "marks": marks}
    open_text = "\n".join((d["memo"] or {}).get("open") or [])
    check("ex5 six models with an input tariff alone and labels of 37 to 40 characters (the audit's SF-2 fixture): all six packs and excluded marks, and one open line, longer than 300 characters, that names all six whole and in order with no count of a rest and carries the whole D-A143 reason, so every excluded label and its reason are in the stored open list (before D-A233 the line was cut at 300 characters inside the reason, ending 'no input and output tok')",
          out["res"]["status"] == "completed" and marks == {lab: TARIFF_WHY for lab in labels} and len(raw) == 1 and len(raw[0]) > 300 and len(lines) == 1 and why == TARIFF_WHY
          and named == labels and rest == 0 and all(lab in open_text for lab in labels) and TARIFF_WHY in open_text, detail)


SCENARIOS = {"ex1": ex1, "ex2": ex2, "ex3": ex3, "ex4": ex4, "ex5": ex5}

if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        SCENARIOS[name]()
    print(f"test_evidence_pool: {sum(1 for _, ok in results if ok)} of {len(results)} checks passed")
