"""Run events: one row per observable step of a run (run_events), rendered to escaped HTML partials by the event stream of the web application (not part of this repository).
Kinds: step, tool, claim, decision, budget, pause, done, intervention. Payloads hold ids, labels, costs and messages;
never prompts, keys or page text."""
import json, sqlite3
from datetime import datetime, timezone
from app.core.sqlguard import require_guarded

GLYPHS = {"clarify": "?", "research_plan": "~", "extract_evidence": "#", "verify": "=", "compare_options": "<>", "plan_generate": "*"}


def emit(conn: sqlite3.Connection, run_id: str, event: str, data: dict) -> int:
    """Inside a caller's transaction the row joins it; otherwise its own BEGIN IMMEDIATE, so the write lock is taken up front
    and the busy timeout applies (a deferred transaction that must upgrade after the heartbeat thread committed fails at once)."""
    require_guarded(conn)
    def body():
        cur = conn.execute("INSERT INTO run_events (run_id, event, data_json) VALUES (?,?,?)", (run_id, event, json.dumps(data, default=str)))
        return cur.lastrowid
    if conn.in_transaction:
        return body()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rid = body(); conn.commit(); return rid
    except Exception:
        conn.rollback(); raise


def step(conn, run_id, stage: str, label: str, detail: str = "", cost_usd: float | None = None, **extra) -> int:
    """extra: structured fields beside the rendered label and detail, such as the evidence packs' included and omitted claim ids; ids and counts only, never text."""
    require_guarded(conn)
    return emit(conn, run_id, "step", {"time": datetime.now(timezone.utc).strftime("%H:%M:%S"), "glyph": GLYPHS.get(stage, "-"), "stage": stage, "label": label,
                                        "detail": detail[:300], "cost": f"${cost_usd:.3f}" if cost_usd is not None else "", **extra})


def budget(conn, run_id, ledger, searches: int, pages: int) -> int:
    require_guarded(conn)
    rep = ledger.spend_report()
    return emit(conn, run_id, "budget", {"spent_usd": round(ledger.committed(), 4), "cap_usd": ledger.cap, "searches": searches, "pages": pages,
                                          "possible_usd": rep["possible_charges_usd"], "billed_usd": rep["billed_usd"]})


def events(conn, run_id: str) -> list[tuple[int, str, dict]]:
    require_guarded(conn)
    return [(r[0], r[1], json.loads(r[2])) for r in conn.execute("SELECT id, event, data_json FROM run_events WHERE run_id=? ORDER BY id", (run_id,))]


def tool(conn, run_id, tool_call_id: str, tool_name: str, state: str, summary: str = "", engine: str | None = None, query: str | None = None) -> int:
    require_guarded(conn)
    data = {"tool_call_id": tool_call_id, "tool": tool_name, "state": state, "summary": summary[:120]}
    if engine:                                                                      # the engine that ran a search (exa, perplexity, parallel) and the query, for the hive on the run screen
        data["engine"] = engine
    if query:
        data["query"] = query[:120]
    return emit(conn, run_id, "tool", data)


def tool_events_missing(conn, run_id: str) -> list[str]:
    """Tool calls of the run without a tool event; empty when every call was announced."""
    require_guarded(conn)
    announced = set()
    for (data,) in conn.execute("SELECT data_json FROM run_events WHERE run_id=? AND event='tool'", (run_id,)):
        announced.add(json.loads(data).get("tool_call_id"))
    return [r[0] for r in conn.execute("SELECT id FROM tool_calls WHERE run_id=? ORDER BY created_at, n", (run_id,)) if r[0] not in announced]


def announce_missing(conn, run_id: str) -> int:
    """Emit a tool event for every tool call that has none (a stage that stopped before announcing its calls); returns the count."""
    require_guarded(conn)
    n = 0
    for tid in tool_events_missing(conn, run_id):
        r = conn.execute("SELECT tool, state, error FROM tool_calls WHERE id=?", (tid,)).fetchone()
        tool(conn, run_id, tid, r[0], r[1], r[2] or ""); n += 1
    return n
