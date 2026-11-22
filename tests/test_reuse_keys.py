"""Full reuse keys (a stored result is reused only under the same prompt version and model identity) and the rule for a model send that
ended unknown, in its wording. Offline, on hand-built stores and through the Runner on
the pipeline fake (tests/pipeline_fake.py; the stub prices two routes, a/x the default and b/y the other vendor). No network, no keys.

    rk1  the verdict key at the store (app/core/coverage_step): a stored verdict is read while the requirement version, the claim set, the
         prompt version and the model identity are the ones in use; each component alone, changed, makes it unread and owed again (the model
         through use_model, the prompt through prompts.VERSIONS, the requirement through its version, the claims through the evidence hash of the key part); a row
         written before the model identity was recorded (model_id NULL, the migration 072 column) never counts inside a run; outside a run (no model in use) a row
         is read as it stands
    rk2  the verdict key through runs: the coverage sends of a run carry <run>:coverage:w0:b0:v3:a-x:d1:... and the rows record the model; a
         quality run under the same prompt and model owes no coverage send (hit); the same project under another route (b/y) judges every
         verdict again under v3:b-y keys with the rows re-recorded (miss); the next run under b/y owes nothing again (hit)
    rk3  the memo key: <run>:compare_options:<worker>:<branch>:v7:<model>:d<n> (:final on a panel run); the stored result is found by that key and by no
         key of another model or another prompt version; the route b/y gives v7:b-y and the comparison sends go to b/y; the tag alone,
         component by component
    rk4  the key of the panel (the models that read a draft comparison): <run>:panel:w0:b0:v3:<model>:d<n> for the fusion panel, panel_direct.v1:<model> without the tool (discovery.panel_server_tools off), the
         configured panel model in the key and on the wire
    rk5  the key of the tree (the bounded exploration of a decision's alternatives, app/core/tree.py): <run>:tree:w0:b0:d<n>:<prompt>.v<n>:<model>:<part> for the three prompts, the decision still the fifth part the ledger's
         tree limits read; the route b/y in every tree key and on the wire
    rk6  the unknown-call rule in its wording, at the ledger: a model send that ended unknown blocks the next send of its own step (the dependent action
         waits: UnknownCallPending), another step's send is claimed, sent and settled meanwhile (independent work continues), and the unknown
         row keeps its reservation, counted against the cap and listed as a possible charge, until the founder resolves it (retried keeps the
         possible charge and frees the step for attempt 2; reconciled replaces it by the provider's figure; skipped is answered without a send)

Run one scenario with its name as the argument, or all of them without one."""
import copy, json, os, pathlib, sys, types
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("NMB_TEST_FIXTURES", "1")

results = []
def check(name, cond, detail=None):
    results.append((name, bool(cond))); assert cond, (name if detail is None else f"{name} [{detail}]")
import background_threads; background_threads.install()

import pipeline_fake as PF
from app import repo as R
from app.core import coverage_step as CS
from app.adapters.model_gateway import CloudGateway
from app.core.ledger import BudgetLedger, UnknownCallPending
from app.core.sqlguard import inspect as insp
from app.graph import prompts, workflow as WF
CV = f"v{prompts.VERSIONS['compare_options']}"; CVN = f"v{prompts.VERSIONS['compare_options'] + 1}"   # the comparison's prompt version as the keys name it, and the next one
from app.runner.recovery import start_run
from schemas import models as M

HARD = [{"id": "P1.REQ-001", "key": "languages", "value": "Hebrew", "hard": True, "applies_to": None}]
A, B = "a/x", "b/y"                                                                      # the fake gateway's two priced routes (tests/pipeline_fake.py gateway_factory)


def drive(runner, res):
    for _ in range(4):
        if res["status"] != "awaiting_user":
            break
        res = runner.resume(res["run_id"], PF.answers_for(res))
    return res


def strong(settings, stages, model):
    """Settings whose strong route for the given stages is the given model (the strongest priced model of the rank)."""
    s = copy.deepcopy(settings); s["models"]["strong_stages"] = list(stages); s["models"]["quality_rank"] = [model]
    s["models"]["prices_per_million"] = dict(s["models"]["prices_per_million"], **{model: [1.0, 1.0]})
    return s


def ctx_for(settings):
    return types.SimpleNamespace(settings=settings, gateway=types.SimpleNamespace(prices={A: (1, 1), B: (1, 1)}, default_model=A))


def keys_of(c, rid, stage):
    return [k[0] for k in c.execute("SELECT DISTINCT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? ORDER BY step_key", (rid, f"%:{stage}:%"))]


def models_sent(stub, schema, since=0):
    return [b["model"] for b in stub.bodies[since:] if PF.schema_of(b) == schema]


def coverage_rows(c):
    return [tuple(x) for x in insp(c).execute("SELECT run_id, requirement_id, product_id, verdict, prompt_version, model_id FROM coverage_checks ORDER BY requirement_id, product_id")]


def close(r, stub):
    r.close(); stub.srv.shutdown(); stub.srv.server_close()


# ---------------------------------------------------------------- rk1: the verdict key at the store, component by component
def rk1():
    path = PF.tmp / "rk1" / "store.db"; path.parent.mkdir(parents=True, exist_ok=True)
    c = R.connect(path); R.apply_schema(c); R.Repo(c).create_project(M.Project(id="P1", owner_id="U1", title="t"))
    with c:
        c.execute("INSERT INTO sources (id, project_id, url, canonical_url, tier, retrieved_at, content_hash, freshness, cache_status) VALUES ('P1.SRC-001','P1','https://a.example','https://a.example',1,'2026-11-01','sha256:one','technical','livecrawl')")
    def claim(n):
        with c:
            c.execute("INSERT INTO claims (id, project_id, text, kind, source_id, quote, scope_status, source_version, verdict, verified_requirement_version, conditions_status, product_id, product_version) VALUES (?,'P1','Hebrew is supported','capability','P1.SRC-001','q','in_scope','sha256:one','supported',1,'complete','acme-speech','')", (f"P1.CLM-{n:03d}",))
    claim(1); claim(2)
    read = lambda rv=1: CS.verdict_of(c, "P1", "P1.REQ-001", rv, "acme-speech", "")
    owed = lambda rv=1: CS.plan("speech", [("acme-speech", "")], HARD, rv, c, "P1")
    saved = prompts.VERSIONS["coverage"]
    try:
        CS.use_model(A)
        with c:
            CS.record(c, "P1", "R1", "P1.REQ-001", 1, "speech", "acme-speech", "", "entails", ["P1.CLM-001"], "the quote names Hebrew")
        row = insp(c).execute("SELECT prompt_version, model_id, judged_json IS NOT NULL FROM coverage_checks WHERE project_id='P1'").fetchone()
        hit = (read(), owed(), CS.hard_status(c, "P1", HARD, 1, "acme-speech", ""))
        check("rk1 hit: the stored verdict records the prompt version and the model identity it was judged under, and under the same requirement version, claim set, prompt version and model it is read, counts (pass) and owes no call",
              tuple(row) == (3, A, 1) and hit[0] is not None and hit[0]["verdict"] == "entails" and hit[1] == [] and hit[2] == "pass", (row, hit))
        CS.use_model(B)
        miss_model = (read(), owed(), CS.hard_status(c, "P1", HARD, 1, "acme-speech", ""))
        CS.use_model(A)
        check("rk1 the model alone: under another model in use nothing reads the verdict (unresolved, never failed), the step is owed again over the same claims with the same evidence hash, and back under the recorded model it is read again",
              miss_model[0] is None and len(miss_model[1]) == 1 and miss_model[2] == "unresolved" and miss_model[1][0]["key_part"].startswith("speech:acme-speech:p1-req-001:") and read() is not None, miss_model)
        prompts.VERSIONS["coverage"] = saved + 1
        miss_prompt = (read(), owed())
        prompts.VERSIONS["coverage"] = saved
        check("rk1 the prompt version alone: under another coverage prompt version nothing reads the verdict and the step is owed again; back under the recorded version it is read (D-A190, the component kept)",
              miss_prompt[0] is None and len(miss_prompt[1]) == 1 and read() is not None, miss_prompt)
        check("rk1 the requirement alone: the verdict is keyed at its requirement version, so at version 2 (a changed requirement) nothing reads it and the step is owed again; at version 1 it is read",
              read(2) is None and len(owed(2)) == 1 and read(1) is not None, (read(2), owed(2)))
        claim(3)
        miss_claims = (read(), owed())
        check("rk1 the claim set alone (D-A161, the component kept): one new supported, in-scope claim of the product makes the verdict unread and owed again, under a key part with another evidence hash",
              miss_claims[0] is None and len(miss_claims[1]) == 1 and miss_claims[1][0]["key_part"] != miss_model[1][0]["key_part"], miss_claims)
        with c:
            CS.record(c, "P1", "R2", "P1.REQ-001", 1, "speech", "acme-speech", "", "entails", ["P1.CLM-003"], "judged again")
            c.execute("UPDATE coverage_checks SET model_id=NULL WHERE project_id='P1'")
        before_rule = (read(), owed())
        CS.use_model(None)
        outside = read()
        check("rk1 a row written before the rule (model_id NULL, the column migration 072 adds) never counts inside a run and is owed again; outside a run (no model in use) the row is read as it stands, a read is not a reuse",
              before_rule[0] is None and len(before_rule[1]) == 1 and outside is not None and outside["verdict"] == "entails", (before_rule, outside))
        # the migration on an older store: the column arrives, the old rows hold NULL
        c2 = R.connect(PF.tmp / "rk1" / "old.db"); R.apply_schema(c2); R.Repo(c2).create_project(M.Project(id="PO", owner_id="U1", title="old"))
        R.apply_fixture(c2, "es_coverage_checks_068.sql")
        with c2:
            c2.execute("INSERT INTO coverage_checks (project_id, run_id, requirement_id, requirement_version, subject, product_id, product_version, verdict, claim_ids_json, note, judged_json) VALUES ('PO','R0','PO.REQ-001',1,'speech','acme-y','','entails','[]','old','[]')")
        cols_before = {r[1] for r in c2.execute("PRAGMA table_info(coverage_checks)")}
        R.migrate(c2)
        cols_after = {r[1] for r in c2.execute("PRAGMA table_info(coverage_checks)")}
        old_row = insp(c2).execute("SELECT verdict, prompt_version, model_id FROM coverage_checks WHERE project_id='PO'").fetchone()
        check("rk1 migration 072: a store without the column gets model_id through migrate(), after 069's rebuild, and its old rows hold NULL in both records (judged again once, D-A161's reading)",
              "model_id" not in cols_before and "model_id" in cols_after and tuple(old_row) == ("entails", None, None), (sorted(cols_before), sorted(cols_after), old_row))
        c2.close()
    finally:
        prompts.VERSIONS["coverage"] = saved; CS.use_model(None)
    c.close()


# ---------------------------------------------------------------- rk2: the verdict key through runs (hit, miss on the model, hit again)
def rk2():
    SE = copy.deepcopy(PF.S); SE["extraction"]["coverage_step"] = True
    sc = PF.Script(questions=False, subjects=("transcription",)); sc.entail_none_for = ("languages", "price")   # the verifier records nothing for the hard requirements: the coverage model is asked
    r, stub = PF.runner_for("rk2", sc, settings=SE)
    res1 = drive(r, r.start("P1", PF.IDEA)); c = r.conn; rid1 = res1["run_id"]
    keys1, rows1, sent1 = keys_of(c, rid1, "coverage"), coverage_rows(c), models_sent(stub, "CoverageOut")
    check("rk2 the run's coverage sends carry <run>:coverage:w0:b0:v3:a-x:d1:<subject>:<product>:<requirement>:<evidence> (the prompt version and the model identity before the decision and the claims), every send went to the route a/x and every stored verdict records prompt version 3 and model a/x",
          res1["status"] == "completed" and keys1 and all(f":coverage:w0:b0:v3:a-x:d1:" in k for k in keys1) and sent1 and set(sent1) == {A} and rows1 and all(r0[4] == 3 and r0[5] == A and r0[0] == rid1 for r0 in rows1),
          {"keys": keys1, "rows": rows1, "sent": sent1})
    n = len(stub.bodies)
    res2 = drive(r, r.requality("P1", "rk2: the same prompt and model", research=False)); rid2 = res2["run_id"]
    sent2, keys2, rows2 = models_sent(stub, "CoverageOut", n), keys_of(c, rid2, "coverage"), coverage_rows(c)
    check("rk2 hit: a quality run of the same project under the same prompt version and model reads every verdict (the same claims), owes no coverage send and leaves the rows as the first run wrote them",
          res2["status"] == "completed" and rid2 != rid1 and sent2 == [] and keys2 == [] and rows2 == rows1, {"sent": sent2, "keys": keys2, "rows": rows2, "res": res2["status"]})
    close(r, stub)
    SE2 = strong(SE, ["coverage", "compare_options"], B)
    stub2 = PF.ModelStub(sc)
    r2 = PF.Runner(PF.tmp / "rk2" / "app.db", settings=SE2, gateway_factory=PF.gateway_factory(stub2, SE2), research=PF.Sim(), instance_id="inst-2", data_dir=PF.tmp / "rk2" / "data")
    res3 = drive(r2, r2.requality("P1", "rk2: another model", research=False)); c2 = r2.conn; rid3 = res3["run_id"]
    sent3, keys3, rows3 = models_sent(stub2, "CoverageOut"), keys_of(c2, rid3, "coverage"), coverage_rows(c2)
    check("rk2 miss on the model: the same project under the route b/y judges every verdict again (as many coverage sends as the first run, every one to b/y) under v3:b-y keys, and every row is re-recorded with model b/y by this run",
          res3["status"] == "completed" and len(sent3) == len(sent1) and set(sent3) == {B} and keys3 and all(":coverage:w0:b0:v3:b-y:d1:" in k for k in keys3)
          and len(rows3) == len(rows1) and all(r0[5] == B and r0[0] == rid3 and r0[4] == 3 for r0 in rows3), {"sent": sent3, "keys": keys3, "rows": rows3})
    n2 = len(stub2.bodies)
    res4 = drive(r2, r2.requality("P1", "rk2: b/y again", research=False)); rid4 = res4["run_id"]
    check("rk2 hit again: the next quality run under b/y owes no coverage send and the rows stand",
          res4["status"] == "completed" and models_sent(stub2, "CoverageOut", n2) == [] and keys_of(c2, rid4, "coverage") == [] and coverage_rows(c2) == rows3, {"sent": models_sent(stub2, "CoverageOut", n2)})
    cmp3 = keys_of(c2, rid3, "compare_options")
    check("rk2 the memo key followed the route too: the b/y run's comparison keys carry v7:b-y and its comparison sends went to b/y (rk3 proves the components)",
          cmp3 and all(f":compare_options:w0:b0:{CV}:b-y:d" in k for k in cmp3) and set(models_sent(stub2, "ComparisonOutV2")) == {B}, cmp3)
    close(r2, stub2)
    check("rk2 the run's coverage model is in use only while its drive runs (SF-2): after both runners completed their runs and closed, no model is in use and a row recorded under a/x counts outside a run as it stands",
          CS.model_in_use() is None and CS.counts(CS.prompt_version(), A), {"after_both_runners_closed": CS.model_in_use(), "a/x counts outside a run": CS.counts(CS.prompt_version(), A)})


# ---------------------------------------------------------------- rk3: the memo key, component by component
def rk3():
    r, stub = PF.runner_for("rk3", PF.Script(questions=False, subjects=("transcription",)))
    res = drive(r, r.start("P1", PF.IDEA)); c = r.conn; rid = res["run_id"]
    keys = keys_of(c, rid, "compare_options"); led = BudgetLedger(c, rid, 10.0)
    real = next((k for k in keys if k.endswith(f":{CV}:a-x:d1")), None)
    other_model = real.replace(f":{CV}:a-x:d1", f":{CV}:b-y:d1") if real else None; other_prompt = real.replace(f":{CV}:a-x:d1", f":{CVN}:a-x:d1") if real else None; old_shape = real.replace(f":{CV}:a-x:d1", f":{CV}:d1") if real else None
    check("rk3 the comparison's send sits under <run>:compare_options:w0:b0:v7:a-x:d1 (the final send of a panel run under the same key with :final): the stored memo is found by that key, the gateway's replay lookup, and by no key of another model (v7:b-y), another prompt version (v8:a-x) or the shape before this rule (v7:d1)",
          res["status"] == "completed" and real and keys == [real] and set(models_sent(stub, "ComparisonOutV2")) == {A} and led.completed_result(real, "model") is not None
          and led.completed_result(other_model, "model") is None and led.completed_result(other_prompt, "model") is None and led.completed_result(old_shape, "model") is None, {"keys": keys})
    close(r, stub)
    base = ctx_for(PF.S); routed = ctx_for(strong(PF.S, ["compare_options"], B)); saved = prompts.VERSIONS["compare_options"]
    try:
        same = (WF.reuse_tag(base, "compare_options"), WF.reuse_tag(ctx_for(copy.deepcopy(PF.S)), "compare_options"))
        by_model = WF.reuse_tag(routed, "compare_options")
        prompts.VERSIONS["compare_options"] = saved + 1
        by_prompt = WF.reuse_tag(base, "compare_options")
    finally:
        prompts.VERSIONS["compare_options"] = saved
    unpriced = ctx_for(strong(PF.S, ["compare_options"], "c/z")); unpriced.gateway.prices = {A: (1, 1), B: (1, 1)}
    check("rk3 the tag, component by component: the same settings give the same tag v7:a-x; the route b/y alone gives v7:b-y; the prompt version alone gives v8:a-x; a strong route the gateway does not price resolves to the default route, as the send does (stage_model is the one resolution call_stage uses)",
          same == (f"{CV}:a-x", f"{CV}:a-x") and by_model == f"{CV}:b-y" and by_prompt == f"{CVN}:a-x" and WF.reuse_tag(unpriced, "compare_options") == f"{CV}:a-x" and WF.stage_model(routed, "compare_options") == B and WF.stage_model(base, "verify") == A,
          (same, by_model, by_prompt))
    pair = ("a/b-c", "a-b/c"); prices = {x: (1, 1) for x in pair}                        # two priced routes whose slash and dash sit in each other's place
    ctxs = [types.SimpleNamespace(settings={}, gateway=types.SimpleNamespace(default_model=x, prices=prices)) for x in pair]
    tags = {p: [WF.reuse_tag(c0, p, s0) for c0 in ctxs] for p, s0 in (("coverage", "coverage"), ("compare_options", "compare_options"), ("panel", "panel"), ("panel_direct", "panel"), ("tree_alternatives", "tree"))}
    k1, k2 = (f"R:compare_options:w0:b0:{WF.reuse_tag(c0, 'compare_options')}:d1" for c0 in ctxs)
    stored = {k1: ("entry-a", json.dumps({"produced_under": pair[0]}))}                  # the first route's answer, stored under its key
    fake = types.SimpleNamespace(ledger=types.SimpleNamespace(completed_result=lambda key, kind: stored.get(key)))
    first = CloudGateway.call(fake, k1, types.SimpleNamespace(model_validate_json=json.loads), [], 0, model=pair[0])
    check("rk3 two distinct model identities never share a key (SF-1): a/b-c and a-b/c give different slugs and different tags for every prompt, the first route's stored answer replays under its own key through the gateway's replay branch, and the second route's key finds nothing stored (the fake routes keep their slugs a-x and b-y)",
          WF.model_slug(pair[0]) != WF.model_slug(pair[1]) and all(t[0] != t[1] for t in tags.values()) and k1 != k2 and first["status"] == "replayed" and first["output"] == {"produced_under": pair[0]}
          and fake.ledger.completed_result(k2, "model") is None and (WF.model_slug(A), WF.model_slug(B)) == ("a-x", "b-y"), {"slugs": [WF.model_slug(x) for x in pair], "tags": tags})


# ---------------------------------------------------------------- rk4: the panel key
def rk4():
    SP = copy.deepcopy(PF.S); SP["discovery"].update({"panel": True, "simulate_panel": True, "panel_cap_usd": 0.05})
    out = {}
    for name, settings in (("fusion", SP), ("direct", {**copy.deepcopy(SP), "discovery": dict(SP["discovery"], panel_server_tools=False)}), ("routed", {**copy.deepcopy(SP), "discovery": dict(SP["discovery"], model=B)})):
        r, stub = PF.runner_for(f"rk4_{name}", PF.Script(questions=False, subjects=("transcription",)), settings=settings)
        res = drive(r, r.start("P1", PF.IDEA)); c = r.conn; rid = res["run_id"]
        led = BudgetLedger(c, rid, 10.0); ks = keys_of(c, rid, "panel"); tag = {"fusion": "v3:a-x", "direct": "panel_direct.v1:a-x", "routed": "v3:b-y"}[name]
        others = (tag.replace("a-x", "b-y") if "a-x" in tag else tag.replace("b-y", "a-x"), tag.replace("v3", "v4").replace("v1", "v2"), tag.replace("v3", "panel_direct.v1") if "v3" in tag else tag.replace("panel_direct.v1", "v3"))   # another model, another prompt version, the other panel prompt
        out[name] = {"status": res["status"], "keys": ks, "sent": models_sent(stub, "PanelOut"),
                     "hits": [CloudGateway.call(types.SimpleNamespace(ledger=led), k, M.PanelOut, [], 0)["status"] for k in ks],                      # the gateway's replay branch over the stored result
                     "misses": [led.completed_result(k.replace(f":{tag}:", f":{t}:"), "model") for k in ks for t in others] + [led.completed_result(k.replace(f":{tag}:", ":"), "model") for k in ks]}
        close(r, stub)
    f, d, m = out["fusion"], out["direct"], out["routed"]
    check("rk4 the stored panel result at the store (SF-3): in each of the three runs the panel's key replays its stored result through the gateway's replay branch (a hit under unchanged inputs), and nothing is stored under the key of another model, another prompt version, the other panel prompt or the shape before the rule (a miss after each change)",
          all(o["hits"] and set(o["hits"]) == {"replayed"} and len(o["misses"]) == 4 * len(o["keys"]) and all(x is None for x in o["misses"]) for o in out.values()), {k: (o["hits"], o["misses"]) for k, o in out.items()})
    check("rk4 the panel key: the fusion panel's send sits under <run>:panel:w0:b0:v3:a-x:d1 (panel.v3 and the default route); without the tool (D-A206) the key names its own prompt, panel_direct.v1:a-x; with [discovery].model b/y the key says v3:b-y and the send goes to b/y; every run completed",
          all(o["status"] == "completed" and o["keys"] and o["sent"] for o in out.values())
          and all(k.endswith(":panel:w0:b0:v3:a-x:d1") for k in f["keys"]) and set(f["sent"]) == {A}
          and all(k.endswith(":panel:w0:b0:panel_direct.v1:a-x:d1") for k in d["keys"]) and set(d["sent"]) == {A}
          and all(k.endswith(":panel:w0:b0:v3:b-y:d1") for k in m["keys"]) and set(m["sent"]) == {B}, out)
    ctx = ctx_for(SP)
    check("rk4 the panel's tag resolves the configured model as the send does: the fusion prompt v3:a-x, the direct prompt panel_direct.v1:a-x, the configured model b/y in both",
          WF.reuse_tag(ctx, "panel", "panel", None) == "v3:a-x" and WF.reuse_tag(ctx, "panel_direct", "panel", None) == "panel_direct.v1:a-x" and WF.reuse_tag(ctx, "panel", "panel", B) == "v3:b-y" and WF.reuse_tag(ctx, "panel_direct", "panel", B) == "panel_direct.v1:b-y")


# ---------------------------------------------------------------- rk5: the tree key
def rk5():
    out = {}
    for name, settings in (("default", PF.S), ("routed", strong(PF.S, ["tree"], B))):
        sc = PF.Script(questions=False); sc.second_product = True
        r, stub = PF.runner_for(f"rk5_{name}", sc, settings=settings)
        res = drive(r, r.start("P1", PF.IDEA)); c = r.conn; rid = res["run_id"]
        keys = [k[0] for k in c.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? ORDER BY planned_at, attempt", (rid, f"{rid}:tree:%"))]
        led = BudgetLedger(c, rid, 1.0); contract = {"alternatives": M.AlternativesOut, "variants": M.VariantsOut, "evaluate": M.BranchEvaluationOut}
        other = (lambda k: k.replace(":a-x:", ":b-y:") if ":a-x:" in k else k.replace(":b-y:", ":a-x:"), lambda k: k.replace(".v2:", ".v3:").replace(".v1:", ".v2:"), lambda k: ":".join(k.split(":")[:5] + k.split(":")[7:]))   # another model, another prompt version (the next version of whichever the key carries), the key without its prompt and model parts (d1:alternatives)
        out[name] = {"status": res["status"], "keys": keys, "parts": [k.split(":", 4)[4] for k in keys], "decisions": [led.tree_decision_of(k) for k in keys],
                     "sent": [b["model"] for b in stub.bodies if PF.schema_of(b) in ("AlternativesOut", "VariantsOut", "BranchEvaluationOut")],
                     "hits": [CloudGateway.call(types.SimpleNamespace(ledger=led), k, contract[k.split(":")[7]], [], 0)["status"] for k in keys],       # the gateway's replay branch over the stored result
                     "misses": [led.completed_result(o(k), "model") for k in keys for o in other]}
        close(r, stub)
    d, m = out["default"], out["routed"]
    check("rk5 the stored tree results at the store (SF-3): in both runs each of the three tree keys replays its stored result through the gateway's replay branch (a hit under unchanged inputs), and nothing is stored under the key of another model, another prompt version or the shape before the rule (a miss after each change)",
          all(o["hits"] == ["replayed"] * 3 and len(o["misses"]) == 9 and all(x is None for x in o["misses"]) for o in out.values()), {k: (o["hits"], o["misses"]) for k, o in out.items()})
    check("rk5 the tree key: the three sends sit under <run>:tree:w0:b0:d1:<prompt>.v1:a-x:<part> (tree_alternatives, tree_variants, tree_evaluate, each part under its own prompt version and the model identity), the decision stays the fifth part the ledger's tree limits read, and the sends went to a/x",
          d["status"] == "completed" and d["parts"] == ["d1:tree_alternatives.v2:a-x:alternatives", "d1:tree_variants.v2:a-x:variants:A1", "d1:tree_evaluate.v2:a-x:evaluate"] and d["decisions"] == ["d1", "d1", "d1"] and set(d["sent"]) == {A}, d)
    check("rk5 the route b/y for the tree stage: every tree key says b-y, every tree send went to b/y, the decision part unchanged",
          m["status"] == "completed" and m["parts"] == ["d1:tree_alternatives.v2:b-y:alternatives", "d1:tree_variants.v2:b-y:variants:A1", "d1:tree_evaluate.v2:b-y:evaluate"] and m["decisions"] == ["d1", "d1", "d1"] and set(m["sent"]) == {B}, m)
    saved = prompts.VERSIONS["tree_variants"]
    try:
        prompts.VERSIONS["tree_variants"] = saved + 1
        tags = (WF.reuse_tag(ctx_for(PF.S), "tree_alternatives", "tree"), WF.reuse_tag(ctx_for(PF.S), "tree_variants", "tree"))
    finally:
        prompts.VERSIONS["tree_variants"] = saved
    check("rk5 one prompt's version alone changes that part's key and no other's", tags == ("tree_alternatives.v2:a-x", f"tree_variants.v{saved + 1}:a-x"), tags)


# ---------------------------------------------------------------- rk6: the unknown-call rule in its wording, at the ledger
def rk6():
    path = PF.tmp / "rk6" / "store.db"; path.parent.mkdir(parents=True, exist_ok=True)
    c = R.connect(path); R.migrate(c); R.Repo(c).create_project(M.Project(id="P1", owner_id="U1", title="t")); start_run(c, "R6", "P1", "T6", "W1")
    led = BudgetLedger(c, "R6", 2.0)
    row = lambda eid: tuple(c.execute("SELECT state, reserved_usd, reported_usd, resolution FROM usage_ledger WHERE id=?", (eid,)).fetchone())
    dep, ind = "R6:verify:w0:b0:P1.SRC-001:g1", "R6:verify:w0:b0:P1.SRC-002:g1"
    e1 = led.claim_dispatch(dep, "model", 0.10); led.mark_sent(e1); led.fail(e1, charged=None)           # the send ended unknown (a lost reply): a possible charge
    at_unknown = (row(e1), led.committed(), led.possible_charges(), led.unknown_calls())
    try:
        led.claim_dispatch(dep, "model", 0.10); waited = None
    except UnknownCallPending as e:
        waited = str(e)
    e2 = led.claim_dispatch(ind, "model", 0.05); led.mark_sent(e2); led.settle(e2, 0.02, result_json=json.dumps({"ok": 1}))   # another step's send: claimed, sent, settled
    after_independent = (row(e1), row(e2), led.committed(), led.possible_charges(), [u["id"] for u in led.unknown_calls()])
    check("rk6 the unknown call and every action that depends on its result wait: the next send of the same step is refused with UnknownCallPending naming the unknown attempt, nothing is resent on its own (one row of the step)",
          waited and e1 in waited and c.execute("SELECT COUNT(*) FROM usage_ledger WHERE step_key=?", (dep,)).fetchone()[0] == 1, waited)
    check("rk6 independent work continues: another step's send is claimed, sent and settled while the unknown one is pending, and the run stays running",
          row(e2) == ("completed", 0.05, 0.02, None) and c.execute("SELECT status FROM runs WHERE id='R6'").fetchone()[0] == "running", after_independent)
    check("rk6 the budget reserved for the call stays reserved until the result is clarified: the unknown row keeps its 0.10 reservation, unresolved, counted against the cap (committed 0.10, then 0.12 with the settled send) and listed as the one possible charge, before and after the independent work",
          at_unknown[0] == ("unknown", 0.10, None, None) and abs(at_unknown[1] - 0.10) < 1e-9 and abs(at_unknown[2] - 0.10) < 1e-9 and [u["id"] for u in at_unknown[3]] == [e1] and abs(at_unknown[3][0]["possible_charge_usd"] - 0.10) < 1e-9
          and after_independent[0] == ("unknown", 0.10, None, None) and abs(after_independent[2] - 0.12) < 1e-9 and abs(after_independent[3] - 0.10) < 1e-9 and after_independent[4] == [e1], (at_unknown, after_independent))
    led.resolve_unknown(e1, "retried")
    e3 = led.claim_dispatch(dep, "model", 0.10)
    retried = (row(e1), e3, led.committed(), led.possible_charges(), led.unknown_calls())
    check("rk6 the founder's retry: the step is free for attempt 2 (a new row, a new reservation), the first row stays unknown with its possible charge still counted (committed 0.22, possible 0.10) and is no longer listed as unresolved",
          retried[0] == ("unknown", 0.10, None, "retried") and e3 == f"{dep}:model:2" and abs(retried[2] - 0.22) < 1e-9 and abs(retried[3] - 0.10) < 1e-9 and retried[4] == [], retried)
    led.release(e3)
    e4 = led.claim_dispatch("R6:verify:w0:b0:P1.SRC-003:g1", "model", 0.30); led.mark_sent(e4); led.fail(e4, charged=None)
    before_rec = (led.committed(), led.possible_charges())
    led.resolve_unknown(e4, "reconciled", reported_usd=0.03)
    reconciled = (row(e4), led.committed(), led.possible_charges())
    check("rk6 the founder's reconciliation clarifies the result: the provider's figure replaces the reservation (the row completed at 0.03, reconciled), the possible charge is gone and the cap holds the figure instead of the 0.30 reserved",
          abs(before_rec[1] - 0.40) < 1e-9 and reconciled[0] == ("completed", 0.30, 0.03, "reconciled") and abs(reconciled[1] - (0.10 + 0.02 + 0.03)) < 1e-9 and abs(reconciled[2] - 0.10) < 1e-9, (before_rec, reconciled))
    e5 = led.claim_dispatch("R6:verify:w0:b0:P1.SRC-004:g1", "model", 0.20); led.mark_sent(e5); led.fail(e5, charged=None)
    led.resolve_unknown(e5, "skipped")
    check("rk6 the founder's skip: the row stays unknown with its possible charge counted, and the gateway answers the step without a send (ledger.skipped names it)",
          row(e5) == ("unknown", 0.20, None, "skipped") and led.skipped("R6:verify:w0:b0:P1.SRC-004:g1") == e5 and abs(led.possible_charges() - 0.30) < 1e-9, (row(e5), led.possible_charges()))
    c.close()


SCENARIOS = {"rk1": rk1, "rk2": rk2, "rk3": rk3, "rk4": rk4, "rk5": rk5, "rk6": rk6}

if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        SCENARIOS[name]()
    print(f"test_reuse_keys: {sum(1 for _, ok in results if ok)} of {len(results)} checks passed")
