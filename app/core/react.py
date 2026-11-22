"""Observable decision-action-observation loop. Each model send and each tool request writes a structured decision
summary linked to its durable action id (ledger entry or tool call id); the next action is chosen by a deterministic
policy from observable signals. Trace completeness requires a nonempty summary for every action."""
import sqlite3
from app.core.sqlguard import require_guarded

RECOVERY = [
    ("Missing or inadequate evidence", "another source (refined search), then a clarification, then an explicit partial result", "searches left, then one question, then partial"),
    ("Malformed output", "one repair with the same model, then flag", "1 repair"),
    ("Contradictory sources", "keep both, prefer the newer official source, expose the conflict under Unresolved checks", "no silent choice"),
    ("Prompt injection in a page", "exclude the page from evidence, flag it, continue", "0 obeyed instructions"),
    ("Provider failure", "one fallback to the other vendor; 402 stops the run", "1 fallback"),
    ("Exhausted budget", "stop research, draft the plan from the evidence gathered, state the reason", "partial plan"),
]


def next_action(obs: dict, state: dict) -> str:
    if obs.get("budget_exhausted"):
        return "stop_partial"
    if obs.get("malformed"):
        return "repair" if state.get("repairs_used", 0) < 1 else "flag"
    if obs.get("provider_failed"):
        return "fallback" if not state.get("fallback_used") else "fail"
    if obs.get("injection"):
        return "exclude_source"
    if obs.get("contradiction"):
        return "expose_conflict"
    if obs.get("sufficient") is False:
        if state.get("searches_left", 0) > 0:
            return "search_more"
        if state.get("can_ask", False):
            return "ask_user"
        return "partial_result"
    return "proceed"


def record_sql(run_id: str, step_key: str, action_id: str, purpose: str, action: str, observation: str, change: str) -> tuple:
    """The summary as one statement, so a caller can write it in the same transaction as the settlement or failure of the
    action it describes: a crash right after that commit never leaves a settled call without
    its summary. The action id must be the real ledger entry or tool call id, never a placeholder."""
    if not all(x and str(x).strip() for x in (action_id, purpose, action, observation, change)):
        raise ValueError("a decision summary needs a nonempty action id, purpose, action, observation and change")
    return ("INSERT INTO reasoning_records (run_id, step_key, action_id, purpose, action, observation, change) VALUES (?,?,?,?,?,?,?)",
            (run_id, step_key, action_id, purpose[:200], action[:200], observation[:300], change[:200]))


def record(conn: sqlite3.Connection, run_id: str, step_key: str, action_id: str, purpose: str, action: str, observation: str, change: str) -> None:
    require_guarded(conn)
    sql, params = record_sql(run_id, step_key, action_id, purpose, action, observation, change)
    if conn.in_transaction:
        conn.execute(sql, params); return
    conn.execute("BEGIN IMMEDIATE")                          # the write lock up front: the busy timeout applies beside the heartbeat thread
    try:
        conn.execute(sql, params); conn.commit()
    except Exception:
        conn.rollback(); raise


def required_actions(conn: sqlite3.Connection, run_id: str) -> set[str]:
    """Every billing attempt and every tool call (including rejected, paused, failed and unknown ones)."""
    require_guarded(conn)
    ids = {r[0] for r in conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND kind='model'", (run_id,))}
    ids |= {r[0] for r in conn.execute("SELECT id FROM tool_calls WHERE run_id=?", (run_id,))}
    return ids


def trace_complete(conn: sqlite3.Connection, run_id: str) -> dict:
    """True only when every required action has a summary with all four fields nonempty."""
    require_guarded(conn)
    needed = required_actions(conn, run_id)
    ok = {r[0] for r in conn.execute("SELECT action_id FROM reasoning_records WHERE run_id=? AND length(trim(purpose))>0 AND length(trim(action))>0 "
                                     "AND length(trim(observation))>0 AND length(trim(change))>0", (run_id,))}
    missing = sorted(needed - ok)
    return {"complete": not missing, "required": len(needed), "missing": missing}
