"""Safety plan in code: hazard register, observable intervention signals and a stop control that prevents new work,
preserves completed results and accounts honestly for calls in flight."""
import json, sqlite3
from app.core.ledger import BudgetLedger
from app.core.sqlguard import require_guarded

HAZARDS = [
    dict(hazard="Unintended spending", guardrail="reservation before every call, cap, completion reserve, revision cap, 402 stops", signal="committed + estimate above cap", test="T3, T18", intervention="run stops with a partial plan; founder may raise the cap"),
    dict(hazard="Leakage through search queries", guardrail="code sanitises queries; queries shown in the timeline; data map", signal="private term list match, founder report", test="T9, T22", intervention="founder reviews queries before a confidential run"),
    dict(hazard="Injected instructions in pages", guardrail="pages are data; injection fixtures; page excluded and flagged", signal="instruction-like text detector", test="T9", intervention="founder sees the flag; no automatic action"),
    dict(hazard="Fabricated or out-of-scope citations", guardrail="exact-quote check; scope and condition verification", signal="quote not found; scope_matches_claim false", test="T11", intervention="claim stays unsupported; hard constraints unresolved"),
    dict(hazard="Recommendations that violate requirements", guardrail="hard-constraint rule; deterministic checks in the tree", signal="unsupported hard requirement", test="T4, T25", intervention="a proven violation rejects the option; a requirement the evidence is silent on leaves a conditional recommendation (D-A408); disagreeing sources leave the decision open (D-A409)"),
    dict(hazard="Accidental deletion", guardrail="confirmation lists what is removed; active runs block deletion", signal="delete request with an active run", test="T14, T21", intervention="founder confirms or stops the run first"),
    dict(hazard="Unbounded loops", guardrail="per-stage tool limit, run limits, model-call limit, tree bounds", signal="counter at limit", test="T24, T25", intervention="stage ends with a partial result"),
    dict(hazard="Interrupted calls", guardrail="call states; unknown calls never resent automatically", signal="unknown call present", test="T10, T17", intervention="founder retries, continues or reconciles"),
]
INTERVENTION_RULES = [                                       # (signal, why, the product's actual action on it, stated per signal)
    ("unsupported_hard_requirements", "a hard requirement has no supported, in-scope claim", "the decisions on it are recommended as not yet proven on it, and the plan says so"),
    ("unresolved_contradictions", "sources contradict each other on a decision-relevant fact", "both sources are kept and the conflict is exposed under the unresolved checks"),
    ("ambiguous_scope", "a requirement change does not say where it applies", "the run pauses on the scope question for the founder's answer"),
    ("cap_reached", "the spend cap or a run limit was reached", "research stops and the plan is drafted from the evidence gathered"),
    ("interrupted_calls", "a call was interrupted and may have been charged", "the run pauses for the founder's choice: retry, continue without, reconcile"),
    ("loop_limit", "a stage reached its tool or model-call limit", "the stage ends with a partial result"),
]


def interventions(signals: dict) -> list[dict]:
    """Observable signals only; a model's self-reported confidence is never a trigger."""
    out = []
    for key, why, action in INTERVENTION_RULES:
        v = signals.get(key)
        if v and (not isinstance(v, (int, float)) or v > 0):
            out.append({"signal": key, "why": why, "action": action})
    return out


class StopController:
    def __init__(self, conn: sqlite3.Connection, run_id: str, ledger: BudgetLedger):
        require_guarded(conn)
        self.conn, self.run_id, self.ledger = conn, run_id, ledger

    def may_start_work(self) -> bool:
        row = self.conn.execute("SELECT status FROM runs WHERE id=?", (self.run_id,)).fetchone()
        return bool(row) and row[0] == "running"

    def stop(self, reason: str = "founder_stop") -> dict:
        """No new work after this; completed results stay; calls in flight become unknown with possible charges. The
        row is closed (ended_at), the tool rows follow their ledger entries, and a pause event names the reason, the calls marked
        unknown with their possible charges and what was kept; a run already ended is reported as it is (stopped False)."""
        import json
        with self.conn:
            cur = self.conn.execute("UPDATE runs SET status='cancelled', stop_reason=?, ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status IN ('running','awaiting_user','interrupted')", (reason, self.run_id))   # a resumable interrupted run (the unknown-call or restart pause) is stopped too; a terminal run stays as it is (stopped False)
        stopped = cur.rowcount > 0
        unknown = self.ledger.recover_after_crash() if stopped else []
        completed = self.conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND state='completed'", (self.run_id,)).fetchone()[0]
        calls = [c for c in self.ledger.unknown_calls() if c["id"] in unknown]
        possible = self.ledger.possible_charges()
        if stopped:
            msg = f"Run stopped ({reason}): no new work starts; {completed} completed call(s) and their results are kept; {len(unknown)} call(s) in flight marked unknown" + (f" ({', '.join(unknown)}), possibly charged up to ${possible:.2f}" if unknown else "") + "."
            with self.conn:
                self.conn.execute("INSERT INTO run_events (run_id, event, data_json) VALUES (?, 'pause', ?)", (self.run_id, json.dumps({"message": msg, "reason": reason, "unknown": unknown, "unknown_calls": calls, "possible_usd": possible, "completed_calls_kept": completed})))
        return {"stopped": stopped, "completed_calls_kept": completed, "in_flight_marked_unknown": unknown, "unknown_calls": calls, "possible_usd": possible}

    def pause(self, reason: str = "founder_pause") -> dict:
        """The smaller sibling of stop. The running run becomes interrupted with this reason, so the next dispatch claim finds it
        and refuses (no new work after the current step); the call in flight finishes and keeps its result; nothing is marked unknown and
        nothing ends: the run is resumable (app/runner/runner.py resume). A pause event says so; a run that is not running is reported as
        it is (paused False)."""
        import json
        from app.runner.recovery import saved_work
        with self.conn:
            cur = self.conn.execute("UPDATE runs SET status='interrupted', stop_reason=? WHERE id=? AND status='running'", (reason, self.run_id))
        paused = cur.rowcount > 0
        if paused:
            saved = saved_work(self.conn, self.run_id)
            msg = f"Run paused ({reason}): no new work starts after the current step; {saved['completed_calls']} completed call(s) and their results are kept; resume when ready."
            with self.conn:
                self.conn.execute("INSERT INTO run_events (run_id, event, data_json) VALUES (?, 'pause', ?)", (self.run_id, json.dumps({"message": msg, "reason": reason, "resumable": True, "saved": saved})))
        return {"paused": paused, "status": (self.conn.execute("SELECT status FROM runs WHERE id=?", (self.run_id,)).fetchone() or [None])[0]}


def observable_signals(conn: sqlite3.Connection, project_id: str, run_id: str, ledger: BudgetLedger, stop_reason: str | None = None) -> dict:
    """The intervention signals (the keys of INTERVENTION_RULES) read from stored records after a stage, never from anything the
    model says about itself: hard requirements without a counting support record (the coverage accessor), claims verified contradicted,
    open questions of a revision run's scope, a cap or run-limit stop, unresolved unknown calls, and tool requests rejected at a limit."""
    require_guarded(conn)
    from app.core import coverage
    version = int(conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone()[0])
    hard = [r[0] for r in conn.execute("SELECT id FROM requirements WHERE project_id=? AND hard=1 AND status='confirmed'", (project_id,))]
    verified = conn.execute("SELECT 1 FROM run_steps WHERE run_id=? AND node IN ('verify', 'verify_group') AND status='completed' LIMIT 1", (run_id,)).fetchone() is not None
    unsupported = [rid for rid in hard if not coverage.requirement_support(conn, project_id, rid, version) and "entails" not in coverage.calculated_requirement(conn, project_id, rid, version)] if verified else []   # a budget the calculation found an option within is no unsupported requirement; measurable once this run has verified something; before that every hard requirement is trivially without support
    with coverage.derived(conn, project_id, run_id=run_id):                                   # the contradiction signal is the open source conflicts (two supported claims that disagree; app/core/reconcile.py keeps claim_conflicts current), read through the derived-state accessor, with the decision-relevance rule: a conflict counts when a decision cites one of its claims
        cited = {r[0] for r in conn.execute("SELECT from_id FROM trace_edges WHERE project_id=? AND kind='claim_decision'", (project_id,))}
        contradicted = sum(1 for (cj,) in conn.execute("SELECT claim_ids_json FROM claim_conflicts WHERE project_id=? AND status='open'", (project_id,)) if cited.intersection(json.loads(cj)))
    kind = (conn.execute("SELECT kind FROM runs WHERE id=?", (run_id,)).fetchone() or ["initial"])[0]
    open_scope = int(conn.execute("SELECT COUNT(*) FROM questions WHERE project_id=? AND status='open' AND tool_call_id IS NULL", (project_id,)).fetchone()[0]) if kind == "revision" else 0
    limits = int(conn.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND state='rejected' AND error LIKE '%limit%'", (run_id,)).fetchone()[0])
    return {"unsupported_hard_requirements": len(unsupported), "unresolved_contradictions": contradicted, "ambiguous_scope": open_scope,
            "cap_reached": 1 if stop_reason in ("budget_reserve_reached", "time_limit", "call_limit", "credit_exhausted") else 0,
            "interrupted_calls": len(ledger.unknown_calls()), "loop_limit": limits, "detail": {"unsupported": unsupported}}
