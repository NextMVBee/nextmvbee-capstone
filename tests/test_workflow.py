"""Offline checks: contracts, schema, interrupt and resume, spend reservation, invalidation. No network, no API keys, stub adapters only.
Run the file as a script; it prints one PASS line per check and the count."""
import json, sqlite3, sys, tempfile, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.core.sqlguard import open_connection, inspect as insp, GuardedConnection
from app.repo import apply_schema
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from schemas.models import STAGE_CONTRACTS, ClarifyOut, Decision
from app.core.invalidation import invalidate
from app.core.ledger import BudgetLedger, BudgetExceeded
from app.graph.skeleton import build_graph

results = []
def check(name, cond):
    results.append((name, bool(cond))); assert cond, name
import background_threads; background_threads.install()                              # an uncaught exception in a background thread is a FAIL line and a nonzero exit

tmp = pathlib.Path(tempfile.mkdtemp())
app = open_connection(tmp / "app.db", check_same_thread=False)
apply_schema(app)
app.execute("INSERT INTO projects (id, owner_id, title) VALUES ('P1','u1','demo')"); app.commit()
check("schema.sql applies and WAL mode is on", app.execute("PRAGMA journal_mode").fetchone()[0] == "wal")

for name, model in STAGE_CONTRACTS.items():
    (ROOT / "schemas/json" / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2))
c = ClarifyOut.model_validate_json('{"requirements":[{"key":"privacy_mode","value":"local_only","source":"user","hard":true}],'
                                   '"questions":[{"text":"What is the monthly budget?","why_blocking":"changes hosting","requirement_key":"budget_monthly_usd"}],'
                                   '"ready_for_research":false}')
check("stage contract validates an example", c.questions[0].requirement_key == "budget_monthly_usd")
try:
    ClarifyOut.model_validate({"requirements": [], "ready_for_research": True, "unexpected": 1}); ok = False
except Exception:
    ok = True
check("unknown fields are rejected", ok)
check("decision ID pattern is enforced", Decision.model_json_schema()["properties"]["id"]["pattern"] == "^[A-Za-z0-9_-]+\\.DEC-\\d{3,}$")

rows = [("REQ-001","DEC-001","req_decision"),("REQ-002","DEC-002","req_decision"),("DEC-001","CMP-001","decision_component"),
        ("DEC-002","CMP-002","decision_component"),("CMP-001","TSK-001","component_task"),("TSK-001","ACC-001","task_test")]
with app:
    for rid in ("REQ-001","REQ-002"):
        app.execute("INSERT INTO requirements (id, project_id, key, value_json, status, source, version) VALUES (?,?,?,?,?,?,1)",
                    (rid, "P1", rid.lower(), '"x"', "confirmed", "user"))
    for d in ("DEC-001","DEC-002"):
        app.execute("INSERT INTO decisions (id, project_id, subject, choice, rationale, importance) VALUES (?,?,?,?,?,?)", (d,"P1","s","c","r","high"))
    for cm in ("CMP-001","CMP-002"):
        app.execute("INSERT INTO components (id, project_id, name, responsibility) VALUES (?,?,?,?)", (cm,"P1","n","r"))
    app.execute("INSERT INTO tasks (id, project_id, component_id, title) VALUES ('TSK-001','P1','CMP-001','t')")
    app.execute("INSERT INTO acceptance_tests (id, project_id, task_id, criterion) VALUES ('ACC-001','P1','TSK-001','c')")
    for f, t, k in rows:
        app.execute("INSERT INTO trace_edges (project_id, from_id, to_id, kind) VALUES ('P1',?,?,?)", (f, t, k))
aff = invalidate(app, "P1", "REQ-001")
check("only dependents of the changed requirement go stale", aff == {"DEC": ["DEC-001"], "CMP": ["CMP-001"], "TSK": ["TSK-001"], "ACC": ["ACC-001"]})
check("unrelated decision stays active", insp(app).execute("SELECT status FROM decisions WHERE id='DEC-002'").fetchone()[0] == "active")

app.execute("INSERT INTO runs (id, project_id, thread_id, status, budget_json) VALUES ('R1','P1','P1-R1','running','{}')"); app.commit()
led = BudgetLedger(app, "R1", cap_usd=1.00)
e1 = led.reserve("s1", "model", 0.60); led.reserve("s1", "model", 0.60)
check("reservation is idempotent per step", abs(led.committed() - 0.60) < 1e-9)
try:
    led.reserve("s2", "model", 0.50); blocked = False
except BudgetExceeded:
    blocked = True
check("budget cap is enforced before the call", blocked)
led.settle(e1, None)
check("missing provider cost keeps the reservation and flags it", app.execute("SELECT cost_uncertain FROM usage_ledger WHERE id=?", (e1,)).fetchone()[0] == 1)

calls = {"model": 0, "search": 0}
def model_stub(stage, state):
    calls["model"] += 1; return ["What is the monthly budget?", "Must data stay on the device?"]
crash = {"armed": True}
def search_stub(q):
    calls["search"] += 1
    if crash["armed"]:
        crash["armed"] = False; raise RuntimeError("simulated provider timeout")
    return ["https://example.com/pricing", "https://example.com/docs"]

app.execute("UPDATE runs SET status='completed' WHERE project_id='P1' AND status='running'")  # one active run per project
app.execute("INSERT INTO runs (id, project_id, thread_id, status, budget_json) VALUES ('R2','P1','P1-R2','running','{}')"); app.commit()
cfg = {"configurable": {"thread_id": "P1-R2"}}
ck1 = sqlite3.connect(tmp / "checkpoints.db", check_same_thread=False)
g1 = build_graph(app, model_stub, search_stub, SqliteSaver(ck1))
out = g1.invoke({"project_id": "P1", "run_id": "R2"}, cfg)
check("run pauses at the clarification interrupt", "__interrupt__" in out)
ck1.close()
ck2 = sqlite3.connect(tmp / "checkpoints.db", check_same_thread=False)
g2 = build_graph(app, model_stub, search_stub, SqliteSaver(ck2))
try:
    g2.invoke(Command(resume={"budget_monthly_usd": 20, "privacy_mode": "cloud_ok"}), cfg); failed = False
except RuntimeError:
    failed = True
check("simulated provider failure surfaces after restart", failed)
final = g2.invoke(None, cfg)
check("run completes when resumed from the checkpoint", final.get("stop_reason") == "complete")
check("clarification model call is not repeated", calls["model"] == 1)
check("no duplicate evidence rows after resume", app.execute("SELECT COUNT(*) FROM sources WHERE project_id='P1'").fetchone()[0] == 2)
check("answers stored as confirmed requirements", app.execute("SELECT COUNT(*) FROM requirements WHERE key='budget_monthly_usd'").fetchone()[0] == 1)

print("\n".join(f"PASS  {n}" for n, ok in results if ok))
print(f"{sum(ok for _, ok in results)}/{len(results)} checks passed")
background_threads.finish()
