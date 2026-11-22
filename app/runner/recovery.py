"""Run lifecycle on one server instance: one active run per project, heartbeats, instance ownership and recovery
on startup. Every run records the worker_id of the process that owns it. A new process recovers every run owned by
another worker at once, whatever the heartbeat says, so a fast restart cannot leave a run stuck as running."""
import json, sqlite3, uuid
from app.core.ledger import BudgetLedger
from app.core.sqlguard import require_guarded


class ActiveRunExists(Exception):
    pass


def new_instance_id() -> str:
    return f"worker-{uuid.uuid4().hex[:12]}"


def start_run(conn, run_id, project_id, thread_id, worker_id, kind="initial", parent_run_id=None, cap_usd=2.5, completion_reserve_usd=0.45, max_model_calls=None, closing_reserve_calls=None):
    """The runner reads [budget].max_model_calls and [extraction].closing_reserve_calls from settings where it reads the cap and passes them here; they are bound to the run in budget_json beside cap_usd and completion_reserve_usd."""
    require_guarded(conn)
    budget = {"cap_usd": cap_usd, "completion_reserve_usd": completion_reserve_usd}
    if max_model_calls is not None and closing_reserve_calls is not None:
        budget.update({"max_model_calls": int(max_model_calls), "closing_reserve_calls": int(closing_reserve_calls)})
    try:
        with conn:
            conn.execute("INSERT INTO runs (id, project_id, thread_id, status, kind, parent_run_id, worker_id, budget_json, heartbeat_at) "
                         "VALUES (?,?,?,'running',?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                         (run_id, project_id, thread_id, kind, parent_run_id, worker_id, json.dumps(budget)))
    except sqlite3.IntegrityError as e:
        raise ActiveRunExists(f"project {project_id} already has an active run") from e


def heartbeat(conn, run_id) -> bool:
    """One beat: heartbeat_at now. Returns whether the row was written."""
    require_guarded(conn)
    with conn:
        cur = conn.execute("UPDATE runs SET heartbeat_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (run_id,))
    return cur.rowcount > 0


LOCKED_NAMES = ("SQLITE_BUSY", "SQLITE_LOCKED")                                                 # the transient lock conditions (another connection writes, or a table lock); sqlite3 maps both to OperationalError


def is_locked(exc) -> bool:
    """A transient lock: SQLITE_BUSY or SQLITE_LOCKED by the error name, or by the message on a build without error names."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    name = getattr(exc, "sqlite_errorname", None)
    if name:
        return name in LOCKED_NAMES
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg or "database table is locked" in msg


def recover_on_startup(conn, instance_id: str, stale_after_s: int = 60) -> list[dict]:
    """Recover runs owned by a previous worker (any heartbeat) and runs of this worker whose heartbeat went stale."""
    require_guarded(conn)
    out = []
    rows = conn.execute("SELECT id, budget_json FROM runs WHERE (status IN ('running','awaiting_user') AND (worker_id IS NULL OR worker_id != ? OR "
                        "(status='running' AND (heartbeat_at IS NULL OR julianday('now') - julianday(heartbeat_at) > ? / 86400.0)))) "
                        "OR (status='interrupted' AND stop_reason IN ('founder_pause','server_restart') AND (worker_id IS NULL OR worker_id != ?) "
                        "AND EXISTS (SELECT 1 FROM usage_ledger u WHERE u.run_id=runs.id AND u.state IN ('planned','sent')))", (instance_id, stale_after_s, instance_id)).fetchall()   # a requested pause of a dead instance whose call never settled is recovered too (its sent call becomes unknown with its possible charge); a quiescent paused run and this instance's own pause are left as they are; the shutdown pause (server_restart, written by the hosted runner of the web application, which is not part of this repository) of a dead instance is taken the same way; a run of this instance that waits for the founder has no heartbeat to go stale; another instance's runs are recovered whatever their state
    for run_id, budget in rows:
        b = json.loads(budget)
        ledger = BudgetLedger(conn, run_id, b.get("cap_usd", 2.0), b.get("completion_reserve_usd", 0.0))
        unknown = ledger.recover_after_crash()                     # every unresolved unknown call, including those found by legacy reconciliation
        possible = ledger.possible_charges()
        billed_lost = float(conn.execute("SELECT COALESCE(SUM(reported_usd),0) FROM usage_ledger WHERE run_id=? AND state='unknown' AND reported_usd IS NOT NULL AND resolution IS NULL", (run_id,)).fetchone()[0])
        saved = saved_work(conn, run_id)
        msg = "The server restarted. Saved results will not run again." + f" Saved: {saved['completed_calls']} completed call(s), {saved['steps']} recorded step(s), {saved['sources']} source(s), {saved['claims']} claim(s)."
        shown = call_details(conn, run_id, ledger.unknown_calls(), float(b.get("cap_usd", 2.0))) if unknown else []
        if unknown:                                                                    # the founder's words; the call ids stay in the details lines and the records
            from app.core import wording as W
            msg = W.interrupted_pause(shown, float(b.get("cap_usd", 2.0)), saved, opening="The server restarted")
            if billed_lost:
                msg += f" ${billed_lost:.3f} was billed for requests whose answer was lost."
        with conn:
            conn.execute("UPDATE runs SET active_seconds=COALESCE(active_seconds,0)+MAX(0, COALESCE((julianday(heartbeat_at)-julianday(active_ticked_at))*86400.0, 0)), active_ticked_at=NULL WHERE id=? AND active_ticked_at IS NOT NULL",
                         (run_id,))                                                   # time driven after the last persisted tick, up to the last heartbeat, is reconciled into the total
            conn.execute("UPDATE runs SET status='interrupted', stop_reason='server_restart' WHERE id=?", (run_id,))
            conn.execute("INSERT INTO run_events (run_id, event, data_json) VALUES (?, 'pause', ?)", (run_id, json.dumps({"message": msg, "reason": "server_restart", "unknown": unknown, "unknown_calls": ledger.unknown_calls(), "possible_usd": possible, "saved": saved, **({"details": [c["details"] for c in shown]} if shown else {})})))   # what was saved and what may have been charged, per call
        out.append({"run_id": run_id, "unknown_calls": unknown, "possible_usd": possible})
    return out


def saved_work(conn, run_id: str) -> dict:
    """What a pause keeps: the run's completed calls and recorded steps, and the project's stored sources and claims."""
    require_guarded(conn)
    pid = (conn.execute("SELECT project_id FROM runs WHERE id=?", (run_id,)).fetchone() or [None])[0]
    q = lambda sql, *a: int(conn.execute(sql, a).fetchone()[0])
    return {"completed_calls": q("SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND state='completed'", run_id),
            "steps": q("SELECT COUNT(*) FROM run_steps WHERE run_id=? AND status='completed'", run_id),
            "sources": q("SELECT COUNT(*) FROM sources WHERE project_id=?", pid) if pid else 0,
            "claims": q("SELECT COUNT(*) FROM claims WHERE project_id=?", pid) if pid else 0}


def call_details(conn, run_id: str, calls: list[dict], cap_usd: float) -> list[dict]:
    """The interrupted calls of a run as the pause shows them: each call of ledger.unknown_calls() with the founder's
    words (app/core/wording.interrupted_call: what happened, the three choices with what each does and costs, retry as the usual choice) and
    its one details line, the only place the call id, the step and the recorded cause (the exception's name) are printed. The cause is the
    call's own decision summary; the amount a retry may cost is the call's reservation, which a new attempt of the same request reserves again."""
    require_guarded(conn)
    from app.core import wording as W
    out = []
    for c in calls:
        row = conn.execute("SELECT reserved_usd FROM usage_ledger WHERE id=?", (c["id"],)).fetchone()
        rec = conn.execute("SELECT observation FROM reasoning_records WHERE run_id=? AND action_id=? ORDER BY id DESC LIMIT 1", (run_id, c["id"])).fetchone()
        cause = str(rec[0]) if rec and rec[0] else "no cause recorded"
        billed = f"; {float(c['billed_usd']):.4f} USD billed" if c.get("billed_usd") else ""
        out.append(dict(c, words=W.interrupted_call(c.get("kind"), cause, float(row[0]) if row else float(c.get("possible_charge_usd") or 0.0), cap_usd),
                        details=f"Details: {c.get('kind')} call {c['id']} of step {c['step_key']}; {cause}; possibly charged up to {float(c.get('possible_charge_usd') or 0.0):.4f} USD{billed}"))
    return out


def interrupt_for_unknown(conn, run_id: str, worker_id: str | None, detail: str = "") -> dict:
    """A send that ended unknown pauses the run for the founder's choice instead of ending it. One BEGIN
    IMMEDIATE: the run, still running and owned by this worker, becomes interrupted with stop_reason unknown_call, and the pause event
    names the unknown calls with their possible charges and what was saved. Returns the stored status and the unknown calls."""
    require_guarded(conn)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status, worker_id, budget_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or row[0] not in ("running", "awaiting_user") or (worker_id is not None and row[1] != worker_id):
            conn.rollback()
            return {"changed": False, "status": row[0] if row else None, "unknown_calls": []}
        b = json.loads(row[2])
        ledger = BudgetLedger(conn, run_id, float(b.get("cap_usd", 2.5)), float(b.get("completion_reserve_usd", 0.0)))
        ledger.mark_sent_unknown()                                               # the graph has returned: a send still 'sent' belongs to a thread that ended; it is unknown and resolvable
        calls = ledger.unknown_calls(); possible = ledger.possible_charges(); saved = saved_work(conn, run_id)
        from app.core import wording as W
        shown = call_details(conn, run_id, calls, float(b.get("cap_usd", 2.5)))
        msg = W.interrupted_pause(shown, float(b.get("cap_usd", 2.5)), saved)              # the founder's words; the call ids, the step and the exception's name stay in the details lines and the records (the call list of this event, the error row below)
        conn.execute("UPDATE runs SET status='interrupted', stop_reason='unknown_call' WHERE id=?", (run_id,))
        conn.execute("INSERT INTO run_events (run_id, event, data_json) VALUES (?, 'pause', ?)", (run_id, json.dumps({"message": msg, "reason": "unknown_call", "criterion": "I3", "unknown": [c["id"] for c in calls], "unknown_calls": calls, "possible_usd": possible, "saved": saved, "details": [c["details"] for c in shown], "detail": detail})))
        conn.execute("INSERT INTO errors (run_id, step_key, category, detail) VALUES (?,?,?,?)", (run_id, calls[0]["step_key"] if calls else run_id, "provider_transient", f"unknown_call: {detail}"[:500]))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return {"changed": True, "status": "interrupted", "unknown_calls": calls, "possible_usd": possible}


CHOICES = {"retry": "retried", "continue": "skipped", "reconcile": "reconciled"}


class ChoiceRefused(ValueError):
    pass


def resolve_call(conn, run_id: str, entry_id: str, choice: str, reported_usd: float | None = None, provider_figure=None) -> dict:
    """The founder's choice on one interrupted call of a run. retry: the attempt stays unknown with resolution retried and
    its possible charge counted; the next drive sends attempt 2. continue: resolution skipped; nothing is resent and the stage proceeds
    without the call's output. reconcile: the provider's figure replaces the possible charge (reported_usd when the founder gives it;
    otherwise provider_figure(generation_id) when the row carries one; zero when the provider has no record), the row is completed
    without a stored result, and the next drive sends the step again as a new attempt. The choice is recorded as a decision summary
    against the call's id. Refused for a call that is not an unresolved unknown call of this run."""
    require_guarded(conn)
    from app.core import react
    if choice not in CHOICES:
        raise ChoiceRefused(f"choice {choice!r}: retry, continue or reconcile")
    row = conn.execute("SELECT run_id, step_key, kind, state, resolution, reserved_usd, reported_usd, generation_id FROM usage_ledger WHERE id=?", (entry_id,)).fetchone()
    if not row or row[0] != run_id:
        raise ChoiceRefused(f"{entry_id} is not a call of run {run_id}")
    if row[3] != "unknown" or row[4] is not None:
        raise ChoiceRefused(f"{entry_id} is {row[3]}{' (' + row[4] + ')' if row[4] else ''}; only an unresolved unknown call takes a choice")
    b = json.loads(conn.execute("SELECT budget_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])
    ledger = BudgetLedger(conn, run_id, float(b.get("cap_usd", 2.5)), float(b.get("completion_reserve_usd", 0.0)))
    resolution = CHOICES[choice]; figure = None; source = None
    if resolution == "reconciled":
        if reported_usd is not None:
            figure, source = float(reported_usd), "the founder's figure"
        elif row[7] and provider_figure is not None:
            got = provider_figure(row[7])
            figure, source = (float(got), f"the provider's figure for {row[7]}") if got is not None else (0.0, f"no provider record for {row[7]}")
        else:
            figure, source = 0.0, "no generation id: the provider has no record to ask for"
    ledger.resolve_unknown(entry_id, resolution, reported_usd=figure)
    obs = {"retried": f"attempt stays unknown; possible charge ${float(row[5]):.4f} counted; a new attempt follows",
           "skipped": f"possible charge ${float(row[5]):.4f} stays counted; the step proceeds without this call; nothing resent",
           "reconciled": f"possible charge ${float(row[5]):.4f} replaced by ${figure or 0:.6f} ({source}); the step is sent again as a new attempt"}[resolution]
    react.record(conn, run_id, row[1], entry_id, "founder's choice on an interrupted call", f"choice {choice}", obs, f"resolution {resolution}")
    return {"id": entry_id, "step_key": row[1], "kind": row[2], "resolution": resolution, "possible_charge_usd": float(row[5]) if resolution != "reconciled" else 0.0, "reported_usd": figure, "source": source, "remaining_unknown": ledger.unknown_calls()}


def resume_run(conn, run_id, worker_id):
    """Take ownership and continue (the caller resolves unknown calls first)."""
    require_guarded(conn)
    with conn:
        conn.execute("UPDATE runs SET status='running', worker_id=?, heartbeat_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status='interrupted'", (worker_id, run_id))


class RunStopped(Exception):
    pass


def cancel_run(conn, run_id: str, reason: str) -> bool:
    """Durable stop of the shared run (any worker): status cancelled with the reason; every later dispatch is rejected
    by assert_run_active. Used for HTTP 402 (credit_exhausted) by the gateway and the tool runner."""
    require_guarded(conn)
    if conn.in_transaction:
        conn.commit()
    with conn:
        cur = conn.execute("UPDATE runs SET status='cancelled', stop_reason=?, ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status IN ('running','awaiting_user')", (reason, run_id))
    return cur.rowcount > 0


TERMINAL = ("completed", "partial", "failed")


def finish_run(conn, run_id: str, status: str, stop_reason: str, done_payload: dict | None = None, worker_id: str | None = None) -> dict:
    """The one application path that closes a run driven to its end (the runner after a drive, the measurement harness after
    a measurement): one BEGIN IMMEDIATE transaction that re-reads the run row and counts its planned, sent and unknown
    calls under the write lock, decides (a run with an open call closes partial with the reason extended by the open-call
    count; an already terminal run changes nothing), updates the row, inserts the done event with the stored status and
    reason only when the row changed, and commits. status is completed, partial or failed. Returns the stored row's status
    and stop_reason and whether the row changed. A dispatch claim takes the same lock (claim_dispatch), so it is either
    counted before the close or refused after it. With worker_id (the runner's instance id) a row that names
    another worker is left unchanged: a run reclaimed and resumed elsewhere is that worker's to close."""
    require_guarded(conn)
    if status not in TERMINAL:
        raise ValueError(f"finish_run: {status!r} is not a terminal status")
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status, stop_reason, worker_id FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or row[0] not in ("running", "awaiting_user") or (worker_id is not None and row[2] != worker_id):
            conn.rollback()
            return {"changed": False, "status": row[0] if row else None, "stop_reason": row[1] if row else None}
        open_calls = int(conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND (state IN ('planned','sent') OR (state='unknown' AND resolution IS NULL))", (run_id,)).fetchone()[0])   # an unknown call the founder resolved (retried, skipped, reconciled) is decided; its possible charge stays in the ledger and the spend report, not in the run's status
        if open_calls and status == "completed":
            status, stop_reason = "partial", f"{stop_reason}; {open_calls} call(s) planned, sent or unknown"
        conn.execute("UPDATE runs SET status=?, stop_reason=?, ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (status, stop_reason, run_id))
        conn.execute("INSERT INTO run_events (run_id, event, data_json) VALUES (?, 'done', ?)", (run_id, json.dumps({**(done_payload or {}), "status": status, "stop_reason": stop_reason}, default=str)))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    on_run_finished({"id": run_id, "status": status, "stop_reason": stop_reason})                       # the one hook of a finished run
    return {"changed": True, "status": status, "stop_reason": stop_reason}


def on_run_finished(run: dict) -> None:
    """The hook of a finished run: called once where finish_run has written the done event and committed,
    with the run's id, status and stop_reason. It does nothing else yet: a "plan ready" email, a webhook or a billing debit attaches
    here later, observing a finished run without touching the pipeline or the evidence chain."""
    return None


def assert_run_active(conn, run_id: str) -> None:
    """Shared dispatch guard for model and tool sends: nothing starts unless the run is running."""
    require_guarded(conn)
    row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row or row[0] != "running":
        raise RunStopped(f"run {run_id} is {row[0] if row else 'missing'}; no new request may start")


def answer_question(conn, question_id: str, answer) -> None:
    """Store the founder's answer on the exact paused question and set the run back to running."""
    require_guarded(conn)
    import json as _json
    with conn:
        conn.execute("UPDATE questions SET status='answered', answer_json=? WHERE id=?", (_json.dumps(answer), question_id))
        conn.execute("UPDATE tool_calls SET state='completed', result_json=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=(SELECT tool_call_id FROM questions WHERE id=?) AND state='paused'",
                     (_json.dumps({"question_id": question_id, "answer": answer}), question_id))                     # the paused observation completes in the same transaction as the answer
        conn.execute("UPDATE runs SET status='running' WHERE id=(SELECT run_id FROM tool_calls WHERE id=(SELECT tool_call_id FROM questions WHERE id=?)) AND status='awaiting_user'", (question_id,))
