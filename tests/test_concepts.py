"""Offline checks for the six course concepts: tool calling, reasoning loop, local retrieval and miss-rate scoring,
bounded exploration tree, roles and coordination, safety and stop, derived data. No internet, no keys.
Provider and research adapters are stubs: these checks prove the control logic, not live integration.
Run the file as a script: it prints one PASS line per check and the count of checks passed."""
import http.server, json, pathlib, sqlite3, sys, tempfile, threading
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from app.core.sqlguard import open_connection, inspect as insp, GuardedConnection
from app.repo import apply_schema
from schemas import models as M
from app.core.ledger import BudgetLedger
from app.core.tools import ToolRunner, ToolRejected, Paused, tools_for, step_key_for
from app.core import react
from app.core.retrieval import chunk, index_source, FTS5Retriever, rank_fusion, miss_rate, EmbeddingRetriever
from app.core.tree import explore, Limits, persist as persist_tree, validate_result
from app.core.roles import run_workers, merge_proposals, ROLES, persist_results
from app.core.safety import HAZARDS, interventions, StopController
from app.core.derived import cost_comparison
from app.adapters.model_gateway import CloudGateway
from app.runner.recovery import start_run

results = []
def check(name, cond, detail=None):
    results.append((name, bool(cond))); assert cond, (name if detail is None else f"{name} [{detail}]")
import background_threads; background_threads.install()                              # an uncaught exception in a background thread is a FAIL line and a nonzero exit
tmp = pathlib.Path(tempfile.mkdtemp())
def fresh_db(name):
    c = open_connection(tmp / name, check_same_thread=False, timeout=5); apply_schema(c); c.execute("PRAGMA foreign_keys=ON")
    c.execute("INSERT INTO projects (id, owner_id, title) VALUES ('P1','U1','Meeting assistant')"); c.commit(); return c

class FakeResearch:
    def __init__(self): self.calls = []
    def search(self, query, freshness, n): self.calls.append(("search", query)); return [{"url": "https://docs.speech-a.example/languages", "title": "Languages", "text": "Supported languages include Arabic (ar) and Hebrew (he)."}]
    def contents(self, urls, freshness): self.calls.append(("contents", urls[0])); return [{"url": urls[0], "text": "Speaker labels are available for English and Spanish."}]

# ---------- tool calling ----------
db = fresh_db("tools.db"); start_run(db, "R1", "P1", "T1", "W1", cap_usd=2.0); L = BudgetLedger(db, "R1", 2.0)
fr = FakeResearch(); tr = ToolRunner(db, L, "R1", "P1", "research_plan", fr)
obs = tr.handle({"tool": "search", "args": {"query": "speech-to-text API Arabic Hebrew", "freshness": "volatile", "num_results": 8}})
check("T24 valid execution: search runs, observation stored, cost reserved from the request with the 15% margin; no provider figure, so cost_uncertain stays set", obs["ok"] and obs["tool_call_id"] == step_key_for("R1", "research_plan") + ":tool:1"
      and db.execute("SELECT state FROM tool_calls WHERE id=?", (obs["tool_call_id"],)).fetchone()[0] == "completed"
      and db.execute("SELECT state, reserved_usd, reported_usd, cost_uncertain FROM usage_ledger WHERE step_key=? AND kind='search'", (tr.step_key,)).fetchone() == ("completed", 0.00805, None, 1))
try:
    tr.handle({"tool": "search", "args": {"query": "x", "num_results": 50}}); ok = False
except ToolRejected as e:
    ok = "invalid arguments" in str(e)
check("T24 invalid arguments rejected and recorded on their own row", ok and db.execute("SELECT state, error FROM tool_calls WHERE n=2 AND step_key=?", (tr.step_key,)).fetchone()[0] == "rejected" and db.execute("SELECT state FROM tool_calls WHERE n=1 AND step_key=?", (tr.step_key,)).fetchone()[0] == "completed")
vr = ToolRunner(db, L, "R1", "P1", "verify", fr)
try:
    vr.handle({"tool": "search", "args": {"query": "anything useful"}}); ok = False
except ToolRejected as e:
    ok = "not allowed" in str(e)
check("T24 prohibited action: search is not allowed in verify (allowlist per stage)", ok and tools_for("verify")[0]["function"]["name"] == "re_fetch" and tools_for("extract_evidence") == [])
small = ToolRunner(db, L, "R1", "P1", "research_plan", fr, worker="w2", stage_limit=2)
small.handle({"tool": "search", "args": {"query": "first query here"}}); small.handle({"tool": "search", "args": {"query": "second query here"}})
try:
    small.handle({"tool": "search", "args": {"query": "third query here"}}); ok = False
except ToolRejected as e:
    ok = "stage limit" in str(e)
check("T24 budget exhaustion: the per-stage tool limit ends the loop", ok)
tight = ToolRunner(db, L, "R1", "P1", "research_plan", fr, worker="w3", run_limits={"searches": 3})
try:
    tight.handle({"tool": "search", "args": {"query": "fourth query here"}}); ok = False
except ToolRejected as e:
    ok = "run search limit" in str(e)
check("T24 global run limits are shared across workers and stages", ok)
n_before = len(fr.calls)
resumed = ToolRunner(db, L, "R1", "P1", "research_plan", fr)          # the stage restarts after a resume: same durable ids
again = resumed.handle({"tool": "search", "args": {"query": "speech-to-text API Arabic Hebrew", "freshness": "volatile", "num_results": 8}})
check("T24 result association and replay: the same durable id returns the stored observation without executing", again["summary"] == "replayed stored observation" and len(fr.calls) == n_before)
try:
    tr.handle({"tool": "ask_user", "args": {"text": "Can audio leave the customer's network?", "why_blocking": "decides transcription", "requirement_key": "privacy.audio"}}); ok = False
except Paused as p:
    ok = db.execute("SELECT status FROM questions WHERE id=?", (p.question_id,)).fetchone()[0] == "open" and db.execute("SELECT state FROM tool_calls WHERE id=?", (p.tool_call_id,)).fetchone()[0] == "paused"
check("T24 ask_user persists the question and pauses", ok)

# continuation after a tool observation through the gateway loop
class Stub:
    def __init__(self):
        self.queue, self.bodies = [], []
        outer = self
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); outer.bodies.append(body)
                item = outer.queue.pop(0) if outer.queue else {"content": "{}"}
                msg = {"role": "assistant", "content": item.get("content")}
                if "tool" in item:
                    msg["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": item["tool"], "arguments": json.dumps(item["args"])}}]
                payload = {"id": f"gen-{len(outer.bodies)}", "object": "chat.completion", "created": 0, "model": body["model"], "choices": [{"index": 0, "finish_reason": "tool_calls" if "tool" in item else "stop", "message": msg}],
                           "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.01}}
                raw = json.dumps(payload).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start(); self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
A_ = Stub(); PR = {"anthropic/claude-sonnet-5.5": (2.0, 10.0), "google/gemini-3.8-flash": (0.75, 3.75)}
gdb = fresh_db("loop.db"); start_run(gdb, "R2", "P1", "T2", "W1"); GL = BudgetLedger(gdb, "R2", 2.0); runner = ToolRunner(gdb, GL, "R2", "P1", "research_plan", FakeResearch())
gw = CloudGateway(api_key="k", default_model="anthropic/claude-sonnet-5.5", fallback_model="google/gemini-3.8-flash", prices=PR, privacy_mode="cloud", ledger=GL, base_url=A_.url)
PLAN = json.dumps({"decision_subjects": ["transcription"], "queries": [{"text": "speech api hebrew pricing", "decision_subject": "transcription", "target_fact": "price", "freshness": "volatile"}]})
A_.queue = [{"tool": "search", "args": {"query": "speech-to-text Hebrew API", "freshness": "volatile"}}, {"tool": "read", "args": {"url": "https://docs.speech-a.example/pricing"}}, {"content": PLAN}]
r = gw.call_with_tools(runner.step_key, M.ResearchPlanOut, [{"role": "system", "content": "plan research"}, {"role": "user", "content": "transcription"}], tools_for("research_plan"), runner, 300, 800)
check("T24 continuation: two tool rounds then a valid contract answer", r["status"] == "ok" and r["tool_calls"] == 2 and r["rounds"] == 3 and A_.bodies[-1]["messages"][-1]["role"] == "tool")
check("T24 every round is an accounted attempt and tool requests are sent with the allowlist", gdb.execute("SELECT COUNT(*) FROM usage_ledger WHERE step_key=? AND kind='model'", (runner.step_key,)).fetchone()[0] == 3
      and {t["function"]["name"] for t in A_.bodies[0]["tools"]} == {"search", "read", "ask_user"})
A_.queue = [{"tool": "re_fetch", "args": {"url": "https://x.example"}}, {"content": PLAN}]
r2 = gw.call_with_tools(runner.step_key + ":x", M.ResearchPlanOut, [{"role": "system", "content": "plan"}], tools_for("research_plan"), runner, 300, 800)
check("T24 a prohibited tool request is returned to the model as an error and the loop continues", r2["status"] == "ok" and '"ok": false' in A_.bodies[-1]["messages"][-1]["content"])

# ---------- ReAct: decision summaries and recovery policy ----------
tc = react.trace_complete(gdb, "R2")
check("ReAct: a nonempty decision summary exists for every model send and every tool call", tc["complete"] and tc["required"] >= 5)
check("ReAct: inadequate evidence leads to another source, then a question, then a partial result",
      react.next_action({"sufficient": False}, {"searches_left": 3}) == "search_more" and react.next_action({"sufficient": False}, {"searches_left": 0, "can_ask": True}) == "ask_user"
      and react.next_action({"sufficient": False}, {"searches_left": 0, "can_ask": False}) == "partial_result")
check("ReAct: malformed output repairs once then flags; provider failure falls back once; budget stops",
      react.next_action({"malformed": True}, {"repairs_used": 0}) == "repair" and react.next_action({"malformed": True}, {"repairs_used": 1}) == "flag"
      and react.next_action({"provider_failed": True}, {}) == "fallback" and react.next_action({"budget_exhausted": True}, {}) == "stop_partial" and len(react.RECOVERY) == 6)

# ---------- RAG: passages, FTS5 retrieval, fusion, miss-rate scoring ----------
rdb = fresh_db("rag.db")
rdb.execute("INSERT INTO sources (id, project_id, url, canonical_url, tier, retrieved_at, content_hash, freshness) VALUES ('P1.SRC-001','P1','https://a.example','https://a.example',1,'2026-10-20','sha256:' || hex(randomblob(32)),'technical')")
text = ("Supported languages include Arabic (ar) and Hebrew (he). " * 20) + ("Pricing is $0.36 per audio hour for the standard tier. " * 20) + ("Speaker labels are available for English and Spanish only. " * 20)
n = index_source(rdb, "P1", "P1.SRC-001", text)
check("RAG: deterministic chunking and indexing", n == len(chunk(text)) and chunk("a" * 1000) == chunk("a" * 1000) and n >= 3)
ret = FTS5Retriever(rdb); top = ret.retrieve("P1", "price per audio hour", k=3)
check("RAG: FTS5 retrieval returns the passage that holds the price first", top and "per audio hour" in rdb.execute("SELECT text FROM passages WHERE id=?", (top[0][0],)).fetchone()[0])
f1 = rank_fusion([[("p2", 1.0), ("p1", 0.9)], [("p1", 5.0), ("p3", 4.0)]]); f2 = rank_fusion([[("p1", 5.0), ("p3", 4.0)], [("p2", 1.0), ("p1", 0.9)]])
check("RAG: rank fusion is deterministic and breaks ties by id", f1 == f2 and f1[0][0] == "p1" and [x[0] for x in rank_fusion([[("b", 1.0)], [("a", 1.0)]])] == ["a", "b"])
score = miss_rate({"q1": ["p1", "p2"], "q2": ["p9"]}, {"q1": {"p1", "p3"}, "q2": {"p9"}})
check("RAG: E2 miss rate uses relevant passages as the denominator and applies the 10% threshold", score == {"relevant": 3, "missed": 1, "miss_rate": 1/3, "threshold": 0.10, "embeddings_justified": True})
try:
    EmbeddingRetriever(); ok = False
except NotImplementedError:
    ok = True
check("RAG: embeddings stay disabled until E2 justifies them", ok)

# ---------- bounded exploration tree ----------
calls = {"n": 0}
def alts(subject): calls["n"] += 1; return [{"label": "Speech API A", "claim_ids": ["P1.CLM-014"]}, {"label": "On-device model C", "claim_ids": []}, {"label": "Speech API B", "claim_ids": ["P1.CLM-040"]}, {"label": "fourth, ignored"}]
def variants(label): calls["n"] += 1; return [{"label": f"{label} / batch"}, {"label": f"{label} / streaming"}, {"label": f"{label} / hybrid"}, {"label": "extra, ignored"}]
def hard(node): return "fail" if "On-device" in node.label else ("unresolved" if "Speech API B" in node.label else "pass")
def evaluate(nodes): calls["n"] += 1; return {x.id: {"fit": 8 if "A" in x.id else 6, "evidence": 7, "cost": 5, "risk": 6, "claim_ids": ["P1.CLM-014"]} for x in nodes}
res = explore("transcription", alts, variants, evaluate, hard)
nodes = res["nodes"]; alive = [x for x in nodes if not x.pruned]
check("T25 bounds: at most 3 alternatives and 3 variants each, no depth beyond 2", sum(1 for x in nodes if x.depth == 1) == 3 and max(x.depth for x in nodes) == 2 and all(sum(1 for c in nodes if c.parent_id == a.id) <= 3 for a in nodes))
pruned = [x for x in nodes if x.pruned]
check("T25 pruning: a confirmed hard-constraint failure is pruned with its reason and kept in the trace", pruned and pruned[0].label == "On-device model C" and pruned[0].reason == "confirmed hard-constraint failure" and not any(c.parent_id == pruned[0].id for c in nodes))
check("T25 unresolved constraints stay explicitly unresolved, never rejected", any(x.hard_status == "unresolved" and not x.pruned for x in nodes))
check("T25 call accounting: expansion, variants and one batched evaluation within 7 calls", res["calls_used"] == 1 + 2 + 1 and calls["n"] == res["calls_used"] and res["selected_id"] is not None and not res["partial"])
calls["n"] = 0; part = explore("transcription", alts, variants, evaluate, hard, limits=Limits(max_model_calls=3), retries=1)
check("T25 partial result when the call limit is reached, retries counted", part["partial"] and "limit" in part["reason"] and part["calls_used"] <= 3)
tdb = fresh_db("tree.db"); start_run(tdb, "R3", "P1", "T3", "W1"); persist_tree(tdb, "R3", res)
check("T25 trace persisted with scores, claims, pruned flags and the selection", tdb.execute("SELECT COUNT(*), SUM(pruned), SUM(selected) FROM tree_nodes WHERE run_id='R3'").fetchone() == (len(nodes), 1, 1))
def hard_b(node): return "fail" if "P1.CLM-099" in node.claim_ids else "pass"
def evaluate_b(nodes): return {x.id: {"fit": 10 if "batch" in x.label else 2, "evidence": 10 if "batch" in x.label else 2, "cost": 10 if "batch" in x.label else 2, "risk": 10 if "batch" in x.label else 2, "claim_ids": ["P1.CLM-099"] if "batch" in x.label else []} for x in nodes}
res_b = explore("transcription", alts, variants, evaluate_b, hard_b); nodes_b = res_b["nodes"]; batch_b = [x for x in nodes_b if "batch" in x.label]; sel_b = next(x for x in nodes_b if x.selected)
check("B-01 (the 8c audit): evidence the evaluation adds is rechecked by code before the selection: a branch whose added claim fails a hard constraint is pruned with its reason and keeps its score in the trace; the best-scored but failing branches never win; the selection falls to a branch the final check passes",
      batch_b and all(x.pruned and x.hard_status == "fail" and x.reason == "confirmed hard-constraint failure (evidence added at evaluation)" and x.score == 9.0 and "P1.CLM-099" in x.claim_ids for x in batch_b)   # the four terms at 10 score 9.0; the currency term (0 without a callable) holds the last 0.10
      and "batch" not in sel_b.label and sel_b.hard_status == "pass" and not res_b["selected_unresolved"] and validate_result(res_b) == [], [(x.label, x.pruned, x.hard_status, x.score) for x in nodes_b])

# ---------- roles and coordination ----------
check("Roles: six roles with objective, contracts, tools, memory, completion and failure", set(ROLES) == {"Clarifier", "Researcher", "Verifier", "Comparer", "Planner", "Reviewer"} and all({"objective", "inputs", "outputs", "tools", "memory", "done", "failure"} <= set(v) for v in ROLES.values()))
def worker(wid, task):
    if task["task_id"] == "t3":
        raise RuntimeError("provider timeout")
    base = {"worker": wid, "task_id": task["task_id"], "kind": "capability", "subject": "Speech API A", "attribute": "language_support", "claim_text": "supports Hebrew", "source_url": "https://docs.speech-a.example/languages", "quote": "Supported languages include Arabic (ar) and Hebrew (he)."}
    if task["task_id"] == "t1":
        return [dict(base, value="ar, he"), dict(base, source_url="https://blog.example/post", quote="Hebrew is not supported.", value="en, es", claim_text="no Hebrew")]
    return [dict(base, value="ar, he", writes=["P1.REQ-001"]), dict(base, value="ar, he", worker=wid, quote="Supported languages include Arabic (ar) and Hebrew (he). ")]
tasks = [{"task_id": "t1"}, {"task_id": "t2"}, {"task_id": "t3"}]
wr = run_workers(tasks, worker, max_workers=3)
check("T26 up to three parallel workers; a failing worker does not stop the others", len(wr) == 3 and sorted(r.status for r in wr) == ["completed", "completed", "failed"] and {r.worker for r in wr} == {"w1-t1", "w2-t2", "w3-t3"})
m1 = merge_proposals({"P1.REQ-001"}, wr); m2 = merge_proposals({"P1.REQ-001"}, list(reversed(wr)))
check("T26 deduplication by source and quote, provenance kept", m1["duplicates"] == 1 and any(len(a["provenance"]) == 2 for a in m1["accepted"]))
check("T26 contradictory evidence is exposed, not resolved silently", len(m1["conflicts"]) == 1 and set(m1["conflicts"][0]["values"]) == {"ar, he", "en, es"})
check("T26 different completion orders give the same merge", [a["quote"] for a in m1["accepted"]] == [a["quote"] for a in m2["accepted"]] and m1["conflicts"] == m2["conflicts"])
check("T26 workers cannot overwrite confirmed requirements; partial failure is recorded and the run continues", len(m1["rejected"]) == 1 and m1["failed_workers"][0][1] == "t3" and len(m1["accepted"]) == 2)
persist_results(tdb, "R3", wr); check("T26 worker results persisted", tdb.execute("SELECT COUNT(*) FROM worker_results WHERE run_id='R3'").fetchone()[0] == 3)

# ---------- safety and human intervention ----------
check("T27 hazard register covers the eight hazards with guardrail, signal, test and intervention", len(HAZARDS) == 8 and all({"hazard", "guardrail", "signal", "test", "intervention"} <= set(h) for h in HAZARDS))
iv = interventions({"unsupported_hard_requirements": 1, "unresolved_contradictions": 0, "ambiguous_scope": True, "model_confidence": 0.2})
check("T27 interventions fire on observable signals only, never on self-reported confidence", [x["signal"] for x in iv] == ["unsupported_hard_requirements", "ambiguous_scope"])
sdb = fresh_db("stop.db"); start_run(sdb, "R4", "P1", "T4", "W1"); SL = BudgetLedger(sdb, "R4", 2.0)
e1 = SL.reserve("R4:s1:w0:b0", "model", 0.05); SL.mark_sent(e1); SL.settle(e1, 0.04, result_json="{}")
e2 = SL.reserve("R4:s2:w0:b0", "model", 0.06); SL.mark_sent(e2)
sc = StopController(sdb, "R4", SL); check("T27 work may start while running", sc.may_start_work())
rep = sc.stop()
check("T27 stop: no new work, completed results kept, in-flight call marked unknown with its possible charge", not sc.may_start_work() and rep["completed_calls_kept"] == 1 and rep["in_flight_marked_unknown"] == [e2] and abs(rep["possible_usd"] - 0.06) < 1e-9
      and sdb.execute("SELECT status, stop_reason FROM runs WHERE id='R4'").fetchone() == ("cancelled", "founder_stop"))

# ---------- derived data ----------
rows = cost_comparison([{"name": "Speech API A", "unit": "audio hour", "unit_price_usd": 0.36, "claim_id": "P1.CLM-021"}, {"name": "Speech API B", "unit": "audio hour", "unit_price_usd": 0.24, "claim_id": "P1.CLM-040", "measured_usd": None}], {"units_per_month": 200, "label": "assumption: 200 audio hours per month"})
check("Derived data: sourced prices, an explicit assumption and calculated estimates are labelled distinctly", rows[0]["option"] == "Speech API B" and rows[0]["monthly_cost_usd"] == 48.0 and rows[0]["unit_price_label"] == "sourced fact" and rows[0]["usage_label"] == "assumption" and rows[0]["monthly_label"] == "calculated estimate" and rows[0]["measured_label"] == "not measured")

# ---------- the bounded tree and the Researcher workers in the workflow (the pipeline fake: the stub model, the simulated research; no network) ----------
import copy as _copy
import pipeline_fake as PF
from app.core.sqlguard import inspect as insp
from app.runner import events as ev

def _tree_rows(c, rid):
    return [dict(zip(("id", "depth", "label", "hard_status", "score", "pruned", "reason", "selected", "parent_id"), r)) for r in c.execute("SELECT id, depth, label, hard_status, score, pruned, reason, selected, parent_id FROM tree_nodes WHERE run_id=? ORDER BY id", (rid,))]
def _tree_keys(c, rid):
    return [r[0].split(":", 4)[4] for r in c.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? ORDER BY planned_at, attempt", (rid, f"{rid}:tree:%"))]
def _decisions(c):
    return [dict(zip(("subject", "choice", "status", "rationale", "rejected_json", "unresolved_json"), r)) for r in insp(c).execute("SELECT subject, choice, status, rationale, rejected_json, unresolved_json FROM decisions ORDER BY id")]

# (a) the tree: two candidate identities for the high-importance decision
scT = PF.Script(questions=False); scT.second_product = True
rT, stubT = PF.runner_for("tree_run", scT); resT = rT.start("P1", PF.IDEA); cT = rT.conn; ridT = resT["run_id"]
nodesT = _tree_rows(cT, ridT); decT = _decisions(cT); branchT = [d for _, e, d in ev.events(cT, ridT) if e == "branch"]; schemasT = [PF.schema_of(b) for b in stubT.bodies]
prunedT = [n for n in nodesT if n["pruned"]]; selT = [n for n in nodesT if n["selected"]]
check("T25 in the workflow: a high-importance decision (hard requirements apply) with two candidate identities takes the bounded tree: three model calls under <run>:tree:w0:b0:d1:<prompt>.v1:a-x:<part> (D-A192: the part's prompt version and the model identity) (the expansion, the variants of the one live alternative, one batched evaluation) and one comparison send for the reader memo (Phase 9b, C3); the dearer version's branch is pruned by code as a confirmed hard-constraint failure (its price above the hard budget under the usage answer), kept in the trace with its reason and expanded no further; the selected leaf is a variant of the other alternative; the decision is active with the canonical candidate label, its rationale is the memo's reader sentence and the provenance names the tree, the pruned alternative is among its rejected options with the reason; one branch event carries the five nodes",
      resT["status"] == "completed" and schemasT.count("AlternativesOut") == 1 and schemasT.count("VariantsOut") == 1 and schemasT.count("BranchEvaluationOut") == 1 and schemasT.count("ComparisonOutV2") == 1   # the one comparison send writes the tree's memo in reader words
      and _tree_keys(cT, ridT) == ["d1:tree_alternatives.v2:a-x:alternatives", "d1:tree_variants.v2:a-x:variants:A1", "d1:tree_evaluate.v2:a-x:evaluate"] and len(nodesT) == 5 and sum(1 for n in nodesT if n["depth"] == 1) == 2 and len(prunedT) == 1 and prunedT[0]["hard_status"] == "fail"
      and prunedT[0]["reason"].startswith("confirmed hard-constraint failure: budget monthly usd (") and not any(n["parent_id"] == prunedT[0]["id"] for n in nodesT)   # the arithmetic names the requirement and the claim
      and len(selT) == 1 and selT[0]["depth"] == 2 and "streaming" in selT[0]["label"] and all(n["score"] is not None for n in nodesT if not n["pruned"])
      and decT[0]["status"] == "active" and decT[0]["choice"] == "Orion Compute 1" and not decT[0]["rationale"].startswith("exploration tree") and "exploration tree" in " ".join(str(r0[0]) for r0 in cT.execute("SELECT observation FROM reasoning_records WHERE run_id=? AND action='the tree''s memo'", (ridT,)).fetchall())   # the reader memo's sentence is the rationale; the tree's sentence lives in the provenance
      and len(branchT) == 1 and len(branchT[0]["nodes"]) == 5 and branchT[0]["selected"] == selT[0]["id"].rsplit(":", 1)[-1] and not branchT[0]["partial"] and branchT[0]["calls"] == 3)

# (b) the partial result: the ledger refuses the third tree call of the decision ([tree].max_model_calls 2), the tree ends partial, the run goes on
S2 = _copy.deepcopy(PF.S); S2["tree"]["max_model_calls"] = 2
scP = PF.Script(questions=False); scP.second_product = True
rP, stubP = PF.runner_for("tree_partial", scP, settings=S2); resP = rP.start("P1", PF.IDEA); cP = rP.conn; ridP = resP["run_id"]
nodesP = _tree_rows(cP, ridP); decP = _decisions(cP); branchP = [d for _, e, d in ev.events(cP, ridP) if e == "branch"]
stepsP = [d for _, e, d in ev.events(cP, ridP) if e == "step" and d.get("label") == "Tree selected no branch"]
check("T25 partial result in the workflow: with the dispatch limit at two tree calls per decision the ledger refuses the batched evaluation; the tree ends partial with the limit as its reason (the refusal never stops the run), the nodes that exist are persisted without a selection, the branch event says partial; the tree's outcome is recorded as a step and the comparison decides on the packs as in the simple configuration (D-A227: one ComparisonOutV2 send under the subject's simple key, the decision active), and the run completes to its plan",
      resP["status"] == "completed" and _tree_keys(cP, ridP) == ["d1:tree_alternatives.v2:a-x:alternatives", "d1:tree_variants.v2:a-x:variants:A1"] and len(nodesP) == 5 and not any(n["selected"] for n in nodesP) and all(n["score"] is None for n in nodesP)
      and len(stepsP) == 1 and "exploration tree: no branch could be selected" in stepsP[0]["detail"] and [PF.schema_of(b) for b in stubP.bodies].count("ComparisonOutV2") == 1 and cP.execute("SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ?", (ridP, f"{ridP}:compare_options:%:memo")).fetchone()[0] == 0
      and decP[0]["status"] == "active" and decP[0]["choice"] == "Orion Compute 1" and not decP[0]["rationale"].startswith("comparison flagged") and len(branchP) == 1 and branchP[0]["partial"] and "limit" in str(branchP[0]["reason"]) and branchP[0]["selected"] is None
      and cP.execute("SELECT status, stop_reason FROM runs WHERE id=?", (ridP,)).fetchone()[:] == ("completed", "complete"), (resP["status"], _tree_keys(cP, ridP), decP[:1], branchP, stepsP))

# (c) the negative: with no tree decision allowed per run ([tree].max_tree_decisions_per_run 0) the same decision takes the simple comparison
S0 = _copy.deepcopy(PF.S); S0["tree"]["max_tree_decisions_per_run"] = 0
scN = PF.Script(questions=False); scN.second_product = True
rN, stubN = PF.runner_for("tree_none", scN, settings=S0); resN = rN.start("P1", PF.IDEA); cN = rN.conn
check("T25 negative: the same two-candidate decision with no tree decision allowed takes the simple comparison (one ComparisonOut, no tree send, no tree node, no branch event) and is active",
      resN["status"] == "completed" and [PF.schema_of(b) for b in stubN.bodies].count("ComparisonOutV2") == 1 and not any(PF.schema_of(b) in ("AlternativesOut", "VariantsOut", "BranchEvaluationOut") for b in stubN.bodies)
      and cN.execute("SELECT COUNT(*) FROM tree_nodes WHERE run_id=?", (resN["run_id"],)).fetchone()[0] == 0 and not [d for _, e, d in ev.events(cN, resN["run_id"]) if e == "branch"] and _decisions(cN)[0]["status"] == "active")

# (d) the Researcher workers: three independent decision subjects, each with its own page, the sources then independent in extraction
QW = [{"text": "gpt mini token pricing", "decision_subject": "llm_summarization_api", "target_fact": "summarisation token price", "freshness": "volatile"}, {"text": "render web service pricing plans", "decision_subject": "backend_hosting_and_storage", "target_fact": "hosting price", "freshness": "volatile"}]
S3 = _copy.deepcopy(PF.S); S3["extraction"]["k_per_subject"] = 1
scW = PF.Script(questions=False, subjects=("transcription", "llm_summarization_api", "backend_hosting_and_storage"), extra_queries=QW)
rW, stubW = PF.runner_for("workers_run", scW, settings=S3, research=PF.Sim(per_query=True, distinct=True)); resW = rW.start("P1", PF.IDEA); cW = rW.conn; ridW = resW["run_id"]
wrows = [tuple(r) for r in cW.execute("SELECT worker, task_id, status, proposals_json, error FROM worker_results WHERE run_id=? ORDER BY task_id", (ridW,))]
markW = json.loads(cW.execute("SELECT output_json FROM run_steps WHERE step_key=? AND status='completed'", (f"{ridW}:research_plan:w0:b0",)).fetchone()[0]); wk = markW.get("workers") or {}
eventsW = ev.events(cW, ridW); worker_events = [d for _, e, d in eventsW if e == "worker"]; stepsW = [d for _, e, d in eventsW if e == "step"]
wkeys = sorted({r[0] for r in cW.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ?", (ridW, f"{ridW}:research_plan:w%-t%"))})
tool_workers = {r[0] for r in cW.execute("SELECT DISTINCT worker FROM tool_calls WHERE run_id=? AND stage='research_plan'", (ridW,))}
par = [s0 for s0 in stepsW if s0.get("label") == "Parallel sources"]
check("T26 in the workflow: three independent decision subjects: three Researcher workers (w1-t1, w2-t2, w3-t3) through run_workers, each on its own handle, each pursuing its subject's questions (its searches under its own worker id) and asking the Researcher role once (a ResearcherOut send under its own key); the coordinator merges the proposals (the transcription worker's two accepted, none duplicated, no conflict, no refused write), persists three completed worker_results rows, announces one worker event per worker and the merge step; the run completes with the transcription decision active",
      resW["status"] == "completed" and [w[1] for w in wrows] == ["t1", "t2", "t3"] and all(w[2] == "completed" for w in wrows) and {w[0] for w in wrows} == {"w1-t1", "w2-t2", "w3-t3"} and wk.get("workers") == 3 and wk.get("completed") == 3 and wk.get("proposals") == 2 and wk.get("accepted") == 2 and wk.get("duplicates") == 0 and wk.get("conflicts") == [] and wk.get("rejected") == 0
      and len(wkeys) == 3 and {k.split(":")[2] for k in wkeys} == {"w1-t1", "w2-t2", "w3-t3"} and {"w1-t1", "w2-t2", "w3-t3"} <= tool_workers and len([w for w in worker_events if not str(w.get("task", "")).startswith("source ")]) == 3
      and any(s0.get("label") == "Research workers" for s0 in stepsW) and [PF.schema_of(b) for b in stubW.bodies].count("ResearcherOut") == 3 and _decisions(cW)[0]["status"] == "active",
      (resW["status"], wrows[:3], wk, wkeys, sorted(tool_workers), len(worker_events)))
check("T26 in the workflow: the sources of different subjects are independent tasks: extraction and verification ran a chunk of at least two sources at once (the Parallel sources step with its wall time and one worker event per source), every source concluded (its extract_source mark), and the claims of every source are verified or carry a reason",
      len(par) >= 1 and max(int(s0["detail"].split(" independent")[0]) for s0 in par) >= 2 and len([w for w in worker_events if str(w.get("task", "")).startswith("source ")]) >= 2
      and cW.execute("SELECT COUNT(*) FROM run_steps WHERE run_id=? AND node='extract_source' AND status='completed'", (ridW,)).fetchone()[0] >= max(2, cW.execute("SELECT COUNT(DISTINCT source_id) FROM claims WHERE project_id='P1' AND source_id IS NOT NULL", ()).fetchone()[0])
      and cW.execute("SELECT COUNT(*) FROM claims WHERE project_id='P1' AND verdict IS NULL AND source_version=(SELECT content_hash FROM sources WHERE id=claims.source_id)", ()).fetchone()[0] == 0,
      (par, [w for w in worker_events if str(w.get("task", "")).startswith("source ")][:3], cW.execute("SELECT COUNT(*) FROM run_steps WHERE run_id=? AND node='extract_source' AND status='completed'", (ridW,)).fetchone()[0], cW.execute("SELECT COUNT(DISTINCT source_id) FROM claims WHERE project_id='P1'", ()).fetchone()[0], cW.execute("SELECT COUNT(*) FROM claims WHERE project_id='P1' AND verdict IS NULL", ()).fetchone()[0]))

# (e) a failing worker never stops the others nor the run: every Researcher answers the failure shape (one repair each, then flagged)
scF = PF.Script(questions=False, subjects=("transcription", "llm_summarization_api", "backend_hosting_and_storage"), extra_queries=QW); scF.worker_proposals = False
rF, stubF = PF.runner_for("workers_fail", scF, settings=S3, research=PF.Sim(per_query=True, distinct=True)); resF = rF.start("P1", PF.IDEA); cF = rF.conn; ridF = resF["run_id"]
frows = [tuple(r) for r in cF.execute("SELECT worker, task_id, status, error FROM worker_results WHERE run_id=? ORDER BY task_id", (ridF,))]
markF = json.loads(cF.execute("SELECT output_json FROM run_steps WHERE step_key=? AND status='completed'", (f"{ridF}:research_plan:w0:b0",)).fetchone()[0]); wkF = markF.get("workers") or {}
check("T26 negative in the workflow: Researcher workers that cannot comply (the failure shape, once repaired, then flagged) are recorded failed with the reason, the other workers and the run go on (the pursuit of every question done, the extraction and the plan made), the merge step names the failures",
      resF["status"] == "completed" and len(frows) == 3 and all(w[2] == "failed" and str(w[3]).startswith("researcher flagged") for w in frows) and wkF.get("completed") == 0 and len(wkF.get("failed") or []) == 3 and wkF.get("proposals") == 0
      and [PF.schema_of(b) for b in stubF.bodies].count("ResearcherOut") == 6 and _decisions(cF)[0]["status"] == "active", (resF["status"], frows, wkF))

# one product under two spellings is one identity; a tier the option's label names is the option's version
from app.core import coverage as _CV
check("D-A61 identity: two spellings of one product and version are one (dicta-dictalm 3.0 and dicta-dicta-lm 3.0; dictalm-3-0), a claim about the tier the option's label names supports an option without a version (KolWrite, Base model), another tier never does, and a version named on one side only never matches",
      _CV.same_product("dicta-dictalm", "3.0", "dicta-dicta-lm", "3.0") and _CV.canonical_identity("dicta-dictalm-3-0", "") == _CV.canonical_identity("dicta-dicta-lm", "3.0")
      and _CV.same_product("kolwrite-kolwrite", "", "kolwrite-kolwrite", "base", "KolWrite API (Base model, pay per audio hour)") and _CV.same_product("kolwrite-kolwrite", "", "kolwrite-kolwrite-base", "", "KolWrite API (Base model)")
      and not _CV.same_product("kolwrite-kolwrite", "", "kolwrite-kolwrite-oracle", "", "KolWrite API (Base model)") and not _CV.same_product("x", "2", "x", "") and not _CV.same_product("x", "", "x", "2", "X plan") and _CV.same_product("x", "", "x", "2", "X version 2")
      and not _CV.same_product("speech-api-a", "v3", "speech-api-b", "v3") and _CV.same_product("speech-api-a", "v3", "Speech API A", "3"))

from types import SimpleNamespace as _NS
from app.graph import workflow as _WF
_o1 = _NS(option="KolWrite (API)", product_id="kolwrite-kolwrite", product_version="", verdict="recommended"); _o2 = _NS(option="Amazon Transcribe (API, batch)", product_id="amazon-web-services-amazon-transcribe", product_version="", verdict="runner-up")
_out = _NS(recommendation=_NS(option="KolWrite (API, Base model, uploaded recordings)", product_id="kolwrite-kolwrite", product_version=""), options=[_o1, _o2])
check("p6-R15: the recommendation is found among the memo's options by its product identity (or its words) when its text differs; a cited claim about the runner-up it is compared with is about an option of the memo; a claim about neither is not; a plan the option names as a version matches a claim whose words name it",
      _WF._pick_option(_out) is _o1 and _WF._pick_option(_NS(recommendation=None, options=[_o1])) is None
      and _WF._about_an_option(_o1, _out, {"product_id": "amazon-web-services-amazon-transcribe", "product_version": "", "text": "Amazon Transcribe batch costs 0.024 USD a minute."}, own_support=True)
      and not _WF._about_an_option(_o1, _out, {"product_id": "amazon-web-services-amazon-transcribe", "product_version": "", "text": "Amazon Transcribe batch costs 0.024 USD a minute."}, own_support=False)
      and not _WF._about_an_option(_o1, _out, {"product_id": "deepgram-nova-3", "product_version": "", "text": "Deepgram Nova-3 costs 0.0043 USD a minute."}, own_support=True)
      and _WF._about_pick(_o1, {"product_id": "kolwrite-kolwrite", "product_version": "base", "text": "KolWrite Base model costs 0.48 USD an hour."}) is False
      and _WF._about_pick(_NS(option="KolWrite (API, Base model)", product_id="kolwrite-kolwrite", product_version="", verdict="recommended"), {"product_id": "kolwrite-kolwrite", "product_version": "base", "text": "x"})
      and _CV.same_product("railway-railway", "hobby", "railway-railway", "", "Railway Hobby plan", "Railway Hobby plan costs 5 USD a month.") and not _CV.same_product("railway-railway", "hobby", "railway-railway", "", "Railway Hobby plan", "Railway Pro plan costs 20 USD a month."))

# (e2) a worker that keeps asking for tools past its stage allowance (eight tool calls, then the send allowance) is a failed worker
# whose searches stay stored; the other workers and the run go on (the round limit is not a stage_failed stop that ends the run with nothing produced).
scL = PF.Script(questions=False, subjects=("transcription", "llm_summarization_api", "backend_hosting_and_storage"), extra_queries=QW); scL.worker_tool_loop = True
rL, stubL = PF.runner_for("workers_loop", scL, settings=S3, research=PF.Sim(per_query=True, distinct=True)); resL = rL.start("P1", PF.IDEA); cL = rL.conn; ridL = resL["run_id"]
lrows = [tuple(r) for r in cL.execute("SELECT worker, task_id, status, error FROM worker_results WHERE run_id=? ORDER BY task_id", (ridL,))]
markL = json.loads(cL.execute("SELECT output_json FROM run_steps WHERE step_key=? AND status='completed'", (f"{ridL}:research_plan:w0:b0",)).fetchone()[0]); wkL = markL.get("workers") or {}
rejL = cL.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND state='rejected'", (ridL,)).fetchone()[0]
check("p6-R13: a Researcher worker whose tool loop runs past its allowances is recorded failed with the round limit as the reason, its completed searches stay stored, the other workers and the run complete (no stage_failed stop)",
      resL["status"] == "completed" and len(lrows) == 3 and all(w[2] == "failed" and "round_limit" in str(w[3]) for w in lrows) and len(wkL.get("failed") or []) == 3 and rejL >= 1
      and cL.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND tool='search' AND state='completed'", (ridL,)).fetchone()[0] >= 3 and _decisions(cL)[0]["status"] == "active", (resL.get("status"), resL.get("stop_reason"), lrows[:3], wkL.get("failed")))
# the loop-limit signal counts the tool requests refused at a limit over the whole run; it is announced once, after the stage that reached the limit
from app.runner import events as _evL
limL = cL.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND state='rejected' AND error LIKE '%limit%'", (ridL,)).fetchone()[0]
loopL = [(x.get("stage"), x.get("value")) for _, e0, x in _evL.events(cL, ridL) if e0 == "intervention" and x.get("signal") == "loop_limit"]
check("D-A410: the tool requests refused at a limit in the research stage are announced once, after that stage, with their count; the extraction, verification, comparison and plan stages, which reached no limit, are not announced as ending with a partial result for them (the journey read 'the compare and plan stages reached the loop limit (13)')",
      limL >= 1 and loopL == [("research_plan", limL)], (limL, loopL))

# a decision no hard requirement binds links to the requirements that bind it (soft ones included), never to nothing
from app.core import applicability as _AP
_reqs = [{"id": "P1.REQ-001", "key": "languages", "value": "he", "hard": True, "applies_to": ["transcription"]}, {"id": "P1.REQ-005", "key": "hosting_and_privacy", "value": "cloud ok", "hard": False, "applies_to": None}]
check("D-A65: the binding helper returns the soft requirements that bind a subject when asked for every kind (the hosting subject binds the general hosting requirement and not the transcription-only language one), and every decision of a three-subject run carries at least one requirement link",
      [r["id"] for r in _AP.binding(_reqs, "backend_hosting_and_storage", hard_only=False)] == ["P1.REQ-005"] and _AP.binding(_reqs, "backend_hosting_and_storage") == []
      and all(cL.execute("SELECT COUNT(*) FROM trace_edges WHERE kind='req_decision' AND to_id=?", (d,)).fetchone()[0] >= 1 for (d,) in cL.execute("SELECT id FROM decisions WHERE project_id='P1'")))

# (f) the above-budget price arrives at the evaluation, not at the expansion: the final claim set is rechecked by code and the dearer version cannot activate
class EvaluationAddsPrice(PF.Script):
    def __init__(self):
        super().__init__(questions=False); self.second_product = True; self.expensive = []
    def __call__(self, body):
        result = super().__call__(body); s0 = PF.schema_of(body)
        if s0 == "AlternativesOut":
            data = json.loads(result["content"]); u0 = PF.user_text(body)
            claims0 = json.loads(u0.split("Supported claims: ", 1)[1].split("\nReturn at most", 1)[0])
            self.expensive = [c0["claim_id"] for c0 in claims0 if str(c0.get("product_version")) in ("v2", "2") and c0.get("kind") == "price"]
            for alt in data["alternatives"]:
                alt["claim_ids"] = [cid for cid in alt["claim_ids"] if cid not in self.expensive]       # the model leaves the price for the evaluator
            self.last_alternatives = data["alternatives"]; return PF.J(data)
        if s0 == "BranchEvaluationOut":
            data = json.loads(result["content"]); branches0 = json.loads(PF.user_text(body).split("Branches: ", 1)[1].split("\nRequirements:", 1)[0])
            for e0, b0 in zip(data["evaluations"], branches0):
                if " 2" in b0["label"]:
                    e0["claim_ids"] = list(e0["claim_ids"]) + self.expensive; e0.update(fit=10, evidence=10, cost=10, risk=10)
                else:
                    e0.update(fit=1, evidence=1, cost=1, risk=1)
            return PF.J(data)
        return result
scB = EvaluationAddsPrice(); rB, stubB = PF.runner_for("tree_eval_adds_price", scB); resB = rB.start("P1", PF.IDEA); cB = rB.conn; ridB = resB["run_id"]
nodesB = _tree_rows(cB, ridB); decB = _decisions(cB); prunedB = [n for n in nodesB if n["pruned"]]; selB = [n for n in nodesB if n["selected"]]
check("B-01 in the workflow (the 8c audit; since Phase 19 package 19.1, D-A407, the budget is calculated by code for every product before the tree, so the dearer version is rejected by arithmetic whether or not a model lists its price): the version 2 branches are pruned as a confirmed hard-constraint failure on the budget with the calculation in the reason (60.00 USD a month above the budget of 50), no decision with version 2 is active, and the version 1 branch is selected and active",
      resB["status"] == "completed" and scB.expensive and prunedB and all(n["hard_status"] == "fail" and n["reason"].startswith("confirmed hard-constraint failure: budget monthly usd (") and "60.00 USD a month" in n["reason"] and "above the budget of 50 USD a month" in n["reason"] and " 2" in n["label"] for n in prunedB)
      and len(selB) == 1 and " 2" not in selB[0]["label"] and not any(d0["status"] == "active" and " 2" in (d0["choice"] or "") for d0 in decB) and decB[0]["status"] == "active" and decB[0]["choice"] == "Orion Compute 1",
      (resB["status"], [(n["label"], n["hard_status"], n["pruned"], n["score"]) for n in nodesB], decB[:1]))

# (g) a Researcher worker that requests ask_user is refused in code before any side effect; the coordinator's ask_user is untouched (tests/test_workflow.py)
class WorkerAsks(PF.Script):
    def __call__(self, body):
        if PF.schema_of(body) == "ResearcherOut":
            if not any(m0.get("role") == "tool" for m0 in body["messages"]):
                return {"tool_calls": [("ask_user", {"text": "Synthetic worker question?", "why_blocking": "synthetic", "requirement_key": "worker_only"})]}
            return PF.J({"proposals": []})
        return super().__call__(body)
scA = WorkerAsks(questions=False, subjects=("transcription", "backend_hosting_and_storage")); rA, stubA = PF.runner_for("worker_ask_user", scA); resA = rA.start("P1", PF.IDEA); cA = rA.conn; ridA = resA["run_id"]
askedA = [tuple(r0) for r0 in cA.execute("SELECT worker, state, error FROM tool_calls WHERE run_id=? AND tool='ask_user' AND worker<>'w0' ORDER BY worker", (ridA,))]
offeredA = sorted({t0["function"]["name"] for b0 in stubA.bodies if PF.schema_of(b0) == "ResearcherOut" for t0 in b0.get("tools", [])})
check("SF-01 in the workflow (the 8c audit): a Researcher worker's ask_user request is refused in code (a rejected tool row per worker naming the worker, no question row, no pause, the run never awaiting_user), the siblings finish (both worker_results completed), the run completes; the workers are offered search and read only",
      resA["status"] == "completed" and offeredA == ["read", "search"] and len(askedA) == 2 and all(st == "rejected" and "for worker" in (err or "") for _w, st, err in askedA)
      and cA.execute("SELECT COUNT(*) FROM questions WHERE project_id='P1' AND requirement_key='worker_only'").fetchone()[0] == 0 and cA.execute("SELECT status, stop_reason FROM runs WHERE id=?", (ridA,)).fetchone()[:] == ("completed", "complete")
      and [r0[0] for r0 in cA.execute("SELECT status FROM worker_results WHERE run_id=? ORDER BY task_id", (ridA,))] == ["completed", "completed"] and not any(e0 == "pause" for _, e0, _d in ev.events(cA, ridA)), (resA["status"], askedA, offeredA))

# (h) a fork whose ledger or gateway construction fails closes the handle it opened
from app.graph.workflow import RunContext as _RC
from app.core.ledger import BudgetLedger as _BL
fdb = fresh_db("fork_cleanup.db"); kept = []
def _mk_ledger(conn0, run_id0):
    l0 = _BL(conn0, run_id0, 2.5); kept.append(l0); return l0
def _mk_gateway(ledger0): raise ValueError("synthetic gateway construction failure")
ctxF = _RC(conn=fdb, settings={}, gateway=None, research=None, project_id="P1", run_id="R1", db_path=fdb.execute("PRAGMA database_list").fetchone()[2], make_ledger=_mk_ledger, make_gateway=_mk_gateway)
errF = None
try:
    ctxF.fork("w1-t1")
except ValueError as e0:
    errF = str(e0)
def _closed(c0):
    try:
        c0.execute("SELECT 1").fetchone(); return False
    except Exception:
        return True
check("SF-02 (the 8c audit): a fork whose gateway construction fails raises the failure and closes the handle it opened (the ledger built on it cannot query), nothing left for a caller that never received the context",
      errF == "synthetic gateway construction failure" and len(kept) == 1 and _closed(kept[0].conn), (errF, len(kept)))

# (h2) a source task whose extractor cannot be built (in its worker thread) still closes the fork it opened; the research workers' forks close as before
import threading as _th
from app.graph import workflow as _wf
class ExtractorRefusesInWorker(_wf.Extractor):
    def __init__(self, *a, **k):
        if _th.current_thread() is not _th.main_thread():
            raise ValueError("synthetic extractor construction failure in a worker thread")
        super().__init__(*a, **k)
forked = []
_orig_fork = _RC.fork
def _recording_fork(self, worker=None):
    cx0 = _orig_fork(self, worker); forked.append(cx0); return cx0
_RC.fork = _recording_fork
try:
    scX = PF.Script(questions=False, subjects=("transcription", "llm_summarization_api", "backend_hosting_and_storage"), extra_queries=QW)
    rX, stubX = PF.runner_for("source_ctor_fails", scX, settings=S3, research=PF.Sim(per_query=True, distinct=True)); rX._extractor_cls = ExtractorRefusesInWorker
    try:
        resX = rX.start("P1", PF.IDEA); statusX = resX["status"]; errX = None
    except Exception as e0:                                                                   # noqa: BLE001
        statusX = None; errX = f"{type(e0).__name__}: {e0}"[:160]
finally:
    _RC.fork = _orig_fork
chunk_forks = [cx0 for cx0 in forked if cx0.worker == "w0"]                                  # the parallel chunk's forks keep the main label; the research workers' carry their ids
failedX = [d0 for _, e0, d0 in (ev.events(rX.conn, resX["run_id"]) if statusX else []) if e0 == "worker" and str(d0.get("task", "")).startswith("source ") and "synthetic extractor construction failure" in str(d0.get("state", ""))]
check("SF-02 (the 8c audit), the source task: the parallel chunk's extractor construction fails in its worker threads and every fork the run opened (the research workers' and the chunk's) is closed afterwards; the failure is the chunk's recorded worker failure (a worker event per source naming it), not a leaked handle",
      len(forked) >= 4 and len(chunk_forks) >= 2 and all(_closed(cx0.conn) for cx0 in forked) and len(failedX) == len(chunk_forks),
      (statusX, errX, len(forked), len(chunk_forks), [cx0.worker for cx0 in forked], failedX[:2]))

# (i) a repair inside the tree is a send the branch event and the rationale count
class RepairFirstAlternative(PF.Script):
    def __init__(self):
        super().__init__(questions=False); self.second_product = True; self.alt_attempts = 0
    def __call__(self, body):
        if PF.schema_of(body) == "AlternativesOut":
            self.alt_attempts += 1
            if self.alt_attempts == 1:
                return PF.J({"malformed": True})
            body = dict(body); body["messages"] = [m0 for m0 in body["messages"] if m0["role"] != "user" or "Supported claims: " in m0["content"]]
        return super().__call__(body)
scR = RepairFirstAlternative(); rR, stubR = PF.runner_for("tree_retry_accounting", scR); resR = rR.start("P1", PF.IDEA); cR = rR.conn; ridR = resR["run_id"]
sentR = cR.execute("SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? AND sent_at IS NOT NULL", (ridR, f"{ridR}:tree:%")).fetchone()[0]
branchR = [d0 for _, e0, d0 in ev.events(cR, ridR) if e0 == "branch"]; decR = _decisions(cR)
check("SF-03 in the workflow (the 8c audit): the first expansion answer is malformed and repaired (two sends under d1:tree_alternatives.v2:a-x:alternatives); the branch event's calls and the tree's provenance line count the four sends the ledger holds for the decision (the rationale is the reader memo's since Phase 9b), the dispatch limits unchanged, the run completes with the decision active",
      resR["status"] == "completed" and scR.alt_attempts == 2 and sentR == 4 and _tree_keys(cR, ridR) == ["d1:tree_alternatives.v2:a-x:alternatives", "d1:tree_alternatives.v2:a-x:alternatives", "d1:tree_variants.v2:a-x:variants:A1", "d1:tree_evaluate.v2:a-x:evaluate"] and len(branchR) == 1 and branchR[0]["calls"] == 4
      and "4 call(s)" in " ".join(str(r0[0]) for r0 in cR.execute("SELECT observation FROM reasoning_records WHERE run_id=? AND action='the tree''s memo'", (ridR,)).fetchall()) and decR[0]["status"] == "active", (resR["status"], sentR, [b0["calls"] for b0 in branchR], decR[:1]))   # the count lives in the provenance, the rationale is the reader memo's

print("\n".join(f"PASS  {n}" for n, ok in results if ok)); print(f"{sum(ok for _, ok in results)}/{len(results)} checks passed")
background_threads.finish()
