"""Regression checks (R1 to R12) for the replay of tool calls, billing attempts, run quotas, cancellation, questions, the tool loop, decision summaries, passages, claim proposals and the tree snapshot. No internet, no keys; stub providers only.

    .venv/bin/python tests/test_audit.py

Prints one PASS line per passed check, then the count."""
import http.server, json, pathlib, sqlite3, sys, tempfile, threading, time
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from app.core.sqlguard import open_connection, inspect as insp, GuardedConnection
from app.repo import apply_schema
from schemas import models as M
from app.core.ledger import BudgetLedger
from app.core.tools import ToolRunner, ToolRejected, Paused, tools_for
from app.core import react
from app.core.retrieval import index_source, delete_source, FTS5Retriever
from app.core.privacy import delete_project
from app.core.tree import explore, persist as persist_tree, validate_result, Node
from app.core.roles import run_workers, merge_proposals
from app.core.safety import StopController
from app.adapters.model_gateway import CloudGateway
from app.runner.recovery import start_run, answer_question, RunStopped, assert_run_active

results = []
def check(name, cond):
    results.append((name, bool(cond))); assert cond, name
import background_threads; background_threads.install()                              # an uncaught exception in a background thread is a FAIL line and a nonzero exit
tmp = pathlib.Path(tempfile.mkdtemp())
def fresh_db(name, projects=("P1",)):
    c = open_connection(tmp / name, check_same_thread=False, timeout=5); apply_schema(c); c.execute("PRAGMA foreign_keys=ON")
    for p in projects:
        c.execute("INSERT INTO projects (id, owner_id, title) VALUES (?,?,?)", (p, "U1", p))
    c.commit(); return c
class Research:
    def __init__(self, fail=False): self.calls, self.fail = [], fail
    def search(self, q, f, n):
        self.calls.append(("search", q))
        if self.fail: raise TimeoutError("provider timeout after sending")
        return [{"url": "https://a.example", "text": "result for " + q}]
    def contents(self, urls, f): self.calls.append(("read", urls[0])); return [{"url": urls[0], "text": "page"}]

# ---------- R1 exact request and result association on replay ----------
db = fresh_db("r1.db"); start_run(db, "R1", "P1", "T", "W"); L = BudgetLedger(db, "R1", 2.5)
rs = Research(); a = ToolRunner(db, L, "R1", "P1", "research_plan", rs); a.handle({"tool": "search", "args": {"query": "first query"}})
b = ToolRunner(db, L, "R1", "P1", "research_plan", rs)
obs = b.handle({"tool": "read", "args": {"url": "https://different.example/new"}})
check("R1 a different request never receives an old observation: it is a new tool call with its own result", obs["tool"] == "read" and obs["data"][0]["text"] == "page" and obs["tool_call_id"].endswith(":tool:2")
      and ("read", "https://different.example/new") in rs.calls and db.execute("SELECT state, tool FROM tool_calls WHERE id LIKE '%:tool:1'").fetchone() == ("completed", "search"))
c = ToolRunner(db, L, "R1", "P1", "research_plan", rs); n = len(rs.calls)
check("R1 the exact same request replays the stored observation", c.handle({"tool": "search", "args": {"query": "first query"}})["summary"] == "replayed stored observation" and len(rs.calls) == n)

# ---------- R2 separate billing attempts keep the previous possible charge ----------
db2 = fresh_db("r2.db"); start_run(db2, "R2", "P1", "T", "W"); L2 = BudgetLedger(db2, "R2", 2.5)
bad = Research(fail=True); t1 = ToolRunner(db2, L2, "R2", "P1", "research_plan", bad)
try:
    t1.handle({"tool": "search", "args": {"query": "price query"}}); ok = False
except ToolRejected:
    ok = True
first = db2.execute("SELECT id, state FROM usage_ledger WHERE run_id='R2'").fetchall()
check("R2 a timed-out tool call is unknown", ok and first == [(f"{t1.step_key}:search:1", "unknown")])
L2.resolve_unknown(first[0][0], "retried"); possible_before = L2.possible_charges()
t2 = ToolRunner(db2, L2, "R2", "P1", "research_plan", Research()); r = t2.handle({"tool": "search", "args": {"query": "price query"}})
rows = db2.execute("SELECT id, state, resolution FROM usage_ledger WHERE run_id='R2' ORDER BY attempt").fetchall()
check("R2 the retry is a new billing attempt; the interrupted attempt stays unknown and its possible charge stays counted",
      r["ok"] and rows == [(f"{t1.step_key}:search:1", "unknown", "retried"), (f"{t1.step_key}:search:2", "completed", None)] and abs(L2.possible_charges() - possible_before) < 1e-9 and possible_before > 0 and abs(L2.committed() - (0.00805 + 0.00805)) < 1e-9)   # both attempts hold the margined reservation (0.007 x 1.15): the unknown one as a possible charge, the retry because the stub reports no provider figure (cost_uncertain stays set)

# ---------- R3 atomic run quotas across parallel workers ----------
db3 = fresh_db("r3.db"); start_run(db3, "R3", "P1", "T", "W"); db3.commit()
outcomes = []
def worker(i):
    c = open_connection(tmp / "r3.db", timeout=5, check_same_thread=False); c.execute("PRAGMA foreign_keys=ON")
    try:
        tr = ToolRunner(c, BudgetLedger(c, "R3", 2.5), "R3", "P1", "research_plan", Research(), worker=f"w{i}", run_limits={"searches": 1})
        tr.handle({"tool": "search", "args": {"query": f"query number {i}"}}); outcomes.append("ok")
    except ToolRejected as e:
        outcomes.append("rejected" if "limit" in str(e) else str(e))
    finally:
        c.close()
th = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]; [x.start() for x in th]; [x.join() for x in th]
check("R3 two parallel workers with a run limit of one search: exactly one executes", sorted(outcomes) == ["ok", "rejected"] and db3.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id='R3' AND tool='search' AND state='completed'").fetchone()[0] == 1)

# ---------- R4 cancellation at the dispatch boundary ----------
db4 = fresh_db("r4.db"); start_run(db4, "R4", "P1", "T", "W"); L4 = BudgetLedger(db4, "R4", 2.5); rs4 = Research()
StopController(db4, "R4", L4).stop()
try:
    ToolRunner(db4, L4, "R4", "P1", "research_plan", rs4).handle({"tool": "search", "args": {"query": "after stop"}}); ok = False
except RunStopped:
    ok = True
check("R4 no tool request starts after a stop", ok and rs4.calls == [] and db4.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id='R4'").fetchone()[0] == 0)
gw4 = CloudGateway(api_key="k", default_model="a/x", fallback_model="b/y", prices={"a/x": (1, 1), "b/y": (1, 1)}, privacy_mode="cloud", ledger=L4, base_url="http://127.0.0.1:9/v1")
try:
    gw4.call("R4:s1:w0:b0", M.ClarifyOut, [{"role": "user", "content": "x"}], 10, 10); ok = False
except RunStopped:
    ok = True
check("R4 no model request starts after a stop", ok and db4.execute("SELECT COUNT(*) FROM usage_ledger WHERE run_id='R4' AND kind='model'").fetchone()[0] == 0)

# ---------- R9 idempotent questions and the pause, answer, resume path ----------
class Stub:
    def __init__(self):
        self.queue, self.bodies = [], []
        outer = self
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); outer.bodies.append(body)
                item = outer.queue.pop(0) if outer.queue else {"content": "{}"}
                if "status" in item:
                    raw = json.dumps({"error": {"message": "stub", "code": item["status"]}}).encode(); self.send_response(item["status"]); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
                msg = {"role": "assistant", "content": item.get("content")}
                if "tool" in item:
                    msg["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": item["tool"], "arguments": json.dumps(item["args"])}}]
                payload = {"id": f"gen-{len(outer.bodies)}", "object": "chat.completion", "created": 0, "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop", "message": msg}], "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60, "cost": 0.01}}
                raw = json.dumps(payload).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start(); self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
A_, B_ = Stub(), Stub(); PR = {"anthropic/claude-sonnet-5.5": (2.0, 10.0), "google/gemini-3.8-flash": (0.75, 3.75)}
PLAN = json.dumps({"decision_subjects": ["transcription"], "queries": [{"text": "speech api hebrew pricing", "decision_subject": "transcription", "target_fact": "price", "freshness": "volatile"}]})
db9 = fresh_db("r9.db"); start_run(db9, "R9", "P1", "T", "W"); L9 = BudgetLedger(db9, "R9", 2.5)
def gw9(timeout=5.0): return CloudGateway(api_key="k", default_model="anthropic/claude-sonnet-5.5", fallback_model="google/gemini-3.8-flash", prices=PR, privacy_mode="cloud", ledger=L9, base_url=A_.url, fallback_base_url=B_.url, timeout_s=timeout)
runner = ToolRunner(db9, L9, "R9", "P1", "research_plan", Research()); SK = runner.step_key
A_.queue = [{"tool": "ask_user", "args": {"text": "Can audio leave the network?", "why_blocking": "decides transcription", "requirement_key": "privacy.audio"}}]
r = gw9().call_with_tools(SK, M.ResearchPlanOut, [{"role": "system", "content": "plan"}, {"role": "user", "content": "idea"}], tools_for("research_plan"), runner, 100, 300)
check("R9 ask_user pauses the run and persists one question linked to the tool call", r["status"] == "paused" and db9.execute("SELECT status FROM runs WHERE id='R9'").fetchone()[0] == "awaiting_user"
      and db9.execute("SELECT COUNT(*), MAX(tool_call_id) FROM questions").fetchone() == (1, r["tool_call_id"]))
again = ToolRunner(db9, L9, "R9", "P1", "research_plan", Research())
try:
    again.handle({"tool": "ask_user", "args": {"text": "Can audio leave the network?", "why_blocking": "decides transcription", "requirement_key": "privacy.audio"}}); ok = False
except Paused as p:
    ok = p.question_id == r["question_id"]
check("R9 replaying the paused request pauses again on the same question; no second question", ok and db9.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1)
answer_question(db9, r["question_id"], {"choice": "approved cloud"})
A_.queue = [{"content": PLAN}]; resumed = ToolRunner(db9, L9, "R9", "P1", "research_plan", Research())
r2 = gw9().call_with_tools(SK, M.ResearchPlanOut, [{"role": "system", "content": "plan"}, {"role": "user", "content": "idea"}], tools_for("research_plan"), resumed, 100, 300)
last_tool_msg = [m for m in A_.bodies[-1]["messages"] if m["role"] == "tool"][-1]
check("R9 after the answer the run resumes from the stored transcript and the exact paused call receives the answer", r2["status"] == "ok" and db9.execute("SELECT status FROM runs WHERE id='R9'").fetchone()[0] == "running"
      and "approved cloud" in last_tool_msg["content"] and db9.execute("SELECT COUNT(*) FROM stage_transcripts WHERE step_key=?", (SK,)).fetchone()[0] == 0)

# ---------- R11 and R12 bounded repair and fallback in the tool loop ----------
db11 = fresh_db("r11.db"); start_run(db11, "R11", "P1", "T", "W"); L11 = BudgetLedger(db11, "R11", 2.5)
def gw11(): return CloudGateway(api_key="k", default_model="anthropic/claude-sonnet-5.5", fallback_model="google/gemini-3.8-flash", prices=PR, privacy_mode="cloud", ledger=L11, base_url=A_.url, fallback_base_url=B_.url)
rn = ToolRunner(db11, L11, "R11", "P1", "research_plan", Research())
A_.queue = [{"status": 503}]; B_.queue = [{"content": PLAN}]
r = gw11().call_with_tools(rn.step_key, M.ResearchPlanOut, [{"role": "system", "content": "plan"}], tools_for("research_plan"), rn, 100, 300)
check("R12 a provider error in a tool-enabled call falls back once to the other vendor", r["status"] == "fallback" and B_.bodies[-1]["model"] == "google/gemini-3.8-flash")
A_.queue = [{"content": json.dumps({"decision_subjects": []})}, {"content": PLAN}]; before = db11.execute("SELECT COUNT(*) FROM usage_ledger WHERE kind='model'").fetchone()[0]
r = gw11().call_with_tools(rn.step_key + ":lim", M.ResearchPlanOut, [{"role": "system", "content": "plan"}], tools_for("research_plan"), rn, 100, 300, max_rounds=1)
check("R11 a repair at the round limit does not send again", r["status"] == "round_limit" and db11.execute("SELECT COUNT(*) FROM usage_ledger WHERE kind='model'").fetchone()[0] == before + 1)
A_.queue = []

# ---------- R10 meaningful action-level decision summaries ----------
db10 = fresh_db("r10.db"); start_run(db10, "R10", "P1", "T", "W"); L10 = BudgetLedger(db10, "R10", 2.5)
tr10 = ToolRunner(db10, L10, "R10", "P1", "research_plan", Research()); o1 = tr10.handle({"tool": "search", "args": {"query": "query one"}}); o2 = tr10.handle({"tool": "search", "args": {"query": "query two"}})
try:
    react.record(db10, "R10", tr10.step_key, o1["tool_call_id"], "", "", "", ""); ok = False
except ValueError:
    ok = True
db10.execute("INSERT INTO reasoning_records (run_id, step_key, action_id, purpose, action, observation, change) VALUES ('R10',?, '', '', '', '', '')", (tr10.step_key,)); db10.commit()
t = react.trace_complete(db10, "R10")
check("R10 empty summaries never count; every tool action needs its own summary", ok and not t["complete"] and sorted(t["missing"]) == sorted([o1["tool_call_id"], o2["tool_call_id"]]))
react.record(db10, "R10", tr10.step_key, o1["tool_call_id"], "find price", "tool:search", "1 result", "added"); react.record(db10, "R10", tr10.step_key, o2["tool_call_id"], "find support", "tool:search", "1 result", "added")
check("R10 the trace is complete once each action has a nonempty summary", react.trace_complete(db10, "R10")["complete"])

# ---------- R5 and R6 refresh and deletion of passages and FTS rows ----------
db5 = fresh_db("r5.db", projects=("P1", "P2"))
for pid, sid in (("P1", "P1.SRC-001"), ("P2", "P2.SRC-001")):
    db5.execute("INSERT INTO sources (id, project_id, url, canonical_url, tier, retrieved_at, content_hash, freshness) VALUES (?,?,?,?,1,'2026-10-20','sha256:' || hex(randomblob(16)),'technical')", (sid, pid, "https://" + pid + ".example", "https://" + pid + ".example"))
db5.commit()
n4 = index_source(db5, "P1", "P1.SRC-001", ("old price marker OLDPRICE in this text. " * 60)); index_source(db5, "P2", "P2.SRC-001", "other project text " * 80)
n1 = index_source(db5, "P1", "P1.SRC-001", "new text with NEWPRICE only")
hits = FTS5Retriever(db5).retrieve("P1", "OLDPRICE", 5)
check("R5 refreshing a source removes obsolete passages and their FTS rows", n4 >= 4 and n1 == 1 and hits == [] and db5.execute("SELECT COUNT(*) FROM passages WHERE source_id='P1.SRC-001'").fetchone()[0] == 1
      and db5.execute("SELECT COUNT(*) FROM passages_fts WHERE text LIKE '%OLDPRICE%'").fetchone()[0] == 0)
check("R5 an empty refresh removes every passage of the source", index_source(db5, "P1", "P1.SRC-001", "") == 0 and db5.execute("SELECT COUNT(*) FROM passages WHERE source_id='P1.SRC-001'").fetchone()[0] == 0)
index_source(db5, "P1", "P1.SRC-001", "back again " * 50); delete_source(db5, "P1.SRC-001")
check("R5 deleting a source removes its passages and FTS rows", db5.execute("SELECT COUNT(*) FROM passages_fts WHERE text LIKE '%back again%'").fetchone()[0] == 0 and db5.execute("SELECT COUNT(*) FROM sources WHERE id='P1.SRC-001'").fetchone()[0] == 0)
db5.execute("INSERT INTO sources (id, project_id, url, canonical_url, tier, retrieved_at, content_hash, freshness) VALUES ('P1.SRC-002','P1','https://b.example','https://b.example',1,'2026-10-20','sha256:x','technical')"); db5.commit()
index_source(db5, "P1", "P1.SRC-002", "private project text " * 50)
class NullCk:
    def delete_thread(self, t): pass
delete_project(db5, NullCk(), "P1")
check("R6 deleting the project removes its passages_fts rows; another project's rows stay", db5.execute("SELECT COUNT(*) FROM passages_fts WHERE text LIKE '%private project%'").fetchone()[0] == 0
      and db5.execute("SELECT COUNT(*) FROM passages_fts").fetchone()[0] == db5.execute("SELECT COUNT(*) FROM passages WHERE project_id='P2'").fetchone()[0] > 0)

# ---------- R7 distinct atomic claims from one passage; malformed proposals; worker identity ----------
base = {"task_id": "t1", "kind": "capability", "subject": "Speech API A", "source_url": "https://a.example/langs", "quote": "Arabic and Hebrew are supported."}
def wf(wid, task):
    if task["task_id"] == "t1":
        return [dict(base, worker=wid, attribute="language_support", value="ar", claim_text="supports Arabic"), dict(base, worker=wid, attribute="language_support", value="he", claim_text="supports Hebrew"), {"worker": wid, "task_id": "t1", "broken": True}]
    return [dict(base, task_id=task["task_id"], worker=wid, attribute="language_support", value="he", claim_text="supports Hebrew")]
wr = run_workers([{"task_id": "t1"}, {"task_id": "t2"}, {"task_id": "t3"}, {"task_id": "t4"}], wf, max_workers=3)
m = merge_proposals(set(), wr)
check("R7 two different atomic claims backed by one passage both survive; the passage is counted once", len(m["accepted"]) == 2 and {a["value"] for a in m["accepted"]} == {"ar", "he"} and m["passages"] == 1)
check("R7 a malformed proposal is reported, not a crash; identical claims from other workers are duplicates with provenance", len(m["malformed"]) == 1 and m["duplicates"] == 3 and len([a for a in m["accepted"] if a["value"] == "he"][0]["provenance"]) == 4)
check("R7 worker ids are unique per task even with more tasks than workers", len({r.worker for r in wr}) == 4)

# ---------- R8 tree snapshot replacement, qualified parents, one selection ----------
def mk(sel):
    nodes = [Node(id="A1", label="A1", depth=1, hard_status="pass", score=7.0, selected=(sel == "A1")), Node(id="A2", label="A2", depth=1, hard_status="pass", score=8.0, selected=(sel == "A2")), Node(id="A1.1", label="v", depth=2, parent_id="A1", hard_status="pass", score=6.0)]
    return {"subject": "transcription", "nodes": nodes, "selected_id": sel, "selected_unresolved": False, "partial": False, "reason": None, "calls_used": 3, "limit": 7}
db8 = fresh_db("r8.db"); start_run(db8, "R8", "P1", "T", "W")
v1 = persist_tree(db8, "R8", mk("A1")); v2 = persist_tree(db8, "R8", {**mk("A2"), "nodes": mk("A2")["nodes"][:2]})
rows = db8.execute("SELECT id, parent_id, selected, version FROM tree_nodes WHERE run_id='R8' ORDER BY id").fetchall()
check("R8 a new snapshot replaces the old one: exactly one selected node, no stale nodes, version advanced", (v1, v2) == (1, 2) and len(rows) == 2 and sum(r[2] for r in rows) == 1 and all(r[3] == 2 for r in rows) and [r[0] for r in rows if r[2]] == ["R8:transcription:A2"])
db8b = fresh_db("r8b.db"); start_run(db8b, "R8b", "P1", "T", "W"); persist_tree(db8b, "R8b", mk("A1"))
check("R8 parent ids are stored fully qualified", db8b.execute("SELECT parent_id FROM tree_nodes WHERE id='R8b:transcription:A1.1'").fetchone()[0] == "R8b:transcription:A1")
bad = mk("A1"); bad["nodes"][1].selected = True; bad["nodes"][0].claim_ids = ["P1.CLM-999"]
issues = validate_result(bad, claims_exist=lambda c: False)
check("R8 validation catches two selections and unknown claims before a tree may drive a decision", any("more than one selected" in i for i in issues) and any("unknown claims" in i for i in issues))

print("\n".join(f"PASS  {n}" for n, ok in results if ok)); print(f"{sum(ok for _, ok in results)}/{len(results)} checks passed")
background_threads.finish()
