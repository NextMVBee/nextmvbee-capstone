"""Worked example: one founder requirement followed to its evidence, its verdict, its cost calculation and the gate that
activates the decision.

Added for the public review copy; it is not a file of the release. It only imports the release's code (app/, schemas/)
and the release's own offline stand-ins (tests/pipeline_fake.py: a stub model server on 127.0.0.1, a simulated search
adapter and one synthetic vendor page). No key, no network, no paid call. Everything printed below is read from the
store the run wrote; the script changes no rule and fails (exit 1) if a printed fact does not hold.

Run from the repository root:  .venv/bin/python examples/worked_example.py
"""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))

import pipeline_fake as PF                                   # the release's stand-in providers and synthetic page
from app.adapters.model_gateway import load_settings
from app.core.sqlguard import inspect
from app.runner.runner import Runner

FAILED = []
def must(label, cond):
    print(("  ok    " if cond else "  FAIL  ") + label)
    if not cond: FAILED.append(label)
def rule(title): print("\n" + title + "\n" + "-" * len(title))
def rows(conn, sql, args=()):
    cur = conn.execute(sql, args); cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def run_project(name, script, usage=PF.USAGE_ANSWER):
    """One run of the whole workflow on the stand-ins: start, answer the one clarification question, finish."""
    stub = PF.ModelStub(script); store = PF.project(name, usage=usage)
    runner = Runner(store, settings=PF.S, gateway_factory=PF.gateway_factory(stub), research=PF.Sim(), instance_id="example", data_dir=PF.tmp / name / "data")
    first = runner.start("P1", PF.IDEA)
    question = first["interrupt"][0]["questions"][0]
    final = runner.resume(first["run_id"], {question["id"]: "eu-west"})
    return runner, stub, first, question, final

class InventedQuote(PF.Script):
    """The stub model of part 2: the same answers, plus one extracted claim whose quote is not on the page."""
    def __call__(self, body):
        item = super().__call__(body)
        if PF.schema_of(body) == "EvidenceExtractionOutV2" and item.get("content"):
            out = json.loads(item["content"])
            if out["claims"]:
                fake = dict(out["claims"][0], text="Speech API A v3: free_tier is 100 free hours every month", attribute="free_tier", value="100 free hours every month",
                            quote="Free tier: 100 free hours every month.", kind="price")
                out["claims"].append(fake); item = dict(item, content=json.dumps(out))
        return item

# ---------------------------------------------------------------------------------------------------------------------
print("NextMVBee worked example (synthetic input, stand-in providers, no key, no network)")
rule("Input")
print("Idea:   " + PF.IDEA)
print("Usage:  " + PF.USAGE_ANSWER + "  (the founder's answer to the usage question)")

runner, stub, first, question, final = run_project("example", PF.Script())
conn = inspect(runner.conn); run_id = first["run_id"]

rule("1. The founder's requirements, and the one question the run stopped for")
reqs = rows(conn, "SELECT id, key, value_json, hard, status FROM requirements ORDER BY id")
for r in reqs:
    print(f"  {r['key']:<20} = {json.loads(r['value_json'])!s:<8} {'must-have' if r['hard'] else 'preference':<10} ({r['status']})")
print(f"  The run paused with status '{first['status']}' and asked: \"{question['text']}\"  Answer given: eu-west")
budget = next(r for r in reqs if r["key"] == "budget_monthly_usd")
must("the monthly budget is recorded as a confirmed must-have", bool(budget["hard"]) and budget["status"] == "confirmed")
must("the run waited for the founder and sent nothing more until the answer", first["status"] == "awaiting_user")

rule("2. The evidence: a quote that is on the stored page, word for word")
claim = rows(conn, "SELECT c.*, s.url, s.text_path, s.content_hash FROM claims c JOIN sources s ON s.id = c.source_id WHERE c.attribute = 'price_per_hour'")[0]
page = (PF.tmp / "example" / "data" / claim["text_path"]).read_text()
start, end = claim["range_start"], claim["range_end"]
print(f"  Source page:  {claim['url']}  (stored copy: {len(page):,} characters, {claim['content_hash'][:19]}...)")
print(f"  Claim:        {claim['text']}")
print(f"  Quote:        \"{claim['quote']}\"")
print(f"  Position:     characters {start} to {end} of the stored page")
must("the stored page holds exactly the quote at that position", page[start:end] == claim["quote"])

rule("3. The verdict of the separate verification step")
print(f"  Claim status: {claim['status']}; scope: {claim['scope_status']}; verified against requirement version {claim['verified_requirement_version']}")
for s in rows(conn, "SELECT s.claim_id, r.key, s.verdict, s.status FROM claim_requirement_support s JOIN requirements r ON r.id = s.requirement_id ORDER BY s.claim_id"):
    print(f"  Support record: claim {s['claim_id']} for must-have '{s['key']}': verdict {s['verdict']}, {s['status']}")
must("the price claim was verified as supported and in scope", claim["status"] == "supported" and claim["scope_status"] == "in_scope")

rule("4. The cost calculation (arithmetic in code, no model involved)")
table = rows(conn, "SELECT * FROM derived_tables")[0]
fields = json.loads(table["fields_json"]); row = dict(zip(fields, json.loads(table["rows_json"])[0]))
print(f"  {row['usage_per_month']:g} {row['price_unit']}s a month  x  {row['unit_price_usd']:.2f} USD per {row['price_unit']}  =  {row['monthly_cost_usd']:.2f} USD a month")
print(f"  Unit price: sourced fact, {row['price_claim']}.  Usage: {row['usage_label']} from the founder's answer.  Budget: {row['budget_usd']:.2f} USD a month.")
check = rows(conn, "SELECT k.verdict, k.note, k.claim_ids_json FROM coverage_checks k JOIN requirements r ON r.id = k.requirement_id WHERE r.key = 'budget_monthly_usd'")[0]
print("  The gate's record for the budget must-have: verdict '" + check["verdict"] + "'")
print("    \"" + check["note"] + "\"")
must("40 meetings of 45 minutes are 30 hours; 30 x 0.60 = 18.00 USD a month", abs(row["usage_per_month"] - 30.0) < 1e-9 and abs(row["monthly_cost_usd"] - 18.0) < 1e-9)
must("the budget must-have is judged by this calculation and rests on the price claim", check["verdict"] == "entails" and claim["id"] in json.loads(check["claim_ids_json"]))

rule("5. The decision and the gate that made it active")
dec = rows(conn, "SELECT * FROM decisions")[0]
cited = [r["from_id"] for r in rows(conn, "SELECT from_id FROM trace_edges WHERE kind = 'claim_decision' AND to_id = ? ORDER BY from_id", (dec["id"],))]
print(f"  Decision:  {dec['subject']} -> {dec['choice']}  (status: {dec['status']}; proposed by the comparison step as: {dec['proposed_status']})")
print(f"  Reason:    {dec['rationale']}")
print(f"  Cites:     {', '.join(cited)}")
print(f"  Left open: {', '.join(json.loads(dec['unresolved_json']))}")
for k in rows(conn, "SELECT r.key, k.verdict FROM coverage_checks k JOIN requirements r ON r.id = k.requirement_id ORDER BY r.id"):
    print(f"  Must-have '{k['key']}' for this option: {k['verdict']}")
must("the decision is active and cites the verified price claim", dec["status"] == "active" and claim["id"] in cited)

rule("6. What the run spent, and the limits it ran under")
ledger = rows(conn, "SELECT kind, COUNT(*) AS n, ROUND(SUM(reserved_usd), 6) AS reserved, ROUND(SUM(reported_usd), 6) AS reported FROM usage_ledger WHERE run_id = ? GROUP BY kind ORDER BY kind", (run_id,))
for l in ledger:
    print(f"  {l['kind']:<9} {l['n']:>2} calls   reserved before sending {l['reserved']:.6f} USD   reported after {l['reported']:.6f} USD")
print("  (Stand-in prices. Each call's worst case is reserved before it is sent and settled to the reported cost after.)")
unsettled = rows(conn, "SELECT COUNT(*) AS n FROM usage_ledger WHERE run_id = ? AND state <> 'completed'", (run_id,))[0]["n"]
must("every call was reserved before sending and is settled", unsettled == 0 and all(l["reserved"] >= l["reported"] for l in ledger))
hosted = load_settings(str(ROOT / "config" / "settings.hosted.toml"))
print(f"  Limits of a hosted run (config/settings.hosted.toml): {hosted['budget']['cap_usd']:.2f} USD, {hosted['budget']['max_minutes']} active minutes, {hosted['budget']['max_model_calls']} model calls")

rule("7. The same run when the model invents a quote")
runner2, stub2, first2, _, final2 = run_project("invented", InventedQuote())
conn2 = inspect(runner2.conn)
rejected = sum(x.get("rejected", 0) for r in rows(conn2, "SELECT responses_json FROM extraction_batches") for x in json.loads(r["responses_json"] or "[]"))
stored = rows(conn2, "SELECT COUNT(*) AS n FROM claims WHERE attribute = 'free_tier'")[0]["n"]
print("  The stub model returned a fourth claim with the quote \"Free tier: 100 free hours every month.\", which is not on the page.")
print(f"  Claims stored with that attribute: {stored}.  Quotes rejected by the extraction step: {rejected}.")
must("a quote that is not on the stored page is refused and never becomes evidence", stored == 0 and rejected == 1)

rule("8. The same run when the usage makes the option cost more than the budget")
heavy = "400 meetings a month in all, 45 minutes each, 3 users"
runner3, stub3, first3, _, final3 = run_project("over", PF.Script(), usage=heavy)
conn3 = inspect(runner3.conn)
check3 = rows(conn3, "SELECT k.verdict, k.note FROM coverage_checks k JOIN requirements r ON r.id = k.requirement_id WHERE r.key = 'budget_monthly_usd'")
dec3 = rows(conn3, "SELECT choice, status, derivation_reason FROM decisions")
print("  Usage:  " + heavy)
for k in check3: print("  Budget must-have: verdict '" + k["verdict"] + "'\n    \"" + k["note"] + "\"")
for d in dec3: print(f"  Decision status: {d['status']}" + (f" ({d['derivation_reason']})" if d["derivation_reason"] else ""))
must("300 hours x 0.60 = 180.00 USD is over the 50 USD budget: the calculation contradicts the must-have", bool(check3) and check3[0]["verdict"] == "contradicts")
must("no decision for that option is active", not any(d["status"] == "active" for d in dec3))

rule("9. The same run when no verified quote supports the languages must-have")
silent = PF.Script(); silent.entail_none_for = ("languages",)        # the verifier finds no claim that supports the requirement
runner4, stub4, first4, _, final4 = run_project("unproven", silent)
conn4 = inspect(runner4.conn)
dec4 = rows(conn4, "SELECT choice, status, derivation_reason FROM decisions")[0]
support4 = [s["key"] for s in rows(conn4, "SELECT r.key FROM claim_requirement_support s JOIN requirements r ON r.id = s.requirement_id WHERE s.status = 'counts'")]
lang_id = rows(conn4, "SELECT id FROM requirements WHERE key = 'languages'")[0]["id"]
print(f"  Must-haves with a counting support record: {', '.join(support4) or 'none'}  (languages has none)")
print(f"  Decision: {dec4['choice']}, status {dec4['status']}; the gate's note: \"{dec4['derivation_reason']}\"  ({lang_id} is the languages must-have)")
must("the option is recommended as 'not yet proven' on the languages must-have, never presented as proven", "languages" not in support4 and "not yet proven" in (dec4["derivation_reason"] or "") and lang_id in dec4["derivation_reason"])

print("\n" + ("WORKED EXAMPLE FAILED: " + "; ".join(FAILED) if FAILED else f"Worked example complete: all checks ok. Final status of the first run: {final['status']}, plan version {final['plan_version']}."))
sys.exit(1 if FAILED else 0)
