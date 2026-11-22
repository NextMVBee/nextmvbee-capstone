"""Spend control with call states. Application code, not the model, enforces the cap.

Every external call (model, search, contents) gets one ledger row per attempt, keyed step_key:kind:attempt.
States: planned -> sent -> completed | failed | unknown.
Reservations run inside BEGIN IMMEDIATE, so concurrent callers are serialised at the database write lock;
every new send gets a new attempt number (next_attempt); a completed attempt stores its validated output, and a
re-run replays that output instead of sending again; an unknown attempt keeps counting as a possible charge even after
the founder chooses to retry it. Tested offline (tests/test_audit.py, tests/test_concepts.py, tests/test_workflow.py and
scenario rk6 of tests/test_reuse_keys.py).
"""
import sqlite3
import time
from app.core.sqlguard import require_guarded

EXA_USD = {"search_base": 0.007, "extra_result": 0.001, "summary": 0.001, "contents_per_type": 0.001}  # exa.ai/docs/reference/pricing, Oct 2026


class BudgetExceeded(Exception):
    pass


LOCK_WAIT_S = 1800.0   # how long a transition that records a call already made waits out the store lock (the run's 30-minute no-progress rule)


def wait_out_lock(fn, *args, **kwargs):
    """A reply in hand is never lost to the store lock. fn writes the outcome of a call already made (a settlement,
    a failure, an unknown, a tool row, the pages a tool fetched); while another connection holds the write lock past the busy timeout (a parallel
    worker's per-source derive) fn is retried whole, as each of its transactions rolls back on the error. A lock held past LOCK_WAIT_S is a stuck
    store, not another worker's store work: the error is raised and the row stays sent, a possible charge (never a lost one)."""
    from app.runner.recovery import is_locked                                     # recovery imports this module
    t0, waits = time.monotonic(), 0
    while True:
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if not is_locked(e) or time.monotonic() - t0 > LOCK_WAIT_S:
                raise
            waits += 1
            time.sleep(min(2.0, 0.05 * waits))


class AllowanceUsed(Exception):
    """The one repair or the one fallback of this stage instance has already been claimed."""


class DispatchRefused(Exception):
    """The run is no longer running: a cancellation landed before this dispatch was claimed."""


class Replay(Exception):
    """Another caller completed this step meanwhile: replay its stored result, reserve and send nothing."""
    def __init__(self, entry_id: str, result_json: str):
        super().__init__(f"replay {entry_id}"); self.entry_id, self.result_json = entry_id, result_json


class InFlight(Exception):
    """Another caller has this step's request in flight (state sent): wait for its result instead of sending again."""
    def __init__(self, entry_id: str):
        super().__init__(f"{entry_id} is in flight"); self.entry_id = entry_id


class CallLimitRefused(Exception):
    """The model-call limit refused this dispatch. reason: headroom (an extract or verify send, handled in the
    node), call_limit (any other send: the run stop with the draft permitted), unknown stage (a step key that does not parse
    or names a stage outside KNOWN_STAGES), or budget_json without call limits (a run row bound without the two values)."""
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason); self.reason, self.detail = reason, detail


class UnknownCallPending(Exception):
    pass


def estimate_tokens(messages) -> int:
    """Rough upper estimate of prompt tokens from the complete billable request: message content, assistant tool_calls
    (names and arguments) and tool results, about 3.5 characters per token plus per-message overhead."""
    import json
    chars = 0
    for m in messages:
        chars += len(str(m.get("content") or ""))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            chars += len(str(fn.get("name", ""))) + len(str(fn.get("arguments", "")))
        chars += len(str(m.get("tool_call_id") or ""))
    return int(chars / 3.5) + 16 * max(1, len(messages))


EXA_SAFETY = 1.15   # every Exa reservation carries the same margin as model calls; the request price is the estimate, the provider's figure settles

SEND_TIMEOUT_S = {"model": 300, "search": 60, "contents": 60, "production": 1320}   # the bound of one send per class, in code: twice the longest completed send measured for the class, rounded up to the next full minute (model 140.48 s, search 14.46 s, contents 12.12 s, production 644.029 s). The bound is the elapsed time of the send (send_within), not the SDK's inactivity timeout; a send past it is an unknown call with its possible charge.


class SendDeadline(Exception):
    """The elapsed time of one send passed its class's bound: the request may have reached the provider, so the caller marks the call unknown."""


def send_within(fn, seconds: float):
    """Run one send (fn, no arguments) and return its result, or raise SendDeadline once `seconds` of elapsed time have passed.
    The bound is the elapsed time of the send. The SDK's numeric timeout is an inactivity timeout and does not bound it: a reply
    trickling below that timeout completes late. The send runs in a daemon thread that touches no database handle; a reply arriving after the deadline is
    discarded, the row having been marked unknown by the caller (its possible charge counted until the founder decides)."""
    import threading
    box = {}
    def run():
        try:
            box["value"] = fn()
        except BaseException as e:                                                   # noqa: BLE001 (re-raised in the caller's thread below)
            box["error"] = e
    t = threading.Thread(target=run, name="send_within", daemon=True)
    t.start(); t.join(float(seconds))
    if t.is_alive():
        raise SendDeadline(f"no reply within the {float(seconds):.0f} s send bound")
    if "error" in box:
        raise box["error"]
    return box["value"]


ALLOWANCE_SQL = "(sent_at IS NOT NULL OR state IN ('planned','sent')) AND COALESCE(resolution,'') NOT IN ('retried','reconciled')"   # the one repair or fallback of a stage instance counts an attempt claimed, in flight or sent (never a reservation released unsent), unless the founder's explicit retry or reconcile of an unknown attempt selected a new attempt in its place (the old attempt keeps its possible or reconciled charge)


def counts_toward_allowance(state: str, sent_at, resolution) -> bool:
    """The Python form of ALLOWANCE_SQL, for the verify protocol's state and forecast: one predicate everywhere."""
    return (sent_at is not None or state in ("planned", "sent")) and (resolution or "") not in ("retried", "reconciled")


def estimate_request_tokens(messages, response_schema=None, tools=None) -> int:
    """The complete billable request: messages (content, tool calls, tool results), the response_format schema and the
    tool schemas, which the provider also tokenises."""
    import json
    extra = 0
    if response_schema is not None:
        extra += len(json.dumps(response_schema, separators=(",", ":")))
    if tools:
        extra += len(json.dumps(tools, separators=(",", ":")))
    return estimate_tokens(messages) + int(extra / 3.5)


def estimate_model_usd(prompt_tokens: int, max_output_tokens: int, price_in_per_m: float, price_out_per_m: float, safety: float = 1.15) -> float:
    """Upper bound on output cost (max_tokens is sent with the request) and an estimate with margin for input cost."""
    return round((prompt_tokens * price_in_per_m + max_output_tokens * price_out_per_m) / 1e6 * safety, 6)


def estimate_exa_search_usd(num_results: int = 10, summaries: int = 0) -> float:
    return round(EXA_USD["search_base"] + max(0, num_results - 10) * EXA_USD["extra_result"] + summaries * EXA_USD["summary"], 6)


def estimate_exa_contents_usd(pages: int, content_types: int = 1, summaries: int = 0) -> float:
    return round(pages * content_types * EXA_USD["contents_per_type"] + summaries * EXA_USD["summary"], 6)


OPENING_STAGES = ("clarify", "research_plan", "discover", "extract_evidence", "verify")      # clause 3 of call_allowance: below the limit minus the call reserve minus the closing reserve; the discovery passes open the research
EVIDENCE_STAGES = ("extract_evidence", "verify")                                   # a refusal of these carries the reason headroom and is handled in the node
CLOSING_STAGES = ("compare_options", "tree", "revise", "coverage", "enrich", "verify_plan", "panel", "editor")   # clause 2 of call_allowance: below the limit minus the call reserve (the plan stage's); the coverage step, the planner enrichment calls, the panel check and the Editor are closing stages too
PLAN_STAGE = "plan_generate"                                                       # every plan send is finishing today: clause 1, below the limit; a plan send that is not finishing falls under clause 2
KNOWN_STAGES = OPENING_STAGES + CLOSING_STAGES + (PLAN_STAGE,)


def stage_of_key(step_key: str) -> str | None:
    """The stage of a step key <run>:<stage>:<worker>:<branch>[:more]; None when the key does not parse (the call limit refuses it)."""
    parts = str(step_key).split(":")
    return parts[1] if len(parts) >= 4 and all(parts[:4]) else None


def run_spend(conn: sqlite3.Connection, run_id: str) -> dict:
    """A run's spend from its ledger rows: billed (provider-reported, real providers), simulated (stub provider), estimates still held
    (planned or sent), possible charges (unknown) and reservations released on failure, kept apart. The runner's spend_report and the
    export snapshot's parent-run figures read it alike, so a parent run's spend is never a literal zero."""
    require_guarded(conn)
    q = lambda sql: float(conn.execute(sql, (run_id,)).fetchone()[0] or 0)   # noqa: E731
    return {"billed_usd": round(q("SELECT SUM(reported_usd) FROM usage_ledger WHERE run_id=? AND reported_usd IS NOT NULL AND state IN ('completed','unknown') AND COALESCE(provider,'')<>'stub'"), 6),   # a reported cost stays billed even if the observation was lost
            "estimated_unreported_usd": round(q("SELECT SUM(reserved_usd) FROM usage_ledger WHERE run_id=? AND state='completed' AND reported_usd IS NULL"), 6),
            "held_estimates_usd": round(q("SELECT SUM(reserved_usd) FROM usage_ledger WHERE run_id=? AND state IN ('planned','sent')"), 6),
            "possible_charges_usd": round(q("SELECT SUM(reserved_usd) FROM usage_ledger WHERE run_id=? AND state='unknown' AND reported_usd IS NULL"), 6),   # never both for one row
            "simulated_usd": round(q("SELECT SUM(reserved_usd) FROM usage_ledger WHERE run_id=? AND provider='stub'"), 6),   # what a stub call would have cost; settled at $0
            "released_on_failure_usd": round(q("SELECT SUM(reserved_usd) FROM usage_ledger WHERE run_id=? AND state='failed'"), 6)}


class BudgetLedger:
    def __init__(self, conn: sqlite3.Connection, run_id: str, cap_usd: float, completion_reserve_usd: float = 0.0, max_model_calls: int | None = None, closing_reserve_calls: int | None = None, owner: str | None = None,
                 tree_calls_per_decision: int | None = None, tree_decisions_per_run: int | None = None):
        """cap_usd: the run's spend cap; completion_reserve_usd: the part of the cap kept for finishing sends; max_model_calls and
        closing_reserve_calls are the two call-limit values bound to the run in runs.budget_json at start_run and passed by the runner; the ledger reads no settings.
        Both None: no call-limit classification (the measurement harness's own ledgers and offline suites that bind no limits); a
        run row bound without the two keys is given max_model_calls 0 by the runner, and every model dispatch is refused.
        owner: the worker id of the runner driving the run; with it, a claim is refused when runs.worker_id names
        another worker (the run was reclaimed and resumed elsewhere), inside the same transaction that reads the status."""
        require_guarded(conn)
        self.conn, self.run_id, self.cap, self.completion_reserve, self.owner = conn, run_id, cap_usd, completion_reserve_usd, owner
        self.max_model_calls = None if max_model_calls is None else int(max_model_calls)
        self.closing_reserve_calls = None if closing_reserve_calls is None else int(closing_reserve_calls)
        self.tree_calls_per_decision = None if tree_calls_per_decision is None else int(tree_calls_per_decision)       # [tree].max_model_calls (7 per decision, retries included) and
        self.tree_decisions_per_run = None if tree_decisions_per_run is None else int(tree_decisions_per_run)           # [tree].max_tree_decisions_per_run (0 as shipped; a positive value opts in), bound by the runner; enforced in _check_tree_limits at the dispatch of every tree send

    def model_call_count(self) -> int:
        """The run's model-call count: model rows in planned, sent, completed and unknown, plus failed rows that were sent
        (sent_at not null); first sends, repairs, fallbacks and worker sends alike."""
        return int(self.conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND kind='model' AND (state IN ('planned','sent','completed','unknown') OR (state='failed' AND sent_at IS NOT NULL))", (self.run_id,)).fetchone()[0])

    def call_allowance(self, step_key: str, finishing: bool) -> tuple[str, int, str]:
        """(stage, the count below which the send is allowed, the refusal reason) for a model send. The stage is validated
        first and an unknown or malformed key is refused before any allowance, finishing or not. Clause 1: a finishing send of a
        known stage below max_model_calls (every plan_generate send is finishing today). Clause 2: the closing stages and a
        plan send that is not finishing below max_model_calls minus one (the call reserve for the plan stage). Clause 3: the
        opening and evidence stages below max_model_calls minus one minus closing_reserve_calls."""
        stage = stage_of_key(step_key)
        if stage not in KNOWN_STAGES:
            raise CallLimitRefused("unknown stage", f"step key {step_key!r}: stage {stage!r} is none of {', '.join(KNOWN_STAGES)}; refused before any allowance")
        m, c = int(self.max_model_calls), int(self.closing_reserve_calls)
        if finishing:
            return stage, m, "call_limit"
        if stage in CLOSING_STAGES or stage == PLAN_STAGE:
            return stage, m - 1, "call_limit"
        return stage, m - 1 - c, ("headroom" if stage in EVIDENCE_STAGES else "call_limit")

    def _check_call_limit(self, step_key: str, finishing: bool) -> None:
        """Inside the dispatch transaction, for model sends only: the exhaustive classification of call_allowance, failing closed."""
        if self.max_model_calls is None and self.closing_reserve_calls is None:
            return
        if self.max_model_calls is None or self.closing_reserve_calls is None or self.max_model_calls <= 0:
            raise CallLimitRefused("budget_json without call limits", f"run {self.run_id}: the run row carries no model-call limit and closing reserve; no model send is allowed")
        stage, below, reason = self.call_allowance(step_key, finishing)
        count = self.model_call_count()
        if count >= below:
            raise CallLimitRefused(reason, f"{count} model call(s) recorded; a {stage} send{' (finishing)' if finishing else ''} is allowed below {below} (max_model_calls {self.max_model_calls}, call reserve 1, closing reserve {self.closing_reserve_calls})")

    def tree_decision_of(self, step_key: str) -> str | None:
        """The decision a tree send belongs to: the fifth part of <run>:tree:<worker>:<branch>:<decision>[:more] (the tree stage dispatches its calls under these keys)."""
        parts = str(step_key).split(":")
        return parts[4] if len(parts) >= 5 and parts[1] == "tree" and parts[4] else None

    def _check_tree_limits(self, step_key: str) -> None:
        """The tree's bounds, enforced where the tree's calls dispatch: a model send of the tree stage is refused
        when its decision already has tree_calls_per_decision model rows (first sends, repairs, fallbacks and unknown attempts alike: retries
        counted) or when the run already holds tree_decisions_per_run other decisions with a tree send. Inside the dispatch transaction."""
        if self.tree_calls_per_decision is None and self.tree_decisions_per_run is None:
            return
        decision = self.tree_decision_of(step_key)
        if decision is None:
            raise CallLimitRefused("tree_key", f"step key {step_key!r} names no decision; a tree send is <run>:tree:<worker>:<branch>:<decision>")
        like = f"{self.run_id}:tree:%"
        rows = self.conn.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? AND (state IN ('planned','sent','completed','unknown') OR (state='failed' AND sent_at IS NOT NULL))", (self.run_id, like)).fetchall()
        per = {}
        for (k,) in rows:
            d = self.tree_decision_of(k)
            if d is not None:
                per[d] = per.get(d, 0) + 1
        if self.tree_calls_per_decision is not None and per.get(decision, 0) >= self.tree_calls_per_decision:
            raise CallLimitRefused("tree_calls", f"{per.get(decision, 0)} tree call(s) recorded for decision {decision}; the limit is {self.tree_calls_per_decision} per decision, retries included")
        if self.tree_decisions_per_run is not None and decision not in per and len(per) >= self.tree_decisions_per_run:
            raise CallLimitRefused("tree_decisions", f"{len(per)} decision(s) already explored by the tree in this run ({', '.join(sorted(per))}); the limit is {self.tree_decisions_per_run} per run; {decision} takes the simple comparison")

    def committed(self) -> float:
        """Spend held against the cap: a provider-reported cost wherever one exists (completed, or unknown because the
        observation was lost), otherwise the reservation of every attempt not yet failed. Never both for one row."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN state='completed' THEN COALESCE(reported_usd, reserved_usd) "
            "WHEN state='unknown' THEN COALESCE(reported_usd, reserved_usd) "
            "WHEN state IN ('planned','sent') THEN reserved_usd ELSE 0 END), 0) FROM usage_ledger WHERE run_id = ?",
            (self.run_id,)).fetchone()
        return float(row[0])

    def possible_charges(self) -> float:
        """Reservations of unknown calls without a provider-reported cost; a reported cost is billed, not possible."""
        return float(self.conn.execute("SELECT COALESCE(SUM(reserved_usd),0) FROM usage_ledger WHERE run_id=? AND state='unknown' AND reported_usd IS NULL", (self.run_id,)).fetchone()[0])

    def remaining(self) -> float:
        return round(self.cap - self.committed(), 6)

    def can_send(self, step_key: str, kind: str) -> None:
        row = self.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND state='unknown' AND resolution IS NULL",
                                (self.run_id, step_key, kind)).fetchone()
        if row:
            raise UnknownCallPending(f"{row[0]} was interrupted; retry, skip or reconcile it first")

    def next_attempt(self, step_key: str, kind: str) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(attempt),0) FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=?", (self.run_id, step_key, kind)).fetchone()
        return int(row[0]) + 1

    def completed_result(self, step_key: str, kind: str):
        """(entry_id, result_json) of the latest completed attempt with a stored result, or None."""
        return self.conn.execute("SELECT id, result_json FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND state='completed' "
                                 "AND result_json IS NOT NULL ORDER BY attempt DESC LIMIT 1", (self.run_id, step_key, kind)).fetchone()

    def attempts_of_kind(self, step_key: str, kind: str, attempt_kind: str) -> int:
        """Persisted count of repair or fallback attempts of a stage instance (survives rounds and resume)."""
        return int(self.conn.execute(f"SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND attempt_kind=? AND {ALLOWANCE_SQL}",
                                     (self.run_id, step_key, kind, attempt_kind)).fetchone()[0])   # counted as the claim counts it

    def spend_report(self) -> dict:
        """Billed (provider-reported, real providers), simulated (stub provider), estimates still held (planned or sent),
        possible charges (unknown) and reservations released on failure, kept apart: run_spend over this run."""
        return run_spend(self.conn, self.run_id)

    def claim_dispatch(self, step_key: str, kind: str, estimate_usd: float, finishing: bool = False, attempt_kind: str = "first",
                       within=None, replay_completed: bool = False, exclusive: bool = False, request_scoped: bool = False) -> str:
        """The atomic dispatch claim, inside one BEGIN IMMEDIATE: the run must be running, no unknown call may be pending
        for this step and kind, a repair or fallback allowance of this stage instance is claimed if asked (one each, ever),
        `within(conn)` runs any statements that must share the transaction (tool quota and slot rows), the run status is
        re-read right before the reservation, the next attempt number is allocated and the reservation inserted.
        replay_completed: a completed result stored for this step by another caller raises Replay instead of reserving.
        exclusive: an attempt of this step already claimed or in flight (state planned or sent) raises InFlight instead of a second send.
        request_scoped: an unknown attempt of this step blocks its own request only, which `within` refuses; the step's other requests go on.
        A cancellation that lands before this transaction fails it with DispatchRefused; two concurrent claimants of the
        same allowance get one success; everything rolls back on any error."""
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT status, worker_id FROM runs WHERE id=?", (self.run_id,)).fetchone()
            if not row or row[0] != "running":
                raise DispatchRefused(f"run {self.run_id} is {row[0] if row else 'missing'}; no new request may start")
            if self.owner is not None and row[1] != self.owner:
                raise DispatchRefused(f"run {self.run_id} is owned by worker {row[1]}, not {self.owner}; no new request may start")   # reclaimed and resumed by another worker
            if replay_completed:
                done = self.conn.execute("SELECT id, result_json FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND state='completed' AND result_json IS NOT NULL ORDER BY attempt DESC LIMIT 1", (self.run_id, step_key, kind)).fetchone()
                if done:
                    raise Replay(done[0], done[1])
            if exclusive:
                flying = self.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND state IN ('planned','sent')", (self.run_id, step_key, kind)).fetchone()   # claimed or sent: one attempt at a time
                if flying:
                    raise InFlight(flying[0])
            pending = self.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND state='unknown' AND resolution IS NULL", (self.run_id, step_key, kind)).fetchone()
            if pending and not request_scoped:
                raise UnknownCallPending(f"{pending[0]} was interrupted; retry, skip or reconcile it first")
            if attempt_kind in ("repair", "fallback"):
                used = self.conn.execute(f"SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND attempt_kind=? AND {ALLOWANCE_SQL}", (self.run_id, step_key, kind, attempt_kind)).fetchone()[0]   # a reservation released without dispatch (failed, never sent) does not consume the allowance; a claimed or in-flight attempt does; an unknown attempt the founder retried or reconciled made way for the selected new attempt
                if used >= 1:
                    raise AllowanceUsed(f"{attempt_kind} allowance of {step_key} already used")
            attempt = int(self.conn.execute("SELECT COALESCE(MAX(attempt),0) FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=?", (self.run_id, step_key, kind)).fetchone()[0]) + 1
            entry_id = f"{step_key}:{kind}:{attempt}"
            if within is not None:
                within(self.conn, entry_id)                                 # tool quota, stage slot and the tool row with its ledger link, in this same transaction
            row = self.conn.execute("SELECT status, worker_id FROM runs WHERE id=?", (self.run_id,)).fetchone()   # re-read right before reserving
            if not row or row[0] != "running":
                raise DispatchRefused(f"run {self.run_id} is {row[0] if row else 'missing'}; cancelled before the reservation")
            if self.owner is not None and row[1] != self.owner:
                raise DispatchRefused(f"run {self.run_id} is owned by worker {row[1]}, not {self.owner}; reclaimed before the reservation")
            if kind == "model":
                self._check_call_limit(step_key, finishing)                      # the count and the allowance by stage, inside this same BEGIN IMMEDIATE
                if stage_of_key(step_key) == "tree":
                    self._check_tree_limits(step_key)                            # the tree bounds (the calls per decision and the decisions per run of settings [tree]) at the tree's dispatch point
            limit = self.cap if finishing else self.cap - self.completion_reserve
            if self.committed() + estimate_usd > limit + 1e-9:
                raise BudgetExceeded(f"reserving {estimate_usd:.4f} USD would pass the {'cap' if finishing else 'cap minus the completion reserve'} ({limit:.2f} USD)")
            self.conn.execute("INSERT INTO usage_ledger (id, run_id, step_key, kind, attempt, reserved_usd, state, attempt_kind) VALUES (?,?,?,?,?,?, 'planned', ?)",
                              (entry_id, self.run_id, step_key, kind, attempt, estimate_usd, attempt_kind))
            self.conn.commit()
        except Exception:
            self.conn.rollback(); raise
        return entry_id

    def reserve(self, step_key: str, kind: str, estimate_usd: float, attempt: int = 1, finishing: bool = False, attempt_kind: str = "first") -> str:
        """Reserve before sending, inside BEGIN IMMEDIATE so two callers cannot both pass the cap check."""
        entry_id = f"{step_key}:{kind}:{attempt}"
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if self.conn.execute("SELECT 1 FROM usage_ledger WHERE id = ?", (entry_id,)).fetchone():
                self.conn.commit(); return entry_id            # idempotent: the same attempt never reserves twice
            limit = self.cap if finishing else self.cap - self.completion_reserve
            if self.committed() + estimate_usd > limit + 1e-9:
                raise BudgetExceeded(f"reserving {estimate_usd:.4f} USD would pass the {'cap' if finishing else 'cap minus the completion reserve'} ({limit:.2f} USD)")
            self.conn.execute("INSERT INTO usage_ledger (id, run_id, step_key, kind, attempt, reserved_usd, state, attempt_kind) VALUES (?,?,?,?,?,?, 'planned', ?)",
                              (entry_id, self.run_id, step_key, kind, attempt, estimate_usd, attempt_kind))
            self.conn.commit()
        except Exception:
            self.conn.rollback(); raise
        return entry_id

    # ---- transition statements: each public method commits one; the tool runner pairs them with its row in one transaction
    def mark_sent_sql(self, entry_id: str) -> tuple:
        return ("UPDATE usage_ledger SET state='sent', sent_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND state='planned'", (entry_id,))

    def settle_sql(self, entry_id: str, reported_usd: float | None, result_json: str | None = None, **meta) -> tuple:
        return ("UPDATE usage_ledger SET state='completed', reported_usd=?, cost_uncertain=?, result_json=COALESCE(?, result_json), model_id=COALESCE(?, model_id), "
                "provider=COALESCE(?, provider), generation_id=COALESCE(?, generation_id), tokens_in=?, tokens_out=?, latency_ms=?, outcome=COALESCE(?, outcome), reasoning_tokens=?, "
                "raw_json=COALESCE(?, raw_json), finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (reported_usd, int(reported_usd is None), result_json, meta.get("model_id"), meta.get("provider"), meta.get("generation_id"),
                 meta.get("tokens_in"), meta.get("tokens_out"), meta.get("latency_ms"), meta.get("outcome"), meta.get("reasoning_tokens"), meta.get("raw_json"), entry_id))   # outcome: typed result of the call (ok, invalid, tool_calls, truncated), durable with the settlement; raw_json: the answer text of an invalid or truncated attempt, never replayed

    def fail_sql(self, entry_id: str, charged: bool | None = False, provider_status: int | None = None) -> tuple:
        state = "unknown" if charged is None else "failed"
        return ("UPDATE usage_ledger SET state=?, provider_status=COALESCE(?, provider_status), finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (state, provider_status, entry_id))

    def atomically(self, *statements) -> None:
        """Run several statements in one BEGIN IMMEDIATE transaction (a ledger transition paired with its tool row). Every caller records the
        outcome of a call already made, so a transient store lock is waited out (wait_out_lock)."""
        wait_out_lock(self._atomically_once, statements)

    def _atomically_once(self, statements) -> None:
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for sql, params in statements:
                self.conn.execute(sql, params)
            self.conn.commit()
        except Exception:
            self.conn.rollback(); raise

    def mark_sent(self, entry_id: str, *extra) -> None:
        """The sent transition, with any statements that must share it (the tool row's sent marker), inside one BEGIN IMMEDIATE: the stop check
        at the wire. A Stop that landed between the durable claim and this transition released the planned
        row (recover_after_crash: planned to failed), so the row is no longer planned and nothing may leave: DispatchRefused, no request sent."""
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(*self.mark_sent_sql(entry_id))
            if cur.rowcount != 1:
                raise DispatchRefused(f"{entry_id} is no longer a planned attempt (the run was stopped after the claim); nothing is sent")
            for sql, params in extra:
                self.conn.execute(sql, params)
            self.conn.commit()                                   # every transition is an explicit commit point
        except Exception:
            self.conn.rollback(); raise

    def settle(self, entry_id: str, reported_usd: float | None, result_json: str | None = None, **meta) -> None:
        self.atomically(self.settle_sql(entry_id, reported_usd, result_json, **meta))

    def fail(self, entry_id: str, charged: bool | None = False, provider_status: int | None = None) -> str:
        """charged False: the provider refused before billing (state failed); None: the request may have reached the provider (unknown).
        The provider's HTTP status is kept on the row."""
        self.atomically(self.fail_sql(entry_id, charged, provider_status))
        return "unknown" if charged is None else "failed"

    def release(self, entry_id: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE usage_ledger SET state='failed' WHERE id = ? AND state='planned'", (entry_id,))

    def mark_sent_unknown(self, why: str = "the run paused while this send was in flight (a worker thread ended before its settlement); a charge is possible") -> list[str]:
        """Inside the caller's transaction: every attempt of this run left 'sent' with no outcome becomes 'unknown' (a possible charge the
        founder resolves: retry, skip or reconcile), its tool rows with it, a summary per row. Without this, a worker thread that ended
        with a send in flight (for example a parallel source worker on a locked store) would leave a sent row the pause does not list as
        an unknown call, and a retry would wait on that row. Returns the ids."""
        ids = [r[0] for r in self.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND state='sent'", (self.run_id,))]
        if not ids:
            return []
        self.conn.execute("INSERT INTO reasoning_records (run_id, step_key, action_id, purpose, action, observation, change) "
                          "SELECT run_id, step_key, id, 'mark an in-flight call unknown at the pause', 'ledger transition', ?, 'the founder retries, skips or reconciles it; nothing is resent on its own' "
                          "FROM usage_ledger WHERE run_id=? AND state='sent'", (why, self.run_id))
        self.conn.execute("UPDATE usage_ledger SET state='unknown' WHERE run_id=? AND state='sent'", (self.run_id,))
        self.conn.execute("UPDATE tool_calls SET state='unknown', error=COALESCE(error, 'interrupted after the request was sent'), finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                          "WHERE run_id=? AND state='sent' AND ledger_entry IN (SELECT id FROM usage_ledger WHERE run_id=? AND state='unknown')", (self.run_id, self.run_id))
        return ids

    def recover_after_crash(self) -> list[str]:
        """After a restart: planned attempts never left (failed), sent attempts may have (unknown). Tool rows follow their
        ledger entries: a planned tool row whose entry is failed becomes failed; a sent tool row whose entry is unknown becomes unknown."""
        with self.conn:
            rec = ("INSERT INTO reasoning_records (run_id, step_key, action_id, purpose, action, observation, change) "   # a summary for every row recovery reconciles, with its real id, in this transaction
                   "SELECT run_id, step_key, id, 'recover after restart', ?, ?, ? FROM {table} WHERE run_id=? AND state=?")
            self.conn.execute(rec.format(table="usage_ledger"), ("release an attempt never sent", "planned attempt found at restart; nothing was sent", "reservation released; the step may run again", self.run_id, "planned"))
            self.conn.execute(rec.format(table="usage_ledger"), ("mark an interrupted call unknown", "sent attempt found at restart; no result stored; a charge is possible", "the founder retries, skips or reconciles it", self.run_id, "sent"))
            self.conn.execute(rec.format(table="tool_calls"), ("release a tool request never sent", "planned tool request found at restart", "reconciled from its ledger entry", self.run_id, "planned"))
            self.conn.execute(rec.format(table="tool_calls"), ("reconcile an interrupted tool request", "sent tool request found at restart", "reconciled from its ledger entry (completed, failed or unknown)", self.run_id, "sent"))
            self.conn.execute("UPDATE usage_ledger SET state='failed' WHERE run_id=? AND state='planned'", (self.run_id,))
            ids = [r[0] for r in self.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND state='sent'", (self.run_id,))]
            self.conn.execute("UPDATE usage_ledger SET state='unknown' WHERE run_id=? AND state='sent'", (self.run_id,))
            self.conn.execute("UPDATE tool_calls SET state='failed', error=COALESCE(error, 'interrupted before the request was sent'), finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                              "WHERE run_id=? AND state='planned' AND (ledger_entry IS NULL OR ledger_entry IN (SELECT id FROM usage_ledger WHERE run_id=? AND state='failed'))", (self.run_id, self.run_id))
            self.conn.execute("UPDATE tool_calls SET state='unknown', error=COALESCE(error, 'interrupted after the request was sent'), finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                              "WHERE run_id=? AND state='sent' AND ledger_entry IN (SELECT id FROM usage_ledger WHERE run_id=? AND state='unknown')", (self.run_id, self.run_id))
            # older data may hold two inconsistent pairs; both are reconciled here
            self.conn.execute("UPDATE tool_calls SET state='failed', error=COALESCE(error, 'ledger entry failed before the send'), finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                              "WHERE run_id=? AND state='sent' AND ledger_entry IN (SELECT id FROM usage_ledger WHERE run_id=? AND state='failed')", (self.run_id, self.run_id))
            self.conn.execute("UPDATE tool_calls SET state='completed', finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                              "WHERE run_id=? AND state='sent' AND result_json IS NOT NULL AND ledger_entry IN (SELECT id FROM usage_ledger WHERE run_id=? AND state='completed')", (self.run_id, self.run_id))
            self.conn.execute("UPDATE tool_calls SET state='unknown', error=COALESCE(error, 'result lost after the settlement; the charge may exist'), finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                              "WHERE run_id=? AND state='sent' AND result_json IS NULL AND ledger_entry IN (SELECT id FROM usage_ledger WHERE run_id=? AND state='completed')", (self.run_id, self.run_id))
            self.conn.execute("UPDATE usage_ledger SET state='unknown' WHERE run_id=? AND state='completed' AND result_json IS NULL AND id IN (SELECT ledger_entry FROM tool_calls WHERE run_id=? AND state='unknown')", (self.run_id, self.run_id))   # the reported cost stays on the row: billed, not a possible charge
            ids = [r[0] for r in self.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND state='unknown' AND resolution IS NULL ORDER BY id", (self.run_id,))]   # every unresolved unknown call, legacy reconciliation included
        return ids

    def sends_made(self, step_key: str, kind: str = "model") -> int:
        """Attempts of this step with persisted send evidence (sent_at set): the only count that bounds a send allowance.
        A planned attempt released by recovery (state failed, sent_at null) never counts as a send."""
        return int(self.conn.execute("SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND sent_at IS NOT NULL", (self.run_id, step_key, kind)).fetchone()[0])

    def was_sent(self, step_key: str, kind: str = "model") -> bool:
        return self.sends_made(step_key, kind) > 0

    def truncated(self, step_key: str) -> bool:
        """True when an attempt of this step was settled with the typed outcome truncated: the strategy for it (split, bump,
        needs_review, sections) is derived from that durable fact on resume, never from a later separate write."""
        return self.conn.execute("SELECT 1 FROM usage_ledger WHERE run_id=? AND step_key=? AND kind='model' AND outcome='truncated' LIMIT 1", (self.run_id, step_key)).fetchone() is not None

    def unknown_calls(self) -> list[dict]:
        """Unresolved unknown calls. possible_charge_usd is the reservation only when no provider cost was reported; a reported
        cost is listed as billed_usd and the possible charge is 0, never both for one row."""
        cur = self.conn.execute("SELECT id, step_key, kind, CASE WHEN reported_usd IS NULL THEN reserved_usd ELSE 0 END, COALESCE(reported_usd, 0) "
                                "FROM usage_ledger WHERE run_id=? AND state='unknown' AND resolution IS NULL ORDER BY id", (self.run_id,))
        return [dict(zip(("id", "step_key", "kind", "possible_charge_usd", "billed_usd"), r)) for r in cur]

    def skipped(self, step_key: str, kind: str = "model") -> str | None:
        """The id of an interrupted attempt of this step the founder continued without (resolution skipped), or None.
        The gateway answers such a step with status skipped and sends nothing; the stage proceeds without that call's output."""
        row = self.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND step_key=? AND kind=? AND state='unknown' AND resolution='skipped' ORDER BY attempt DESC LIMIT 1", (self.run_id, step_key, kind)).fetchone()
        return row[0] if row else None

    def resolve_unknown(self, entry_id: str, resolution: str, reported_usd: float | None = None) -> None:
        """retried or skipped: the row stays unknown and its possible charge stays counted; reconciled: the provider figure replaces it."""
        assert resolution in ("retried", "skipped", "reconciled")
        with self.conn:
            if resolution == "reconciled":
                self.conn.execute("UPDATE usage_ledger SET resolution='reconciled', state='completed', reported_usd=? WHERE id=?", (reported_usd, entry_id))
            else:
                self.conn.execute("UPDATE usage_ledger SET resolution=? WHERE id=?", (resolution, entry_id))
