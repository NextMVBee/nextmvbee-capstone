"""Lifecycle sequences across interruptions: a question asked after a search keeps its identity (F1), the send allowance
survives a resume (F2), and an invalid request never changes completed history (F3). Stub providers only.
Run the file as a script; it prints one PASS line per check and the count."""
import http.server, json, pathlib, sqlite3, sys, tempfile, threading
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from app.core.sqlguard import open_connection, inspect as insp, GuardedConnection
from app.repo import apply_schema
from schemas import models as M
from app.core.ledger import BudgetLedger
from app.core.tools import ToolRunner, ToolRejected, tools_for
from app.adapters.model_gateway import CloudGateway
from app.runner.recovery import start_run, answer_question
from app.core import react

results = []
def check(name, cond):
    results.append((name, bool(cond))); assert cond, name
import background_threads; background_threads.install()                              # an uncaught exception in a background thread is a FAIL line and a nonzero exit
tmp = pathlib.Path(tempfile.mkdtemp())
class Research:
    def __init__(self): self.calls = []
    def search(self, q, f, n): self.calls.append(("search", q)); return [{"url": "https://a.example", "text": "result for " + q}]
    def contents(self, urls, f): self.calls.append(("read", urls[0])); return [{"url": urls[0], "text": "page " + urls[0]}]
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
                if "tools" in item:
                    msg["tool_calls"] = [{"id": f"call_{k+1}", "type": "function", "function": {"name": tl, "arguments": json.dumps(ar)}} for k, (tl, ar) in enumerate(item["tools"])]
                payload = {"id": f"gen-{len(outer.bodies)}", "object": "chat.completion", "created": 0, "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop", "message": msg}], "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60, "cost": 0.01}}
                raw = json.dumps(payload).encode(); self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start(); self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
A_, B_ = Stub(), Stub(); PR = {"anthropic/claude-sonnet-5.5": (2.0, 10.0), "google/gemini-3.8-flash": (0.75, 3.75)}
PLAN = json.dumps({"decision_subjects": ["transcription"], "queries": [{"text": "speech api hebrew pricing", "decision_subject": "transcription", "target_fact": "price", "freshness": "volatile"}]})
Q = {"text": "Which deployment region?", "why_blocking": "needed for the provider choice", "requirement_key": "region"}
TOOLS = tools_for("research_plan"); MSG = [{"role": "user", "content": "plan"}]
def setup(name):
    c = open_connection(tmp / f"{name}.db", check_same_thread=False, timeout=5); apply_schema(c); c.execute("PRAGMA foreign_keys=ON")
    c.execute("INSERT INTO projects (id, owner_id, title) VALUES ('P1','U1','p')"); c.commit(); start_run(c, name, "P1", "T", "W"); l = BudgetLedger(c, name, 2.5); rs = Research()
    runner = lambda: ToolRunner(c, l, name, "P1", "research_plan", rs)
    gw = lambda: CloudGateway(api_key="k", default_model="anthropic/claude-sonnet-5.5", fallback_model="google/gemini-3.8-flash", prices=PR, privacy_mode="cloud", ledger=l, base_url=A_.url, fallback_base_url=B_.url)
    return c, l, rs, runner, gw
def unresolved(c): return c.execute("SELECT COUNT(*) FROM tool_calls WHERE state IN ('planned','sent','paused')").fetchone()[0]
def sends(c): return c.execute("SELECT COUNT(*) FROM usage_ledger WHERE kind='model'").fetchone()[0]

# ---------- F1: search, then question, then answer, then resume ----------
c, l, rs, tr, gw = setup("f1"); sk = tr().step_key
A_.queue = [{"tools": [("search", {"query": "provider deployment region"})]}, {"tools": [("ask_user", Q)]}]
a = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300)
answer_question(c, a["question_id"], {"region": "EU_ONLY_SENTINEL"}); A_.queue = [{"content": PLAN}]
b = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300); enc = json.dumps(A_.bodies[-1]["messages"])
check("F1 a question asked after a search keeps its identity: the answer reaches the model, no replay mismatch, no repeated search, no unresolved tool call",
      a["status"] == "paused" and b["status"] == "ok" and "EU_ONLY_SENTINEL" in enc and "replay mismatch" not in enc and rs.calls.count(("search", "provider deployment region")) == 1 and unresolved(c) == 0
      and c.execute("SELECT state FROM tool_calls WHERE tool='ask_user'").fetchone()[0] == "completed")
# multiple prior tools before the question
c, l, rs, tr, gw = setup("f1b"); sk = tr().step_key
A_.queue = [{"tools": [("search", {"query": "first"})]}, {"tools": [("read", {"url": "https://a.example/one"})]}, {"tools": [("search", {"query": "second"})]}, {"tools": [("ask_user", Q)]}]
a = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300); n_calls = len(rs.calls)
answer_question(c, a["question_id"], {"region": "EU_SENTINEL_B"}); A_.queue = [{"content": PLAN}]
b = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300)
check("F1 three prior tools, then a question: resume repeats nothing and completes", b["status"] == "ok" and len(rs.calls) == n_calls and unresolved(c) == 0 and 'EU_SENTINEL_B' in json.dumps(A_.bodies[-1]["messages"]))
# mixed batch: two requests in one message, the second pauses
c, l, rs, tr, gw = setup("f1c"); sk = tr().step_key
A_.queue = [{"tools": [("search", {"query": "batch search"}), ("ask_user", Q)]}]
a = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300); n_calls = len(rs.calls)
answer_question(c, a["question_id"], {"region": "US_SENTINEL_C"}); A_.queue = [{"content": PLAN}]
b = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300); msgs = A_.bodies[-1]["messages"]
check("F1 a mixed batch resumes: the completed request is not re-sent, the paused one receives its answer, both results are in the transcript",
      b["status"] == "ok" and len(rs.calls) == n_calls and sum(1 for m in msgs if m["role"] == "tool") == 2 and 'US_SENTINEL_C' in json.dumps(msgs) and unresolved(c) == 0)

# ---------- F2: send allowance survives resume ----------
c, l, rs, tr, gw = setup("f2"); sk = tr().step_key
A_.queue = [{"content": "{}"}, {"tools": [("search", {"query": "provider price"})]}]
a = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300, max_rounds=2); before = sends(c)
A_.queue = [{"content": PLAN}]
b = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300, max_rounds=2)
check("F2 invalid output, repair asks for a tool, limit of two sends reached: resume sends nothing more", a["status"] == "round_limit" and before == 2 and b["status"] == "round_limit" and sends(c) == 2)
c, l, rs, tr, gw = setup("f2b"); sk = tr().step_key
A_.queue = [{"status": 503}]; B_.queue = [{"tools": [("search", {"query": "after fallback"})]}]
a = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300, max_rounds=2); before = sends(c)
A_.queue = [{"content": PLAN}]; B_.queue = [{"content": PLAN}]
b = gw().call_with_tools(sk, M.ResearchPlanOut, MSG, TOOLS, tr(), 100, 300, max_rounds=2)
check("F2 provider error, fallback asks for a tool, limit reached: the fallback send is counted and resume sends nothing more", before == 2 and b["status"] == "round_limit" and sends(c) == 2)
A_.queue = [{"content": PLAN}]
d = gw().call_with_tools(sk + ":new", M.ResearchPlanOut, MSG, TOOLS, ToolRunner(c, l, "f2b", "P1", "research_plan", rs, worker="w9"), 100, 300, max_rounds=2)
check("F2 a new stage instance has its own allowance", d["status"] == "ok")

# ---------- F3: invalid requests never mutate completed history ----------
c, l, rs, tr, gw = setup("f3"); original = {"tool": "search", "args": {"query": "original query"}}
tr().handle(original)
for bad in ({"tool": "forbidden_delete", "args": {}}, {"tool": "search", "args": {"query": "x"}}, {"tool": "read", "args": {"url": "ftp://nope"}}):
    try:
        tr().handle(bad); ok = False
    except ToolRejected:
        ok = True
    assert ok
states = c.execute("SELECT n, state FROM tool_calls ORDER BY n").fetchall()
replayed = tr().handle(original)
check("F3 forbidden names and invalid arguments get their own rejected rows; the completed search is untouched and replays with no second send",
      states == [(1, "completed"), (2, "rejected"), (3, "rejected"), (4, "rejected")] and replayed["summary"] == "replayed stored observation" and rs.calls.count(("search", "original query")) == 1)
tc = react.trace_complete(c, "f3")
check("F3 rejected requests are required actions in the trace until summarised", tc["required"] == 4)

print("\n".join(f"PASS  {n}" for n, ok in results if ok)); print(f"{sum(ok for _, ok in results)}/{len(results)} checks passed")
background_threads.finish()
