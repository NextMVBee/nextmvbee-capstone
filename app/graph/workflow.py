"""The workflow of a run: from the founder's idea to a cited plan. A graph with deterministic routers, one interrupt per
node, a small graph state and the domain records in the application database.

Nodes of an initial run: clarify (a model call that stores requirements and assumptions and opens questions, two rounds
at most), ask (the one interrupt of a round, for the founder's answers), research_plan (call_with_tools: search, read
with offset or query, ask_user which pauses the run), extract_evidence (batched extraction through
app.core.extraction.Extractor for every source stored in the run), verify (a separate call judges the claims, re_fetch
allowed, verdicts stored through app.core.coverage.record_verification), compare_options (a comparison per decision
subject over the verified claims; hard requirements count only on supported evidence) and plan_generate
(app.core.extraction.generate_plan: a truncated whole plan is assembled by sections and stored only when consistent).

Requirements. The clarify node stores a requirement the model marks as the founder's statement as assumed and opens one
confirmation question (questions.kind confirmation); the ask node applies the founder's yes, no or corrected value. A
requirement the model proposes as hard gets one hardness question (questions.kind hardness): only the founder's yes sets
the hard flag. The later nodes read the confirmed requirements.

Step keys and replay. Every model send goes through the gateway with a step key <run>:<stage>:<worker>:<branch>[:item].
A completed step replays from the ledger and sends nothing. Node writes are guarded by run_steps marks, written in the
same transaction as the records, so a node that is executed again never duplicates a record. No node passes max_tokens:
the gateway resolves the output ceiling of each stage from settings [output].

Budget stops. Every external dispatch first checks the run's active time against settings [budget].max_minutes. A
reservation refused at the completion reserve (budget_reserve_reached), the time limit (time_limit), a call limit
(call_limit) or a stage's interrupted send the founder continued without (skipped_call) stops research and routes to
the plan node, which drafts the plan from the evidence gathered: the reason is recorded, and the coverage report goes
into the pause event and into the plan's unresolved checks. After a time stop exactly one draft of the plan is
permitted. Exhausted credits, a stopped run or a failed stage end the run without a plan. A send that ended unknown
pauses the run for the founder's choice, and the founder's own pause keeps the last checkpoint; in both cases the next
drive enters the node again and replays every completed step.

Revision runs. A requirement change (app/core/invalidation.apply_change) opens a revision run (runs.kind revision) whose
graph starts at the revise node. The model's RevisionOut may ask scope questions, which are stored and pause the run
like clarify questions (SCOPE_ROUNDS rounds at most). Code re-decides every stale decision the invalidation computed
(the model's decision_ids_to_redo is restricted to that set) and pursues at most four new queries as searches. The
extract, verify, compare (the stale subjects only) and plan nodes follow; a revision that researches the stale subjects
again (state rediscover) goes through research_plan first, and a usage correction goes straight to the comparison. The
plan node stores the new version with the change summary and its diff and runs the consistency sweep.

Snapshots. Every plan version created here carries its signed export snapshot (app/core/snapshot.py), built and
validated inside the transaction that creates the version; a snapshot that does not validate fails the creation of the
version. The Markdown export of a version is rendered from that snapshot alone."""
import json, pathlib, re, sqlite3, time
from dataclasses import dataclass, field
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core import coverage
from app.core import applicability as AP
from app.core import coverage_step as CS
from app.core import budget as BG, derived, discovery as DS, evidence, evidence_pack as EP, freshness, interventions as IV, react, reconcile, retrieval, roles, safety, tree
from app.core import exclusions as EX
from app.core.ids import next_number
from app.core.sqlguard import require_guarded
from app.core import snapshot as snapshot_mod
from app.core import textlimit as TL
from app.core import wording as W
from app.core.extraction import Extractor, batch_ranges, generate_plan, record_strategy
from app.adapters.model_gateway import max_bump
from app.core.invalidation import consistency_sweep, blocking_issues
from app.core.ledger import BudgetExceeded, UnknownCallPending, CallLimitRefused, ALLOWANCE_SQL, counts_toward_allowance
from app.core.tools import ToolRunner, ToolRejected, tools_for, step_key_for, RUN_LIMITS
from app.graph import prompts
from app.repo import Repo
from app.runner import events as ev
from app.runner.recovery import RunStopped, answer_question, is_locked
from schemas import models as M

STAGES = ("clarify", "research_plan", "extract_evidence", "verify", "compare_options", "plan_generate")
_after_plan_draft = [None]            # test hook holder: a callable run after generate_plan returned and before the plan records are stored
_after_verify = [None]                # test hook holder: after every verdict is stored, before the verify stage mark
_after_compare = [None]               # test hook holder: after the decisions are stored, before the compare stage mark
SCOPE_ROUNDS = 2                       # the rounds in which a revision may ask scope questions, as clarify has two rounds; a question still open afterwards stops the run
CLOSING_STOPS = ("budget_reserve_reached", "time_limit", "call_limit")   # the limits of a run that may refuse a closing call of the plan; the plan the run wrote is stored without that call
DRAFT_STOPS = IV.DRAFT_STOPS   # defined once, with the intervention criteria (I4): stop research and draft the plan from the evidence gathered (call_limit: a send refused at a call limit; skipped_call: the founder continued without a stage's interrupted send)
HARD_STOPS = ("credit_exhausted", "unknown_call", "run_stopped", "stage_failed")


QUESTIONS_A_ROUND = 3                                                                  # the questions the ask node puts to the founder in one round, as the web application tells the founder at the start


class RunState(TypedDict, total=False):
    rediscover: bool                  # a quality run that researches the stale subjects again (the planner with the discovery passes) before re-deciding
    project_id: str
    run_id: str
    idea: str
    round: int
    questions_open: int
    decision_subjects: list
    decisions: list                   # the project's decisions loaded at start (subject, question, candidate pool rule, research questions, criteria); the planner's decisions to support
    stop_reason: str
    plan_version: int
    status: str
    change: dict                      # a revision run: the requirement change (key, old and new value and id, requirement version)
    stale: list                       # the stale decision ids the invalidation computed; the set the revision re-decides
    change_summary: str               # the model's change summary, the new version's change_summary


VERIFY_ROUNDS = 3                                                                             # a verification group's tool loop: the first send and up to two re_fetch turns (the ledger counts each as a send)


class StopRun(Exception):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason); self.reason, self.detail = reason, detail


class RunPaused(Exception):
    """The founder paused the run (app/core/safety.StopController.pause: the row interrupted with founder_pause) and a dispatch of
    this stage was refused for it. The node raises instead of recording a stop, so the graph keeps its last checkpoint and no stop reason
    enters the state; the next drive (resume) re-enters the node, replays every completed step and goes on."""
    def __init__(self, stage: str, detail: str = ""):
        super().__init__(f"{stage}: paused ({detail})"); self.stage, self.detail = stage, detail


class InterruptedCall(Exception):
    """A send of this stage ended unknown (a crash, a timeout, a connection failure after the send). The node raises
    instead of ending the run, so the graph keeps its last checkpoint and the runner pauses the run for the founder's choice; the
    next drive re-enters the node, which replays every completed step and reaches the call as the founder resolved it."""
    def __init__(self, stage: str, detail: str = ""):
        super().__init__(f"{stage}: unknown_call ({detail})"); self.stage, self.detail = stage, detail


@dataclass
class RunContext:
    conn: sqlite3.Connection
    settings: dict
    gateway: object
    research: object
    project_id: str
    run_id: str
    data_dir: object = None
    worker: str = "w0"
    branch: str = "b0"
    started: float = field(default_factory=time.monotonic)
    extractor_cls: type = Extractor
    db_path: object = None                                                                 # what a worker's context needs to open its own handle
    make_ledger: Callable | None = None                                                    # (conn, run_id) -> BudgetLedger with the run's bounds (the Runner's)
    make_gateway: Callable | None = None                                                   # (ledger) -> the gateway over that ledger (the Runner's)
    open_kwargs: dict = field(default_factory=dict)                                        # the handle's mode and production flag
    shadow: object = None                                                                  # the main context a worker's context reads the active time through; it never ticks the run row itself

    @property
    def ledger(self):
        return self.gateway.ledger

    @property
    def repo(self):
        return Repo(self.conn)

    def key(self, stage: str, item: str | None = None) -> str:
        k = step_key_for(self.run_id, stage, self.worker, self.branch)
        return f"{k}:{item}" if item else k

    last_tick: float = 0.0

    def tick(self) -> float:
        """Persist the active time since the last tick on the run row (it is persisted during execution, at every dispatch)
        and return the persisted total. The total is the row's value, never a drive added twice.
        A worker's context (shadow set) reads the row's total plus the time since the main context's last tick and
        writes nothing, so three parallel workers never count one interval three times; the main context ticks once after them."""
        now = time.monotonic()
        if self.shadow is not None:
            main = self.shadow
            row = self.conn.execute("SELECT COALESCE(active_seconds, 0) FROM runs WHERE id=?", (self.run_id,)).fetchone()
            return float(row[0] if row else 0.0) + max(0.0, now - (main.last_tick or main.started))
        delta = now - (self.last_tick or self.started); self.last_tick = now
        with self.conn:
            self.conn.execute("UPDATE runs SET active_seconds=COALESCE(active_seconds,0)+?, active_ticked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (max(0.0, delta), self.run_id))
        row = self.conn.execute("SELECT COALESCE(active_seconds, 0) FROM runs WHERE id=?", (self.run_id,)).fetchone()
        return float(row[0]) if row else 0.0

    def elapsed_active(self) -> float:
        return self.tick()

    def fork(self, worker: str | None = None) -> "RunContext":
        """A context for one worker thread: its own handle on the same store (app.repo.connect: WAL, the busy timeout, foreign keys),
        its own ledger with the run's bounds and its own gateway over it. The ledger's counters are the store's rows, so the run's
        limits, reservations and call counts stay shared; nothing else is copied but the settings and the ids. The worker label
        enters the step keys when given (a research worker); a source task keeps the main label so its per-source keys are the
        same whichever thread runs them (a resume finds them). The caller closes the handle (close_fork)."""
        from app.repo import connect
        conn = connect(self.db_path, **self.open_kwargs)
        try:                                                                              # the handle is closed if the ledger or the gateway cannot be built
            ledger = self.make_ledger(conn, self.run_id)
            gateway = self.make_gateway(ledger)
        except BaseException:
            conn.close(); raise
        import dataclasses
        return dataclasses.replace(self, conn=conn, gateway=gateway, worker=worker or self.worker, shadow=self, last_tick=0.0)

    def close_fork(self) -> None:
        if self.shadow is not None:
            self.conn.close()

    def over_time(self) -> bool:
        return self.elapsed_active() > 60.0 * float(self.settings.get("budget", {}).get("max_minutes", 15))

    draft_consumed: bool = False

    def draft_permit(self) -> str | None:
        """The durable, run-scoped permit on the run row: None before a time stop, 'permitted' after it, 'used' once consumed."""
        r = self.conn.execute("SELECT draft_permit FROM runs WHERE id=?", (self.run_id,)).fetchone()
        return r[0] if r else None

    def grant_draft(self) -> bool:
        """After a time stop, grant the one draft once; a run whose permit was already used never gets another (restart included)."""
        with self.conn:
            cur = self.conn.execute("UPDATE runs SET draft_permit='permitted' WHERE id=? AND draft_permit IS NULL", (self.run_id,))
        return cur.rowcount == 1

    def check_time(self, stage: str) -> None:
        """The shared deadline before every external dispatch: nodes, loops, extraction batches and
        condition follow-ups, tool-loop sends and tool executions all call it; a StopRun time_limit is the workflow's stop.
        After a time stop exactly one draft of the plan is permitted: the permit is consumed durably at
        the first actual plan dispatch, so repairs, fallbacks, sections and restarts after it are refused."""
        what = stage.split(" ", 1)[1] if " " in stage else ""
        if stage.startswith("plan_generate") and what == "first":                             # the actual dispatch of the first plan send consumes the permit, durably (after a time stop or a call_limit stop alike)
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute("UPDATE runs SET draft_permit='used' WHERE id=? AND draft_permit='permitted'", (self.run_id,))
                self.conn.commit()
            except Exception:
                self.conn.rollback(); raise
            if cur.rowcount == 1:
                self.draft_consumed = True
                return
        if not self.over_time():
            return
        if stage.startswith("plan_generate") and (what == "plan" or what.startswith("section")) and self.draft_permit() == "permitted":
            return                                                                            # the caller's announcement before the one dispatch; the gateway consumes it
        permit = self.draft_permit()
        raise StopRun("time_limit", f"{self.settings['budget']['max_minutes']} minutes of active run time reached during {stage}" + ("; the one permitted draft was already made" if permit == "used" else ""))

    # ---- node idempotency: run_steps marks, written in the same transaction as the records
    def done(self, key: str) -> dict | None:
        row = self.conn.execute("SELECT output_json FROM run_steps WHERE step_key=? AND status='completed'", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def mark(self, key: str, node: str, output: dict) -> None:
        self.conn.execute("INSERT OR REPLACE INTO run_steps (step_key, run_id, node, input_hash, status, output_json, completed_at) VALUES (?,?,?,?,'completed',?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                          (key, self.run_id, node, key, json.dumps(output, default=str)))

    def tx(self):
        return self.repo._tx()

    # ---- records as the prompts see them (ids, keys, values; never page text)
    def requirements(self, confirmed_only: bool = False) -> list[dict]:
        mapped = "applies_to" in {r[1] for r in self.conn.execute("PRAGMA table_info(requirements)")}            # the applies_to column (migration 056); a store without it reads every requirement as unmapped
        rows = self.conn.execute(f"SELECT id, key, value_json, status, hard, {'applies_to' if mapped else 'NULL'} FROM requirements WHERE project_id=? AND status<>'superseded' ORDER BY id", (self.project_id,)).fetchall()
        out = [{"id": r[0], "key": r[1], "value": json.loads(r[2]), "status": r[3], "hard": bool(r[4]), "applies_to": AP.applies_to_of({"applies_to": r[5]})} for r in rows]
        return [r for r in out if r["status"] == "confirmed"] if confirmed_only else out

    def requirement_version(self) -> int:
        return int(self.conn.execute("SELECT requirement_version FROM projects WHERE id=?", (self.project_id,)).fetchone()[0])

    def counts(self) -> tuple[int, int]:
        q = "SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND tool IN {} AND state IN ('planned','sent','completed','unknown') AND ledger_entry IS NOT NULL"
        return (self.conn.execute(q.format("('search')"), (self.run_id,)).fetchone()[0], self.conn.execute(q.format("('read','re_fetch')"), (self.run_id,)).fetchone()[0])

    def run_sources(self) -> list[str]:
        """Sources stored by this run's tool calls, in first-seen order."""
        seen = []
        for (res,) in self.conn.execute("SELECT result_json FROM tool_calls WHERE run_id=? AND state='completed' AND tool IN ('search','read','re_fetch') ORDER BY created_at, n", (self.run_id,)):
            for p in (json.loads(res) if res else []) or []:
                if isinstance(p, dict) and p.get("source_id") and p["source_id"] not in seen and not p.get("refreshed"):
                    seen.append(p["source_id"])
        return seen

    def run_claims(self) -> list[dict]:
        with coverage.derived(self.conn, self.project_id):                                     # claim status is derived: the accessor derives first and opens the reads context
            rows = self.conn.execute("SELECT c.id, c.text, c.kind, c.source_id, c.quote, c.status, c.scope_status, c.conditions_json, c.subject, c.attribute, c.value, c.range_start, c.range_end, c.decision_subject, c.product_id, c.product_version, c.source_version "
                                     "FROM claims c WHERE c.project_id=? ORDER BY c.id", (self.project_id,)).fetchall()
            return [dict(zip(("id", "text", "kind", "source_id", "quote", "status", "scope_status", "conditions_json", "subject", "attribute", "value", "range_start", "range_end", "decision_subject", "product_id", "product_version", "source_version"), r)) for r in rows]


def _stop_from_status(status: str, detail: str | None) -> StopRun | None:
    if status == "credit":
        return StopRun("credit_exhausted", detail or "")
    if status == "unknown":
        return StopRun("unknown_call", detail or "")
    if status in ("stopped", "round_limit"):
        return StopRun("run_stopped" if status == "stopped" else "stage_failed", detail or status)
    return None


def stage_of_key(key: str) -> str:
    parts = key.split(":")
    return parts[1] if len(parts) > 1 else key


def _verdict_problems(out, valid: set) -> list[str]:
    """The verdict ids of a verification response that are not of the group or appear twice (an unknown or a duplicate id makes the response invalid)."""
    ids = [v.claim_id for v in out.verdicts]
    return [i for i in ids if i not in valid] + [i for i in set(ids) if ids.count(i) > 1]


def truncated_attempts(ctx: RunContext, key: str) -> list[str]:
    """Ledger entries of this step settled with the typed outcome truncated, oldest first."""
    return [r[0] for r in ctx.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND step_key=? AND kind='model' AND outcome='truncated' ORDER BY attempt", (ctx.run_id, key))]


STOP_WORDS = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are", "be", "that", "this", "by", "as", "at", "it", "its", "from", "may", "will", "can", "than"}


def same_meaning(text: str, existing: list[str], threshold: float = 0.6) -> bool:
    """Two assumptions mean the same when the token sets of their words (stop words dropped) overlap by at least the
    threshold (Jaccard); deterministic, no model."""
    def toks(s):
        return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP_WORDS and len(w) > 2}
    a = toks(text)
    if not a:
        return False
    for e in existing:
        b = toks(e)
        if b and len(a & b) / len(a | b) >= threshold:
            return True
    return False


def first_week_from_steps(tasks: list[dict], steps_by_task: dict, hours_per_day: float = 8.0, days: int = 5) -> list[str]:
    """The first week built from the tasks' sub-steps (the planner enrichment gives each task two to four concrete sub-steps with
    hours): the tasks in topological order, their sub-steps filling days of hours_per_day; a day lists the sub-steps worked on it with the task
    they belong to; a sub-step longer than a day spans consecutive days as one line; a day never repeats a task title with "continued"."""
    by_id = {t["id"]: t for t in tasks}
    order, seen = [], set()

    def visit(tid):
        if tid in seen or tid not in by_id:
            return
        seen.add(tid)
        for d in sorted(by_id[tid].get("depends_on") or []):
            visit(d)
        order.append(tid)
    for tid in sorted(by_id):
        visit(tid)
    units = []                                                                            # (text, hours) in order
    for tid in order:
        steps = steps_by_task.get(tid) or []
        if not steps:
            return first_week(tasks, hours_per_day, days)                                  # a task without sub-steps: the task-level week (no "continued" lines)
        for st in steps:
            units.append((f"{st['text']} ({by_id[tid]['title']})", float(st.get("hours") or 1.0)))
    lines, day, used, today = [], 1, 0.0, []
    i = 0
    while i < len(units) and day <= days:
        text, hours = units[i]
        if hours > hours_per_day and not today:                                           # one long sub-step spans whole days as one line
            span = max(1, round(hours / hours_per_day))
            last = min(days, day + span - 1)
            lines.append((f"Day {day}" if last == day else f"Day {day} to {last}") + f": {text}")
            day = last + 1; i += 1; continue
        if used + hours > hours_per_day + 0.5 and today:
            lines.append(f"Day {day}: " + "; ".join(today)); day += 1; used = 0.0; today = []
            continue
        today.append(text); used += hours; i += 1
    if today and day <= days:
        lines.append(f"Day {day}: " + "; ".join(today)); day += 1
    left = [t for t, _ in units[i:]]
    if left:
        lines.append("After the first week: " + "; ".join(dict.fromkeys(x.split(" (")[-1].rstrip(")") for x in left)))
    return lines


def first_week(tasks: list[dict], hours_per_day: float = 8.0, days: int = 5) -> list[str]:
    """The first week from the task ladder in code: the tasks in topological order (depends_on first, then by id), each
    taking its high effort estimate at hours_per_day a day; one line per day naming the tasks worked; the tasks beyond the week on the last line."""
    by_id = {t["id"]: t for t in tasks}
    order, seen = [], set()

    def visit(tid):
        if tid in seen or tid not in by_id:
            return
        seen.add(tid)
        for d in sorted(by_id[tid].get("depends_on") or []):
            visit(d)
        order.append(tid)
    for tid in sorted(by_id):
        visit(tid)
    lines, day, used, today = [], 1, 0.0, []
    left = []
    for tid in order:
        hours = float(by_id[tid].get("effort_high") or by_id[tid].get("effort_low") or hours_per_day)
        if day > days:
            left.append(tid); continue
        if hours > hours_per_day and not today:                                           # a task longer than a day spans consecutive days as one line, never "continued"
            span = max(1, round(hours / hours_per_day)); last = min(days, day + span - 1)
            lines.append((f"Day {day}" if last == day else f"Day {day} to {last}") + f": {by_id[tid]['title']} ({tid}, {hours:g} hours)")
            day = last + 1; continue
        today.append(f"{by_id[tid]['title']} ({tid})"); used += hours
        if used >= hours_per_day:
            lines.append(f"Day {day}: " + "; ".join(today)); used = 0.0; day += 1; today = []
    if today and day <= days:
        lines.append(f"Day {day}: " + "; ".join(today)); day += 1
    if left:
        lines.append(f"After the first week: " + "; ".join(f"{by_id[x]['title']} ({x})" for x in left))
    return lines


def strong_model(settings: dict, stage: str) -> str | None:
    """The strongest model of settings [models].quality_rank that has a pinned price, for the stages of [models].strong_stages
    (the coverage, comparison and memo, and planner enrichment calls); None for every other stage or without a rank."""
    m = (settings or {}).get("models") or {}
    if stage not in (m.get("strong_stages") or []):
        return None
    prices = m.get("prices_per_million") or {}
    return next((x for x in (m.get("quality_rank") or []) if x in prices), None)


def stage_model(ctx, stage: str, model: str | None = None) -> str:
    """The model identity of a stage's send, known before the send: the caller's route or the stage's strong
    route (strong_model) when the gateway prices it, else the gateway's default route. The fallback that may answer a provider error is
    recorded on the ledger row, never in the key: a key names the route the configuration assigns."""
    m = model or strong_model(ctx.settings, stage)
    prices = getattr(ctx.gateway, "prices", None) or {}
    return m if m and m in prices else str(getattr(ctx.gateway, "default_model", "") or "")


def model_slug(model: str) -> str:
    """A model identity as a key part, one slug per identity: letters, digits, dot and underscore stand, a dash of the id is doubled,
    and the vendor's slash, a variant's colon or any other character becomes one dash; so a/b-c (a-b--c) and a-b/c (a--b-c) never share a key."""
    return "".join("--" if ch == "-" else ch if (ch.isascii() and ch.isalnum()) or ch in "._" else "-" for ch in str(model or "")) or "x"


def reuse_tag(ctx, prompt_stage: str, stage: str | None = None, model: str | None = None) -> str:
    """The part of a reuse key that names the prompt version and the model identity: v<n>:<model> when the prompt is the stage's own
    (coverage, compare_options, panel), <prompt>.v<n>:<model> when the stage sends another prompt (the tree's three prompts, panel_direct).
    A prompt change or a model change gives another key, so nothing stored under the old one is replayed; the same prompt and model with the
    same inputs give the same key and replay."""
    stage = stage or prompt_stage
    pv = f"v{prompts.VERSIONS[prompt_stage]}" if prompt_stage == stage else f"{prompt_stage}.v{prompts.VERSIONS[prompt_stage]}"
    return f"{pv}:{model_slug(stage_model(ctx, stage, model))}"


def _send(ctx, key, contract, msgs, finishing, tools, runner, max_rounds, bump, model=None, extra_body=None, server_tools=None, prompt_tokens_est=0):
    est = int(prompt_tokens_est or 0)                                                      # an agentic send carries its engine fees as prompt tokens (app/core/discovery.estimate_as_prompt_tokens)
    try:
        deadline = lambda what: ctx.check_time(f"{stage_of_key(key)} {what}")           # the shared deadline before every actual attempt, inside the gateway
        if tools is not None:
            return ctx.gateway.call_with_tools(key, contract, msgs, tools, runner, est, finishing=finishing, max_rounds=max_rounds, bump=bump, before_dispatch=deadline)
        if extra_body or server_tools:
            return ctx.gateway.call(key, contract, msgs, est, finishing=finishing, bump=bump, max_rounds=max_rounds, before_dispatch=deadline, model=model, extra_body=extra_body, server_tools=server_tools)
        return ctx.gateway.call(key, contract, msgs, est, finishing=finishing, bump=bump, max_rounds=max_rounds, before_dispatch=deadline, model=model)
    except BudgetExceeded as e:
        raise StopRun("budget_reserve_reached", str(e)[:200]) from e
    except UnknownCallPending as e:
        raise StopRun("unknown_call", str(e)[:200]) from e
    except RunStopped as e:
        raise StopRun("run_stopped", str(e)[:200]) from e


def _pick_option(out):
    """The memo option the recommendation names: by its text, else by its product identity, else by the same words."""
    rec = getattr(out, "recommendation", None)
    if not rec:
        return None
    opts = list(out.options)
    by_text = next((o for o in opts if o.option == rec.option), None)
    if by_text:
        return by_text
    if rec.product_id:
        by_id = next((o for o in opts if o.product_id and coverage.same_product(rec.product_id, rec.product_version or "", o.product_id, o.product_version or "")), None)
        if by_id:
            return by_id
    canon = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return next((o for o in opts if canon(o.option) and (canon(o.option) in canon(rec.option) or canon(rec.option) in canon(o.option))), None) or next((o for o in opts if o.verdict == "recommended"), None)


def _disagreements(conn, pid: str, hard: list, rv: int, rec, by_id: dict) -> dict:
    """The evidence state sources_disagree: per binding hard requirement, the claims about the pick whose support was withdrawn for an open conflict (the reason
    app/core/reconcile.py records): the sources disagree, so neither value counts and the decision waits; {} when no such claim."""
    out = {}
    members: dict = {}
    with coverage.derived(conn, pid):                                                      # the open conflicts are derived rows: read inside the accessor's context
        for (cj,) in conn.execute("SELECT claim_ids_json FROM claim_conflicts WHERE project_id=? AND status='open'", (pid,)).fetchall():
            ids = json.loads(cj)
            for x in ids:
                members[x] = ids
    for h in hard:
        gone = {x: why for x, why in coverage.withdrawn_support(conn, pid, h["id"], rv).items() if x in by_id and str(why or "").startswith("conflicting evidence") and _about_pick(rec, by_id[x])}
        if gone:
            out[h["id"]] = {m: gone.get(m) or "conflicting evidence (the preferred claim of the same conflict)" for x in gone for m in members.get(x, [x])}   # both sides of the conflict, the preferred claim included
    return out


def _disagree_line(option: str, hard: list, disagree: dict, by_id: dict) -> str:
    """The founder's sentence when sources disagree: the attribute, the claims by id, what follows."""
    parts = []
    for h in hard:
        if h["id"] in disagree:
            ids = sorted(disagree[h["id"]])
            attrs = sorted({str((by_id.get(x) or {}).get("attribute") or h.get("key") or "").replace("_", " ") for x in ids})
            parts.append(f"{', '.join(attrs)} ({', '.join(ids)})")
    return f"Sources disagree about {option} on {'; '.join(parts)}: the sources give conflicting values, so neither counts as support until the conflict is settled, and the decision stays open."


def _evidence_state(conn, pid: str, hard: list, rv: int, rec, by_id: dict, lacking: list) -> tuple[str, str]:
    """The state of a pick that lacks support on the given requirements, sources_disagree when its sources are in an open conflict and not_proven otherwise: (rationale, evidence state)."""
    disagree = {k: v for k, v in _disagreements(conn, pid, hard, rv, rec, by_id).items() if k in {h["id"] for h in lacking}}
    if disagree:
        return _disagree_line(rec.option, hard, disagree, by_id), "sources_disagree"
    return CS.unproven_line(rec.option, lacking), "not_proven"


def _reject_option(memo: dict | None, rejected: list, rec, line: str) -> None:
    """The rejected candidate's memo option reads rejected with the rejection line in its risk, and the rejected list names it."""
    for o in (memo or {}).get("options") or []:
        if o.get("option") == rec.option or ((o.get("product_id"), o.get("product_version") or "") == (rec.product_id, rec.product_version or "")):
            o["verdict"] = "rejected"; o["risk"] = (TL.to_sentence_end(line, 300)[0] if not str(o.get("risk") or "").startswith(CS.REJECTED) else o["risk"])   # cut to a sentence end or a whole word, never a slice
    mine = [r for r in rejected if r.get("option") == rec.option]
    for r in mine:
        r["reason"] = TL.to_sentence_end(line, 200)[0]                                       # an option the comparison did not pick and the evidence or the calculation rejects reads rejected with that line, never with the memo's own risk
    if not mine:
        rejected.append({"option": rec.option, "reason": TL.to_sentence_end(line, 200)[0]})


def _about_pick(rec, claim: dict) -> bool:
    """The cited claim is about the recommended product (its tier named in the option's label or in the claim's words)."""
    return bool(rec and rec.product_id) and coverage.same_product(rec.product_id, rec.product_version, claim.get("product_id"), claim.get("product_version"), rec.option, claim.get("text") or "")


def _about_an_option(rec, out, claim: dict, own_support: bool = False) -> bool:
    """A cited claim is about the pick, or, when the pick has cited evidence of its own, about another option of the memo (the comparison
    sentence it serves); a claim about no option, or a pick whose only evidence is another product's, is refused (for example,
    Vega cannot use Orion's evidence)."""
    if _about_pick(rec, claim):
        return True
    if not own_support:
        return False
    cid, cver, text = claim.get("product_id"), claim.get("product_version"), claim.get("text") or ""
    return any(o.product_id and coverage.same_product(o.product_id, o.product_version or "", cid, cver, o.option, text) for o in out.options)


def _signed_price_set(block: dict) -> dict:
    """The price claims a signed budget rests on, with the date each was read: the eligible manifest; an older block without one
    gives the claims its own rows used less the ids its note left out, as document_model.recompute_budget reads it."""
    if block.get("eligible") is not None:
        return {e["claim_id"]: e.get("accessed") for e in block["eligible"] if e.get("claim_id")}
    out = {cid: None for r0 in block.get("rows") or [] for cid in r0.get("claims") or []}
    noted = str(block.get("note") or "").split("Left out:", 1)
    for m0 in re.finditer(r"([A-Za-z0-9_-]+\.CLM-\d+) \(", noted[1] if len(noted) > 1 else ""):
        out.pop(m0.group(1), None)
    return out


NO_RATIONALE = "no supported rationale"


def _kept_why(why, citable) -> list:
    """A why sentence is kept on non-empty citations that are all citable; a sentence without a
    citation, or with one outside the subject's supported, in-scope claims, is dropped."""
    return [w0 for w0 in why if w0.claim_ids and all(c0 in citable for c0 in w0.claim_ids)]


def _rationale_of(kept_why) -> str:
    """The rationale comes from the kept sentences alone; with none kept it is the fixed reason, never the model's removed text."""
    return kept_why[0].text if kept_why else NO_RATIONALE


def rq_covered_of(rq_status: dict) -> set:
    """The one evidence predicate of the draft and the final comparison: a research question counts
    on supported evidence; its processing note (needs review, partial) is reported, never a gate. So a reading that
    stopped at a limit covers the draft and the final comparison alike."""
    return {k for k, v in rq_status.items() if v[0] == "supported"}


LABEL_GENERIC = ("plan", "tier", "api", "service")

# ---- the contradiction handling
DIFFERENCE_WORDS = {"different", "differ", "differs", "differing", "difference", "differences", "wording", "worded", "same", "equivalent", "duplicate", "twice", "superseded", "older", "newer", "disputed", "dispute", "disagree", "disagrees",
                    "conflict", "conflicting", "contradictory", "contradict", "contradicts", "flagged"}   # the words that state a difference between the contradicting statements (a noun such as plan, tier, SKU, version, region or source names what may differ, not that it does, and states none)
EFFECT_WORDS = {"treat", "treated", "assume", "assumed", "assuming", "unusable", "rejected", "reject", "superseded", "recommend", "recommended", "watch", "later", "disputed", "confirm", "confirmed", "settle", "settles", "settled",
                "depends", "depend", "excluded", "ignored", "discounted", "prefer", "preferred", "trust", "trusted", "chosen", "pick", "picked", "unproven", "unresolved", "open", "still", "stands", "stays", "applies", "apply"}   # the words that name the effect on the recommendation
_EFFECT_PHRASES = re.compile(r"not (?:a |an )?(?:real )?conflict|no (?:real )?conflict|not conflicting|not contradictory|not a contradiction|runner-up|not now|bake-off|no effect|does not change|does not affect|carried into open|left open|stays open")


def _stem(word: str) -> str:
    """A light stem for the comparison of attribute words with a memo's words (prices, pricing, price; invoices, invoice; waives, waiver)."""
    w = word.lower()
    for suffix in ("ing", "ies", "es", "ed", "er", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            w = w[: -len(suffix)] + ("y" if suffix == "ies" else "")
            break
    return w


def _word_match(a: str, b: str) -> bool:
    """Two words name the same thing: equal, or equal after the light stem, or one stem the other's prefix when the shorter has five letters or more."""
    sa, sb = _stem(a), _stem(b)
    if a == b or sa == sb:
        return True
    short, long_ = sorted((sa, sb), key=len)
    return len(short) >= 5 and long_.startswith(short)


def _names_terms(text: str, terms) -> bool:
    """A term is named by a word of the text (the light stem rule: prices names pricing); False for no term."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    terms = [t for t in terms if t]
    return bool(terms) and any(any(_word_match(t, w) for w in words) for t in terms)


def _figures_of(text: str) -> set[float]:
    """The figures of a text as numbers (20.00 and $20 are one figure; 1,000 is a thousand)."""
    out = set()
    for m in re.findall(r"\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _term_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", (text or "").lower().replace("_", " ")) if len(w) >= 3 and w not in ("and", "the", "for", "per", "with")]


def addressed(item: dict, memo: dict, label: str, product_id: str = "", product_version: str = "", attributes: dict | None = None) -> bool:
    """A must_address item is addressed when the memo names one of its claim ids anywhere, or when a
    text of the memo about the item's candidate explains the relevant difference and its effect on the recommendation: the text is about the item (it names
    the attribute's words, or the claims' attributes, or a figure of every value), it states the difference (DIFFERENCE_WORDS: different, superseded,
    disputed, a wording; a noun such as plan or tier alone states none) and it names the effect (EFFECT_WORDS or _EFFECT_PHRASES: not a real conflict, treated as, assumed,
    rejected, superseded, left open; an open line carries its effect by its place: the item stays open for the founder). The candidate's texts: the risk and the fit of the memo option (never its verdict label) of the same canonical identity (the pack's
    product id and version, so the option 'Google Maps Platform (plan)' is the pack 'Google Maps Platform'; an option of another identity never matches by its label), or of the same label when the pack or the option gives no identity,
    and the open and change_mind lines that name every product word of the candidate beyond the vendor's word (a Google Cloud line or a Google Cloud Platform line is not the Google Maps Platform candidate's). The figures alone, or the values verbatim, address nothing. attributes:
    claim id -> the claim's attribute, for a verifier item that names requirement and question ids."""
    texts = [w0.get("text") or "" for w0 in memo.get("why") or []] + [f"{o0.get('risk') or ''} {o0.get('fit') or ''}" for o0 in memo.get("options") or []] + list(memo.get("open") or []) + list(memo.get("change_mind") or [])
    blob = " ".join(str(t0) for t0 in texts).lower()
    ids0 = [str(x0) for x0 in item.get("claim_ids") or [] if x0]
    if any(x0.lower() in blob for x0 in ids0):
        return True
    canon = coverage.canonical_identity(product_id, product_version or "") if product_id else None

    def _mine(o0: dict) -> bool:
        """An option that gives an identity matches the pack's by canonical identity alone; the label matches only when the pack or the option gives none."""
        if canon is not None and o0.get("product_id"):
            return coverage.canonical_identity(o0["product_id"], o0.get("product_version") or "") == canon
        return AP.norm(o0.get("option") or "") == AP.norm(label)
    mine0 = [o0 for o0 in memo.get("options") or [] if _mine(o0)]
    vendor0 = (product_id or "").lower().split("-")[0]
    product_words = ({w for w in re.findall(r"[a-z]+", (label or "").lower()) if len(w) >= 3} | {w for w in re.split(r"[-\s]+", (product_id or "").lower()) if len(w) >= 3}) - {vendor0} - set(LABEL_GENERIC)
    product_words = product_words or ({vendor0} if len(vendor0) >= 3 else set())              # a line is the candidate's when it names the product words beyond the vendor's (never google alone); an identity with no such word is named by the vendor's word (fly, vercel)
    names_product = lambda t0: product_words and product_words <= set(re.findall(r"[a-z]+", str(t0).lower()))   # every product word (maps and platform), not any one: a Google Cloud Platform line shares platform and is not the Google Maps Platform candidate's
    candidate_texts = [(f"{o0.get('risk') or ''} {o0.get('fit') or ''}", False) for o0 in mine0]                               # the risk and the fit prose; the verdict is a label code reads for the recommendation, never the explanation's effect
    candidate_texts += [(str(t0), True) for t0 in memo.get("open") or [] if names_product(t0)] + [(str(t0), False) for t0 in memo.get("change_mind") or [] if names_product(t0)]   # an open line carries its effect by its place: the item stays open for the founder
    about_terms = [_term_words(str(item.get("about") or ""))] + [_term_words(str((attributes or {}).get(cid0) or "")) for cid0 in ids0]
    about_terms = [t for t in about_terms if t]
    values0 = [str(v0) for v0 in item.get("values") or [] if str(v0).strip()]
    value_figures = [_figures_of(v0) for v0 in values0]
    for text0, in_open in candidate_texts:
        low = text0.lower(); words = set(re.findall(r"[a-z]+", low)); figs = _figures_of(low)
        about_ok = any(_names_terms(low, terms) for terms in about_terms) or (bool(value_figures) and all(f0 and (f0 & figs) for f0 in value_figures))   # about the item: a word of its attribute, or a figure of every one of its values
        difference_ok = bool(words & DIFFERENCE_WORDS)
        effect_ok = in_open or bool(words & EFFECT_WORDS) or bool(_EFFECT_PHRASES.search(low))
        if about_ok and difference_ok and effect_ok:
            return True
    return False


def contradiction_items(packs: list, memo: dict, attributes: dict | None = None) -> list[dict]:
    """The must_address items of a decision's packs the memo did not address (addressed), each with its candidate's label and identity, the
    attribute, the claim ids and the values: the record the decision carries (memo_json.contradictions) and the card shows, so the question can list
    the items by id and attribute and the founder reads the values and the claims where the decision is."""
    out = []
    for p0 in packs if isinstance(packs, list) else []:
        for it0 in p0.get("must_address") or []:
            if addressed(it0, memo, p0.get("label") or "", p0.get("product_id") or "", p0.get("product_version") or "", attributes):
                continue
            out.append({"label": p0.get("label") or p0.get("product_id") or "", "product_id": p0.get("product_id") or "", "product_version": p0.get("product_version") or "", "kind": it0.get("kind") or "conflict",
                        "about": str(it0.get("about") or ""), "claim_ids": list(it0.get("claim_ids") or []), "values": list(it0.get("values") or []), "conflict_key": it0.get("conflict_key") or ""})
    return out


def contradiction_line(it: dict) -> str:
    """One item by its option, its attribute and its claim ids (the record's line; the founder's words are conflict_words and refused_words)."""
    about = str(it.get("about") or "").replace("_", " ") or "a fact"
    return f"{it.get('label') or it.get('product_id')} on {about} ({', '.join(it.get('claim_ids') or [])})" + ("" if it.get("values") else ": the verifier found that the source does not state the claim")


def conflict_words(it: dict) -> str:
    """A contradiction between sources in words: the option, the attribute and each value with the source that states it (the claim id beside it)."""
    about = str(it.get("about") or "").replace("_", " ") or "a fact"
    stated = [f"{x.get('value') or 'a value'} ({x.get('source') or 'a source'}, {x.get('claim_id')})" for x in it.get("sources") or []] or [str(v) for v in it.get("values") or []]
    return f"the sources give different values for {about} of {it.get('label') or it.get('product_id')}: " + " against ".join(stated)


def refused_words(it: dict) -> str:
    """A claim the verifier refused, in its own words: the source does not state it, so it is not used as evidence; never a contradiction between sources."""
    return f"One claim about {it.get('label') or it.get('product_id')} was refused by the verifier ({', '.join(it.get('claim_ids') or [])}): its source does not state it, so it is not used as evidence."


def label_identity(label: str) -> str:
    """A planner label as a canonical product identity, its
    trailing generic word (plan, tier, api, service) left off, so "Railway Hobby plan" and the extraction's "Railway Hobby" (product
    railway-railway at version hobby) are one identity; a version or another product stays distinct ("Deepgram Nova plan" is not
    deepgram-nova-2)."""
    words = re.findall(r"[a-z0-9.]+", (label or "").lower())
    while len(words) > 1 and words[-1] in LABEL_GENERIC:
        words.pop()
    return coverage.canonical_identity("-".join(words), "")


def eligible_candidates(conn, pid: str, subject: str, rel: list | None = None) -> dict:
    """The one typed eligible set of a subject, shared by the planner's gap step, the draft and the
    final comparison and the activation. kept: the subject's kept candidate rows (label, product_class, specialised_for); dropped: the
    normalised labels dropped under the subject (for example, a product of another class); identities: among the subject's verified claims, every product
    identity that no dropped label names (by the claim's product id or by its subject, spelled exactly or as the same canonical identity:
    label_identity), two spellings of one identity collapsed into the first, as {(product_id,
    product_version): label}, the label coverage.identity_label; refused: the identities the rules left out, each with the reason
    (refuse_identities); excluded: those of them the subject's supported claims name, the pool's
    candidates for the packs when no eligible identity carries a supported claim (evidence_pack.pool_identities)."""
    require_guarded(conn)
    subj = AP.norm(subject)
    rows = conn.execute("SELECT label, product_class, specialised_for, status, subject_class, reason FROM candidates WHERE project_id=? AND subject=? ORDER BY label", (pid, subj)).fetchall()
    kept = [{"label": r0[0], "product_class": r0[1], "specialised_for": r0[2]} for r0 in rows if r0[3] == "kept"]
    model_subject = any(is_model_class(r0[4]) for r0 in rows)                                  # a model subject's options are model versions, the unit a token tariff belongs to
    dropped_why = {AP.norm(r0[0]): (r0[5] or "another class") for r0 in rows if r0[3] == "dropped"}   # the planner's reason beside the dropped label
    dropped_ids = {label_identity(d0): why for d0, why in dropped_why.items()}                # the planner's dropped label as a canonical product identity
    ident, refused = refuse_identities(rel, model_subject, dropped_why, dropped_ids)
    supported = {(c0.get("product_id") or "", c0.get("product_version") or "") for c0 in rel or [] if c0.get("status") == "supported"}
    return {"kept": kept, "dropped": sorted(dropped_why), "identities": ident, "refused": refused, "excluded": {k: v for k, v in refused.items() if k in supported}}   # the identities the supported claims name that the pool rules excluded, with the reason


def options_of_class(conn, pid: str, subject: str, options: list[dict], keep: tuple = ()) -> tuple[list[dict], list[dict]]:
    """An option listed in a decision belongs to that decision's class by the class the store records for it (so the LLM model decision
    lists no image model among its options). The store's record is the planner's typed
    catalogue (candidates: the label, the subject it was kept under, the subject's class). An option whose identity (its product id and version,
    or its label, as a canonical identity) is a kept candidate of another subject whose class is not this subject's, and is no kept candidate of
    this subject, is no option of this decision: it leaves the memo's options and is named on the memo (options_other_class) with the class the
    store records. keep: identities never removed (the recommendation and the runner-up: the eligible set judges the pick). Returns
    (the options that stay, the ones that left)."""
    require_guarded(conn)
    subj = AP.norm(subject)
    rows = conn.execute("SELECT subject, label, subject_class FROM candidates WHERE project_id=? AND status='kept'", (pid,)).fetchall()
    own_class = {r0[2] for r0 in rows if r0[0] == subj}
    here = {label_identity(r0[1]) for r0 in rows if r0[0] == subj}
    other = {label_identity(r0[1]): r0[2] for r0 in rows if r0[0] != subj and r0[2] not in own_class}
    if not own_class or not other:
        return list(options), []
    stay, left = [], []
    for o0 in options:
        ids = [coverage.canonical_identity(o0.get("product_id"), o0.get("product_version")) if o0.get("product_id") else "", label_identity(re.sub(r"\s*\((?:the\s+)?(?:api|plan|tier|service)\)\s*$", "", o0.get("option") or "", flags=re.I))]   # the label without the suffix coverage.identity_label writes ("(the API)", "(the plan)")
        cls = next((other[x] for x in ids if x and x in other), None)
        if cls is None or any(x in here for x in ids if x) or any(x in keep for x in ids if x):
            stay.append(o0)
        else:
            left.append({"option": o0.get("option"), "product_id": o0.get("product_id") or "", "product_version": o0.get("product_version") or "", "class": cls})
    return stay, left


def refuse_identities(rel: list | None, model_subject: bool, dropped_why: dict | None = None, dropped_ids: dict | None = None) -> tuple[dict, dict]:
    """The eligible set's rules over a subject's claims alone (one code path for the run, which reads the
    catalogue from the store, and for the export of a version stored before the cuts were recorded, which reads the version's own snapshot and has
    no store to ask): (identities, refused) as eligible_candidates returns them, from rel, whether the subject is a model class, and the planner's
    dropped labels by normalised label and by canonical identity (each with the planner's reason; empty when the caller has none)."""
    dropped_why = dropped_why or {}; dropped_ids = dropped_ids or {}
    dropped = set(dropped_why)
    tariffs = token_tariffs(rel) if model_subject else {}                                        # the input and output token prices each exact identity's own supported claims give
    ident: dict = {}; canon_seen: dict = {}; refused: dict = {}
    for c0 in rel or []:
        key0 = (c0.get("product_id") or "", c0.get("product_version") or "")
        if not key0[0]:
            continue
        drop = next((d0 for d0 in (AP.norm(key0[0]), AP.norm(c0.get("subject") or "")) if d0 in dropped), None)
        if drop is not None:
            refused.setdefault(key0, f"dropped under this subject by the planner: {dropped_why[drop]} (D-A41)"); continue   # the dropped identity is excluded with the planner's reason
        ck = coverage.canonical_identity(*key0)
        drop = next((d0 for d0 in (ck, label_identity(c0.get("subject") or "")) if d0 in dropped_ids), None)
        if drop is not None:                                                                   # the extraction's spelling of the dropped label ("Railway Hobby" for the planner's "Railway Hobby plan") is the dropped identity, by the typed identity or by the claim's subject
            refused.setdefault(key0, f"dropped under this subject by the planner: {dropped_ids[drop]} (D-A41)"); continue
        if ck in canon_seen and canon_seen[ck] != key0:
            continue
        if model_subject and not names_model_version(*key0):                                   # a model family with no version ("Claude (Anthropic API)") is no option of a model subject: no token tariff attaches to it
            refused.setdefault(key0, "names no model version (D-A133)"); continue
        if model_subject and names_two_models(*key0):                                          # judged on the recorded identity alone, never on a label
            refused.setdefault(key0, f"names more than one model or version ({' '.join(x for x in key0 if x)}) (D-A143)"); continue
        if model_subject and tariffs.get(key0, set()) != {"million_tokens_in", "million_tokens_out"}:
            refused.setdefault(key0, "no input and output token prices of its own on record (D-A143)"); continue   # one model, and its own input and output token prices on record, or no option
        canon_seen[ck] = key0
        ident.setdefault(key0, coverage.identity_label(*key0))                                  # the recorded product name and version, never the subject text of the claim met first
    return ident, {k: v for k, v in refused.items() if k not in ident}


def predecessor_of(conn, pid: str, subject: str, current_subjects) -> dict | None:
    """The earlier name of a subject a later plan renamed, read from the planner's own record: the candidates
    row's subject_class of the subject, taken only when it is neither the subject's own name nor the name of a subject of the current plan (two
    live subjects of one class never share claims; a class that is just a class joins nothing because no question carries it as its subject).
    Returns {"subject": <the predecessor name>, "plan_version": <the newest plan version that decided it, or None>} or None."""
    require_guarded(conn)
    cls = AP.norm((conn.execute("SELECT subject_class FROM candidates WHERE project_id=? AND subject=? LIMIT 1", (pid, AP.norm(subject))).fetchone() or [""])[0])
    if not cls or cls == AP.norm(subject) or cls in {AP.norm(s0) for s0 in current_subjects or ()}:
        return None
    pv = conn.execute("SELECT MAX(plan_version) FROM decisions WHERE project_id=? AND subject=?", (pid, cls)).fetchone()[0]
    return {"subject": cls, "plan_version": int(pv) if pv is not None else None}


def subject_claims(conn, pid: str, subject: str, claims: list, rq_claims: dict, rq_subject: dict, current_subjects) -> list:
    """The claims a comparison of `subject` may read (status not unverified): those attached to the subject's own research questions or
    named for it by the extractor, and those attached to the questions of the subject's predecessor name (predecessor_of) when a
    later plan renamed the subject; a claim the extractor tagged with the predecessor's name that no such question attaches is not joined. The one selection of the draft and the final comparison, of scripts/eval/pack_stats.py and of
    scripts/eval/retrieval_diagnosis.py, so the measurements read what the comparison reads. Nothing of a joined claim changes: its rows, its
    questions and its verdict stay as its own run left them; the pack line names where it came from (evidence_pack.load_inputs)."""
    require_guarded(conn)
    norm = coverage._norm
    names = {norm(subject)}
    pred = predecessor_of(conn, pid, subject, current_subjects)
    if pred:
        names.add(norm(pred["subject"]))
    own = {cid for cid, rqs in rq_claims.items() if any(norm(rq_subject.get(rq, "")) in names for rq in rqs)}
    own |= {c["id"] for c in claims if norm(c.get("decision_subject") or "") == norm(subject)}   # the extractor's tag joins for the subject's own name only: the predecessor joins through its questions alone, so every joined claim has an origin
    return [c for c in claims if c["status"] != "unverified" and c["id"] in own]


def is_model_class(product_class: str | None) -> bool:
    """A subject class whose products are models priced per token (llm_model, language_model, llm)."""
    cls = AP.norm(product_class or "")
    return "llm" in cls.split("_") or cls.endswith("model")


def token_tariffs(claims: list | None) -> dict:
    """Per exact identity (product id, version), the token units its own supported, in-scope price claims
    give a price in, as the budget rules read them (budget.price_of: million_tokens_in, million_tokens_out).
    budget.price_of is the one tariff reading: a cache-only or batch-only rate is no base tariff wherever it is stated,
    an uncached rate is the ordinary one, and a compound tariff the rules cannot read gives none."""
    out: dict = {}
    for c in claims or []:
        if c.get("kind") != "price" or c.get("status") != "supported" or c.get("scope_status") not in ("in_scope", "conditional"):
            continue
        unit = (BG.price_of(c) or {}).get("unit")
        if unit in ("million_tokens_in", "million_tokens_out"):
            out.setdefault((c.get("product_id") or "", c.get("product_version") or ""), set()).add(unit)
    return out


def citable_of(conn, pid: str, rel: list, requirement_version: int) -> set:
    """The claims of a subject a comparison may cite: supported, in scope and valid at the current versions
    (coverage.claim_rows), unless the claim is in an open conflict. An open conflict on another attribute, or sources of its question left
    unprocessed, do not remove it (for example, a hosting option's claims stay citable when their question was not covered in full)."""
    require_guarded(conn)
    ids = sorted({c["id"] for c in rel if c["status"] == "supported" and c["scope_status"] in ("in_scope", "conditional")})
    valid = {row["id"] for row in coverage.claim_rows(conn, pid, ids, requirement_version) if row["valid"]}
    return valid - coverage.open_conflict_claims(conn, pid)


def unprocessed_of(conn, pid: str, cited: list, rq_claims: dict) -> list:
    """The sources the latest run left unprocessed for the questions of the cited claims, each with its reason, listed with the decision."""
    require_guarded(conn)
    marks = coverage.run_report_marks(conn, pid)["unprocessed"]; left: dict = {}
    for rq in sorted({rq for cid in cited for rq in rq_claims.get(cid, ())}):
        for x in marks.get(rq) or []:
            left.setdefault(x["source_id"], x.get("reason") or "")
    return [{"source_id": k, "reason": v} for k, v in sorted(left.items())]


def names_two_models(product_id: str, product_version: str = "") -> bool:
    """The one-model rule judges the recorded
    identity, never a claim's label (labels describe attributes: "context window and max output tokens" names one model): a product_version
    that holds more than one version (coverage.holds_many_versions), or a product_id that joins two names with and or or (keep_joins records the
    other joiners so) or repeats a model word after the vendor (openai-gpt-4o-gpt-4-1)."""
    ids = (product_id or "").lower().split("-")[1:]
    if coverage.holds_many_versions(product_version) or {"and", "or"} & set(ids):
        return True
    words = [w for w in ids if len(w) >= 3 and w.isalpha() and w != (product_id or "").lower().split("-")[0]]   # the vendor's word repeated (atlas/atlas-text-4 in the product field) is no model word
    return len(words) != len(set(words))


def names_model_version(product_id: str, product_version: str = "") -> bool:
    """An identity names a model version when its version, or its product part after the vendor, carries a number
    ("openai-gpt-4o", "anthropic-claude-3-5-haiku", "anthropic-claude-haiku" at 5.5); a family ("anthropic-claude", "ai21-jamba") names none."""
    product = "-".join((product_id or "").split("-")[1:])
    return bool(re.search(r"\d", f"{product} {product_version or ''}"))


def eligible_pick(ident: dict, rec):
    """The eligible identity a pick names (exactly, else the same identity under another spelling), or None when it names none;
    a pick outside the set is refused with the reason and never activates under the model's label."""
    if rec is None or not rec.product_id:
        return None
    key = (rec.product_id, rec.product_version or "")
    if key in ident:
        return key
    return next((k for k in ident if coverage.same_product(k[0], k[1], rec.product_id, rec.product_version or "")), None)


VERDICT_RANK = {"recommended": 0, "runner-up": 1, "bake-off": 2, "watch": 3, "later": 4, "not now": 5, "rejected": 6}


def _lead_record(text: str, claim_ids: list) -> dict:
    """The lead of the plan on one page as the version stores it. A lead the model wrote whole is stored whole. A lead
    that reaches its contract's limit inside a sentence arrived cut (the provider enforces the schema's maxLength by cutting the string): it is
    stored to its last whole sentence with the note of what was left out, never as if whole; with no whole sentence the text is empty (the
    document's lead is then the founder's problem statement, a rule of the document renderer, which is not part of this repository) and the note says so."""
    from app.core import textlimit as TL
    kept, note = TL.lead_whole(text)
    return {"text": kept, "claim_ids": list(claim_ids) if kept else [], **({"note": note} if note else {})}


def _merge_options(out):
    """Option rows of one canonical identity are one row: the first row stands, the others'
    fit, tariffs (price, monthly cost) and risk are joined into it, the claim ids united, the strongest verdict kept; a row without a
    product identity stays; a runner-up of the pick's own identity is dropped. The pick and the runner-up are then read against the
    merged table (_pick_option matches by identity). Returns (the memo, the number of rows merged away)."""
    index: dict = {}; rows: list = []
    from app.core import textlimit as TL
    def join(a, b, n):                                                                       # two rows' cells joined: whole when they fit, else to a sentence end or a clause end; never sliced at the limit
        whole = "; ".join(dict.fromkeys(x for x in (a, b) if x))
        return whole if len(whole) <= n else TL.to_statement_end(whole, n)[0]
    for o in out.options:
        ck = coverage.canonical_identity(o.product_id, o.product_version) if o.product_id else None
        if ck is None or ck not in index:
            if ck is not None:
                index[ck] = len(rows)
            rows.append(o); continue
        first = rows[index[ck]]
        rows[index[ck]] = first.model_copy(update={"fit": join(first.fit, o.fit, 300), "price": join(first.price, o.price, 200), "monthly_cost": join(first.monthly_cost, o.monthly_cost, 120), "risk": join(first.risk, o.risk, 300),
                                                   "verdict": min((first.verdict, o.verdict), key=lambda v: VERDICT_RANK.get(v, 9)), "claim_ids": list(dict.fromkeys(list(first.claim_ids) + list(o.claim_ids)))[:12]})
    merged = len(out.options) - len(rows)
    runner_up = out.runner_up
    if runner_up and out.recommendation and runner_up.product_id and out.recommendation.product_id and coverage.canonical_identity(runner_up.product_id, runner_up.product_version) == coverage.canonical_identity(out.recommendation.product_id, out.recommendation.product_version):
        runner_up = None
    if merged == 0 and runner_up is out.runner_up:
        return out, 0
    return out.model_copy(update={"options": rows, "runner_up": runner_up}), merged


def call_stage(ctx: RunContext, stage: str, key: str, contract, msgs: list[dict], finishing: bool = False, tools=None, runner=None, max_rounds=None, extra_body=None, server_tools=None, model=None, prompt_tokens_est=0, tolerate_round_limit: bool = False) -> dict:
    """One stage call with two policies. Truncation: a
    truncated answer is sent once more under the same step key at the ceiling raised within [output].max_bump; the bump
    is recorded as a strategy summary against the truncated ledger entry; a second truncation flags the stage. The state
    is derived from the typed ledger outcomes, so a resume after a crash continues with the bump or the flag and never
    repeats a settled send. Failure shape: a failure_reason answer is asked once more with the repair turn, then
    flagged. Budget and run stops surface as StopRun. Returns the gateway result; status 'flagged' carries the reason."""
    if max_rounds is None:
        max_rounds = int(ctx.settings["tools"]["stage_limit"]) + 1                        # one effective send allowance per stage instance, resolved once
    prior = truncated_attempts(ctx, key)
    factor = max_bump(ctx.settings)
    if len(prior) >= 2:                                                                       # the bump was already tried and truncated too: flagged, nothing sent
        record_strategy(ctx.conn, ctx.run_id, key, prior[1], f"stage {stage}", "needs_review", "typed outcome truncated found on the bumped attempt on resume", "stage flagged; no further send")
        return {"status": "flagged", "detail": "truncated twice (ceiling and one bump); stage flagged", "entry": prior[1]}
    bump = factor if prior else None
    model = stage_model(ctx, stage, model) or None                                        # the strong route of the coverage, comparison and enrichment stages, or a caller's route (for example the discovery model); the one resolution the reuse keys share (a route the gateway does not price is the default route)
    if prior:
        record_strategy(ctx.conn, ctx.run_id, key, prior[0], f"stage {stage}", "bump", "typed outcome truncated found on resume", f"one send at {factor}x the ceiling; no identical retry")
    r = _send(ctx, key, contract, msgs, finishing, tools, runner, max_rounds, bump, model=model, extra_body=extra_body, server_tools=server_tools, prompt_tokens_est=prompt_tokens_est)
    if r["status"] == "truncated" and bump is None:
        entry = r.get("entry") or (truncated_attempts(ctx, key) or [None])[-1]           # the truncated attempt's ledger id (the tool loop reports it too; the ledger is the fallback)
        record_strategy(ctx.conn, ctx.run_id, key, entry, f"stage {stage}", "bump", r.get("detail", "output stopped at the ceiling"), f"one send at {factor}x the ceiling ([output].max_bump); no identical retry")
        r = _send(ctx, key, contract, msgs, finishing, tools, runner, max_rounds, factor, model=model, extra_body=extra_body, server_tools=server_tools, prompt_tokens_est=prompt_tokens_est)
    if r["status"] == "truncated":                                                          # the second truncation (fresh, or the bumped send of a resume): needs_review the moment it occurs
        record_strategy(ctx.conn, ctx.run_id, key, r.get("entry") or truncated_attempts(ctx, key)[-1], f"stage {stage}", "needs_review", r.get("detail", "output stopped at the bumped ceiling"), "stage flagged; no further send")
    if r["status"] == "paused":
        return r
    stop = _stop_from_status(r["status"], r.get("detail"))
    if stop and not ((stage == "verify" or tolerate_round_limit) and r["status"] == "round_limit"):   # a Researcher worker whose send allowance is used keeps what its tools stored and is flagged, never the run; a verification composition whose send allowance is used on any path (first, repair, tool-loop continuation) is a settled, flagged composition, never a stage error; stage_failed keeps the stage errors it names
        raise stop
    if r["status"] in ("failed", "invalid", "truncated", "allowance_used", "provider_error", "round_limit", "skipped"):   # skipped: the founder continued without the stage's interrupted call; the stage is flagged, nothing resent
        return {"status": "flagged", "detail": f"{r['status']}: {r.get('detail') or ''}"[:300], "entry": r.get("entry")}
    out = r.get("output")
    reason = getattr(out, "failure_reason", None)
    if not reason:
        return r
    return failure_shape(ctx, stage, key, contract, msgs, reason, finishing=finishing, max_rounds=max_rounds, prior=r)


def failure_shape(ctx: RunContext, stage: str, key: str, contract, msgs: list[dict], reason: str, finishing: bool = False, max_rounds=None, prior: dict | None = None, accept=None) -> dict:
    """A valid answer that fails the stage's contract in substance (the model's failure_reason, or a code rule such as the typed eligible set,
    given as accept(output)): one repair under the stage's single repair allowance (shared with schema repairs), then flagged."""
    r = prior or {}
    accept = accept or (lambda out: True)
    if r.get("entry"):
        react.record(ctx.conn, ctx.run_id, key, r["entry"], f"stage {stage}", "failure shape returned", reason[:300], "one repair under the stage's repair allowance, then flag")
    try:                                                                                      # the one durable repair allowance of the stage instance, shared with schema repairs; no fresh scope
        r2 = ctx.gateway.repair(key, contract, msgs + [prompts.repair_turn(reason)], 0, finishing=finishing, max_rounds=max_rounds, before_dispatch=lambda what: ctx.check_time(f"{stage} {what}"))   # the model that failed answers the repair; the stage's send allowance holds
    except BudgetExceeded as e:
        raise StopRun("budget_reserve_reached", str(e)[:200]) from e
    except UnknownCallPending as e:
        raise StopRun("unknown_call", str(e)[:200]) from e
    except RunStopped as e:
        raise StopRun("run_stopped", str(e)[:200]) from e
    if r2["status"] == "truncated":                                                           # a truncated semantic repair is recorded as needs_review the moment it occurs
        record_strategy(ctx.conn, ctx.run_id, key, r2.get("entry") or truncated_attempts(ctx, key)[-1], f"stage {stage}", "needs_review", r2.get("detail", "repair output stopped at the ceiling"), "stage flagged; no further send")
    stop = _stop_from_status(r2["status"], r2.get("detail"))
    if stop and r2["status"] != "round_limit":
        raise stop
    if r2["status"] == "repaired" and not getattr(r2.get("output"), "failure_reason", None) and accept(r2["output"]):
        return dict(r2, repaired_failure=True)
    why = reason if r2["status"] == "repaired" else f"{reason} | repair: {r2['status']} {r2.get('detail') or ''}"
    ev.emit(ctx.conn, ctx.run_id, "intervention", {"stage": stage, "message": f"{stage} could not comply: {why[:200]}", "step_key": key})
    return {"status": "flagged", "detail": why[:300], "entry": r2.get("entry") or r.get("entry"), "gave_up": True}


# ---------------------------------------------------------------- nodes
def build_workflow(ctx: RunContext, checkpointer, kind: str = "initial"):
    """The compiled graph of a run: the initial workflow (clarify to plan) or, for kind revision, the revision workflow
    (revise to plan). The runner reads the kind from the run row (app/runner/runner.py)."""
    conn, pid, rid = ctx.conn, ctx.project_id, ctx.run_id
    ctx_main = ctx                                                                         # the helpers below take cx (a worker's context) and default to the main one
    CS.use_model(stage_model(ctx, "coverage"))                                             # the coverage stage's route of this run; a stored verdict counts only under it (coverage_step.counts)
    ResearcherOut = roles.researcher_contract()

    def guarded(stage, fn):
        """StopRun inside the node (including the deadline at its dispatch points) becomes the state's stop_reason with its record and events."""
        def node(state: RunState) -> RunState:
            reason = state.get("stop_reason")
            drafting = stage == "plan_generate" and reason in DRAFT_STOPS                  # the draft plan runs after a budget or time stop
            if reason and not drafting:
                return {}
            try:                                                                             # the deadline is checked at every dispatch point, never at node entry: a node whose results replay sends nothing
                out = fn(state)
            except StopRun as s:
                if s.reason == "unknown_call":
                    raise InterruptedCall(stage, s.detail) from s                         # the run pauses for the founder's choice; the checkpoint stays at the node's entry
                if s.reason == "run_stopped" and _paused():
                    raise RunPaused(stage, s.detail) from s                               # the founder's pause; the checkpoint stays, no stop reason enters the state
                if s.reason == "stage_failed" and _skipped_in(stage):
                    s = StopRun("skipped_call", s.detail)                                 # the founder continued without this stage's send: an explicit partial result, the plan drafted from the evidence gathered, not a failed run
                out = _stop(state, stage, s)
            except BudgetExceeded as e:                                                   # a dispatch failure anywhere in the node is the workflow's stop, never an escaped exception
                out = _stop(state, stage, StopRun("budget_reserve_reached", str(e)[:200]))
            except UnknownCallPending as e:
                raise InterruptedCall(stage, str(e)[:200]) from e
            except RunStopped as e:
                if _paused():
                    raise RunPaused(stage, str(e)[:200]) from e
                out = _stop(state, stage, StopRun("run_stopped", str(e)[:200]))
            except CallLimitRefused as e:                                                 # a refusal outside the extract and verify nodes is the run stop call_limit with the draft permitted
                out = _stop(state, stage, StopRun("call_limit", str(e)[:200]))
            _interventions(stage, (out or {}).get("stop_reason") or state.get("stop_reason"))
            return out
        return node

    def _paused() -> bool:
        """The run row paused by the founder (interrupted with a pause reason), as the dispatch guard saw it."""
        row = conn.execute("SELECT status, stop_reason FROM runs WHERE id=?", (rid,)).fetchone()
        return bool(row) and row[0] == "interrupted" and row[1] == "founder_pause"

    def _skipped_in(stage: str) -> bool:
        """An interrupted send of this stage the founder continued without (resolution skipped)."""
        return conn.execute("SELECT 1 FROM usage_ledger WHERE run_id=? AND kind='model' AND state='unknown' AND resolution='skipped' AND step_key LIKE ? LIMIT 1", (rid, f"{rid}:{stage}:%")).fetchone() is not None

    def _interventions(stage: str, stop_reason: str | None) -> None:
        """After every stage, safety.interventions over the observable signals; each signal that fires is announced once per
        stage as an intervention event with the reason and the product's action (the run's own handling of it: the unresolved decision, the
        exposed conflict, the scope question, the drafted plan, the founder's choice on an interrupted call, the partial stage)."""
        try:
            signals = safety.observable_signals(conn, pid, rid, ctx.ledger, stop_reason)
        except Exception as e:                                                               # noqa: BLE001 (a signal read must never turn a finished stage into a failure; the gap is recorded)
            ev.emit(conn, rid, "intervention", {"stage": stage, "message": f"intervention signals not read after {stage}: {type(e).__name__}", "signal": "unread"}); return
        earlier = [json.loads(d) for (d,) in conn.execute("SELECT data_json FROM run_events WHERE run_id=? AND event='intervention'", (rid,))]
        seen = {(e0.get("signal"), e0.get("stage")) for e0 in earlier}
        signals["loop_limit"] = max(0, int(signals.get("loop_limit") or 0) - sum(int(e0.get("value") or 0) for e0 in earlier if e0.get("signal") == "loop_limit"))   # the run-wide count of tool requests refused at a limit is announced once, after the stage that reached the limit (the count since the last announcement), so a later stage is not announced as having reached a limit for an earlier stage's refusals
        for iv in safety.interventions(signals):
            if (iv["signal"], stage) in seen:
                continue
            ev.emit(conn, rid, "intervention", {"stage": stage, "signal": iv["signal"], "message": f"after {stage}: {iv['why']} ({iv['signal']}: {signals.get(iv['signal'])}); {iv['action']}", "action": iv["action"], "value": signals.get(iv["signal"])})

    def _stop(state, stage, s: StopRun) -> RunState:
        ev.announce_missing(conn, rid)                                                    # every tool call of the stage has its event before the pause
        searches, pages = ctx.counts()
        ev.budget(conn, rid, ctx.ledger, searches, pages)
        payload = {"message": f"{stage}: {s.reason} ({s.detail})".strip(), "stage": stage, "reason": s.reason}
        unresolved = []
        if s.reason in DRAFT_STOPS:
            payload["criterion"] = "I4"                                                      # a cap stop is the founder's interrupt I4 (the one draft follows)
            if not conn.in_transaction:
                reconcile.reconcile_project(conn, pid, run_id=rid)                           # the report reads derived state: derive first
            coverage.write_report_into_stop(conn, pid, unresolved, payload, s.reason)        # the report rides on the pause event and into the plan
            payload["message"] += f"; the plan is drafted from the evidence gathered ({len(unresolved)} subject(s) not covered)"
        ev.emit(conn, rid, "pause", payload)
        with conn:
            conn.execute("INSERT INTO errors (run_id, step_key, category, detail) VALUES (?,?,?,?)", (rid, ctx.key(stage), "policy" if s.reason in DRAFT_STOPS else ("credit" if s.reason == "credit_exhausted" else "internal"), f"{s.reason}: {s.detail}"[:500]))
        return {"stop_reason": s.reason, "status": "partial" if s.reason in DRAFT_STOPS else "stopped"}

    # ---- clarify
    def _pause_once(message: str, questions: list, reason: str, criterion: str, **more) -> None:
        """The pause event of a round of questions, written once. A node that pauses is executed again from its top when the founder answers;
        a second pause event after the answer would put the run screen's banner back to "questions need an answer" while research
        runs. The event is skipped when the run's latest pause event already carries the same message and question ids."""
        last = conn.execute("SELECT data_json FROM run_events WHERE run_id=? AND event='pause' ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        prev = json.loads(last[0]) if last and last[0] else {}
        if prev.get("message") == message and [q0.get("id") for q0 in prev.get("questions") or []] == [q0.get("id") for q0 in questions]:
            return
        ev.emit(conn, rid, "pause", {"message": message, "questions": questions, "reason": reason, "criterion": criterion, **more})

    def clarify(state: RunState) -> RunState:
        rnd = int(state.get("round") or 1)
        key = ctx.key("clarify", f"r{rnd}")
        if (d := ctx.done(key)) is not None:
            return {"round": rnd, "questions_open": d["questions"]}
        qa = [{"question": r[0], "answer": json.loads(r[1]) if r[1] else None} for r in conn.execute("SELECT text, answer_json FROM questions WHERE project_id=? ORDER BY id", (pid,))]
        assumptions = [r[0] for r in conn.execute("SELECT text FROM assumptions WHERE project_id=? AND status='open'", (pid,))]
        msgs = prompts.messages("clarify", {"idea": state["idea"], "confirmed_requirements": ctx.requirements(), "open_assumptions": assumptions, "qa": qa, "clarification_round": rnd})
        r = call_stage(ctx, "clarify", key, M.ClarifyOut, msgs)
        if r["status"] == "flagged":
            raise StopRun("stage_failed", f"clarify: {r['detail']}")
        out: M.ClarifyOut = r["output"]
        repo = ctx.repo; version = ctx.requirement_version(); n_q = 0
        with ctx.tx():
            existing = {r[0] for r in conn.execute("SELECT key FROM requirements WHERE project_id=? AND status<>'superseded'", (pid,))}
            new_user_keys = []
            for q in out.requirements:
                if q.key in existing:
                    continue
                stated = conn.execute("SELECT answer_json FROM questions WHERE project_id=? AND requirement_key=? AND kind='hardness' AND status='answered' ORDER BY id DESC LIMIT 1", (pid, q.key)).fetchone()   # the founder's answer to the hardness question of the key, carried unchanged into any later row of it
                confirmed = conn.execute("SELECT answer_json FROM questions WHERE project_id=? AND requirement_key=? AND kind='confirmation' AND status='answered' ORDER BY id DESC LIMIT 1", (pid, q.key)).fetchone()   # the founder's confirmation of the key (yes or a corrected value); the model's source user is a proposal
                founder_said = bool(confirmed and json.loads(confirmed[0]) != "no")
                repo.upsert_requirement(M.Requirement(id=repo.next_id(pid, "REQ"), project_id=pid, key=q.key, value=q.value, status="confirmed" if (q.source == "user" and founder_said) else "assumed",
                                                      source=q.source, hard=bool(stated and json.loads(stated[0]) == "yes"), version=version)); existing.add(q.key)   # the model's hard flag is a proposal, never written to the row
                if q.source == "user":
                    new_user_keys.append((q.key, q.value))
            asked = {r[0] for r in conn.execute("SELECT requirement_key FROM questions WHERE project_id=? AND kind='hardness'", (pid,))}   # one hardness question per key, open or answered, ever
            proposed = [q.key for q in out.requirements if q.hard]
            legacy = [r[0] for r in conn.execute("SELECT DISTINCT key FROM requirements WHERE project_id=? AND hard=1 AND status<>'superseded' ORDER BY key", (pid,))] if rnd == 1 else []   # a hard flag the model set in a stored project gets its question at the first clarify of a new run
            values_now = {r0[0]: json.loads(r0[1]) if r0[1] else None for r0 in conn.execute("SELECT key, value_json FROM requirements WHERE project_id=? AND status<>'superseded'", (pid,))}
            for k in [x for x in dict.fromkeys(proposed + legacy) if x not in asked]:        # one question per key: a key both proposed hard and carrying a legacy flag is asked once
                qid = repo.next_id(pid, "Q")
                conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status, kind) VALUES (?,?,?,?,?,?,'open','hardness')",
                             (qid, pid, W.hardness_question(k, values_now.get(k)), W.HARDNESS_WHY, k, rnd))   # the answer is yes or no against this question's id; the question names the requirement by its label and value, and the why line carries no id
                asked.add(k); n_q += 1
            asked_conf = {r[0] for r in conn.execute("SELECT requirement_key FROM questions WHERE project_id=? AND kind='confirmation'", (pid,))}   # one confirmation question per key, open or answered, ever
            for k, v in new_user_keys:
                if k in asked_conf:
                    continue
                qid = repo.next_id(pid, "Q")
                conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status, kind) VALUES (?,?,?,?,?,?,'open','confirmation')",
                             (qid, pid, W.confirmation_question(k, v), W.CONFIRMATION_WHY, k, rnd))   # the question names the requirement by its label, never by its key
                asked_conf.add(k); n_q += 1
            existing = [r0[0] for r0 in conn.execute("SELECT text FROM assumptions WHERE project_id=?", (pid,)).fetchall()]
            for a in out.assumptions:
                if same_meaning(a, existing):                                                 # an assumption that repeats an existing one in meaning is not stored again
                    continue
                existing.append(a)
                repo.upsert_assumption(M.Assumption(id=repo.next_id(pid, "ASM"), project_id=pid, text=a[:300], impact="medium"))
            for c in out.contradictions:
                repo.upsert_assumption(M.Assumption(id=repo.next_id(pid, "ASM"), project_id=pid, text=f"Contradiction: {c}"[:300], impact="high"))
            if rnd < 2 and not out.ready_for_research:
                founder_keys = {r0[0] for r0 in conn.execute("SELECT r.key FROM requirements r WHERE r.project_id=? AND r.status<>'superseded' AND r.source='user' AND (r.status='confirmed' OR EXISTS (SELECT 1 FROM questions q WHERE q.project_id=r.project_id AND q.requirement_key=r.key AND q.kind='confirmation' AND (q.status='open' OR (q.status='answered' AND q.answer_json<>'\"no\"'))))", (pid,))}   # the keys the founder stated (the confirmation asked in this clarify, answered yes or corrected); a confirmed source-user requirement entered before clarify (the Start screen, the CLI) has no per-key question and is the founder's too: a model's value question never carries one of them
                for q in out.questions[:3]:
                    qid = repo.next_id(pid, "Q")
                    rkey = q.requirement_key if q.requirement_key not in founder_keys else None   # a model's question never carries a key the founder stated, so its answer cannot overwrite the founder's confirmed value (for example, a hosting question the model keyed core_functionality); the answer stays a question-and-answer row the next clarify round reads
                    conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status) VALUES (?,?,?,?,?,?,'open')", (qid, pid, W.founder_text(q.text, values_now), W.founder_text(q.why_blocking, values_now), rkey, rnd))   # a model's question is stored in the founder's language (a key by its label, no id)
                    n_q += 1
            n_q = int(conn.execute("SELECT COUNT(*) FROM questions WHERE project_id=? AND status='open' AND tool_call_id IS NULL", (pid,)).fetchone()[0])   # an open hardness question routes to the ask node whatever ready_for_research says
            ctx.mark(key, "clarify", {"questions": n_q, "requirements": len(out.requirements), "ready": out.ready_for_research})
        ev.step(conn, rid, "clarify", f"Clarify round {rnd}", f"{len(out.requirements)} requirement(s), {len(out.assumptions)} assumption(s), {n_q} question(s)")
        return {"round": rnd, "questions_open": n_q}

    def ask(state: RunState) -> RunState:
        rnd = int(state.get("round") or 1)
        open_qs = [dict(zip(("id", "text", "why_blocking", "requirement_key", "kind"), r)) for r in conn.execute("SELECT id, text, why_blocking, requirement_key, kind FROM questions WHERE project_id=? AND status='open' AND tool_call_id IS NULL ORDER BY id", (pid,))]
        open_all, open_qs = open_qs, open_qs[:QUESTIONS_A_ROUND]                               # up to three questions a round, in the order of their ids; the rest wait for the next round (an answer sent for one of them is taken all the same)
        with conn:
            conn.execute("UPDATE runs SET status='awaiting_user' WHERE id=? AND status='running'", (rid,))
        criteria = sorted({IV.criterion_of(q.get("kind"), revision=bool(state.get("change"))) for q in open_qs}) or [IV.criterion_of(None, revision=bool(state.get("change")))]   # every interrupt names its criterion (I1 the founder's answers before research, I8 a revision's questions); the node's own criterion when every question was answered before the resume (IV.interrupt refuses an interrupt without one)
        _pause_once(f"{len(open_qs)} question(s) need an answer before " + ("the re-run" if state.get("change") else "research"), open_qs, "clarify", criteria[0] if criteria else "I1", criteria=criteria)
        answers = IV.interrupt({"kind": "clarify", "criteria": criteria, "questions": open_qs})   # exactly one interrupt per node
        key = ctx.key("clarify", f"answers{rnd}")
        new_stale: list = []; new_reverify: list = []; declined = False
        if ctx.done(key) is None:
            repo = ctx.repo; version = ctx.requirement_version()
            with ctx.tx():
                for q in open_all:
                    if q not in open_qs and answers.get(q["id"]) is None:
                        continue                                                             # a question of a later round, not asked yet and not answered: it stays open
                    if q.get("kind") == "hardness":                                          # yes or no against the question's id sets the flag and nothing else; an unanswered hardness question stays open (the run waits)
                        a = answers.get(q["id"])
                        word = str(a).strip().lower() if a is not None else None
                        if word == "yes":                                                     # yes and no are the only accepted literals; anything else leaves the question open
                            flag = 1
                        elif word == "no":
                            flag = 0
                        else:
                            continue
                        conn.execute("UPDATE requirements SET hard=? WHERE project_id=? AND key=? AND status<>'superseded'", (flag, pid, q["requirement_key"]))
                        conn.execute("UPDATE questions SET status='answered', answer_json=? WHERE id=?", (json.dumps("yes" if flag else "no"), q["id"]))
                        continue
                    if q.get("kind") == "revision":                                          # yes starts the re-run, no keeps the change and ends the run before any send; anything else leaves the question open (the run waits)
                        a = answers.get(q["id"])
                        word = str(a).strip().lower() if a is not None else None
                        if word not in ("yes", "no"):
                            continue
                        conn.execute("UPDATE questions SET status='answered', answer_json=? WHERE id=?", (json.dumps(word), q["id"]))
                        declined = declined or word == "no"
                        continue
                    if q.get("kind") == "confirmation":                                      # the founder confirms (yes), rejects (no) or corrects (any other answer: the value) the requirement the model marked as the founder's; nothing else changes, the hard flag included; an unanswered question stays open and the run waits
                        a = answers.get(q["id"])
                        if a is None:
                            continue
                        word = str(a).strip().lower()
                        if word == "yes":
                            conn.execute("UPDATE requirements SET status='confirmed' WHERE project_id=? AND key=? AND status='assumed'", (pid, q["requirement_key"]))
                        elif word == "no":
                            conn.execute("UPDATE requirements SET status='superseded' WHERE project_id=? AND key=? AND status='assumed'", (pid, q["requirement_key"]))
                        else:
                            conn.execute("UPDATE requirements SET value_json=?, status='confirmed' WHERE project_id=? AND key=? AND status<>'superseded'", (json.dumps(a), pid, q["requirement_key"]))
                        conn.execute("UPDATE questions SET status='answered', answer_json=? WHERE id=?", (json.dumps(word if word in ("yes", "no") else a), q["id"]))   # the canonical outcome is stored (yes, no) so a later clarify reads it as the parser accepted it; a corrected value is stored as given
                        continue
                    a = answers.get(q["id"], answers.get(q["requirement_key"]))
                    if a is None:
                        conn.execute("UPDATE questions SET status='skipped' WHERE id=?", (q["id"],)); continue
                    conn.execute("UPDATE questions SET status='answered', answer_json=? WHERE id=?", (json.dumps(a), q["id"]))
                    if q["requirement_key"]:
                        existing = conn.execute("SELECT id, value_json FROM requirements WHERE project_id=? AND key=? AND status<>'superseded'", (pid, q["requirement_key"])).fetchone()
                        if existing and state.get("change"):                                     # in a revision run a scope answer that changes an existing requirement is a change like any other: it takes the versioned path (apply_change: the superseding row at the next requirement version, the hard flag carried) and its stale decisions join the run's set; the same value changes nothing
                            if json.dumps(a) != existing[1]:
                                from app.core.invalidation import apply_change
                                changed = apply_change(conn, pid, existing[0], a)
                                new_stale.extend(x for x in changed["stale"]["DEC"] if x not in new_stale)
                                new_reverify.extend(x for x in changed["reverify"] if x not in new_reverify)   # the scope change's impact claims join the change's reverify ids
                        elif existing:
                            conn.execute("UPDATE requirements SET value_json=?, status='confirmed', source='user' WHERE id=?", (json.dumps(a), existing[0]))
                        else:
                            repo.upsert_requirement(M.Requirement(id=repo.next_id(pid, "REQ"), project_id=pid, key=q["requirement_key"], value=a if isinstance(a, (str, int, float, bool, list)) else str(a), status="confirmed", source="user", hard=False, version=version))
                conn.execute("UPDATE runs SET status='running' WHERE id=? AND status='awaiting_user'", (rid,))
                ctx.mark(key, "clarify", {"answered": len(answers), "stale": new_stale, "reverify": new_reverify, "declined": declined})
        else:
            new_stale = list((ctx.done(key) or {}).get("stale") or []); new_reverify = list((ctx.done(key) or {}).get("reverify") or []); declined = bool((ctx.done(key) or {}).get("declined"))
        ev.step(conn, rid, "clarify", f"Answers recorded (round {rnd})", f"{len(answers)} answer(s)")
        still_open = int(conn.execute("SELECT COUNT(*) FROM questions WHERE project_id=? AND status='open' AND tool_call_id IS NULL", (pid,)).fetchone()[0])
        out = {"round": rnd + 1, "questions_open": still_open}
        if state.get("change"):
            out["stale"] = sorted(set(state.get("stale") or []) | set(new_stale))                 # the decisions a scope answer's change made stale are re-decided by this revision
            if new_reverify:
                out["change"] = dict(state["change"], reverify=sorted(set(state["change"].get("reverify") or []) | set(new_reverify)))   # a scope change's impact claims join the current change's reverify ids
        if declined:                                                                         # the founder's no ends the run before any send; the change stands, its stale decisions wait for a later revision
            ev.emit(conn, rid, "pause", {"message": "Revision declined: the founder kept the change and ended the run before any send; the re-opened decisions stay stale for a later revision", "reason": "revision_declined", "criterion": "I8"})
            out.update({"stop_reason": "revision_declined", "status": "stopped", "questions_open": 0})
        return out

    # ---- the subjects a run decides, and the fresh reads of their zero-hour pages
    def _run_subjects(state: RunState):
        """The subjects this run decides: for a revision or a quality run, the subjects of its stale decisions and the planner's additions
        (state decision_subjects); None for an initial run (every subject is its own). The fresh reads and the verification sweep cover these
        subjects only; the claims of other subjects keep their verdicts until a run decides those subjects."""
        if not state.get("stale") and not state.get("change") and not state.get("rediscover"):
            return None
        subs = {AP.norm(s0) for s0 in (state.get("decision_subjects") or []) if s0}
        if state.get("stale"):
            subs |= {AP.norm(r0[0] or "") for r0 in conn.execute(f"SELECT DISTINCT subject FROM decisions WHERE project_id=? AND id IN ({','.join('?' * len(state['stale']))})", (pid, *state["stale"])).fetchall()}
        return sorted(s0 for s0 in subs if s0)

    def _fresh_reads(subjects) -> dict:
        """Every zero-hour page marked stale that carries a claim of the subjects this run decides is read fresh once, through the read tool
        under the run's page limit and budget (durable ids under the worker 'fresh', replayed on resume); the store keeps a verdict whose sentence is
        byte for byte the same and clears a changed one (invalidate_source_refresh), and every claim of the page carries the new READ date. A refused
        read (the page limit, a fetch error) leaves the page marked and its claims as they are. A stub adapter reads fresh only when the settings say so."""
        rcfg = ctx.settings.get("research") or {}
        if getattr(ctx.research, "simulated", False) and not rcfg.get("simulate_fresh_reads"):
            return {"read": 0, "refused": []}
        todo = coverage.sources_for_fresh_read(conn, pid, subjects)
        if not todo:
            return {"read": 0, "refused": []}
        limit = int(ctx.settings["tools"]["stage_limit"])                                   # the configured per-stage bound, never the work list's size (a long list would otherwise be read under one stage instance)
        run_limits = {"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}
        runners: dict[int, ToolRunner] = {}
        read, refused = 0, []
        for i, (sid, url) in enumerate(todo):                                               # page i goes to stage instance i // limit (the workers fresh, fresh1, fresh2, ...: distinct durable ids, the same on resume because todo is sorted by source id)
            k = i // max(limit, 1)
            if k not in runners:
                runners[k] = ToolRunner(conn, ctx.ledger, rid, pid, "research_plan", ctx.research, worker="fresh" if k == 0 else f"fresh{k}", branch=ctx.branch, stage_limit=limit,
                                        run_limits=run_limits, data_dir=ctx.data_dir, settings=ctx.settings, allowed=roles.WORKER_TOOLS)
            ctx.check_time("research_plan fresh read")
            try:
                obs = runners[k].handle({"tool": "read", "args": {"url": url, "freshness": "volatile"}})
                ev.tool(conn, rid, obs["tool_call_id"], "read", "completed", f"{sid} read fresh (a zero-hour page of a subject this run decides, D-A114)", engine="exa", query=url[:120])
                read += 1
            except ToolRejected as e:                                                        # the page limit or a refused fetch: the page keeps its mark, its claims their verdicts
                refused.append({"source_id": sid, "reason": str(e)[:160]})
                if "run page limit" in str(e).lower() or "run search limit" in str(e).lower():   # the run quota alone ends the fresh reads; a stage rejection never passes for it
                    break
            except UnknownCallPending as e:
                raise StopRun("unknown_call", str(e)[:200]) from e
            except RunStopped as e:
                raise StopRun("run_stopped", str(e)[:200]) from e
        ev.step(conn, rid, "research_plan", "Fresh reads", f"{read} zero-hour page(s) read fresh for {len(subjects) if subjects is not None else 'every'} subject(s); {len(refused)} refused; {len(runners)} stage instance(s) of at most {limit} call(s)")
        return {"read": read, "refused": refused}

    # ---- research_plan
    def research_plan(state: RunState) -> RunState:
        key = ctx.key("research_plan")
        if (d := ctx.done(key)) is not None:
            return {"decision_subjects": d["decision_subjects"]}
        run_subjects = _run_subjects(state)
        coverage.invalidate_stale(conn, pid, subjects=run_subjects, mark_fresh=not getattr(ctx.research, "simulated", False) or bool((ctx.settings.get("research") or {}).get("simulate_fresh_reads")))
        fresh = _fresh_reads(run_subjects)
        for r in ctx.requirements(confirmed_only=True):
            coverage.ensure_subject(conn, pid, "requirement", r["id"])
        runner = ToolRunner(conn, ctx.ledger, rid, pid, "research_plan", ctx.research, worker=ctx.worker, branch=ctx.branch, stage_limit=int(ctx.settings["tools"]["stage_limit"]),
                            run_limits={"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}, data_dir=ctx.data_dir, settings=ctx.settings)
        searches, pages = ctx.counts()
        reqs = ctx.requirements(confirmed_only=True)
        stale_subjects = []
        if state.get("rediscover") and state.get("stale"):                                   # a quality run with research plans for the subjects of the stale decisions
            stale_subjects = [r0[0] for r0 in conn.execute(f"SELECT DISTINCT subject FROM decisions WHERE project_id=? AND id IN ({','.join('?' * len(state['stale']))}) ORDER BY subject", (pid, *state["stale"])).fetchall()]
        msgs = prompts.messages("research_plan", {"confirmed_requirements": reqs, "decision_subjects": stale_subjects or state.get("decisions") or "propose them from the requirements", "existing_fresh_claims": [], "search_budget_remaining": int(ctx.settings["budget"]["max_searches"]) - searches})   # the loaded decisions are the decisions to support
        r = call_stage(ctx, "research_plan", key, M.ResearchPlanOutV2, msgs, tools=tools_for("research_plan"), runner=runner)
        if r["status"] == "paused":
            with conn:
                conn.execute("UPDATE runs SET status='awaiting_user' WHERE id=? AND status='running'", (rid,))
            q = conn.execute("SELECT id, text, why_blocking, requirement_key FROM questions WHERE id=?", (r["question_id"],)).fetchone()
            ev.emit(conn, rid, "pause", {"message": "the research planner asks a blocking question", "questions": [dict(zip(("id", "text", "why_blocking", "requirement_key"), q))] if q else [], "reason": "ask_user", "tool_call_id": r["tool_call_id"], "criterion": "I1"})
            answers = IV.interrupt({"kind": "ask_user", "criterion": "I1", "question_id": r["question_id"], "tool_call_id": r["tool_call_id"]})   # the answer is stored by the runner before the resume
            if r["question_id"] in (answers or {}):
                answer_question(conn, r["question_id"], answers[r["question_id"]])
            r = call_stage(ctx, "research_plan", key, M.ResearchPlanOutV2, msgs, tools=tools_for("research_plan"), runner=runner)   # the transcript resumes; the paused tool replays with the answer
            if r["status"] == "paused":
                raise StopRun("stage_failed", "research_plan paused twice in one node")
        for tc in conn.execute("SELECT id, tool, args_json, state, error FROM tool_calls WHERE run_id=? AND stage='research_plan' ORDER BY n", (rid,)).fetchall():   # the rows are read into a list before any write
            if not conn.execute("SELECT 1 FROM run_events WHERE run_id=? AND event='tool' AND data_json LIKE ?", (rid, f'%"tool_call_id": "{tc[0]}"%')).fetchone():
                ev.tool(conn, rid, tc[0], tc[1], tc[3], tc[4] or "")
        subjects, queries = [], []
        planner = {"applicability": None, "candidates": {"kept": 0, "dropped": []}, "gap_searches": [], "language_queries": {}, "missing_language_queries": []}
        if r["status"] not in ("flagged", "replayed") and (r["output"].candidates or r["output"].applicability) and not r["output"].subjects:   # a new typed answer (candidates or an applicability map) without typed subjects is a contract failure: one repair, then flagged; a stored answer replays as it is
            r = failure_shape(ctx, "research_plan", key, M.ResearchPlanOutV2, msgs, "every decision subject must be listed under subjects with its product_class (the candidates and the applicability map cannot be typed without them)", max_rounds=int(ctx.settings["tools"]["stage_limit"]) + 1, prior=r, accept=lambda o0: bool(o0.subjects))
        if r["status"] != "flagged":
            out: M.ResearchPlanOutV2 = r["output"]; subjects = list(out.decision_subjects); queries = [q.model_dump() for q in out.queries]
            for s0 in out.subjects:                                                          # a subject the planner typed but did not list is a subject too
                if AP.norm(s0.name) not in {AP.norm(x) for x in subjects}:
                    subjects.append(s0.name)
            hard_now = [x for x in reqs if x["hard"]]
            with ctx.tx():
                planner["applicability"] = AP.apply_planner_map(conn, pid, hard_now, subjects, [a.model_dump() for a in out.applicability])   # the planner's applicability map, written to the requirements
            classes = {AP.norm(s0.name): AP.norm(s0.product_class) for s0 in out.subjects}
            typed = bool(out.subjects)                                                            # the candidate rules apply to a typed answer; a planner answer without subject classes (the older shape of a stored run) takes no candidate rule
            kept_by: dict[str, list] = {AP.norm(s): [] for s in subjects}
            with ctx.tx():
                for c0 in out.candidates:
                    subj = AP.norm(c0.subject); sclass = classes.get(subj)
                    ok = sclass is None or AP.norm(c0.product_class) == sclass
                    DS.insert_candidate(conn, pid, rid, subj, label=c0.label, vendor=c0.vendor, product=c0.product, version=c0.version, access_route=c0.access_route, kind=c0.kind, product_class=AP.norm(c0.product_class), subject_class=sclass or "",
                                        specialised_for=c0.specialised_for, status="kept" if ok else "dropped", reason=None if ok else f"class {AP.norm(c0.product_class)} is not the subject's {sclass}", replace=True)   # the identity beside the label (a bare label split into vendor and product), the label composed by code
                    if ok:
                        kept_by.setdefault(subj, []).append(c0)
                    else:
                        planner["candidates"]["dropped"].append({"subject": subj, "label": c0.label[:120]})
                        planner["candidates"]["kept"] = sum(len(v) for v in kept_by.values())
            planner["candidates"]["kept"] = sum(len(v) for v in kept_by.values())
            langs_by: dict[str, list[str]] = {}
            hard_now = [dict(h, applies_to=(conn.execute("SELECT applies_to FROM requirements WHERE id=?", (h["id"],)).fetchone() or [None])[0]) for h in hard_now]   # the map just written, read back before it binds the language rule
            for h in hard_now:
                lang = AP.language_of(h)
                if lang:
                    for s1 in subjects:
                        if AP.binds(h, s1):
                            langs_by.setdefault(AP.norm(s1), []).append(lang)
            for s1 in subjects:
                subj = AP.norm(s1); have = kept_by.get(subj, [])
                for lang in dict.fromkeys(langs_by.get(subj, [])):
                    mine = [q for q in queries if AP.norm(q.get("decision_subject")) == subj and (q.get("language") or "en") == lang]
                    planner["language_queries"][f"{subj}:{lang}"] = len(mine)
                    if not mine:
                        planner["missing_language_queries"].append(f"{subj}:{lang}")        # the planner wrote no query in the requirement's language for this subject (recorded; code writes none)
                have = eligible_candidates(conn, pid, s1)["kept"]                            # the one typed eligible set (the subject's kept rows as written above) decides the gap
                gap_why = DS.gap_reason(have, langs_by.get(subj)) if typed else None     # fewer than three typed candidates, or no language-specialised one: the gap is searched once more (one search, in the requirement's language first); the two triggers are in app.core.discovery.gap_reason
                if gap_why:
                    lang = (langs_by.get(subj) or ["en"])[0]
                    gap_q = {"text": f"{s1} {classes.get(subj) or ''}".strip()[:200], "decision_subject": s1, "target_fact": f"candidates: {classes.get(subj) or subj}"[:200], "freshness": "technical", "language": lang}
                    queries.append(gap_q); planner["gap_searches"].append({"subject": subj, "language": lang, "reason": gap_why})
        rqs = coverage.persist_research_questions(conn, pid, {"queries": queries}, run_id=rid) if queries else []
        planner["discovery"] = _discover(key, runner, subjects, classes if r["status"] != "flagged" else {}, reqs, kept_by if r["status"] != "flagged" else {}) if r["status"] != "flagged" else None   # the discovery passes (Perplexity and Parallel), the union, the reads
        planner["reach"] = _reach(runner, subjects, classes if r["status"] != "flagged" else {}, queries, rqs, planner["discovery"]) if planner["discovery"] else {}   # one research question per distinct discovered candidate, within the allowance, before the workers take their tasks
        tasks = _worker_tasks(subjects, queries, rqs)
        if len(tasks) >= 2:                                                                   # two or more independent decision subjects: the Researcher workers, merged by the coordinator
            pursued, workers = _run_research_workers(key, tasks)
        else:
            pursued, workers = _pursue(key, runner, queries, rqs), None
        sources = ctx.run_sources()
        with ctx.tx():                                                                        # a question's base links are its admitted candidates and its claims' sources, written at admission; retrieval alone links nothing
            ctx.mark(key, "research_plan", {"decision_subjects": subjects, "queries": len(queries), "question_ids": rqs, "sources": sources, "pursued": pursued, "flagged": r["status"] == "flagged", "workers": workers, "planner": planner, "fresh_reads": fresh, "run_subjects": run_subjects})
        searches, pages = ctx.counts(); ev.budget(conn, rid, ctx.ledger, searches, pages)
        ev.step(conn, rid, "research_plan", "Research plan", f"{len(subjects)} decision subject(s), {len(queries)} research question(s), {len(sources)} source(s) stored")
        if state.get("rediscover"):
            return {"decision_subjects": list(dict.fromkeys((state.get("decision_subjects") or []) + subjects))}   # the stale subjects stay, the planner's additions join
        return {"decision_subjects": subjects}

    def _discover(key: str, runner: ToolRunner, subjects: list[str], classes: dict, reqs: list, kept_by: dict) -> dict | None:
        """For each typed subject, OpenRouter's agentic search with the Perplexity engine (P) and with the Parallel engine (L),
        each one chat completion through the gateway (the server tools web_search and web_fetch, max_tool_calls at most 12, a session per run
        and subject, the run date and the binding requirements in the prompt, DiscoveryOut): leads only. The union with the Exa pages of the
        run (found_by per URL and product, agreement first) decides what our fetcher reads next (the read tool, the run's page limit), each
        page's found_by recorded; a lead product of the subject's class joins the candidates table (status kept) for the comparison, a lead of
        another class is recorded dropped. Nothing here is evidence."""
        dcfg = ctx.settings.get("discovery") or {}
        engines = [e for e in (dcfg.get("engines") or []) if e in ("perplexity", "parallel")]
        if not engines or not classes or getattr(ctx.research, "simulated", False) and not dcfg.get("simulate"):
            return None
        model = dcfg.get("model") or None
        prices = getattr(ctx.gateway, "prices", None) or {}
        if model and model not in prices:
            model = None
        price = prices.get(model or ctx.gateway.default_model) or (1.0, 5.0)
        max_calls = max(1, min(int(dcfg.get("max_tool_calls", DS.MAX_TOOL_CALLS)), 12))
        summary = {"subjects": {}, "reads": 0, "candidates_added": 0, "dropped": 0, "engines": engines, "model": model or ctx.gateway.default_model}
        run_srcs = {r0[0]: {"url": r0[1], "title": r0[2] or "", "source_id": r0[0]} for r0 in conn.execute(f"SELECT id, url, title FROM sources WHERE project_id=? AND id IN ({','.join('?' * len(ctx.run_sources())) or 'NULL'})", (pid, *ctx.run_sources())).fetchall()}
        usage_row = conn.execute("SELECT answer_json, usage_json FROM questions WHERE project_id=? AND kind='usage' AND status='answered' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        usage = (json.loads(usage_row[1]) if usage_row and usage_row[1] else (json.loads(usage_row[0]) if usage_row and usage_row[0] else "none"))
        hard_now = [dict(h, applies_to=(conn.execute("SELECT applies_to FROM requirements WHERE id=?", (h["id"],)).fetchone() or [None])[0]) for h in reqs if h["hard"]]
        for s1 in subjects:
            subj = AP.norm(s1); sclass = classes.get(subj) or ""
            if not sclass:
                continue
            binding = [{"id": h["id"], "key": h["key"], "value": h["value"]} for h in hard_now if AP.binds(h, s1)]
            known = [c0.label for c0 in kept_by.get(subj, [])]
            found: list[tuple[str, dict]] = []
            for engine in engines:
                dkey = f"{rid}:discover:{ctx.worker}:{ctx.branch}:{re.sub(r'[^a-z0-9]+', '-', subj)[:40]}:{engine}"
                msgs = prompts.messages("discover", {"decision_subject": s1, "product_class": sclass, "binding_requirements": binding, "known_candidates": known, "usage": usage})
                ev.step(conn, rid, "discover", f"Searching with {DS.ENGINES[engine]['label']}", f"{s1[:80]}: candidates of class {sclass}")
                n_res = int(dcfg.get("max_results", 10)); n_fetch = int(dcfg.get("fetch_uses", 4))
                r0 = call_stage(ctx, "discover", dkey, M.DiscoveryOut, msgs, model=model,
                                extra_body=DS.extra_body(f"{rid}:{subj}", max_calls),   # max_tool_calls is the step bound the provider enforces; no stop_server_tools_when (it would override that bound by a cost condition the wire did not enforce); the gateway reserves the worst case of this very bound (DS.request_estimate)
                                server_tools=[DS.search_tool(engine, n_res, int(dcfg.get("max_total_results", 40))), DS.fetch_tool(n_fetch)],
                                prompt_tokens_est=DS.estimate_as_prompt_tokens(engine, max_calls, price, max_results=n_res, fetch_uses=n_fetch))
                if r0["status"] == "flagged":
                    summary["subjects"].setdefault(subj, {})[engine] = f"flagged: {r0.get('detail', '')[:120]}"
                    continue
                out = r0["output"].model_dump()
                for a in r0.get("annotations") or []:                                       # the URLs the engine read, from the url_citation annotations
                    if a.get("url") and a["url"] not in out["urls_read"]:
                        out["urls_read"].append(a["url"])
                found.append((engine, out))
                summary["subjects"].setdefault(subj, {})[engine] = {"candidates": len(out["candidates"]), "urls": len(out["urls_read"]), "cost_usd": r0.get("cost_usd")}
                ev.tool(conn, rid, dkey, "search", "completed", f"{len(out['candidates'])} lead(s), {len(out['urls_read'])} URL(s) read", engine=engine, query=s1[:120])
            if not found:
                continue
            u = DS.union(list(run_srcs.values()), found, [{"label": c0.label, "product_class": c0.product_class} for c0 in kept_by.get(subj, [])])
            with ctx.tx():
                for pr in u["products"]:
                    if "planner" in pr["found_by"] and pr["agreement"] == 0:
                        continue
                    pclass = AP.norm(pr.get("product_class") or "")
                    ok = (not pclass) or pclass == sclass
                    DS.insert_candidate(conn, pid, rid, subj, vendor=pr["vendor"], product=pr["product"], access_route=pr.get("access_route") or "", urls=pr.get("urls") or [], product_class=pclass or sclass, subject_class=sclass,
                                        status="kept" if ok else "dropped", reason=f"found by {', '.join(e for e in pr['found_by'] if e != 'planner')}" if ok else f"class {pclass} is not the subject's {sclass} (found by {', '.join(pr['found_by'])})")   # vendor and product from the lead, its plan or mode as the access route (a facet of the one record), its urls kept; the label composed by code
                    summary["candidates_added" if ok else "dropped"] += 1
            # the reads: the union's top URLs our store does not hold yet, by agreement, within the subject's allowance and the run's page limit
            to_read = [x for x in u["urls"] if not x.get("source_id") and not conn.execute("SELECT 1 FROM sources WHERE project_id=? AND canonical_url LIKE ?", (pid, x["url"].replace("https://", "%") + "%")).fetchone()][: int(dcfg.get("reads_per_subject", 4))]
            for x in to_read:
                fresh = "volatile" if re.search(r"pric|plan|cost|billing", x["url"]) else "technical"
                req = {"tool": "read", "args": {"url": x["url"], "freshness": fresh}}
                try:
                    if not runner.is_replay(req):
                        ctx.check_time("discover read")
                    obs = runner.handle(req)
                    for pg in obs.get("data") or []:
                        if isinstance(pg, dict) and pg.get("source_id"):
                            from app.adapters.research import record_found_by
                            record_found_by(conn, pg["source_id"], x["found_by"])
                    summary["reads"] += 1
                    ev.tool(conn, rid, obs["tool_call_id"], "read", "completed", f"read {x['url'][:80]} (found by {', '.join(x['found_by'])})")
                except ToolRejected as e:
                    summary.setdefault("read_refusals", []).append(str(e)[:120])
                    break
                except (BudgetExceeded, UnknownCallPending, RunStopped) as e:
                    raise StopRun("budget_reserve_reached" if isinstance(e, BudgetExceeded) else ("unknown_call" if isinstance(e, UnknownCallPending) else "run_stopped"), str(e)[:200]) from e
            summary["subjects"].setdefault(subj, {})["union"] = {"urls": len(u["urls"]), "products": len(u["products"]), "read": len(to_read)}
            summary["subjects"][subj]["products"] = [{k0: pr.get(k0) for k0 in ("vendor", "product", "product_class", "access_route", "found_by", "agreement")} | {"urls": list(pr.get("urls") or [])[:4]} for pr in u["products"]]   # the union's ranked products, read by _reach
        ev.step(conn, rid, "discover", "Discovery union", f"{len(summary['subjects'])} subject(s); {summary['candidates_added']} candidate(s) added, {summary['dropped']} dropped; {summary['reads']} page(s) read")
        return summary

    def _reach(runner: ToolRunner, subjects: list[str], classes: dict, queries: list[dict], rqs: list[str], discovery: dict) -> dict:
        """The research reach: after the discovery passes, one research question per distinct discovered candidate of
        the subject's class (app.core.discovery.reach_questions: distinct by canonical identity, in the union's own order, a planned query's candidate
        skipped, each naming the vendor pricing or documentation URL the pass read), persisted as the planner's questions are and appended to the
        queries the workers pursue. Within the run's existing limits and no new one: per subject the stage limit ([tools].stage_limit, the Researcher
        worker's stage instance) less the subject's planned questions and the two calls kept for the Researcher's own requests (DS.REACH_RESERVE); with
        one subject the coordinator's own stage instance, whose planner and discovery calls count; and across subjects the run's searches left
        ([budget].max_searches less the searches made and the questions already planned). The trace: one step per subject with the ids."""
        stage_limit = int(ctx.settings["tools"]["stage_limit"]); searches, _pages = ctx.counts()
        left = int(ctx.settings["budget"]["max_searches"]) - searches - len(queries)
        executed = conn.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND step_key=? AND state<>'rejected'", (rid, runner.step_key)).fetchone()[0] if len(subjects) < 2 else 0   # one subject: the coordinator's stage instance pursues the questions itself
        out: dict = {}
        for s1 in subjects:
            subj = AP.norm(s1); prods = ((discovery.get("subjects") or {}).get(subj) or {}).get("products") or []
            if not prods or left <= 0:
                continue
            planned = [q for q in queries if AP.norm(q.get("decision_subject") or "") == subj]
            room = max(0, min(stage_limit - executed - len(planned) - DS.REACH_RESERVE, left))
            qs = DS.reach_questions(s1, classes.get(subj) or "", prods, planned, room, DS.run_date()[:4])
            if not qs:
                out[subj] = {"questions": [], "allowance": room}
                continue
            plain = [{"text": q["text"], "decision_subject": q["decision_subject"], "target_fact": q["target_fact"], "include_domains": [], "recency_days": None, "freshness": q["freshness"], "language": q["language"]} for q in qs]
            ids = coverage.persist_research_questions(conn, pid, {"queries": plain}, run_id=rid)
            queries.extend(plain); rqs.extend(ids); left -= len(qs)
            out[subj] = {"questions": [{"rq": rq, "identity": q["identity"], "url": q["url"]} for q, rq in zip(qs, ids)], "allowance": room}
            ev.step(conn, rid, "research_plan", "Reach", f"{s1[:60]}: {len(qs)} research question(s) for the discovered candidates ({room} allowed by the stage limit and the searches left)", questions=ids)
        return out

    def _worker_tasks(subjects: list[str], queries: list[dict], rqs: list[str]) -> list[dict]:
        """One independent task per decision subject the planner named, with the (query, question id) pairs it proposed for the subject;
        a query naming no listed subject joins the first task."""
        tasks = [{"task_id": f"t{n}", "subject": subj, "pairs": []} for n, subj in enumerate(subjects, start=1)]
        by_norm = {coverage._norm(t["subject"]): t for t in tasks}
        for q, rq in zip(queries, rqs):
            t = by_norm.get(coverage._norm(q.get("decision_subject") or ""))
            (t or (tasks[0] if tasks else None) or {"pairs": []})["pairs"].append((q, rq))
        return tasks

    def _run_research_workers(key: str, tasks: list[dict]) -> tuple[list[dict], dict]:
        """Up to [workers].max_research_workers Researcher workers (app/core/roles.run_workers, clamped at three), one per independent decision
        subject, each on its own context (handle, ledger, gateway; the counters are the store's), each pursuing its subject's questions and then
        asking the Researcher role once with the search and read tools (never ask_user: a worker cannot pause the run) for EvidenceProposal records;
        the coordinator (merge_proposals) validates, deduplicates by claim identity with provenance, exposes conflicts and refuses writes to
        confirmed requirements and active decisions; the results are persisted (worker_results) and announced (worker events, a step). A run stop
        raised inside a worker ends the run after the workers return. The proposals are the Researcher's record; the claims the plan cites still
        come from the extraction and verification contracts unchanged."""
        nmax = int(ctx.settings.get("workers", {}).get("max_research_workers", roles.MAX_WORKERS))
        pursued_all: list = []; stops: list = []; found_by: dict = {}
        subject_of = {t["task_id"]: t["subject"] for t in tasks}

        def worker_fn(wid, task):
            cx = ctx.fork(worker=wid)
            try:
                wkey = cx.key("research_plan")
                wrunner = ToolRunner(cx.conn, cx.ledger, rid, pid, "research_plan", cx.research, worker=wid, branch=cx.branch, stage_limit=int(ctx.settings["tools"]["stage_limit"]),
                                     run_limits={"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}, data_dir=ctx.data_dir, settings=ctx.settings,
                                     allowed=roles.WORKER_TOOLS)                           # a worker's tools are search and read; any other request is refused in code before any side effect (ask_user belongs to the coordinator)
                try:
                    pursued_all.extend(_pursue(wkey, wrunner, [q for q, _ in task["pairs"]], [rq for _, rq in task["pairs"]], cx=cx))
                    found = []
                    for (res,) in cx.conn.execute("SELECT result_json FROM tool_calls WHERE run_id=? AND step_key=? AND tool='search' AND state='completed' ORDER BY n", (rid, wkey)):
                        for pg in (json.loads(res) if res else []) or []:
                            if isinstance(pg, dict) and pg.get("source_id") and pg["source_id"] not in {f["source_id"] for f in found}:
                                found.append({"source_id": pg["source_id"], "source_version": pg.get("source_version"), "url": pg.get("url"), "title": (pg.get("title") or "")[:120]})
                    found_by[task["task_id"]] = [f["source_id"] for f in found]
                    msgs = prompts.messages("researcher_worker", {"task_id": task["task_id"], "subject": task["subject"], "facts": [q.get("target_fact") for q, _ in task["pairs"]], "worker": wid, "sources": found})
                    r0 = call_stage(cx, "research_plan", wkey, ResearcherOut, msgs, tools=[t for t in tools_for("research_plan") if t["function"]["name"] != "ask_user"], runner=wrunner, tolerate_round_limit=True)   # a worker that reads past its allowance is a failed worker with its searches and reads stored, not a failed run
                    if r0["status"] == "paused":
                        raise RuntimeError("the worker asked the founder a question; a worker cannot pause the run")
                    if r0["status"] == "flagged":
                        raise RuntimeError(f"researcher flagged: {r0['detail']}"[:200])
                    return [dict(pr.model_dump(), worker=wid, task_id=task["task_id"]) for pr in r0["output"].proposals]
                except (StopRun, BudgetExceeded, UnknownCallPending, RunStopped, CallLimitRefused) as e:
                    stops.append((task["task_id"], e)); raise
            finally:
                cx.close_fork()

        t0 = time.monotonic()
        results = roles.run_workers(tasks, worker_fn, max_workers=nmax)
        ctx.tick()                                                                            # the parallel interval, counted once
        wall = time.monotonic() - t0
        if stops:
            _, e = sorted(stops, key=lambda x: x[0])[0]
            if isinstance(e, StopRun):
                raise e
            if isinstance(e, BudgetExceeded):
                raise StopRun("budget_reserve_reached", str(e)[:200]) from e
            if isinstance(e, UnknownCallPending):
                raise StopRun("unknown_call", str(e)[:200]) from e
            if isinstance(e, RunStopped):
                raise StopRun("run_stopped", str(e)[:200]) from e
            raise e
        with coverage.derived(conn, pid, run_id=rid):                                     # a decision's status is derived: read inside the accessor's reads context
            confirmed_ids = {r0[0] for r0 in conn.execute("SELECT id FROM requirements WHERE project_id=? AND status='confirmed'", (pid,))} | {r0[0] for r0 in conn.execute("SELECT id FROM decisions WHERE project_id=? AND status='active'", (pid,))}
        merge = roles.merge_proposals(confirmed_ids, results)
        roles.persist_results(conn, rid, results)
        for wr in results:
            cost = float(conn.execute("SELECT COALESCE(SUM(reported_usd),0) FROM usage_ledger WHERE run_id=? AND step_key LIKE ?", (rid, f"{rid}:research_plan:{wr.worker}:%")).fetchone()[0] or 0)
            if not conn.execute("SELECT 1 FROM run_events WHERE run_id=? AND event='worker' AND data_json LIKE ?", (rid, f'%"worker": "{wr.worker}"%')).fetchone():
                ev.emit(conn, rid, "worker", {"worker": wr.worker, "task": f"{wr.task_id}: {subject_of.get(wr.task_id, '')[:80]}", "state": wr.status + (f" ({wr.error})" if wr.error else "") + f"; {len(wr.proposals)} proposal(s), {len(found_by.get(wr.task_id, []))} source(s)", "cost": f"${cost:.4f}"})
        summary = {"workers": len(results), "completed": sum(1 for r0 in results if r0.status == "completed"), "failed": [(r0.worker, r0.error) for r0 in results if r0.status == "failed"], "proposals": sum(len(r0.proposals) for r0 in results),
                   "accepted": len(merge["accepted"]), "duplicates": merge["duplicates"], "passages": merge["passages"], "conflicts": merge["conflicts"], "rejected": len(merge["rejected"]), "malformed": len(merge["malformed"]),
                   "sources_by_task": found_by, "wall_seconds": round(wall, 1), "max_workers": max(1, min(nmax, roles.MAX_WORKERS))}
        ev.step(conn, rid, "research_plan", "Research workers", f"{summary['workers']} worker(s) ({summary['completed']} completed, {len(summary['failed'])} failed) in {summary['wall_seconds']} s; {summary['proposals']} proposal(s): {summary['accepted']} accepted, {summary['duplicates']} duplicate(s), {len(summary['conflicts'])} conflict(s), {summary['rejected']} refused write(s), {summary['malformed']} malformed")
        return pursued_all, summary

    def _pursue(key: str, runner: ToolRunner, queries: list[dict], rqs: list[str], cx=None) -> list[dict]:
        """Every research question is pursued until it has evidence or a recorded reason; the query is the model's own.
        Shared by the research_plan node and the revise node of a revision run, whose new queries are research_plan searches;
        a Researcher worker pursues its subject's questions on its own context (cx)."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        pursued = []
        deep_ok = not getattr(ctx.research, "simulated", False) and "deep" in (ctx.settings.get("research") or {}).get("discovery_type", "deep")   # the first query of a subject runs deep with the subject's other queries as additionalQueries (the live adapter only)
        first_of: dict[str, int] = {}
        for n0, q0 in enumerate(queries):
            first_of.setdefault(coverage._norm(q0.get("decision_subject") or ""), n0)
        for n0, (q, rq) in enumerate(zip(queries, rqs)):                                     # the mapping is positional and explicit, one id per input query
            if any(coverage.query_matches_question(sq, q["text"], q["target_fact"]) for sq, _ in _searches(cx)):
                continue
            req = {"tool": "search", "args": {"query": q["text"][:200], "freshness": q.get("freshness", "technical"), "num_results": int(ctx.settings["research"].get("num_results", 8)), "language": (q.get("language") or "en")[:8]}}   # the query's language is part of the request (the cache key)
            subj0 = coverage._norm(q.get("decision_subject") or "")
            variants = [q1["text"][:200] for n1, q1 in enumerate(queries) if n1 != n0 and coverage._norm(q1.get("decision_subject") or "") == subj0]
            if deep_ok and first_of.get(subj0) == n0 and variants:
                req["args"]["search_type"] = "deep"; req["args"]["additional_queries"] = variants[:10]
                req["args"]["num_results"] = 10
            if not runner.is_replay(req):
                ctx.check_time("research_plan search")                                        # only before a new dispatch; a replayed search sends nothing
            try:
                obs = runner.handle(req)
                n = len(obs["data"]) if isinstance(obs.get("data"), list) else 0
                ev.tool(conn, rid, obs["tool_call_id"], "search", "completed", f"research question {rq}: {n} result(s)" + (" (deep, with the subject's variants)" if req["args"].get("search_type") == "deep" else ""), engine="exa", query=q["text"][:120])
                react.record(conn, rid, key, obs["tool_call_id"], f"pursue research question {rq}", "search with the question's query", f"{n} result(s)" if n else "no result", "sources linked to the question" if n else "question stays pending: no result")
                pursued.append({"rq": rq, "tool_call_id": obs["tool_call_id"], "results": n, "reason": None if n else "no result"})
            except ToolRejected as e:
                reason = str(e)[:200]
                if e.tool_call_id:
                    ev.tool(conn, rid, e.tool_call_id, "search", "rejected", f"research question {rq}: {reason}")
                    react.record(conn, rid, key, e.tool_call_id, f"pursue research question {rq}", "search refused", reason, "question stays pending: " + reason[:120])
                with conn:
                    conn.execute("INSERT INTO errors (run_id, step_key, category, detail) VALUES (?,?,?,?)", (rid, key, "policy", f"research question {rq} not searched: {reason}"))
                pursued.append({"rq": rq, "tool_call_id": e.tool_call_id, "results": 0, "reason": reason})
            except BudgetExceeded as e:                                                     # the reserve refused a search: the workflow's budget stop
                raise StopRun("budget_reserve_reached", f"research question {rq}: {str(e)[:160]}") from e
            except UnknownCallPending as e:
                raise StopRun("unknown_call", str(e)[:200]) from e
            except RunStopped as e:
                raise StopRun("run_stopped", str(e)[:200]) from e
        return pursued

    # ---- revise (the first node of a revision run, kind revision)
    def revise(state: RunState) -> RunState:
        rnd = int(state.get("round") or 1)
        key = ctx.key("revise", f"r{rnd}")
        change = dict(state.get("change") or {}); stale = [x for x in (state.get("stale") or [])]
        if (d := ctx.done(key)) is not None:
            return {"round": rnd, "questions_open": d["questions"], "decision_subjects": d["subjects"], "change_summary": d["summary"]}
        open_before = int(conn.execute("SELECT COUNT(*) FROM questions WHERE project_id=? AND status='open' AND tool_call_id IS NULL AND kind IN ('revision', 'hardness')", (pid,)).fetchone()[0])
        if open_before:                                                                          # the revision question and the stated hardness are answered at the ask node before this node's first send
            return {"round": rnd, "questions_open": open_before}
        repo = ctx.repo
        if change.get("kind") == "usage":                                                        # a corrected usage answer re-decides the cost-bearing decisions on the evidence in the store: no send here, no question, no new query
            with coverage.derived(conn, pid, run_id=rid):
                subjects_u = list(dict.fromkeys(r0[0] for r0 in conn.execute(f"SELECT subject FROM decisions WHERE project_id=? AND id IN ({','.join('?' * len(stale)) or 'NULL'}) ORDER BY id", (pid, *stale)).fetchall()))
                carried_u = [r0[0] for r0 in conn.execute(f"SELECT subject FROM decisions WHERE project_id=? AND status IN ('active','unresolved') AND id NOT IN ({','.join('?' * len(stale)) or 'NULL'}) ORDER BY id", (pid, *stale)).fetchall()]   # the current decisions the usage record does not price, carried unchanged
            summary_u = f"The usage figures were corrected; {len(stale)} decision(s) the usage figures price are decided again on the evidence already verified" + (f", {len(carried_u)} carried unchanged." if carried_u else ".")
            with ctx.tx():
                ctx.mark(key, "revise", {"questions": 0, "question_ids": [], "subjects": subjects_u, "summary": summary_u, "redo": list(stale), "refused": [], "reuse": [], "queries": 0, "flags": [], "queries_deferred_to_planner": False})
            ev.step(conn, rid, "revise", f"Revision round {rnd}", W.correction_groups(subjects_u, carried_u))   # both groups in the founder's words; the comparison is sent for the first group only
            return {"round": rnd, "questions_open": 0, "decision_subjects": subjects_u, "change_summary": summary_u}
        with coverage.derived(conn, pid, run_id=rid):                                           # decision and claim statuses are derived state
            rows = conn.execute(f"SELECT id, subject, choice, rationale, status FROM decisions WHERE project_id=? AND id IN ({','.join('?' * len(stale)) or 'NULL'}) ORDER BY id", (pid, *stale)).fetchall()
            stale_rows = [dict(zip(("id", "subject", "choice", "rationale", "status"), r)) for r in rows]
            stale_subjects_norm = {AP.norm(x["subject"] or "") for x in stale_rows}
            mine_rev = coverage.claim_subjects(conn, pid)
            cap_rev = int((ctx.settings.get("budget") or {}).get("revision_fresh_claims_per_subject", 80))
            fresh, shown_rev, not_shown_rev = [], {}, {}
            for c in ctx.run_claims():                                                            # the revision model sees the fresh claims of the subjects it re-decides, at most cap_rev a subject, and the count of the rest; sending every claim of the project made the input very large and the model's list of claims to reuse overran its output ceiling
                if c["status"] not in ("supported", "partial"):
                    continue
                subj0 = next(iter(sorted(mine_rev.get(c["id"], set()) & stale_subjects_norm)), None)
                if subj0 is None:
                    continue
                if shown_rev.get(subj0, 0) < cap_rev:
                    fresh.append({"claim_id": c["id"], "text": c["text"], "status": c["status"], "scope_status": c["scope_status"], "decision_subject": c.get("decision_subject")}); shown_rev[subj0] = shown_rev.get(subj0, 0) + 1
                else:
                    not_shown_rev[subj0] = not_shown_rev.get(subj0, 0) + 1
            if not_shown_rev:
                fresh.append({"more_fresh_claims_not_listed_by_subject": not_shown_rev})
        answered = []
        for earlier in range(1, rnd):                                                             # the scope questions of the earlier rounds with the founder's answers (the revise prompt)
            m = ctx.done(ctx.key("revise", f"r{earlier}")) or {}
            for qid in m.get("question_ids", []):
                q = conn.execute("SELECT text, answer_json, status FROM questions WHERE id=?", (qid,)).fetchone()
                if q and q[2] == "answered":
                    answered.append({"question": q[0], "answer": json.loads(q[1]) if q[1] else None})
        plan_version = int(conn.execute("SELECT COALESCE(MAX(version),0) FROM plan_versions WHERE project_id=? AND snapshot_sig IS NOT NULL", (pid,)).fetchone()[0])   # the latest signed plan version
        hard_rows = [x for x in ctx.requirements(confirmed_only=True) if x["hard"]]
        subjects_now = list(dict.fromkeys(x["subject"] for x in stale_rows))
        applic = [{"requirement_id": h["id"], "key": h["key"], "value": h["value"], "binds": [s for s in subjects_now if AP.binds(h, s)], "language": AP.language_of(h)} for h in hard_rows]   # the applicability map and the language rule as inputs
        msgs = prompts.messages("revise", {"old_value": change.get("old_value"), "new_value": change.get("new_value"), "requirement_key": change.get("requirement_key"), "stale_decisions": stale_rows,
                                           "fresh_claims": fresh, "plan_version": plan_version, "scope_answers": answered or "none", "applicability": applic})
        r = call_stage(ctx, "revise", key, M.RevisionOut, msgs)
        if r["status"] == "flagged":
            raise StopRun("stage_failed", f"revise: {r['detail']}")
        out: M.RevisionOut = r["output"]
        subjects = list(dict.fromkeys(x["subject"] for x in stale_rows))                        # every stale decision is re-decided (the set the invalidation computed); the model's list is checked against it, never widened
        redo = [x for x in out.decision_ids_to_redo if x in stale]; refused = [x for x in out.decision_ids_to_redo if x not in stale]
        if refused and r.get("entry"):
            react.record(conn, rid, key, r["entry"], "restrict the revision to the stale set", "decision ids outside the stale set refused", f"{len(refused)} id(s) refused", "only the stale decisions computed by invalidate are re-decided")
        if out.scope_questions:                                                                  # the change's reach is unclear: the questions are persisted and the run pauses (the ask node) in every allowed round; with the rounds exhausted the run stops with the question recorded, never planning as if it were settled
            qids = []
            with ctx.tx():
                for q in out.scope_questions[:2]:
                    qid = repo.next_id(pid, "Q")
                    conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status) VALUES (?,?,?,?,?,?,'open')", (qid, pid, q.text, q.why_blocking, q.requirement_key, rnd)); qids.append(qid)
                n_q = int(conn.execute("SELECT COUNT(*) FROM questions WHERE project_id=? AND status='open' AND tool_call_id IS NULL", (pid,)).fetchone()[0])
                ctx.mark(key, "revise", {"questions": n_q, "question_ids": qids, "subjects": [], "summary": out.change_summary, "redo": redo, "refused": refused, "queries": 0})
            asked_rounds = sum(1 for earlier in range(1, rnd) if (ctx.done(ctx.key("revise", f"r{earlier}")) or {}).get("questions"))   # the rounds that asked scope questions (a round that only answered the revision question is none)
            if asked_rounds >= SCOPE_ROUNDS:
                ev.step(conn, rid, "revise", f"Revision round {rnd}", f"{len(stale)} stale decision(s); {n_q} scope question(s) still open after {SCOPE_ROUNDS} rounds")
                raise StopRun("stage_failed", f"revise: the scope question(s) {', '.join(qids)} stay open after {SCOPE_ROUNDS} rounds of scope questions; answer them and open a new revision")
            ev.step(conn, rid, "revise", f"Revision round {rnd}", f"{len(stale)} stale decision(s); {n_q} scope question(s) for the founder")
            return {"round": rnd, "questions_open": n_q, "change_summary": out.change_summary}
        queries = [q.model_dump() for q in out.new_queries[:4]]
        deferred = bool(queries) and bool(state.get("rediscover"))                               # with research, the planner and the engines search for the stale subjects: the queries are left to the planner node, because a pursue here would mark the research_plan key done and the planner node would return at its done-check without a send
        if queries and not deferred:                                                                               # new research for new gaps only: at most four queries, pursued as research_plan searches (the search tool belongs to the research_plan stage) under the run's cap and limits
            rkey = ctx.key("research_plan")
            runner = ToolRunner(conn, ctx.ledger, rid, pid, "research_plan", ctx.research, worker=ctx.worker, branch=ctx.branch, stage_limit=int(ctx.settings["tools"]["stage_limit"]),
                                run_limits={"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}, data_dir=ctx.data_dir, settings=ctx.settings)
            rqs = coverage.persist_research_questions(conn, pid, {"queries": queries}, run_id=rid)
            pursued = _pursue(rkey, runner, queries, rqs)
            with ctx.tx():
                ctx.mark(rkey, "research_plan", {"decision_subjects": subjects, "queries": len(queries), "question_ids": rqs, "sources": ctx.run_sources(), "pursued": pursued, "flagged": False})
        with ctx.tx():
            ctx.mark(key, "revise", {"questions": 0, "question_ids": [], "subjects": subjects, "summary": out.change_summary, "redo": redo, "refused": refused, "reuse": list(out.reuse_claim_ids), "queries": len(queries), "flags": list(out.consistency_flags), "queries_deferred_to_planner": deferred})
        searches, pages = ctx.counts(); ev.budget(conn, rid, ctx.ledger, searches, pages)
        ev.step(conn, rid, "revise", f"Revision round {rnd}", f"{len(stale)} stale decision(s) to re-decide, {len(redo)} named by the model, {len(queries)} new quer{'y' if len(queries) == 1 else 'ies'}, {len(out.reuse_claim_ids)} claim(s) to reuse")
        return {"round": rnd, "questions_open": 0, "decision_subjects": subjects, "change_summary": out.change_summary}

    def _searches(cx=None) -> list[tuple[str, list[str]]]:
        """(query, source ids) of every completed search of the research stage, in order."""
        cx = cx or ctx_main; conn = cx.conn
        out = []
        for args, res in conn.execute("SELECT args_json, result_json FROM tool_calls WHERE run_id=? AND stage='research_plan' AND tool='search' AND state='completed' ORDER BY created_at, n", (rid,)):
            a = json.loads(args); pages = json.loads(res) if res else []
            out.append((a.get("query", ""), [p["source_id"] for p in pages if isinstance(p, dict) and p.get("source_id")]))
        return out

    # ---- extract_evidence (subjects drive extraction; candidates and order from retrieval; verification per source in groups; headroom)
    VERDICT_TOKENS_E2C = 137          # the measured size of one verdict in tokens (reasoning included); the group shrinks when the verify ceiling would not hold one verdict per claim; the ceiling is never raised

    def _xs(name):
        return ctx.settings["extraction"][name]

    def _mean_seconds(stage: str, default: float, cx=None) -> float:
        """The run's measured mean seconds per send of a stage (usage_ledger.latency_ms of its own rows), or the settings default before any measurement."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        row = conn.execute("SELECT AVG(latency_ms) FROM usage_ledger WHERE run_id=? AND kind='model' AND latency_ms IS NOT NULL AND step_key LIKE ?", (rid, f"%:{stage}:%")).fetchone()
        return float(row[0]) / 1000.0 if row and row[0] is not None else float(default)

    def _mean_other_seconds(default: float, cx=None) -> float:
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        row = conn.execute("SELECT AVG(latency_ms) FROM usage_ledger WHERE run_id=? AND kind='model' AND latency_ms IS NOT NULL AND step_key NOT LIKE '%:extract_evidence:%' AND step_key NOT LIKE '%:verify:%'", (rid,)).fetchone()
        return float(row[0]) / 1000.0 if row and row[0] is not None else float(default)

    def _group_size() -> int:
        return max(1, min(int(_xs("claims_per_verify_send")), int(ctx.settings["output"]["verify"]) // VERDICT_TOKENS_E2C))

    def _reverify_first(state) -> list[str]:
        """The order of a revision's verification within a source: the claims the re-opened decisions
        cite, then the rest of the change's re-verification set; the page's other pending claims after them. [] outside a revision."""
        ch = state.get("change") or {}
        cited = sorted(set(ch.get("reverify_cited") or []))
        return cited + sorted(set(ch.get("reverify") or []) - set(cited))

    def _pending_claims(sid: str, ids: list | None = None, cx=None, first: list | None = None) -> list[dict]:
        """The claims of the source at its current version without a verdict (claims.verdict is a base column; a superseded version's claims wait for re-extraction);
        with ids, those claims of the source at its current version whatever their verdict (a composed group is re-sent as composed, so the replay matches).
        With first, the pending claims in that order ahead of the rest, the id order kept within each rank."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        cur = conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (sid, pid)).fetchone()
        if not cur:
            return []
        if ids is None:
            rows = conn.execute("SELECT id, text, kind, quote, range_start, range_end, conditions_json, source_version FROM claims WHERE project_id=? AND source_id=? AND source_version=? AND verdict IS NULL ORDER BY id", (pid, sid, cur[0])).fetchall()
            if first:
                rank = {cid: n for n, cid in enumerate(first)}
                rows.sort(key=lambda r: rank.get(r[0], len(rank)))                                  # the cited claims, the change's set, the rest; stable, so the id order holds within each rank
        else:
            rows = [r for cid in ids for r in conn.execute("SELECT id, text, kind, quote, range_start, range_end, conditions_json, source_version FROM claims WHERE project_id=? AND source_id=? AND source_version=? AND id=?", (pid, sid, cur[0], cid)).fetchall()]
        return [dict(zip(("id", "text", "kind", "quote", "range_start", "range_end", "conditions_json", "source_version"), r)) for r in rows]

    def _subjects_of_claims(claim_ids, cx=None) -> dict:
        """claim id -> the subject ids its provenance names (validated at extraction)."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        out = {}
        for cid in claim_ids:
            r = conn.execute("SELECT provenance_json FROM claims WHERE id=? AND project_id=?", (cid, pid)).fetchone()
            out[cid] = sorted({s for e in json.loads((r[0] if r else None) or "[]") for s in (e.get("subject_ids") or [])})
        return out

    def _report_merge(unprocessed: dict | None = None, unverified: dict | None = None, subject_reasons: dict | None = None, cx=None) -> None:
        """The run's explicit record of what remains per subject, merged into one durable mark the coverage report reads (coverage.run_report_marks).
        The read, the merge and the mark share one BEGIN IMMEDIATE, so two source workers reporting at once never lose an entry."""
        cx = cx or ctx_main; ctx = cx
        rkey = ctx.key("extract_evidence", "report")
        with ctx.tx():
            cur = ctx.done(rkey) or {"unprocessed": {}, "unverified": {}, "subject_reasons": {}}
            for sid, items in (unprocessed or {}).items():
                have = cur["unprocessed"].setdefault(sid, [])
                for it in items:
                    if it not in have:
                        have.append(it)
            for sid, items in (unverified or {}).items():
                have = cur["unverified"].setdefault(sid, [])
                seen = {x["claim_id"] for x in have}
                for it in items:
                    if it["claim_id"] not in seen:
                        have.append(it); seen.add(it["claim_id"])
                    else:                                                                    # a version-change reason supersedes an earlier stop; a later current-work stop replaces that version-change reason after public re-extraction
                        for prior in have:
                            if prior["claim_id"] == it["claim_id"] and (it.get("reason") == "source version changed" or prior.get("reason") == "source version changed"):
                                prior["reason"] = it["reason"]
            cur["subject_reasons"].update(subject_reasons or {})
            ctx.mark(rkey, "extract_report", cur)

    def _verdict_scope(out, hard_reqs: list):
        scope = ("conditional" if out.conditions_found else "in_scope") if out.scope_matches_claim else "out_of_scope"
        entailed = [x for x in out.requirements_entailed if x in {h["id"] for h in hard_reqs}] if (out.verdict == "supported" and out.scope_matches_claim) else []   # only a supported, in-scope verdict can entail; only listed hard requirements
        return scope, entailed

    def _apply_verdict(c: dict, sid: str, out, hard_reqs: list, rv: int, cx=None):
        """One verdict written through record_verification (the base write and the derivation in one BEGIN IMMEDIATE, which
        joins the caller's open transaction through coverage._tx_immediate); fresh_until and the claim event follow in _verdict_event."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        scope, entailed = _verdict_scope(out, hard_reqs)
        return coverage.record_verification(conn, pid, c["id"], out.verdict, scope, rv, conditions_found=list(out.conditions_found), requirements_entailed=entailed, run_id=rid)

    def _verdict_event(c: dict, sid: str, out, hard_reqs: list, cx=None) -> None:
        """After the verdict is committed: fresh_until and the claim event with its badge."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        src = conn.execute("SELECT url, retrieved_at, published_at, tier, freshness, cache_status, content_hash FROM sources WHERE id=?", (sid,)).fetchone()
        fresh = freshness.claim_freshness(c["kind"], src[4] if src else "technical", src[5] if src else "unknown", src[1] if src else None, src[2] if src else None, src[3] if src else 3)
        prov = {"source_id": sid, "source_version": c["source_version"], "source_current": bool(src) and src[6] == c["source_version"], "retrieved_at": src[1] if src else None, "cache_status": src[5] if src else None}
        scope, _ = _verdict_scope(out, hard_reqs)
        with conn:
            conn.execute("UPDATE claims SET fresh_until=? WHERE id=? AND project_id=?", (fresh["fresh_until"], c["id"], pid))
        conds = json.loads(c["conditions_json"] or "[]") + list(out.conditions_found)
        ev.emit(conn, rid, "claim", {"claim_id": c["id"], "badge": evidence.badge(out.verdict, scope, conds, src[3] if src else None), "verdict": f"{out.verdict} ({scope})",
                                     "freshness": fresh["label"], "settles_alone": fresh["settles_alone"], "warning": fresh["warning"], "fresh_until": fresh["fresh_until"], **prov})

    def _send_group(gkey: str, sid: str, group: list[dict], hard_reqs: list, label: str, kind: str = "first", cx=None) -> dict:
        """One dispatch for a composition of claims of one source: the slices of 1,200 characters either side of each claim range, the
        confirmed hard requirements, re_fetch available. kind "first": the call_stage call (the stored response replayed when one
        exists; its one bump on truncation inside, as for every stage). kind "repair": the one repair of the key through the gateway's
        existing repair allowance, asked when the stored own response named an unknown or a duplicate claim id, failed the contract or
        returned a failure shape. The response is not interpreted here: SETTLE derives everything from the ledger rows the call created."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        from app.adapters.research import source_text, SourceVersionMismatch
        src = conn.execute("SELECT url, retrieved_at, published_at FROM sources WHERE id=?", (sid,)).fetchone() or ("", "", "")
        try:
            found = source_text(conn, pid, sid, ctx.data_dir)
        except SourceVersionMismatch:
            found = None
        text = found[1] if found else ""
        lines = []
        for c in group:
            a, b = int(c["range_start"] or 0), int(c["range_end"] or 0)
            passage = retrieval.slice_around(text, a, b, 1200) if text else (c["quote"] or "")   # exactly 1200 characters either side of the claim range
            lines.append(f"Claim {c['id']} ({c['kind']}): {c['text']}\n<untrusted_source url=\"{src[0]}\" retrieved=\"{src[1]}\" published=\"{src[2]}\" range=\"{a}-{b}\">{passage}</untrusted_source>")
        msgs = prompts.messages("verify", {"source_id": sid, "url": src[0] or "", "retrieved_at": src[1] or "", "published_at": src[2] or "", "hard_requirements": hard_reqs, "claims": "\n".join(lines)})
        if kind == "repair":
            why = "the previous answer failed validation; return the complete JSON of the contract"
            prior = ctx.ledger.completed_result(gkey, "model")
            if prior is not None:
                try:
                    out = M.VerificationOutV2.model_validate_json(prior[1])
                    bad = _verdict_problems(out, {c["id"] for c in group})
                    why = f"unknown or duplicate claim ids in verdicts: {', '.join(sorted(set(bad)))}; exactly one verdict per listed claim id and no other id" if bad else (out.failure_reason or why)
                except Exception:                                                            # noqa: BLE001 (a stored result that is not of the contract: the generic repair turn)
                    pass
            runner = ToolRunner(conn, ctx.ledger, rid, pid, "verify", ctx.research, worker=label, branch=ctx.branch, stage_limit=int(ctx.settings["tools"]["stage_limit"]),
                                run_limits={"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}, data_dir=ctx.data_dir, settings=ctx.settings)
            try:                                                                             # the owed repair runs inside the tool-loop policy at its repair point, with re_fetch offered as in an uninterrupted repair
                r = ctx.gateway.repair(gkey, M.VerificationOutV2, msgs + [prompts.repair_turn(why)], 0, max_rounds=VERIFY_ROUNDS, before_dispatch=lambda what: ctx.check_time(f"verify {what}"), tools=tools_for("verify"), runner=runner)
            except BudgetExceeded as e:
                raise StopRun("budget_reserve_reached", str(e)[:200]) from e
            except UnknownCallPending as e:
                raise StopRun("unknown_call", str(e)[:200]) from e
            except RunStopped as e:
                raise StopRun("run_stopped", str(e)[:200]) from e
            s = _stop_from_status(r["status"], r.get("detail"))
            if s and r["status"] != "round_limit":
                raise s
            return r
        runner = ToolRunner(conn, ctx.ledger, rid, pid, "verify", ctx.research, worker=label, branch=ctx.branch, stage_limit=int(ctx.settings["tools"]["stage_limit"]),
                            run_limits={"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}, data_dir=ctx.data_dir, settings=ctx.settings)
        return call_stage(ctx, "verify", gkey, M.VerificationOutV2, msgs, tools=tools_for("verify"), runner=runner, max_rounds=VERIFY_ROUNDS)

    def _verify_source(sid: str, hard_reqs: list, rv: int, reports: list, cx=None, first: list | None = None) -> dict:
        """The per-source verification protocol, one path. Every decision
        is a function of durable state; the uninterrupted run is that function applied repeatedly and a resume is the same function applied to
        the same state. Each iteration of the loop:
        (a) DECIDE, one BEGIN IMMEDIATE that reads the source's current hash, this run's compositions of the source with their marks, the ledger
            rows of each started composition's exact key (states, attempt kinds, typed outcomes, the stored result and its usability under the
            response validation, the stage transcript of a tool turn) and the member rows, and derives from them, inside the transaction: the attempt
            history of a started composition and its owed dispatch (no sent row: a first send; a completed tool turn with its stored transcript:
            the tool-loop continuation through call_stage, attempt kind first; one truncated attempt alone: the one bump, through call_stage's
            rule on the same key; one completed unusable answer (contract, member ids or failure shape) with the repair unused: the repair;
            two truncated, a used repair or fallback, the round limit or a settled provider failure: none); the controls of a new composition
            (had_group, the continuation count, the continuation size from the recorded splits, no_progress from the recorded marks); the
            classification of every started composition by the resume cases (rows 1 to 8 in the comments of the loop), handled before any fresh composition. No variable
            of an earlier iteration decides anything.
        (b) One action per transaction: DECIDE ends with exactly one of close, void, replay, settle-as-flagged, dispatch, compose, stop or
            headroom; after every write the loop returns to the top and re-reads.
        (c) One settlement function, `settle`: the mark of a composition from (composition, its ledger rows, its member rows): completed with
            verified = the own verdicts of a usable response stored at the composed version (no_progress when a continuation produced none);
            flagged with no verdict when the own responses are settled, none usable and no dispatch owed (the continuation size, half the
            composition's, recorded when two truncations exhausted the bump; no_progress for a continuation). DECIDE alone calls it, for a
            composition it finds in that state, after a send as on resume; the word "ok" is the record of the response the send of the
            previous iteration returned, no decision rests on it.
        (d) One dispatch per iteration, kind derived: SEND makes one call of the kind DECIDE derived (first and the tool-loop continuation through
            call_stage, the repair through the gateway's repair allowance). SETTLE stores: the verdicts of a usable response through the existing
            member guard and record_verification, one transaction per member, nothing else, no mark; the loop returns to DECIDE, which marks
            from the re-read state (row 1 when a refresh landed between the verdict commits and the mark, row 4 when every covered member holds
            its verdict at the composed version, rows 5 to 7 otherwise).
        (e) The durable admission allowance: the forecast's group
            allowance for a source is a decision of this run written at admission into the source's admission record (a run_steps mark under the
            extract_evidence stage, keyed by run and source, with groups_allowed and the forecast's inputs); every DECIDE, uninterrupted, on
            re-entry and on resume by a new instance, reads it there, never from a fresh forecast. The units used are derived from this run's
            ledger for the source: the group keys with a first-kind row plus the repair rows (a further first-kind row under a key that has
            one is a continuation of its unit; a usable replay costs none). Beyond the allowance a unit still owed voids the composition with
            the forecast reason and its claims are reported headroom; no fresh composition is admitted. The call-limit refusal is read first.
        (f) UNWIND: only the named raises (the allowance refusal, the call-limit refusal, the time stop, the founder's stop) void the composition,
            and only when nothing stored can settle it (no usable own response, no row in flight or unknown); a usable settled response is replayed
            by DECIDE, an in-flight or unknown record is kept, any other exception propagates and DECIDE classifies on resume.
        Superseded claims without verdicts are reported "source version
        changed"; current claims remain verification work. The extract path derives this run's prior extraction from its actual ledger sends,
        not a conclusion mark, and does not extract a later source version. Compositions are immutable; old rows stay byte-identical.
        Returns {"verified", "unverified": {claim_id: reason}, "status": done | headroom}; verified is the current durable own-verdict count.
        On a worker's context (cx) the same protocol runs over the worker's own handle and ledger; the keys are the run's (the main label)."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        max_cont = int(_xs("max_verify_continuations")); size = _group_size()
        num = sid.rsplit("-", 1)[-1]
        prefix = ctx.key("verify", f"{sid}:g")
        verified, status, stop, came_back = 0, "done", None, None
        forecast_reason = "the forecast allowance covers no further group of this source"
        admit_key = ctx.key("extract_evidence", f"admission:{sid}")                                   # the source's admission record of this run (e)

        def member_rows(ids):
            return {r[0]: (r[1], r[2], r[3]) for cid in ids for r in conn.execute("SELECT id, verdict, source_id, source_version FROM claims WHERE id=? AND project_id=?", (cid, pid)).fetchall()}

        def void(gkey, comp, reason, stale=()):
            ctx.mark(gkey, "verify_group", dict(comp, verified=[], void=reason, **({"stale": list(stale)} if stale else {})))

        def usable_own(gkey, members):
            """The latest completed own response with a stored result, parsed, when it passes the validation for the composed members
            (the contract, every id a member, no duplicate id, no failure shape); None when there is none or it is not usable."""
            prior = ctx.ledger.completed_result(gkey, "model")
            if prior is None:
                return None
            try:
                out = M.VerificationOutV2.model_validate_json(prior[1])
            except Exception:                                                                # noqa: BLE001 (a stored result that is not of the contract is not usable)
                return None
            if out.failure_reason or _verdict_problems(out, set(members)):
                return None
            return out

        def own_state(gkey, members):
            """The durable state of a composition's own send, from its ledger rows: in flight or unknown; the usable response; the number of
            truncated attempts; the dispatch still owed (first, repair or None) by the existing rules of call_stage and the gateway; whether
            that dispatch opens a new allowance unit (a first send of a key with no sent row, or a repair) or continues the existing one."""
            rows = conn.execute("SELECT state, outcome, attempt_kind, sent_at, resolution FROM usage_ledger WHERE run_id=? AND step_key=? AND kind='model' ORDER BY attempt", (rid, gkey)).fetchall()
            in_flight = any(r[0] in ("planned", "sent") or (r[0] == "unknown" and r[4] is None) for r in rows)   # an unresolved unknown attempt awaits the founder; a resolved one (retried, skipped, reconciled) is not in flight
            skipped = any(r[0] == "unknown" and r[4] == "skipped" for r in rows)
            own = usable_own(gkey, members)
            sent = [r for r in rows if (r[3] is not None or r[0] in ("completed", "unknown")) and r[4] not in ("retried", "reconciled")]   # a retried or reconciled attempt owes a fresh first send: it is no settled answer of the composition
            truncated = sum(1 for r in rows if r[1] == "truncated")
            unit = False
            if own is not None or in_flight:
                owed = None
            elif skipped:
                owed = None                                                                  # the founder's choice: the composition settles flagged without a verdict; nothing resent
            elif not sent:
                owed, unit = "first", True                                                   # never dispatched (a reservation released by recovery is no send): a new unit
            elif sent[-1][0] == "completed" and sent[-1][1] == "tool_calls" and len(sent) < 3 and truncated < 2:
                owed = "first"                                                               # a completed tool turn (a first or a repair answered by a tool request) with its stored transcript and allowance left: the tool-loop continuation of call_stage, part of its unit
            elif len(sent) >= 3 or truncated >= 2 or any(r[2] in ("repair", "fallback") and counts_toward_allowance(r[0], r[3], r[4]) for r in rows):
                owed = None                                                                  # the round limit, the bump, the repair or the fallback is used: the key is settled (a repair or fallback row counts as the ledger's allowance counts it; one released unsent, or one the founder retried or reconciled, leaves the repair owed)
            elif truncated == 1 and len(sent) == 1:
                owed = "first"                                                               # the one bump of call_stage, on the same key, part of its unit
            elif sent[-1][0] == "completed":
                owed, unit = "repair", True                                                  # one completed answer, not usable (contract, ids or failure shape): the one repair, a new unit
            else:
                owed = None                                                                  # a provider failure the gateway's policy settled: nothing more to send
            return {"rows": len(rows), "in_flight": in_flight, "own": own, "owed": owed, "unit": unit, "truncated": truncated}

        def observed_versions(gkey):
            """The text hashes of the live pages the composition's re_fetch turns returned (the tool rows of the group's own stage
            instance, completed, in call order, each hash once), so the mark names the version the verdicts were judged against beside
            the composed version the verdict rows carry; empty when the verifier fetched nothing. Only the pages in the
            usable response's own context count: the gateway deletes the transcript on a schema-valid answer, so a
            later send of the key (the application's repair of an answer the member guard refused) starts from the stored source again
            and the pages fetched before that answer are not before it; the pages counted are the re_fetch rows made after the latest
            earlier answer settled ok and before the usable response (the ledger's insertion order), which keeps the gateway's own
            schema repair, whose messages continue the loop's, as the uninterrupted answer."""
            skey = step_key_for(rid, "verify", f"g{num}x{gkey.rsplit('g', 1)[-1]}", ctx.branch)
            usable = ctx.ledger.completed_result(gkey, "model")
            if usable is None:
                return []
            upto = conn.execute("SELECT rowid FROM usage_ledger WHERE id=?", (usable[0],)).fetchone()[0]
            since = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM usage_ledger WHERE run_id=? AND step_key=? AND kind='model' AND state='completed' AND outcome='ok' AND rowid<?", (rid, gkey, upto)).fetchone()[0]
            seen = []
            for (rj,) in conn.execute("SELECT t.result_json FROM tool_calls t JOIN usage_ledger l ON l.id=t.ledger_entry WHERE t.step_key=? AND t.tool='re_fetch' AND t.state='completed' AND l.rowid>? AND l.rowid<? ORDER BY t.n", (skey, since, upto)).fetchall():
                for page in (json.loads(rj or "[]") if rj else []):
                    h = (page.get("content_hash") or page.get("source_version")) if isinstance(page, dict) else None
                    if h and h not in seen:
                        seen.append(h)
            return seen

        def settle(gkey, comp, state, rows, returned=False):
            """The one settlement function (c): writes the mark of a composition from its durable state; True when a mark was written.
            returned records that the response of the usable settlement came back in the previous iteration's send (the word "ok")."""
            members = comp.get("claims", []); vers = comp.get("versions") or {}
            if state["own"] is not None:
                own_ids = {v.claim_id for v in state["own"].verdicts}
                ids = [cid for cid in members if cid in own_ids and cid in rows and rows[cid][0] is not None and rows[cid][2] == vers.get(cid)]
                extra = {"outcome": "ok"} if returned else {}
                if comp.get("continuation") and not ids:
                    extra["no_progress"] = True
                observed = observed_versions(gkey)
                if observed:
                    extra["observed_versions"] = observed                                     # the live text the verifier judged against, beside the composed version in versions
                ctx.mark(gkey, "verify_group", dict(comp, verified=ids, **extra)); return True
            if state["owed"] is None and not state["in_flight"]:
                extra = {"outcome": "flagged"}
                if state["truncated"] >= 2:
                    extra["split"] = max(1, int(comp.get("size") or len(members)) // 2)       # still truncated after the one bump: the claims without a verdict form the next groups of half the size
                if comp.get("continuation"):
                    extra["no_progress"] = True
                ctx.mark(gkey, "verify_group", dict(comp, verified=[], **extra)); return True
            return False

        def c8_refusal(gkey):
            """The call-limit refusal the gateway would raise at this dispatch, from the same durable count (the run's two limit values and the model-call count)."""
            if ctx.ledger.max_model_calls is None or ctx.ledger.closing_reserve_calls is None:
                return None
            _, below, reason = ctx.ledger.call_allowance(gkey, False)
            return reason if ctx.ledger.model_call_count() >= below else None

        while True:
            action = None
            with ctx.tx():                                                                   # (a) DECIDE
                cur_hash = (conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (sid, pid)).fetchone() or [None])[0]
                groups = [(k, json.loads(oj or "{}"), st) for k, oj, st in conn.execute("SELECT step_key, output_json, status FROM run_steps WHERE run_id=? AND node='verify_group' AND step_key LIKE ? ORDER BY step_key", (rid, prefix + "%")).fetchall()]
                current = [(k, d, st) for k, d, st in groups if d.get("versions") and all(v == cur_hash for v in d["versions"].values())]   # the controls come from this run's compositions at the hash just read; compositions of a superseded version are history
                had_group = {cid for _, d, _ in current for cid in d.get("claims", [])}
                continuations = sum(1 for _, d, _ in current if d.get("continuation"))
                half = min([int(d["split"]) for _, d, st in current if st == "completed" and d.get("split")] + [size])
                no_progress = any(st == "completed" and d.get("no_progress") for _, d, st in current)
                pending = _pending_claims(sid, cx=cx, first=first)                           # a revision composes the cited claims first, then the change's set, then the page's other pending claims
                adm = ctx.done(admit_key); groups_allowed = adm.get("groups_allowed") if adm else None; units = conn.execute("SELECT COUNT(DISTINCT step_key) FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? AND attempt_kind='first' AND (sent_at IS NOT NULL OR state IN ('completed','unknown'))", (rid, prefix + "%")).fetchone()[0] + conn.execute(f"SELECT COUNT(*) FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? AND attempt_kind='repair' AND {ALLOWANCE_SQL}", (rid, prefix + "%")).fetchone()[0]; forecast_stop = f"headroom (forecast: the remaining calls or time cover {groups_allowed} group(s) of this source)"; allowance_left = groups_allowed is None or units < groups_allowed   # (e) the allowance from the admission record, the units from the ledger: group keys with a first-kind row that has send evidence plus every repair row of the run for the source that counts toward the allowance (a repair reservation released unsent is no unit, as the ledger's claim counts it)
                for gkey, comp, st in groups:                                                # the resume clause: one row of the classification per started composition, before any fresh composition
                    if st != "started":
                        continue
                    members = comp.get("claims", []); vers = comp.get("versions") or {}
                    rows = member_rows(members)
                    state = own_state(gkey, members)
                    if any(v != cur_hash for v in vers.values()) or any(cid not in rows or rows[cid][1] != sid or rows[cid][2] != vers.get(cid) for cid in members):
                        own_ids = {v.claim_id for v in state["own"].verdicts} if state["own"] is not None else set()
                        void(gkey, comp, "source version changed", stale=[cid for cid in members if cid in own_ids and not (cid in rows and rows[cid][0] is not None and rows[cid][2] == vers.get(cid))])
                        action = ("written",); break                                         # row 1: a verdict never crosses versions, settled send or not; the own verdicts that could not be stored are listed stale
                    still_pending = [c for c in pending if c["id"] in members]
                    if state["in_flight"]:
                        action = ("send", gkey, comp, still_pending, "first", False); break    # row 3: the existing path reaches the gateway, which refuses a second send of an unknown key; no dispatch
                    if state["own"] is not None:
                        covered = [cid for cid in members if cid in {v.claim_id for v in state["own"].verdicts}]
                        if all(rows[cid][0] is not None for cid in covered):
                            settle(gkey, comp, state, rows, returned=(came_back == gkey)); action = ("written",); break   # row 4: close; verified = the own verdicts stored at the composed version
                        action = ("send", gkey, comp, [c for c in _pending_claims(sid, members, cx=cx) if vers.get(c["id"]) == c["source_version"]], "first", False); break   # row 5: the stored response replayed through the existing guard, never re-sent, outside the allowance
                    if not still_pending:
                        void(gkey, comp, "verified by another send"); action = ("written",); break   # rows 2b and 6: no usable own response and no member pending at the composed version
                    if state["owed"] is None:
                        settle(gkey, comp, state, rows); action = ("written",); break         # row 7: settled as the uninterrupted run would (flagged; the split after two truncations; no_progress for a continuation)
                    refusal = c8_refusal(gkey)
                    if refusal:
                        void(gkey, comp, f"send refused: {refusal}"); action = ("headroom", f"headroom ({refusal})"); break   # the call-limit refusal the gateway would raise, recorded first with its literal
                    if state["unit"] and not allowance_left:
                        void(gkey, comp, forecast_reason); action = ("headroom", forecast_stop); break   # rows 2a and 8 beyond the allowance: a unit still owed
                    group = still_pending if state["owed"] == "first" else [c for c in _pending_claims(sid, members, cx=cx) if vers.get(c["id"]) == c["source_version"]]
                    action = ("send", gkey, comp, group, state["owed"], state["unit"]); break   # rows 2a and 8: one dispatch of the derived kind; a unit when it opens one
                if action is None:
                    if not pending:
                        action = ("stop", None)
                    elif not allowance_left:
                        action = ("headroom", forecast_stop)
                    else:
                        fresh = [c for c in pending if c["id"] not in had_group]
                        if fresh:
                            group, is_cont = fresh[:size], False
                        elif no_progress:
                            action = ("stop", "no progress"); group = []
                        elif continuations >= max_cont:
                            action = ("stop", f"continuation limit {max_cont} reached"); group = []
                        else:
                            group, is_cont = pending[:half], True
                        if group:                                                            # compose: the members at the hash read above, written in this transaction; the dispatch is the next iteration's
                            gkey = f"{prefix}{len(groups)}"
                            comp = {"claims": [c["id"] for c in group], "versions": {c["id"]: c["source_version"] for c in group}, "continuation": is_cont, "size": len(group)}
                            conn.execute("INSERT OR IGNORE INTO run_steps (step_key, run_id, node, input_hash, status, output_json) VALUES (?,?,?,?,'started',?)", (gkey, rid, "verify_group", gkey, json.dumps(comp)))
                            action = ("written",)
            kind = action[0]
            came_back = None
            if kind == "written":
                continue                                                                     # (b) one action per transaction: re-read before the next decision
            if kind == "stop":
                stop = action[1]; break
            if kind == "headroom":
                status, stop = "headroom", action[1]; break
            _, gkey, comp, group, dkind, unit = action
            members = comp.get("claims", [])
            # (e) no instance counter: the units are derived from the ledger in every DECIDE (the dispatch below adds the row the next DECIDE counts)
            try:                                                                             # (d) SEND, outside any transaction; (f) UNWIND for the named raises
                _send_group(gkey, sid, group, hard_reqs, f"g{num}x{gkey.rsplit('g', 1)[-1]}", dkind, cx=cx)
            except BaseException as e:
                named = isinstance(e, CallLimitRefused) or (isinstance(e, StopRun) and e.reason in ("time_limit", "run_stopped"))
                if named:
                    with ctx.tx():                                                           # the ledger read, the stop reason and the void mark share one BEGIN IMMEDIATE
                        st_now = own_state(gkey, members)
                        if not st_now["in_flight"] and st_now["own"] is None:                 # nothing stored can settle it: no usable own response, no row in flight or unknown (a usable response is replayed by DECIDE; an unknown record is kept)
                            reason = e.reason
                            if reason == "run_stopped":
                                reason = (conn.execute("SELECT stop_reason FROM runs WHERE id=?", (rid,)).fetchone() or [reason])[0] or reason
                            void(gkey, comp, f"send refused: {reason}" if isinstance(e, CallLimitRefused) else str(reason))
                if isinstance(e, CallLimitRefused):                                           # the allowance of the verify stage is spent: handled here, the remaining claims stay explicit
                    status, stop = "headroom", f"headroom ({e.reason})"; break
                raise
            own = usable_own(gkey, members)                                                  # SETTLE stores the verdicts of a usable response, one transaction per member, nothing else; DECIDE marks
            if own is not None:
                by_id = {c["id"]: c for c in group}                                          # the members the send carried, at the composed version
                for v in own.verdicts:
                    c = by_id.get(v.claim_id)
                    if c is None:
                        continue                                                             # not of this composition at the current version
                    with ctx.tx():                                                           # the member guard and the verdict write share one BEGIN IMMEDIATE; record_verification joins it
                        cur_row = conn.execute("SELECT verdict, source_id, source_version FROM claims WHERE id=? AND project_id=?", (c["id"], pid)).fetchone()
                        if cur_row is None or cur_row[0] is not None:
                            written = None                                                   # already stored (a replay after a crash)
                        elif cur_row[1] != sid or cur_row[2] != comp["versions"].get(c["id"]) or conn.execute("SELECT content_hash FROM sources WHERE id=?", (sid,)).fetchone()[0] != cur_row[2]:
                            written = None                                                   # the member moved while the group was out: no verdict across versions (DECIDE records it as stale)
                        else:
                            written = _apply_verdict(c, sid, v, hard_reqs, rv, cx=cx)
                    if written is not None:
                        reports.append(written); _verdict_event(c, sid, v, hard_reqs, cx=cx); verified += 1
            came_back = gkey                                                                 # the record of what came back, for the word "ok" of the mark DECIDE writes next; no decision rests on it
        with ctx.tx():                                                                       # terminal DECIDE: the returned figures describe durable work at the current hash, never verdicts a refresh invalidated
            cur_hash = (conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (sid, pid)).fetchone() or [None])[0]
            rows = {cid: (version, verdict) for cid, version, verdict in conn.execute("SELECT id, source_version, verdict FROM claims WHERE project_id=? AND source_id=?", (pid, sid)).fetchall()}
            marks = [json.loads(oj or "{}") for oj, in conn.execute("SELECT output_json FROM run_steps WHERE run_id=? AND node='verify_group' AND status='completed' AND step_key LIKE ?", (rid, prefix + "%")).fetchall()]
            verified = len({cid for mark in marks if not mark.get("void") for cid in mark.get("verified", [])
                            if mark.get("versions", {}).get(cid) == cur_hash and cid in rows and rows[cid][0] == cur_hash and rows[cid][1] is not None})
            unverified = {cid: "source version changed" for cid, (version, verdict) in rows.items() if version != cur_hash and verdict is None}
            if stop or status == "headroom":
                unverified.update({cid: (stop or "no verdict received") for cid, (version, verdict) in rows.items() if version == cur_hash and verdict is None})
        return {"verified": verified, "unverified": unverified, "status": status}

    def _link_candidates(sid: str, served: list, cx=None) -> None:
        """The admitted candidate becomes a base link of every subject whose candidate list holds it (requirements included)."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        for subject_id, _prio, _rank in served:
            coverage.link_sources(conn, pid, "requirement" if ".REQ-" in subject_id else "research_question", subject_id, [sid])

    def extract_evidence(state: RunState) -> RunState:
        key = ctx.key("extract_evidence")
        if ctx.done(key) is not None:
            return {}
        subjects = coverage.subjects_for_extraction(conn, pid)
        k_per, scan, ext_k = int(_xs("k_per_subject")), int(_xs("max_passages_scanned")), int(_xs("extension_k"))
        plan_key = ctx.key("extract_evidence", "plan")
        plan = ctx.done(plan_key)
        if plan is None:                                                                    # the candidate lists and the base set, computed once and kept durably
            cands = []
            for s in subjects:
                c = retrieval.candidate_sources(conn, pid, s["key_facts"], k_per, scan)
                cands.append({"subject_id": s["id"], "priority": s["priority"], "sources": c["sources"], "mark": c["mark"], "inspected": c["inspected"], "scanned": c["scanned"]})   # the passages actually inspected (the loop's iterations), beside the rows fetched; never the configured limit
            order = retrieval.base_set(cands)
            plan = {"candidates": cands, "order": [d["source_id"] for d in order], "keys": {d["source_id"]: list(d["key"]) for d in order}, "served": {d["source_id"]: d["subjects"] for d in order},
                    "subjects": [{"id": s["id"], "priority": s["priority"]} for s in subjects], "extensions": {}}
            if state.get("change"):                                                              # a revision admits the sources its own queries found and the sources holding the impact set's claims awaiting re-verification; the project's earlier backlog stays where it is, for an initial run or a later research run
                own = set(ctx.run_sources())
                fresh_read = {p0["source_id"] for (res0,) in conn.execute("SELECT result_json FROM tool_calls WHERE run_id=? AND state='completed' AND tool='read' AND step_key LIKE ?", (rid, f"{rid}:research_plan:fresh%")).fetchall()
                              for p0 in (json.loads(res0) if res0 else []) or [] if isinstance(p0, dict) and p0.get("source_id")}
                changed_now = {r0[0] for r0 in conn.execute("SELECT DISTINCT source_id FROM source_versions WHERE project_id=? AND superseded_at >= (SELECT started_at FROM runs WHERE id=?)", (pid, rid)).fetchall()}
                own -= (fresh_read - changed_now)                                                # a page read fresh whose bytes did not change is not the revision's new research (otherwise a revision would admit every page it read again); a changed page stays admitted for its new sentences
                rev_ids = sorted(set((state.get("change") or {}).get("reverify") or [])); rev_in = ",".join("?" * len(rev_ids)) or "NULL"   # the impact set of this change (an authorised scope change's included), never every pending claim of the project
                reverify_src = [r[0] for r in conn.execute(f"SELECT DISTINCT c.source_id FROM claims c JOIN sources s ON s.id=c.source_id AND s.content_hash=c.source_version WHERE c.project_id=? AND c.verdict IS NULL AND c.invalidated_at IS NOT NULL AND c.source_id IS NOT NULL AND c.id IN ({rev_in}) ORDER BY c.source_id", (pid, *rev_ids))]
                kept = [sid for sid in plan["order"] if sid in own or sid in reverify_src]
                for sid in reverify_src:
                    if sid not in kept:                                                          # a source of the impact set outside the candidate lists: visited for its re-verification, served for the subjects its claims' provenance names
                        kept.append(sid)
                        pend = [r[0] for r in conn.execute(f"SELECT c.id FROM claims c JOIN sources s ON s.id=c.source_id AND s.content_hash=c.source_version WHERE c.project_id=? AND c.source_id=? AND c.verdict IS NULL AND c.invalidated_at IS NOT NULL AND c.id IN ({rev_in})", (pid, sid, *rev_ids))]
                        plan["served"][sid] = [(s, 2, 0) for s in sorted({s for subs in _subjects_of_claims(pend).values() for s in subs})]
                own_rqs = [r[0] for r in conn.execute("SELECT id FROM research_questions WHERE project_id=? AND run_id=? ORDER BY id", (pid, rid))]
                for sid in sorted(own):
                    if sid not in kept:                                                          # a source the revision's own query found that the candidate lists do not rank: admitted, served for the questions this run asked
                        kept.append(sid); plan["served"][sid] = [(s, 2, 0) for s in own_rqs]
                plan["revision"] = {"own_sources": sorted(own), "reverify_sources": reverify_src, "backlog_left": [sid for sid in plan["order"] if sid not in kept]}
                cited_ids = sorted(set((state.get("change") or {}).get("reverify_cited") or []))
                if cited_ids:                                                                    # the sources holding the claims the re-opened decisions cite are verified first, so the sweep decides those decisions first and the allowance leaves the rest
                    first = {r0[0] for r0 in conn.execute(f"SELECT DISTINCT source_id FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(cited_ids))})", (pid, *cited_ids)).fetchall()}
                    kept = [sid for sid in kept if sid in first] + [sid for sid in kept if sid not in first]
                    plan["revision"]["cited_first"] = [sid for sid in kept if sid in first]
                plan["order"] = kept
            with ctx.tx():
                ctx.mark(plan_key, "extract_evidence", plan)
        ex = ctx.extractor_cls(conn, ctx.gateway, rid, pid, ctx.settings, data_dir=ctx.data_dir, worker=ctx.worker, branch=ctx.branch, before_dispatch=lambda what: ctx.check_time(f"extract_evidence {what}"))
        ex_main = ex
        rv = ctx.requirement_version()
        hard_reqs = [{"id": r["id"], "key": r["key"], "value": r["value"]} for r in ctx.requirements(confirmed_only=True) if r["hard"]]
        results, reports, groups_per_source = [], [], []
        done_sources = {k.rsplit(":src:", 1)[-1] for (k,) in conn.execute("SELECT step_key FROM run_steps WHERE run_id=? AND node='extract_source' AND status='completed' AND step_key LIKE ?", (rid, ctx.key("extract_evidence", "src:") + "%")).fetchall()}
        for k, oj in conn.execute("SELECT step_key, output_json FROM run_steps WHERE run_id=? AND node='extract_source' AND status='completed' AND step_key LIKE ? ORDER BY completed_at, step_key", (rid, ctx.key("extract_evidence", "src:") + "%")).fetchall():
            if int((json.loads(oj or "{}")).get("claims") or 0) > 0:                        # the groups per claim-yielding source of this run, rebuilt from its durable marks on resume
                n0 = conn.execute("SELECT COUNT(*) FROM run_steps WHERE run_id=? AND node='verify_group' AND step_key LIKE ?", (rid, ctx.key("verify", f"{k.rsplit(':src:', 1)[-1]}:g") + "%")).fetchone()[0]
                if n0:
                    groups_per_source.append(int(n0))
        for s_done in done_sources:
            ex.sync_aliases(s_done)                                                          # an alias that arrived after a source's extraction joins its claims' provenance; no send
        served_of = lambda sid: plan["served"].get(sid) or [(s, 2, 0) for s in plan.get("extensions", {}).get(sid, [])]

        def subjects_of_source(sid):
            return sorted({x[0] for x in served_of(sid)})

        def remaining_calls(cx=None) -> int | None:
            """The calls left before the call reserve and the closing reserve (None when the ledger carries no limits)."""
            cx = cx or ctx_main
            L = cx.ledger
            if L.max_model_calls is None or L.closing_reserve_calls is None:
                return None
            return int(L.max_model_calls) - L.model_call_count() - 1 - int(L.closing_reserve_calls)

        def remaining_time(cx=None) -> float:
            """The active time left after the closing time (closing_reserve_calls at the measured mean of a non-extraction send, or the default)."""
            cx = cx or ctx_main
            sec = float(_xs("seconds_per_send_default")); cr = int(_xs("closing_reserve_calls"))
            return 60.0 * float(ctx.settings["budget"]["max_minutes"]) - cx.elapsed_active() - cr * _mean_other_seconds(sec, cx=cx)

        forecasts: dict = {}                                                                 # the figures of each source's admission forecast, kept for its admission record

        def revision_sends(sid: str | None, cx=None) -> tuple[int, int]:
            """For a revision run, the sends it will make before closing besides the source's own: the
            re-verification groups of the impact set's claims on the other sources still pending, and the closing sends (one comparison with
            one repair per stale subject, the plan with its one repair) beyond the ledger's call reserve and closing reserve already set aside."""
            if not state.get("change"):
                return 0, 0
            cx = cx or ctx_main; conn = cx.conn
            gsize = _group_size()
            rev_ids = sorted(set((state.get("change") or {}).get("reverify") or [])); rev_in = ",".join("?" * len(rev_ids)) or "NULL"   # the impact set of this change
            rows = conn.execute(f"SELECT c.source_id, COUNT(*) FROM claims c JOIN sources s ON s.id=c.source_id AND s.content_hash=c.source_version WHERE c.project_id=? AND c.verdict IS NULL AND c.invalidated_at IS NOT NULL AND c.source_id IS NOT NULL AND c.source_id<>? AND c.id IN ({rev_in}) GROUP BY c.source_id", (pid, sid or "", *rev_ids)).fetchall()
            reverify_groups = sum(-(-int(n) // gsize) for _, n in rows)
            stale = list(state.get("stale") or [])
            subjects_n = int(conn.execute(f"SELECT COUNT(DISTINCT subject) FROM decisions WHERE project_id=? AND id IN ({','.join('?' * len(stale)) or 'NULL'})", (pid, *stale)).fetchone()[0]) if stale else 0
            closing = 2 * subjects_n + 2                                                     # one comparison with one repair per stale subject (the revision re-decides a subject once, however many stale decisions of it the carried set holds), the plan with its one repair
            return reverify_groups, closing

        def evidence_allowance(kind: str) -> int:
            """The sends one evidence stage instance may make, by the ledger's definition of a send (every attempt with
            send evidence counts): a verification group's tool loop (VERIFY_ROUNDS sends: the first and its re_fetch turns), an extraction batch's
            continuations, plus the one repair and the one fallback each may claim. The forecast counts this permitted maximum."""
            if kind == "group":
                return VERIFY_ROUNDS + 2
            return 1 + int(ctx.settings["research"].get("max_continuations_per_batch", 0)) + 2

        def forecast(batches: int, groups: int, sid: str | None = None, cx=None) -> tuple[bool, str]:
            """Do the given sends fit the remaining calls and the remaining time at the run's measured means (the default before any measurement)?
            In a revision run every send before closing counts: the other sources' re-verification groups and the closing sends beyond the reserves."""
            cx = cx or ctx_main
            reverify_groups, closing = revision_sends(sid, cx=cx); cr = int(_xs("closing_reserve_calls")); beyond = max(0, closing - 1 - cr)
            ga, ba = (evidence_allowance("group"), evidence_allowance("batch")) if state.get("change") else (1, 1)   # the permitted sends per group and per batch in a revision's forecast; an initial run's forecast counts a unit as one send
            need = batches * ba + groups * ga + reverify_groups * ga + beyond
            remaining = remaining_calls(cx)
            if sid is not None:
                forecasts[sid] = {"batches": batches, "groups": groups, "reverify_groups_elsewhere": reverify_groups, "closing_sends": closing, "closing_beyond_reserve": beyond, "batch_allowance": ba, "group_allowance": ga,
                                  "total": batches * ba + groups * ga + reverify_groups * ga + closing, "remaining_calls": remaining}
            if remaining is not None and need > remaining:
                return False, f"headroom: {need} send(s) forecast ({batches} batch(es) of up to {ba}, {groups} group(s) of up to {ga}" + (f", {reverify_groups} re-verification group(s) elsewhere, {closing} closing send(s)" if state.get("change") else "") + f"), {remaining} allowed before the call reserve and the closing reserve"
            sec = float(_xs("seconds_per_send_default"))
            need_t = batches * ba * _mean_seconds("extract_evidence", sec, cx=cx) + (groups + reverify_groups) * ga * _mean_seconds("verify", sec, cx=cx) + beyond * _mean_other_seconds(sec, cx=cx); left_t = remaining_time(cx)
            if need_t > left_t:
                return False, f"headroom: {need_t:.0f} s forecast for {need} send(s), {max(0.0, left_t):.0f} s remain after the closing time"
            return True, ""

        def groups_that_fit(groups: int, remaining: int | None, left_t: float, per: float) -> int:
            """Of a source's real pending groups, how many the remaining calls and the remaining time cover; the forecast decision before verification starts.
            The remaining calls, the remaining time and the measured seconds per send are read before the decision's transaction (the active-time read ticks the run row), so the transaction holds only the pending read, the cut and its record."""
            fit = int(groups)
            if remaining is not None:
                fit = min(fit, max(0, remaining))
            if per > 0:
                fit = min(fit, max(0, int(left_t // per)))
            return fit

        def headroom(sid, cx=None, exw=None) -> tuple[bool, str]:
            """The admission forecast: planned batches plus the expected verification groups (the run's measured mean of groups per claim-yielding source, at least one) through forecast."""
            cx = cx or ctx_main; conn = cx.conn; ex = exw or ex_main
            from app.adapters.research import source_text
            version, text = source_text(conn, pid, sid, ctx.data_dir)                       # raises when the bytes fail their version check; the forecast writes nothing (a refused candidate leaves no batch row)
            rows = ex.batches(sid, version)
            batches = len([b for b in rows if b["status"] not in ("completed", "needs_review")]) if rows else len(batch_ranges(len(text), ex.batch_chars, ex.overlap))
            pending_here = int(conn.execute("SELECT COUNT(*) FROM claims WHERE project_id=? AND source_id=? AND source_version=? AND verdict IS NULL", (pid, sid, version)).fetchone()[0])   # the forecast's own count at the version its source read returned (not the admission's pending read, which keeps its transaction; no second source read)
            groups = -(-pending_here // _group_size()) if pending_here else (max(1, round(sum(groups_per_source) / len(groups_per_source))) if groups_per_source else 1)   # a source whose claims exist (the impact set's re-verification) has its exact group count; a source not yet extracted keeps the measured mean
            return forecast(batches, groups, sid, cx=cx)

        def record_unprocessed(sources, reason, cx=None):
            unp = {}
            for sid in sources:
                for subj in subjects_of_source(sid):
                    unp.setdefault(subj, []).append({"source_id": sid, "reason": reason})
            if unp:
                _report_merge(unprocessed=unp, cx=cx)

        def source_task(sid: str, cx, ex) -> tuple[str, str | None]:
            """The per-source contract (the admission forecast, the extraction, the attachment and derivation, the admission record, the
            verification sequence, the conclusion), on the given context and extractor: the main ones in sequence, a worker's own in a parallel
            chunk. Returns (done | skip | headroom, the reason the later sources are recorded with)."""
            from app.adapters.research import SourceVersionMismatch
            conn = cx.conn; ctx = cx
            with ctx.tx():                                                               # this run's extraction sends, at any version, are the admission fact; a skipped source mark is no evidence of unextracted work
                cur_hash = conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (sid, pid)).fetchone()
                extraction_sends = [k for k, in conn.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND sent_at IS NOT NULL AND step_key LIKE ?",
                                                           (rid, f"{rid}:extract_evidence:%:{sid}:%")).fetchall()]
                started = bool(extraction_sends); adm_src = ctx.done(ctx.key("extract_evidence", f"admission:{sid}")); admitted = adm_src is not None or started   # the admission record of this run and source, read in this bound source read: its presence decides the bypass of a fresh forecast; this run's extraction sends admit the source by their own rule
                refreshed = started and (cur_hash is None or any(k.rsplit(f":{sid}:", 1)[-1].split(":", 1)[0] != cur_hash[0][7:15] for k in extraction_sends))
                res = {"status": "completed", "claims": conn.execute("SELECT COUNT(*) FROM claims WHERE project_id=? AND source_id=?", (pid, sid)).fetchone()[0]} if refreshed else None
            try:
                ok, why = (True, "") if admitted else headroom(sid, cx=cx, exw=ex)                      # a source with an admission record, with or without an extraction send of this run, receives no fresh headroom forecast; one this run already extracted replays before any check
            except Exception as e:                                                       # the source's bytes failed their version check or are missing: not admitted, recorded, the loop moves on
                ok, why = False, f"not admitted: {type(e).__name__}"
                results.append({"source_id": sid, "status": "failed", "claims": 0}); record_unprocessed([sid], why, cx=cx); return ("skip", why)
            if not ok:
                if why.startswith("headroom") and forecast(1, 1, None, cx=cx)[0]:               # the source's own forecast exceeds what is left while a minimal source (one batch, one group) still fits, so the source is too large, not the run short of calls: skipped, and the next source goes on
                    reason = f"too large for the allowance ({why[len('headroom: '):] if why.startswith('headroom: ') else why})"
                    record_unprocessed([sid], reason, cx=cx); return ("skip", reason)
                record_unprocessed([sid], why, cx=cx); return ("headroom", why)
            _link_candidates(sid, served_of(sid), cx=cx)
            subjects_here = coverage.subjects_for_extraction(conn, pid) or subjects           # the list as coverage reads it before this source's sends
            def before_extract_dispatch(what):                                          # reuse the extractor's existing deadline callback: a refresh after admission still cannot open this run's extraction of a later version
                with ctx.tx():
                    current_hash = (conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (sid, pid)).fetchone() or [None])[0]
                    sends = [k for k, in conn.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND sent_at IS NOT NULL AND step_key LIKE ?", (rid, f"{rid}:extract_evidence:%:{sid}:%")).fetchall()]
                    if sends and (current_hash is None or any(k.rsplit(f":{sid}:", 1)[-1].split(":", 1)[0] != current_hash[7:15] for k in sends)):
                        raise SourceVersionMismatch(f"{sid}: source version changed; this run already sent its extraction")
                ctx.check_time(f"extract_evidence {what}")
            ex.before_dispatch = before_extract_dispatch
            try:
                res = res if refreshed else ex.extract_source(sid, subjects_here)             # a later version is the next run's extraction work; existing current claims (including a public re-extraction's) still enter verification below; same-version replay and continuation stay as they are
            except SourceVersionMismatch:                                               # the callback refused before any send; a batch planned in the admission interval is the next run's work
                with ctx.tx():
                    res = {"status": "completed", "claims": conn.execute("SELECT COUNT(*) FROM claims WHERE project_id=? AND source_id=?", (pid, sid)).fetchone()[0]}
            results.append({"source_id": sid, "status": res["status"], "claims": res.get("claims", 0)})
            ev.step(conn, rid, "extract_evidence", f"Extracted {sid}", f"{res['status']}; {res.get('claims', 0)} claim(s); batches {res.get('batches')}")
            if conn.execute("SELECT injection_flag FROM sources WHERE id=? AND project_id=?", (sid, pid)).fetchone()[0]:   # the flag is shown; the page stays excluded; no automatic action
                ev.emit(conn, rid, "intervention", {"message": f"{sid} flagged: instruction-like text in the page; excluded from evidence, nothing extracted from it, until you decide (include or exclude)", "signal": "injection", "source_id": sid, "criterion": "I9"})
            if res["status"] in ("budget_stop", "stopped", "unknown", "credit"):
                _attach_source_claims(sid, cx=cx); coverage.attach_claims_by_subject(conn, pid, source_id=sid)
                raise StopRun({"budget_stop": "budget_reserve_reached", "stopped": "run_stopped", "unknown": "unknown_call", "credit": "credit_exhausted"}[res["status"]], f"extraction of {sid} stopped: {res['status']}")
            _attach_source_claims(sid, cx=cx); coverage.attach_claims_by_subject(conn, pid, source_id=sid)
            reconcile.reconcile_project(conn, pid, run_id=rid)                           # new claims and links are base writes; the derivation follows each source
            if res["status"] == "headroom":
                record_unprocessed([sid], "headroom: batches not processed", cx=cx); return ("headroom", "headroom")
            admit_key = ctx.key("extract_evidence", f"admission:{sid}"); over_now, rc_now, lt_now, per_now = (ctx.over_time(), remaining_calls(cx), remaining_time(cx), _mean_seconds("verify", float(_xs("seconds_per_send_default")), cx=cx)) if adm_src is None else (None, None, None, None)   # the record read in the bound source read above decides only whether the time and call figures are read (their reads tick the run row and must not sit inside the transaction); it authorises nothing
            with ctx.tx():                                                               # the pending read, the cut and the record of the forecast decision share one BEGIN IMMEDIATE
                adm = ctx.done(admit_key); decide = adm is None and adm_src is None; pending_now = _pending_claims(sid, cx=cx); gsize = _group_size(); expected_now = (-(-len(pending_now) // gsize) if pending_now else 0) if decide else 0   # the record's absence is read inside the writer's transaction; an existing record is preserved unchanged
                fit = (groups_that_fit(expected_now, rc_now, lt_now, per_now) if (expected_now and not over_now) else expected_now) if decide else 0; (ctx.mark(admit_key, "extract_admission", {"groups_allowed": fit if (expected_now and fit < expected_now) else None, "remaining_calls": rc_now, "remaining_time": lt_now, "seconds_per_send": per_now, "pending_groups": expected_now, "over_time": over_now, **({"forecast": forecasts[sid]} if sid in forecasts else {})}) if decide else None)   # the admission forecast's figures (every send counted) travel with the record; the real pending-group count decides before verification; the decision and its inputs are the source's admission record, written once, in this transaction, for the decided first admission only
                if expected_now and fit < expected_now:
                    cut = pending_now[fit * gsize:]; subj_cut = _subjects_of_claims([c["id"] for c in cut], cx=cx); unv0 = {}
                    for c in cut:
                        for s_id in subj_cut.get(c["id"]) or ["(no subject)"]:
                            unv0.setdefault(s_id, []).append({"claim_id": c["id"], "reason": f"headroom (forecast: the remaining calls or time cover {fit} of {expected_now} group(s) of this source)"})
                    _report_merge(unverified=unv0, cx=cx)                                       # the decision is recorded before any verification send
            vres = _verify_source(sid, hard_reqs, rv, reports, cx=cx, first=_reverify_first(state))   # the source's pending claims in groups, before the next source (one sequence per source); the allowance is read from the admission record inside DECIDE
            n_groups = conn.execute("SELECT COUNT(*) FROM run_steps WHERE run_id=? AND node='verify_group' AND step_key LIKE ?", (rid, ctx.key("verify", f"{sid}:g") + "%")).fetchone()[0]
            if n_groups:
                groups_per_source.append(int(n_groups))                                  # the measured count of the source's groups, retained for later admissions
            if vres["unverified"]:
                subj = _subjects_of_claims(vres["unverified"], cx=cx); unv = {}
                for cid, reason in vres["unverified"].items():
                    for s_id in subj.get(cid) or ["(no subject)"]:
                        unv.setdefault(s_id, []).append({"claim_id": cid, "reason": reason})
                _report_merge(unverified=unv, cx=cx)
            reconcile.reconcile_project(conn, pid, run_id=rid)
            while True:                                                                  # the source's conclusion is derived inside its own transaction from the current-version state at that moment
                with ctx.tx():
                    current_hash = (conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (sid, pid)).fetchone() or [None])[0]
                    claim_rows = {cid: (version, verdict) for cid, version, verdict in conn.execute("SELECT id, source_version, verdict FROM claims WHERE project_id=? AND source_id=?", (pid, sid)).fetchall()}
                    group_marks = [json.loads(oj or "{}") for oj, in conn.execute("SELECT output_json FROM run_steps WHERE run_id=? AND node='verify_group' AND status='completed' AND step_key LIKE ?", (rid, ctx.key("verify", f"{sid}:g") + "%")).fetchall()]
                    vres["verified"] = len({cid for mark in group_marks if not mark.get("void") for cid in mark.get("verified", [])
                                            if mark.get("versions", {}).get(cid) == current_hash and cid in claim_rows and claim_rows[cid][0] == current_hash and claim_rows[cid][1] is not None})
                    vres["unverified"] = {cid: reason for cid, reason in vres["unverified"].items() if cid in claim_rows and claim_rows[cid][1] is None}
                    superseded = [cid for cid, (version, verdict) in claim_rows.items() if version != current_hash and verdict is None]
                    vres["unverified"].update({cid: "source version changed" for cid in superseded})
                    if superseded:                                                       # also covers a refresh after the terminal verification read; the reason and the source conclusion share this snapshot and transaction
                        subj = _subjects_of_claims(superseded, cx=cx); unv = {}
                        for cid in superseded:
                            for s_id in subj.get(cid) or ["(no subject)"]:
                                unv.setdefault(s_id, []).append({"claim_id": cid, "reason": "source version changed"})
                        _report_merge(unverified=unv, cx=cx)
                    left = [c for c in _pending_claims(sid, cx=cx) if c["id"] not in vres["unverified"] or vres["unverified"][c["id"]] == "source version changed"]   # current claims are work; a superseded-version reason never explains a current claim (a public re-extraction may have landed)
                    open_groups = conn.execute("SELECT COUNT(*) FROM run_steps WHERE run_id=? AND node='verify_group' AND status='started' AND step_key LIKE ?", (rid, ctx.key("verify", f"{sid}:g") + "%")).fetchone()[0]
                    if not left and not open_groups:
                        ctx.mark(ctx.key("extract_evidence", f"src:{sid}"), "extract_source", {"status": res["status"], "claims": res.get("claims", 0), "verified": vres["verified"], "unverified": len(vres["unverified"])})
                        break
                more = _verify_source(sid, hard_reqs, rv, reports, cx=cx)                          # current work left: the verification sequence re-enters for it (the admission record's allowance and the ledger's units, one path); its stop, if any, is recorded like the first
                vres = {"verified": more["verified"], "unverified": {**vres["unverified"], **more["unverified"]}, "status": more["status"] if more["status"] == "headroom" else vres["status"]}
                if more["unverified"]:
                    subj = _subjects_of_claims(more["unverified"], cx=cx); unv = {}
                    for cid, reason in more["unverified"].items():
                        for s_id in subj.get(cid) or ["(no subject)"]:
                            unv.setdefault(s_id, []).append({"claim_id": cid, "reason": reason})
                    _report_merge(unverified=unv, cx=cx)
                reconcile.reconcile_project(conn, pid, run_id=rid)
            done_sources.add(sid)
            if vres["status"] == "headroom":
                return ("headroom", "headroom")
            return ("done", None)

        def process(order: list[str]) -> str:
            """The sources in the admitted order, in chunks of independent tasks: a chunk holds consecutive sources whose served
            subjects are pairwise disjoint, at most [workers].max_research_workers (clamped at roles.MAX_WORKERS); a chunk of one runs the per-source
            contract in sequence, a larger chunk runs it through run_workers, one worker per source on its own context, the counters,
            reservations and marks being the store's; a stop raised in a worker ends the run once the chunk returns; a headroom result stops
            the admission of every later source (a source too large for the allowance is skipped instead). The wall time of each parallel chunk is announced."""
            nmax = max(1, min(int(ctx.settings.get("workers", {}).get("max_research_workers", roles.MAX_WORKERS)), roles.MAX_WORKERS))
            todo = [sid for sid in order if sid not in done_sources]
            pos = 0
            while pos < len(todo):
                chunk = [todo[pos]]; used = set(subjects_of_source(todo[pos])); pos += 1
                while pos < len(todo) and len(chunk) < nmax and used:
                    subj = set(subjects_of_source(todo[pos]))
                    if not subj or subj & used:
                        break
                    used |= subj; chunk.append(todo[pos]); pos += 1
                if len(chunk) == 1:
                    outcomes = {chunk[0]: source_task(chunk[0], ctx, ex)}
                else:
                    outcomes, stops = {}, []
                    t0 = time.monotonic()

                    def task_fn(wid, task):
                        cx = ctx.fork()
                        try:                                                             # the task's cleanup starts before its extractor is built
                            exw = ctx.extractor_cls(cx.conn, cx.gateway, rid, pid, ctx.settings, data_dir=ctx.data_dir, worker=ctx.worker, branch=ctx.branch)
                            try:
                                outcomes[task["task_id"]] = source_task(task["task_id"], cx, exw)
                            except BaseException as e:
                                stops.append((order.index(task["task_id"]), e)); raise
                        finally:
                            cx.close_fork()
                        return []
                    wres = roles.run_workers([{"task_id": sid} for sid in chunk], task_fn, max_workers=nmax)
                    ctx.tick()                                                               # the parallel interval, counted once
                    secs = time.monotonic() - t0
                    for wr in wres:
                        cost = float(conn.execute("SELECT COALESCE(SUM(reported_usd),0) FROM usage_ledger WHERE run_id=? AND kind='model' AND (step_key LIKE ? OR step_key LIKE ?)", (rid, f"{rid}:extract_evidence:%:{wr.task_id}:%", f"{rid}:verify:%:{wr.task_id}:g%")).fetchone()[0] or 0)
                        out_w = outcomes.get(wr.task_id)
                        ev.emit(conn, rid, "worker", {"worker": wr.worker, "task": f"source {wr.task_id}", "state": ("completed" if wr.status == "completed" else f"failed ({wr.error})") + (f"; {out_w[0]}" if out_w else ""), "cost": f"${cost:.4f}"})
                    ev.step(conn, rid, "extract_evidence", "Parallel sources", f"{len(chunk)} independent source(s) extracted and verified at once in {secs:.1f} s: {', '.join(chunk)}")
                    others = [x for x in stops if not is_locked(x[1])]
                    if others:
                        raise sorted(others, key=lambda x: x[0])[0][1]
                    for i0, e0 in sorted(stops, key=lambda x: x[0]):                           # a worker that met a locked store is not a failed run; its source runs again in sequence on the coordinator's context
                        sid0 = order[i0]
                        ev.step(conn, rid, "extract_evidence", "Locked source retried", f"{sid0}: the worker met a locked store ({str(e0)[:80]}); extracted and verified in sequence")
                        outcomes[sid0] = source_task(sid0, ctx, ex)
                hr = [o for o in outcomes.values() if o[0] == "headroom"]
                if hr:
                    record_unprocessed(todo[pos:], hr[0][1] or "headroom")
                    return "headroom"
            return "done"

        def process_naming_time_stop(order: list[str]) -> str:
            """When the active-time limit stops a send inside the sequence, the unprocessed candidates and the unverified claims are named with the reason time_limit before the existing stop path takes over."""
            try:
                return process(order)
            except StopRun as s0:
                if s0.reason == "time_limit":
                    left = [sid for sid in order if sid not in done_sources]
                    record_unprocessed(left, "time_limit")
                    unv = {}
                    for sid in left:
                        for c in _pending_claims(sid):
                            for s_id in _subjects_of_claims([c["id"]]).get(c["id"]) or ["(no subject)"]:
                                unv.setdefault(s_id, []).append({"claim_id": c["id"], "reason": "time_limit"})
                    if unv:
                        _report_merge(unverified=unv)
                raise

        outcome = process_naming_time_stop(list(plan["order"]))
        if outcome == "done" and not state.get("change"):                                    # the extension, once, for a subject whose admitted candidates are all operationally done and whose evidence is still pending or insufficient; a revision run takes no extension from the project's backlog, which stays for an initial run or a later research run
            admitted = set(plan["order"]) | {s for ext in plan.get("extensions", {}).values() for s in ext}
            with coverage.derived(conn, pid, run_id=rid):
                ev_status = {r[0]: r[1] for r in conn.execute("SELECT subject_id, evidence_status FROM coverage WHERE project_id=?", (pid,)).fetchall()}
            reasons = {}
            for s in sorted(subjects, key=lambda x: (x["priority"], x["id"])):
                if ev_status.get(s["id"]) not in ("pending", "insufficient"):
                    continue
                if s["id"] in plan.get("extensions", {}):
                    ext_sources = plan["extensions"][s["id"]]; mark = plan.get("extension_marks", {}).get(s["id"], "k_reached")
                else:
                    c = retrieval.candidate_sources(conn, pid, s["key_facts"], ext_k, scan, exclude=tuple(admitted))
                    ext_sources = [src for src, _ in c["sources"]]; mark = c["mark"]
                    plan.setdefault("extensions", {})[s["id"]] = ext_sources; plan.setdefault("extension_marks", {})[s["id"]] = mark
                    for src in ext_sources:
                        plan["served"].setdefault(src, []).append((s["id"], s["priority"], 0))
                    with ctx.tx():
                        ctx.mark(plan_key, "extract_evidence", plan)
                    admitted |= set(ext_sources)
                if ext_sources:
                    outcome = process_naming_time_stop(ext_sources)
                    if outcome != "done":
                        break
                with coverage.derived(conn, pid, run_id=rid):
                    after = conn.execute("SELECT evidence_status FROM coverage WHERE project_id=? AND subject_id=?", (pid, s["id"])).fetchone()
                if after and after[0] in ("pending", "insufficient"):                        # only a still unsupported subject carries the reason: the selection marks of the candidate lists are never a covered subject's reason
                    reasons[s["id"]] = {"ranking_exhausted": "candidates_exhausted", "scan_limited": "scan_limited"}.get(mark, "extension_exhausted")
            if reasons:
                _report_merge(subject_reasons=reasons)
        with ctx.tx():
            ctx.mark(key, "extract_evidence", {"sources": results, "outcome": outcome, "base_set": plan["order"], "extensions": plan.get("extensions", {})})
        for cid in [c["id"] for c in ctx.run_claims()]:
            ev.emit(conn, rid, "claim", {"claim_id": cid, "badge": "extracted"})
        _emit_verification_events(reports)
        return {}

    def _attach_source_claims(sid: str, cx=None) -> None:
        """A claim joins a research question only when its source was retrieved for that question, or when
        its product subject names the question (then the claim's source is linked to the question as its evidence)."""
        cx = cx or ctx_main; conn = cx.conn; ctx = cx
        rows = [(r[0], r[1]) for r in conn.execute("SELECT id, subject FROM claims WHERE project_id=? AND source_id=?", (pid, sid))]
        if not rows:
            return
        qrows = {r[0]: (r[1], r[2], r[3]) for r in conn.execute("SELECT id, decision_subject, target_fact, query_text FROM research_questions WHERE project_id=?", (pid,)).fetchall()}
        searches = _searches(cx)
        for (rq, sv) in conn.execute("SELECT subject_id, source_versions_json FROM coverage WHERE project_id=? AND subject_type='research_question'", (pid,)).fetchall():   # materialised before the writes below
            qrow = qrows.get(rq)
            linked = bool(qrow) and any(sid in sids for sq, sids in searches if coverage.query_matches_question(sq, qrow[2] or "", qrow[1] or ""))   # retrieved by the question's own search; a candidate link is admission, not evidence
            qtext = (qrow[2] or "") + " " + (qrow[1] or "") if qrow else ""
            ids = [cid for cid, subj in rows if linked or coverage.subject_matches_question(subj, qtext)]
            if ids:
                if not linked:
                    coverage.link_sources(conn, pid, "research_question", rq, [sid])
                coverage.attach_claims(conn, pid, "research_question", rq, ids)

    def _emit_verification_events(reports: list) -> None:
        """What the inline derivations changed, for the events (both sources kept, newer official preferred, exposed, never chosen silently)."""
        rep = {"conflicts_opened": [], "conflicts_reopened": [], "support_withdrawn": []}
        for r0 in reports:
            for k in rep:
                rep[k] += (r0 or {}).get(k, [])
        for cf in rep["conflicts_opened"] + rep["conflicts_reopened"]:
            for cid in cf["claim_ids"]:
                ev.emit(conn, rid, "claim", {"claim_id": cid, "badge": evidence.CONFLICTING, "conflict": cf["key"], "preferred": cf["preferred_claim_id"]})
            ev.emit(conn, rid, "intervention", {"message": f"Conflicting evidence on {cf['key']}: {' vs '.join(cf['values'])}; both sources kept, {cf['preferred_claim_id']} preferred ({cf['reason']}); listed under unresolved checks", "signal": "unresolved_contradictions", "conflict": cf["key"]})
        for w in rep["support_withdrawn"]:
            ev.emit(conn, rid, "claim", {"claim_id": w["claim_id"], "badge": evidence.SUPPORTED_WITH_CONDITIONS, "support_withdrawn": w["requirement_id"], "reason": w["reason"]})

    # ---- verify (the sweep over every source that still has pending claims, in groups; the per-source sequence of the extract node did most of the work)
    def verify(state: RunState) -> RunState:
        key = ctx.key("verify")
        if ctx.done(key) is not None:
            return {}
        rv = ctx.requirement_version(); verified = 0
        reports = []
        hard_reqs = [{"id": r["id"], "key": r["key"], "value": r["value"]} for r in ctx.requirements(confirmed_only=True) if r["hard"]]
        plan = ctx.done(ctx.key("extract_evidence", "plan")) or {}
        order = list(plan.get("order", [])) + [s for ext in plan.get("extensions", {}).values() for s in ext]
        pending_sources = coverage.pending_sources_for(conn, pid, subjects=_run_subjects(state), claim_ids=(state.get("change") or {}).get("reverify") or [])   # the sweep covers the subjects this run decides and the claims the change binds, not the whole project
        concluded = {k.rsplit("src:", 1)[-1] for k, in conn.execute("SELECT step_key FROM run_steps WHERE run_id=? AND node='extract_source'", (rid,)).fetchall()}   # a source verified in the extract path has its stop recorded (no progress, continuation limit, headroom); the sweep never resends for it
        pending_sources = [s for s in pending_sources if s not in concluded]
        order = [s for s in order if s in pending_sources] + [s for s in pending_sources if s not in order]
        for i, sid in enumerate(order):
            res = _verify_source(sid, hard_reqs, rv, reports, first=_reverify_first(state))
            verified += res["verified"]
            unv = {}
            for cid, reason in res["unverified"].items():
                for s_id in _subjects_of_claims([cid]).get(cid) or ["(no subject)"]:
                    unv.setdefault(s_id, []).append({"claim_id": cid, "reason": reason})
            if res["status"] == "headroom":
                for later in order[i + 1:]:
                    for c in _pending_claims(later):
                        for s_id in _subjects_of_claims([c["id"]]).get(c["id"]) or ["(no subject)"]:
                            unv.setdefault(s_id, []).append({"claim_id": c["id"], "reason": "headroom"})
            if unv:
                _report_merge(unverified=unv)
            if res["status"] == "headroom":
                break
        _emit_verification_events(reports)
        for tc in conn.execute("SELECT id, tool, state, error FROM tool_calls WHERE run_id=? AND stage='verify' ORDER BY created_at, n", (rid,)).fetchall():
            if not conn.execute("SELECT 1 FROM run_events WHERE run_id=? AND event='tool' AND data_json LIKE ?", (rid, f'%"tool_call_id": "{tc[0]}"%')).fetchone():
                ev.emit(conn, rid, "tool", {"tool_call_id": tc[0], "tool": tc[1], "state": tc[2], "summary": (tc[3] or "")[:120]})
        coverage.derived(conn, pid, run_id=rid)                                              # the guarded accessor
        if _after_verify[0] is not None:
            _after_verify[0]()                                                                # test hook: a crash after every verdict is stored and before the stage mark
        with ctx.tx():
            ctx.mark(key, "verify", {"verified": verified})
        ev.step(conn, rid, "verify", "Verification", f"{verified} claim(s) verified; {len(coverage.covered_subjects(conn, pid))} subject(s) covered")
        return {}

    # ---- compare_options (simple comparison per decision subject)
    def next_plan_version() -> int:
        return snapshot_mod.next_version(conn, pid, rid)   # above the latest signed version (this run's own half-written row is swept by the store), and above a version row another run left without its snapshot

    def compare_options(state: RunState) -> RunState:
        key = ctx.key("compare_options")
        if ctx.done(key) is not None:
            return {}
        subjects = state.get("decision_subjects") or []
        # every coverage and status read of this node runs inside the accessor's reads context
        with coverage.derived(conn, pid, run_id=rid):
            reqs = ctx.requirements(confirmed_only=True); hard = [r for r in reqs if r["hard"]]; soft = [r for r in reqs if not r["hard"]]
            covered = set(coverage.covered_subjects(conn, pid))
            rq_claims = {}
            for (rq, cj) in conn.execute("SELECT subject_id, claim_ids_json FROM coverage WHERE project_id=? AND subject_type='research_question'", (pid,)).fetchall():
                for cid in json.loads(cj):
                    rq_claims.setdefault(cid, set()).add(rq)
            claims = ctx.run_claims(); repo = ctx.repo; decided = []
            rq_subject = {r[0]: r[1] for r in conn.execute("SELECT id, decision_subject FROM research_questions WHERE project_id=?", (pid,))}
            rq_status = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT subject_id, evidence_status, processing_status FROM coverage WHERE project_id=? AND subject_type='research_question'", (pid,)).fetchall()}
            rq_processing = {k: v[1] for k, v in rq_status.items()}
            rq_covered = rq_covered_of(rq_status)                                    # a question counts on supported evidence; its processing note (needs review, partial) is reported, never a gate
            rv_now = ctx.requirement_version()
            budget = derived.find_budget(reqs); usage_answer = None; usage_asked = False          # the budget requirement where one exists; the founder's usage answer once a cost-bearing decision is compared; asked once in this node whatever the answer (an answer not given leaves the rows uncomputable; a later run asks again)

            def _usage_asks() -> tuple[int, bool]:
                """The usage-figure interrupts this run has raised and seen answered: the answers that named no monthly volume (each asked again)
                and whether one was read. langgraph matches resume values to a node's interrupts by their order, so a later execution of the node raises
                the same interrupts first, in the same order, and each returns its stored value at once."""
                n_fail = int(conn.execute("SELECT COUNT(*) FROM run_events WHERE run_id=? AND event='step' AND data_json LIKE ?", (rid, '%"label": "Usage answer not read"%')).fetchone()[0])
                read = bool(conn.execute("SELECT 1 FROM run_events WHERE run_id=? AND event='step' AND data_json LIKE ? LIMIT 1", (rid, '%"label": "Usage answered"%')).fetchone())
                return n_fail, read

            def _usage_answer():
                """The project's one usage question, asked through the existing question path (persisted, the run paused, answered on the resume) when a cost-bearing
                decision is first compared, in a revision run only if unanswered; the answer in the founder's words. An answer not given leaves the question open (the rows
                say not computable) and a later run asks again. The figures are asked alone (the length of one output is asked afterwards,
                only when the answer does not carry it, in the founder's item word); an answer in which no monthly volume is recognised is not stored as answered: the
                question is asked again, up to twice more in this node, saying which figures were recognised and which are needed; an answer stored without figures
                is opened again."""
                row = conn.execute("SELECT id, status, answer_json, usage_json, why_blocking FROM questions WHERE project_id=? AND kind='usage' AND text <> ? AND text NOT LIKE ? ORDER BY id DESC LIMIT 1", (pid, derived.USAGE_SCOPE_QUESTION, derived.LENGTH_QUESTION_LIKE)).fetchone()   # the figures' own row (neither the scope question nor the length question, in any item word)
                if row and row[1] == "answered" and not row[3] and derived.usage_record(json.loads(row[2]) if row[2] else None) is None and not derived.parse_usage(json.loads(row[2]) if row[2] else None):   # an answer stored without any figure is no answer: the row is open again and says what it needs; an answer whose figures parse_usage reads (hours a month) stands
                    read0 = derived.usage_read(json.loads(row[2]) if row[2] else None)
                    with ctx.tx():
                        conn.execute("UPDATE questions SET status='open', why_blocking=? WHERE id=?", (derived.usage_read_line(read0), row[0]))
                    row = (row[0], "open", row[2], None, derived.usage_read_line(read0))
                n_fail, was_read = _usage_asks()
                if row and row[1] == "answered":
                    for _ in range(n_fail + (1 if was_read else 0)):
                        IV.interrupt({"kind": "usage", "criterion": "I2", "questions": [{"id": row[0], "text": derived.USAGE_QUESTION, "why_blocking": derived.USAGE_WHY, "requirement_key": None, "kind": "usage"}]})   # this run's usage answers are consumed again (returned at once) so every later interrupt of the node keeps its place
                    record = json.loads(row[3]) if row[3] else None
                    record = _summary_length(row[0], record if record is not None else derived.parse_usage_numbers(json.loads(row[2]) if row[2] else None)) or record   # a record without the summary's length asks its one question (alone, before the scope question); an answer stored without its record is read for its four figures
                    if derived.usage_scope_open(record):                                      # a record whose scope is open asks its one clarification question
                        _usage_scope(row[0], record)
                    return json.loads(row[2]) if row[2] else None
                q = {"id": row[0] if row else None, "text": derived.USAGE_QUESTION, "why_blocking": (row[4] if row else None) or derived.USAGE_WHY, "requirement_key": None, "kind": "usage"}
                if row is None:
                    q["id"] = repo.next_id(pid, "Q")
                    with ctx.tx():
                        conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, round, status, kind) VALUES (?,?,?,?,1,'open','usage')", (q["id"], pid, q["text"], q["why_blocking"]))
                a, numbers, figures = None, None, False
                for attempt in range(3):
                    with ctx.tx():
                        conn.execute("UPDATE runs SET status='awaiting_user' WHERE id=? AND status='running'", (rid,))
                    _pause_once("1 question needs an answer before the cost comparison: the usage per month" + (f" (asked again, attempt {attempt + 1})" if attempt else ""), [q], "usage", "I2")
                    answers = IV.interrupt({"kind": "usage", "criterion": "I2", "questions": [q]})   # exactly one question per interrupt; a re-ask is the node's next interrupt
                    a = (answers or {}).get(q["id"])
                    if a is None:
                        break
                    read = derived.usage_read(a); numbers = read["record"]; figures = bool(numbers) or bool(derived.parse_usage(a))   # a figure in the units the prices are quoted in (hours, minutes, users, GB) is an answer too
                    if figures:
                        break
                    q = dict(q, why_blocking=derived.usage_read_line(read))
                    if attempt >= n_fail:                                                         # a new unread answer (an earlier execution of the node recorded the others)
                        with ctx.tx():
                            conn.execute("UPDATE questions SET why_blocking=? WHERE id=? AND status='open'", (q["why_blocking"], q["id"]))
                        ev.step(conn, rid, "compare_options", "Usage answer not read", f"{q['id']}: no monthly volume recognised; {len(read['recognised'])} other figure(s) read; the question is asked again and names what it needs")
                    a = None
                with ctx.tx():
                    if figures:
                        conn.execute("UPDATE questions SET status='answered', answer_json=?, usage_json=?, why_blocking=? WHERE id=?", (json.dumps(a), json.dumps(numbers) if numbers else None, derived.USAGE_WHY, q["id"]))   # the figures beside the founder's words, with the scope, or the items record
                        if derived.record_length(numbers):                                        # the figures answer carries the words of one item: a length question left open by an earlier run is closed by it, never asked again
                            conn.execute("UPDATE questions SET status='answered', answer_json=?, usage_json=? WHERE project_id=? AND kind='usage' AND status='open' AND text LIKE ?", (json.dumps(f"{derived.record_length(numbers):g} words (stated in the usage answer)"), json.dumps(numbers), pid, derived.LENGTH_QUESTION_LIKE))
                    conn.execute("UPDATE runs SET status='running' WHERE id=? AND status='awaiting_user'", (rid,))
                ev.step(conn, rid, "compare_options", "Usage answered" if figures else "Usage question left open", f"{q['id']}: " + ((derived.usage_read_line(derived.usage_read(a)) if numbers else "the figures are read in the units the prices are quoted in") if figures else "the cost estimates say not computable until it is answered"))
                if not figures:
                    return None
                numbers = _summary_length(q["id"], numbers) or numbers                            # the length of one output is asked only when the figures answer does not carry it, alone, in the founder's item word
                _usage_scope(q["id"], numbers)                                                   # an ambiguous scope asks its one clarification question
                return a

            def _needs_summary_length() -> bool:
                """The project compares a model priced per output token (a kept candidate of a model class), so the budget needs the summary's length."""
                return any(is_model_class(r0[0]) for r0 in conn.execute("SELECT DISTINCT subject_class FROM candidates WHERE project_id=? AND status='kept'", (pid,)).fetchall())

            def _length_question(item: str | None = None) -> dict:
                """The summary length question's row (kind usage), inserted open when the project has none. Asked in the founder's item
                word once the record names one (How long is one pack, in words?); an open row's text follows the word."""
                lrow = conn.execute("SELECT id, text, status FROM questions WHERE project_id=? AND kind='usage' AND text LIKE ? ORDER BY id DESC LIMIT 1", (pid, derived.LENGTH_QUESTION_LIKE)).fetchone()
                text = derived.length_question(item) if item else (lrow[1] if lrow else derived.SUMMARY_LENGTH_QUESTION)
                lq = {"id": lrow[0] if lrow else repo.next_id(pid, "Q"), "text": text, "why_blocking": derived.SUMMARY_LENGTH_WHY, "requirement_key": None, "kind": "usage"}
                if lrow is None:
                    with ctx.tx():
                        conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, round, status, kind) VALUES (?,?,?,?,1,'open','usage')", (lq["id"], pid, lq["text"], lq["why_blocking"]))
                elif lrow[1] != text and lrow[2] == "open":
                    with ctx.tx():
                        conn.execute("UPDATE questions SET text=? WHERE id=?", (text, lq["id"]))
                return lq

            def _summary_length(qid: str, record: dict | None) -> dict | None:
                """A usage record without the summary's length asks the one length question through this same path
                (persisted, the run paused, answered on the resume), alone and before the scope question; the words are written on the figures' record
                and on the length row (the later reads take the latest answered usage row) and the budget converts them at 4/3 tokens a word. Asked once
                in this node whatever the answer; never a guessed quantity. A question asked alone earlier in this run is consumed again first, since
                langgraph matches resume values to interrupts by their order in the node."""
                item = derived.item_of(record)                                                   # the founder's item word on an items record (the question is asked in it)
                lrow = conn.execute("SELECT id, status, answer_json, text FROM questions WHERE project_id=? AND kind='usage' AND text LIKE ? ORDER BY id DESC LIMIT 1", (pid, derived.LENGTH_QUESTION_LIKE)).fetchone()
                asked_alone = bool(conn.execute("SELECT 1 FROM run_events WHERE run_id=? AND event='step' AND (data_json LIKE ? OR data_json LIKE ?) LIMIT 1", (rid, '%"label": "Summary length answered"%', '%"label": "Summary length question left open"%')).fetchone())
                if asked_alone and lrow:
                    IV.interrupt({"kind": "usage", "criterion": "I2", "questions": [{"id": lrow[0], "text": lrow[3], "why_blocking": derived.SUMMARY_LENGTH_WHY, "requirement_key": None, "kind": "usage"}]})   # returned at once: this run's answer is consumed again so a later interrupt of the node keeps its value
                if not record or derived.record_length(record) or record.get("tokens_out_per_item") or not _needs_summary_length():
                    return record                                                                # an answer that states the output tokens of one item needs no length either
                if lrow and lrow[1] == "answered":
                    words = derived.parse_summary_words(json.loads(lrow[2]) if lrow[2] else None)
                    if not words:
                        return record
                    merged = derived.with_length(record, words)
                    with ctx.tx():
                        conn.execute("UPDATE questions SET usage_json=? WHERE id IN (?, ?)", (json.dumps(merged), qid, lrow[0]))
                    return merged
                if asked_alone:
                    return record                                                                # asked once in this node (never beside the usage figures)
                lq = _length_question(item)
                with ctx.tx():
                    conn.execute("UPDATE runs SET status='awaiting_user' WHERE id=? AND status='running'", (rid,))
                _pause_once("1 question needs an answer before the cost comparison: " + (f"the length of one {item} in words" if item else "the summary's length in words"), [lq], "usage", "I2")
                answers3 = IV.interrupt({"kind": "usage", "criterion": "I2", "questions": [lq]})
                a3 = (answers3 or {}).get(lq["id"]); words = derived.parse_summary_words(a3) if a3 is not None else None
                merged = derived.with_length(record, words) if words else record
                with ctx.tx():
                    if words:                                                                     # an answer that names no single length stores nothing; the length question stays open and the next run asks it again
                        conn.execute("UPDATE questions SET status='answered', answer_json=?, usage_json=? WHERE id=?", (json.dumps(a3), json.dumps(merged), lq["id"]))
                        conn.execute("UPDATE questions SET usage_json=? WHERE id=?", (json.dumps(merged), qid))
                    conn.execute("UPDATE runs SET status='running' WHERE id=? AND status='awaiting_user'", (rid,))
                ev.step(conn, rid, "compare_options", "Summary length answered" if words else "Summary length question left open", f"{lq['id']}: " + (f"{words:g} words a {item or 'summary'}, {words * 4 / 3:.0f} output tokens each" if words else ("the answer names no single word count: nothing recorded, the question stays open and the next run asks it again" if a3 is not None else "the output-token rows say not computable until it is answered")))
                return merged

            def _usage_scope(qid: str, record: dict | None) -> None:
                """A usage record that names users and no scope word is ambiguous; one clarification question (kind usage) through
                this same path, persisted, the run paused; the answer settles the scope, written on the figures' record and on the clarification row (the
                later reads take the latest answered usage row). A record stored without the scope field is read as given."""
                if not derived.usage_scope_open(record):
                    return
                row2 = conn.execute("SELECT id, status, answer_json FROM questions WHERE project_id=? AND kind='usage' AND text=? ORDER BY id DESC LIMIT 1", (pid, derived.USAGE_SCOPE_QUESTION)).fetchone()
                if row2 and row2[1] == "answered":
                    settled = derived.settle_usage_scope(record, json.loads(row2[2]) if row2[2] else None)
                else:
                    q2 = {"id": row2[0] if row2 else repo.next_id(pid, "Q"), "text": derived.USAGE_SCOPE_QUESTION, "why_blocking": derived.USAGE_SCOPE_WHY, "requirement_key": None, "kind": "usage"}
                    with ctx.tx():
                        if row2 is None:
                            conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, round, status, kind) VALUES (?,?,?,?,1,'open','usage')", (q2["id"], pid, q2["text"], q2["why_blocking"]))
                        conn.execute("UPDATE runs SET status='awaiting_user' WHERE id=? AND status='running'", (rid,))
                    _pause_once("1 question needs an answer before the cost comparison: the scope of the monthly figure", [q2], "usage", "I2")
                    answers2 = IV.interrupt({"kind": "usage", "criterion": "I2", "questions": [q2]})
                    a2 = (answers2 or {}).get(q2["id"])
                    settled = derived.settle_usage_scope(record, a2) if a2 is not None else dict(record)
                    with ctx.tx():
                        if a2 is not None:
                            conn.execute("UPDATE questions SET status='answered', answer_json=?, usage_json=? WHERE id=?", (json.dumps(a2), json.dumps(settled), q2["id"]))
                        conn.execute("UPDATE runs SET status='running' WHERE id=? AND status='awaiting_user'", (rid,))
                    ev.step(conn, rid, "compare_options", "Usage scope answered" if a2 is not None else "Usage scope question left open", f"{q2['id']}: the meetings figure is {settled.get('scope') or 'read as given until the scope is answered'}")
                with ctx.tx():
                    conn.execute("UPDATE questions SET usage_json=? WHERE id=?", (json.dumps(settled), qid))

            def _usage_numbers():
                """The four numbers of the answered usage question (stored with the answer, or parsed from an answer stored without them)."""
                row = conn.execute("SELECT answer_json, usage_json FROM questions WHERE project_id=? AND kind='usage' AND status='answered' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
                if not row:
                    return None
                if row[1]:
                    return json.loads(row[1])
                numbers = derived.parse_usage_numbers(json.loads(row[0]) if row[0] else None) or derived.usage_record(json.loads(row[0]) if row[0] else None)   # an answer stored without its record: its four numbers, read as given (no scope field), or its items record
                if numbers:                                                                      # the numbers are written beside such an answer once
                    with ctx.tx():
                        conn.execute("UPDATE questions SET usage_json=? WHERE project_id=? AND kind='usage' AND status='answered' AND usage_json IS NULL", (json.dumps(numbers), pid))
                return numbers

            def memo_of(out):
                """The tree's ComparisonOut-shaped result as a memo (ComparisonOutV2); a ComparisonOutV2 stays as it is."""
                if isinstance(out, M.ComparisonOutV2):
                    return out
                rec0 = next((o for o in out.options if o.option == out.recommended), None)
                options = [M.MemoOption(option=TL.shorten_words(o.option, 120), product_id=o.product_id or "", product_version=o.product_version or "", fit=TL.to_sentence_end("; ".join(o.strengths), 300)[0], risk=TL.to_sentence_end("; ".join(o.risks), 300)[0],   # the cells to a sentence end or a whole word, never a slice
                                        verdict="rejected" if not o.meets_hard_constraints else ("recommended" if (rec0 and o.option == rec0.option) else "watch"), claim_ids=list(o.claim_ids)[:12]) for o in out.options[:6]]
                return M.ComparisonOutV2(decision_subject=out.decision_subject, recommendation=M.MemoPick(option=TL.shorten_words(rec0.option, 120), product_id=rec0.product_id or "", product_version=rec0.product_version or "") if rec0 else None,
                                         runner_up=None, why=[M.MemoWhy(text=TL.to_sentence_end(out.rationale or "", 400)[0], claim_ids=list(rec0.claim_ids)[:10])] if rec0 else [], options=options, change_mind=[], open=[TL.to_sentence_end(u, 300)[0] for u in out.unresolved][:6])

            named_picks: dict = {}                                                                # (subject, product id, version) -> the words the recommendation names its option with, once the memo is written
            calc_inputs: dict = {}                                                                # (subject, product id, version) -> the judgement as computed (its input claims, the words its selection read, its range)

            def _name_pick(subject, rec, ident, hard_s, citable, by_id, memo_pick=None) -> None:
                """Once the memo names its pick, the pick's budget is calculated on the rows of what the recommendation names: the candidate's
                label and the recommendation's own words together are the words the selection reads the plan from (a named plan's price stands before
                a starting price or a free tier that names no plan). The stored verdict is rewritten only when the outcome changed."""
                key0 = eligible_pick(ident, rec) if rec is not None and rec.product_id else None
                if key0 is None:
                    return
                words = f"{ident[key0]} {rec.option}".strip()
                said = memo_pick.option if memo_pick is not None and (memo_pick.product_id or "", memo_pick.product_version or "") == (rec.product_id or "", rec.product_version or "") else ""
                if said and said != rec.option:
                    words = f"{words} {said}"                                                   # the recommendation's own words, where the memo writes its pick in other words than the option's label ("a Basic plan, such as 2 vCPU and 4 GB at 24 USD a month" beside "(Basic plan)"): the selection reads the plan from them too
                if named_picks.get((subject, *key0)) != words:
                    named_picks[(subject, *key0)] = words
                    _budget_by_calculation(subject, hard_s, rv_now, ident, citable, by_id)

            def _budget_by_calculation(subject, hard_s, rv, ident, citable, by_id) -> dict:
                """A binding hard budget requirement is judged by arithmetic, not by a
                sentence in a source. For each product identity of the subject: its current, supported, in-scope price claims (the citable set) at the
                founder's usage record against the whole budget figure (app/core/budget.py option_judgement, the plan budget's own rules). Within: the
                verdict entails, the price claims it used its support. Over: contradicts, the arithmetic its proof (the candidate is rejected as a proven
                violation is). Not computable: none, with the budget row's own reason (not established). Stored in coverage_checks with the
                calculation as its note, recomputed at every comparison (the usage record may have been corrected); {identity: judgement}, {} when no
                hard budget binds the subject."""
                if not budget or not budget.get("hard") or not any(h["id"] == budget["requirement_id"] for h in hard_s):
                    return {}
                record = _usage_numbers(); out = {}
                item = AP.norm(subject).replace("_", " ")
                prices = [x for x in sorted(citable) if x in by_id and by_id[x].get("kind") == "price"]
                settle = {r0[0]: evidence.settles_alone("price", r0[1], r0[2], r0[3]) for r0 in conn.execute(f"SELECT c.id, s.cache_status, s.published_at, s.tier FROM claims c LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=? AND c.id IN ({','.join('?' * len(prices)) or 'NULL'})", (pid, *prices)).fetchall()} if prices else {}   # the claim-kind rule the derived status applies (app/core/reconcile.py rule b): a volatile price counts only from a copy that settles it
                for (p0, v0), label in sorted(ident.items()):
                    if not p0:
                        continue
                    every = [x for x in prices if (by_id[x].get("product_id") or "") == p0 and (by_id[x].get("product_version") or "") == (v0 or "")]
                    mine = [{**{k0: by_id[x].get(k0) for k0 in ("id", "text", "attribute", "value", "product_id", "product_version", "kind")}, "conditions": json.loads(by_id[x].get("conditions_json") or "[]") if by_id[x].get("conditions_json") else []}
                            for x in every if settle.get(x, (False, ""))[0]]
                    unsettled = next((f"the price on record is not confirmed ({x}: {settle[x][1]})" for x in every if x in settle and not settle[x][0]), None) if not mine else None
                    j = BG.option_judgement(item, label, v0 or "", mine, record, budget["ceiling_usd"], unsettled=unsettled, names=named_picks.get((subject, p0, v0 or "")))
                    calc_inputs[(subject, p0, v0 or "")] = j
                    verdict = {BG.WITHIN: "entails", BG.OVER: "contradicts"}.get(j["state"], "none")
                    stored = CS.stored_verdict(conn, pid, budget["requirement_id"], rv, p0, v0 or "")
                    if stored != (verdict, sorted(set(j["claim_ids"])), BG.CALCULATED + j["line"]):   # written only when the calculation's outcome changed (or no counting row holds it): an unchanged row keeps the run that wrote it
                        with ctx.tx():
                            CS.record(conn, pid, rid, budget["requirement_id"], rv, subject, p0, v0 or "", verdict, j["claim_ids"], BG.CALCULATED + j["line"])
                    out[(p0, v0 or "")] = j
                if out:
                    ev.step(conn, rid, "coverage", "Budget by calculation", f"{subject[:60]}: {sum(1 for j in out.values() if j['state'] == BG.WITHIN)} within, {sum(1 for j in out.values() if j['state'] == BG.OVER)} over, {sum(1 for j in out.values() if j['state'] == BG.NOT_COMPUTABLE)} not computable against {budget['ceiling_usd']:g} USD a month")
                return out

            def _coverage_step(i, subject, hard_s, rv, ident, rel, citable, by_id):
                """For each product among the subject's candidates and each binding hard requirement without a verdict at this
                requirement version: the verifier's entailment records give entails without a send; otherwise one coverage call over the product's
                supported, in-scope claims and quotes (the strong route), when settings [extraction].coverage_step is on."""
                enabled = bool((ctx.settings.get("extraction") or {}).get("coverage_step", True))
                products = [k for k in ident if k[0]]
                calculated = _budget_by_calculation(subject, hard_s, rv, ident, citable, by_id)   # the hard budget is judged by arithmetic for every product, before and instead of any coverage call
                for chk in CS.plan(subject, products, hard_s, rv, conn, pid):
                    if calculated and chk["requirement"]["id"] == budget["requirement_id"]:
                        continue                                                             # no model is asked whether a page states that the option fits the budget
                    mine = [by_id[x] for x in citable if (by_id[x].get("product_id") or "") == chk["product_id"] and (by_id[x].get("product_version") or "") == chk["product_version"]]
                    support = coverage.supporting_claims(conn, pid, chk["requirement"]["id"], rv, [c0["id"] for c0 in mine])
                    if support:
                        with ctx.tx():
                            CS.record(conn, pid, rid, chk["requirement"]["id"], rv, subject, chk["product_id"], chk["product_version"], "entails", support, "the verifier's entailment records")
                        continue
                    if not enabled or not mine:
                        continue
                    listed = [{"id": c0["id"], "text": c0["text"], "quote": c0.get("quote"), "source_id": c0.get("source_id"), "conditions": json.loads(c0.get("conditions_json") or "[]")} for c0 in mine]
                    ckey = ctx.key("coverage", f"{reuse_tag(ctx, 'coverage')}:d{i}:{chk['key_part']}")   # the verdict's reuse key names the prompt version, as it names the claims, and the model identity
                    r0 = call_stage(ctx, "coverage", ckey, M.CoverageOut, prompts.messages("coverage", CS.prompt_values(chk, listed, ident.get((chk["product_id"], chk["product_version"]), chk["product_id"]))))
                    if r0["status"] == "flagged":
                        continue
                    with ctx.tx():
                        res0 = CS.apply(conn, pid, rid, chk, rv, r0["output"], {c0["id"] for c0 in listed})
                    ev.step(conn, rid, "coverage", f"Coverage {chk['requirement']['key']}", f"{chk['product_id']} {chk['product_version']}: {res0['verdict']} ({len(res0['claim_ids'])} claim(s))")

            tcfg = ctx.settings.get("tree", {})                                                      # the bounds from settings [tree]; the ledger enforces the two call limits at dispatch
            tree_limits = tree.Limits(max_alternatives=int(tcfg.get("max_alternatives", 3)), max_variants=int(tcfg.get("max_variants", 3)), max_depth=int(tcfg.get("max_depth", 2)), max_model_calls=int(tcfg.get("max_model_calls", 7)))
            max_tree = int(tcfg.get("max_tree_decisions_per_run", 2))
            tree_used = {r0[0] for r0 in conn.execute("SELECT DISTINCT decision_subject FROM tree_nodes WHERE run_id=?", (rid,)).fetchall()}   # durable: the subjects this run explored (a resume counts them)

            def _tree_compare(i: int, skey: str, subject: str, rel: list, citable: set, by_id: dict, ident: dict, pack_lines: list, pack_ids: set, pack_support: set, usage_answer, budget) -> dict:
                """The Comparer's bounded tree for one high-importance decision through app.core.tree.explore: the expansion, the variants
                and one batched evaluation are model calls under <run>:tree:<worker>:<branch>:d<i>:<prompt>.v<n>:<model>:<part> (the ledger refuses the eighth call of a
                decision and the third decision of a run at dispatch; such a refusal ends the tree as a partial result, never the run); the hard
                constraints are checked by code (a cited price claim above a hard budget under the usage answer fails the branch; counting support
                for every hard requirement passes it; anything else stays unresolved); the trace is persisted (tree_nodes) and announced (a branch
                event); the selected branch becomes the recommendation the existing decision rules judge (citations, identity, support)."""
                dkey = f"d{i}"
                tkey = lambda part, pstage: f"{rid}:tree:{ctx.worker}:{ctx.branch}:{dkey}:{reuse_tag(ctx, pstage, 'tree')}:{part}"   # the decision stays the fifth part (the ledger's tree limits read it), then the part's prompt version and the model identity
                tree_claims = [dict(vc, kind=by_id[vc["claim_id"]].get("kind"), value=by_id[vc["claim_id"]].get("value")) for vc in pack_lines if vc["claim_id"] in by_id]   # the packs' lines (product id and version on each), nothing outside them
                reqs_in = [{"id": h["id"], "key": h["key"], "value": h["value"], "hard": True} for h in hard] + [{"id": x["id"], "key": x["key"], "value": x["value"], "hard": False} for x in soft]
                alt_index: dict = {}; entries: list = []; refused_alts: list = []

                def ask(part, contract, stage_name, values):
                    try:
                        r0 = call_stage(ctx, "tree", tkey(part, stage_name), contract, prompts.messages(stage_name, values))
                    except CallLimitRefused as e:
                        if e.reason in ("tree_calls", "tree_decisions", "tree_key"):
                            raise tree.CallLimit(e.detail or e.reason) from e                 # the ledger's dispatch limit: the tree ends partial with the reason
                        raise
                    if r0.get("entry"):
                        entries.append(r0["entry"])
                    return r0

                def expand_alternatives(subj):
                    r0 = ask("alternatives", M.AlternativesOut, "tree_alternatives", {"subject": subj, "requirements": reqs_in, "claims": tree_claims})
                    alts = [] if r0["status"] == "flagged" else [{"label": a.label, "claim_ids": list(dict.fromkeys(x for x in a.claim_ids if x in pack_ids)), "product_id": getattr(a, "product_id", "") or "", "product_version": getattr(a, "product_version", "") or ""} for a in r0["output"].alternatives]   # a repeated id is one claim, before the first hard check counts them; an id outside the packs is dropped
                    alts, bad = tree.resolve_alternatives(alts, tree_claims)                    # the identity from the claims passed; a family slug beside a versioned identity of its vendor is refused (the family stays a label, never a node)
                    refused_alts.extend(bad)
                    for j, a in enumerate(alts[: tree_limits.max_alternatives]):
                        alt_index[a["label"]] = j + 1
                    return alts

                def expand_variants(label):
                    r0 = ask(f"variants:A{alt_index.get(label, 0)}", M.VariantsOut, "tree_variants", {"label": label, "requirements": reqs_in, "claims": tree_claims})
                    vs = [] if r0["status"] == "flagged" else [{"label": v.label, "claim_ids": list(dict.fromkeys(x for x in v.claim_ids if x in pack_ids)), "kind": getattr(v, "kind", "product") or "product", "access_route": getattr(v, "access_route", "") or ""} for v in r0["output"].variants]   # as for the alternatives: a repeated id is one claim and an id outside the packs is dropped; the kind and the route are kept (tree.explore folds a route or an approach into its parent as a facet)
                    vs, bad = tree.resolve_variants(vs, tree_claims)                              # a product variant is judged by the family rule as an alternative is, by the identity its cited claims name (or its label when it cites none); a route or an approach passes as a facet
                    refused_alts.extend(bad)
                    return vs

                def evaluate(nodes):
                    branches = [{"branch_id": n.id, "label": n.label, "depth": n.depth, "hard_status": n.hard_status, "claim_ids": n.claim_ids} for n in nodes]
                    r0 = ask("evaluate", M.BranchEvaluationOut, "tree_evaluate", {"branches": branches, "requirements": reqs_in, "claims": tree_claims})
                    if r0["status"] == "flagged":
                        return {}
                    return {e.branch_id: {"fit": e.fit, "evidence": e.evidence, "cost": e.cost, "risk": e.risk, "claim_ids": [x for x in e.claim_ids if x in pack_support]} for e in r0["output"].evaluations}   # added evidence stays inside the subject's citable set: the packs' supporting lines

                usage = derived.parse_usage(usage_answer) if usage_answer else {}

                def node_identity(node):
                    if getattr(node, "product_id", ""):
                        return node.product_id, node.product_version or ""                        # the identity the resolution gave the node from its cited claims stands; a facet's claims and the evaluation's join its evidence, never change its identity
                    own = coverage.majority_identity([by_id[x] for x in node.claim_ids if x in by_id], node.label)   # the identity most of the node's claims name
                    if own[0]:
                        return own
                    parent = next((n0 for n0 in getattr(node, "_siblings", []) if n0.id == node.parent_id), None) if node.parent_id else None
                    return node_identity(parent) if parent is not None else ("", "")           # a variant inherits its parent's product identity

                def node_claims(node):
                    own = [by_id[x] for x in node.claim_ids if x in by_id]
                    if any(c0.get("product_id") for c0 in own):
                        return own
                    parent = next((n0 for n0 in getattr(node, "_siblings", []) if n0.id == node.parent_id), None) if node.parent_id else None
                    return node_claims(parent) if parent is not None else []                   # the claims that give the node its identity (node_identity's inheritance)

                prune_reasons: dict = {}                                                            # why a node fails, by node id, for the tree's prune reason
                hard_records: dict = {}                                                             # what each node's hard check read (the verdicts, their row ids and dates, the claim ids), by node id; the last read of a node stands

                def hard_check(node) -> str:
                    prune_reasons.pop(node.id, None)
                    ids = [x for x in node.claim_ids if x in citable]
                    if budget and budget.get("hard") and usage:                                  # the budget is arithmetic in code, first: a cited price above the hard ceiling fails the branch whatever the verdicts say
                        for c in (by_id[x] for x in ids if by_id[x].get("kind") == "price"):
                            up = derived.unit_price(c.get("value"))
                            if up is None:
                                continue
                            monthly, _label, why = derived.monthly_estimate(up[0], up[1], usage)
                            if why is None and derived.budget_check(monthly, budget).startswith("above budget (hard"):
                                pid0, ver0 = node_identity(node)                                 # the arithmetic's failure is recorded as a verdict's is: the requirement, the claim read with its date, the identity, the figures; the reason names them
                                read_at = (conn.execute("SELECT checked_at FROM claims WHERE id=?", (c["id"],)).fetchone() or [None])[0]
                                hard_records[node.id] = [{"requirement_id": budget["requirement_id"], "verdict": "above budget", "verdict_id": None, "checked_at": read_at, "claim_ids": [c["id"]], "product_id": pid0, "product_version": ver0 or "",
                                                          "basis": "arithmetic", "monthly_usd": float(monthly), "ceiling_usd": float(budget["ceiling_usd"])}]
                                prune_reasons[node.id] = (f"confirmed hard-constraint failure: {str(budget.get('key') or 'budget').replace('_', ' ')} ({c['id']} states {str(c.get('value') or '')!r}: "
                                                          f"{float(monthly):,.2f} USD a month at the stated usage, above the ceiling of {float(budget['ceiling_usd']):,.2f} USD)")
                                return "fail"
                    pid0, ver0 = node_identity(node)
                    if pid0 and hard:
                        hard_records[node.id] = CS.hard_records(conn, pid, hard, rv_now, pid0, ver0)   # the node keeps what it read, whatever the status below
                        if CS.tie_unresolved(conn, pid, hard, rv_now, node_claims(node)):       # an even split between identities with different verdicts is unresolved, never a pruning
                            return "unresolved"
                        cs = CS.hard_status(conn, pid, hard, rv_now, pid0, ver0)              # the tree's evaluation reads the coverage verdicts
                        if cs == "pass" and not all(coverage.named_in_quote(conn, pid, h, (CS.verdict_of(conn, pid, h["id"], rv_now, pid0, ver0) or {}).get("claim_ids") or []) for h in hard if AP.languages_of(h)):
                            cs = "unresolved"                                                  # a coverage verdict counts for a language requirement through a quote that names the languages
                        if cs == "fail":
                            prune_reasons[node.id] = "confirmed hard-constraint failure: " + ", ".join(str(v["requirement"].get("key") or "").replace("_", " ") + " (" + CS.violation_quote(conn, pid, v) + ")" for v in CS.violations(conn, pid, hard, rv_now, pid0, ver0))   # the violated requirement named and the contradicting claim quoted by id
                            return cs
                        if cs == "unproven":                                                   # no supporting evidence is not a contradiction: the branch is unresolved, survives, is scored and may be selected (conditional); the memo's gate then leaves the decision unresolved with its settling check
                            return "unresolved"
                        if cs == "pass":
                            return cs
                    if not ids:
                        return "unresolved" if hard else "pass"
                    for h in hard:
                        if not coverage.supporting_claims(conn, pid, h["id"], rv_now, ids):
                            return "unresolved"
                    return "pass"

                def currency_of(node) -> int:
                    """The currency term by code, never by the model: the node's identity (does it name a version) and the newest read
                    among its citable claims' sources (is it the vendor's own page, read within the freshness window of its class and the claim's kind), through
                    tree.currency_score against the run's clock; a node without a citable claim is judged on its identity alone."""
                    pid0, ver0 = node_identity(node)
                    ids0 = [x for x in node.claim_ids if x in citable and x in by_id]
                    newest0 = None
                    if ids0:
                        row0 = conn.execute(f"SELECT s.url, s.retrieved_at, s.tier, s.freshness, c.kind FROM claims c JOIN sources s ON s.id=c.source_id WHERE c.project_id=? AND c.id IN ({','.join('?' * len(ids0))}) ORDER BY s.retrieved_at DESC LIMIT 1", (pid, *ids0)).fetchone()
                        newest0 = dict(zip(("url", "retrieved_at", "tier", "freshness", "kind"), row0)) if row0 else None
                    return tree.currency_score(pid0, ver0, newest0, run_started)

                rows0 = conn.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? AND (state IN ('planned','sent','completed','unknown') OR (state='failed' AND sent_at IS NOT NULL))", (rid, f"{rid}:tree:%:{dkey}:%")).fetchall()
                retries = max(0, len(rows0) - len({r0[0] for r0 in rows0}))                     # the repairs, bumps and fallbacks already spent on this decision count against its seven (a replayed first send does not)
                tres = tree.explore(subject, expand_alternatives, expand_variants, evaluate, hard_check, limits=tree_limits, retries=retries, fail_reason=lambda node: prune_reasons.get(node.id), currency=currency_of)   # the currency term is computed by code before the scoring
                rows1 = conn.execute("SELECT step_key FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? AND sent_at IS NOT NULL", (rid, f"{rid}:tree:{ctx.worker}:{ctx.branch}:d{i}:%")).fetchall()
                tres["calls_used"] = max(tres["calls_used"], len(rows1))                     # the trace's count is the decision's sends on the ledger, repairs inside the tree included
                for n in tres["nodes"]:
                    n.claim_ids = [x for x in n.claim_ids if x in by_id]
                    n.hard_check = hard_records.get(n.id, [])                                   # the verdict records behind the node's hard_status travel with the node
                version = tree.persist(conn, rid, tres)
                if refused_alts:                                                                # a family slug the tree refused is a cut of this decision's weighing: one row per identity
                    EX.record(conn, pid, rid, subject[:200], None, {(a.get("product_id") or coverage.normalise_identity(a.get("label") or "", "")[0], ""): a["reason"] for a in refused_alts if a.get("product_id") or a.get("label")},
                              labels={(a.get("product_id") or coverage.normalise_identity(a.get("label") or "", "")[0], ""): a["label"] for a in refused_alts}, stage=EX.STAGE_TREE, rule="D-A301")
                nodes = tres["nodes"]; sel = next((n for n in nodes if n.selected), None); pruned = [n for n in nodes if n.pruned]
                ev.emit(conn, rid, "branch", {"subject": subject[:120], "nodes": [{"id": n.id, "depth": n.depth, "label": n.label[:120], "score": n.score, "currency": n.currency, "pruned": n.pruned, **({"product_id": n.product_id[:120], "product_version": (n.product_version or "")[:80]} if getattr(n, "product_id", "") else {}), **({"facets": [{"label": f["label"][:120], "kind": f["kind"], "access_route": f["access_route"][:120]} for f in n.facets]} if getattr(n, "facets", None) else {})} for n in nodes],   # the node's resolved identity in the live record
                                              "selected": tres["selected_id"], "partial": tres["partial"], "reason": tres["reason"], "calls": tres["calls_used"], "version": version,
                                              **({"refused": [{"label": a["label"][:120], "reason": a["reason"][:300]} for a in refused_alts]} if refused_alts else {})})   # the facets folded into their nodes and the family slugs refused, in the live record
                if entries:
                    react.record(conn, rid, skey, entries[-1], f"explore {subject[:80]}", "bounded tree (T25)", f"{len(nodes)} node(s), {len(pruned)} pruned, {sum(len(getattr(n, 'facets', []) or []) for n in nodes)} facet(s), {len(refused_alts)} proposed node(s) refused as a family without a version, {tres['calls_used']} call(s){', partial: ' + str(tres['reason'])[:120] if tres['partial'] else ''}",
                                 f"selected {tres['selected_id'] or 'none'}" + (" (hard constraints unresolved: conditional)" if tres["selected_unresolved"] else ""))
                if sel is None:
                    return {"status": "flagged", "detail": f"exploration tree: no branch could be selected ({tres['reason'] or 'no evaluated branch'})", "entry": entries[-1] if entries else None}
                import types

                def identity(n):
                    if getattr(n, "product_id", ""):
                        return n.product_id, n.product_version or ""                               # the node's resolved identity, as node_identity reads it
                    own = coverage.majority_identity([by_id[x] for x in n.claim_ids if x in by_id], n.label)          # the identity most of the node's claims name
                    if own[0]:
                        return own
                    parent = next((n0 for n0 in nodes if n0.id == n.parent_id), None) if n.parent_id else None
                    return identity(parent) if parent is not None else ("", "")                  # a variant inherits the parent product's id and version; the selection names a product
                pid_, ver_ = identity(sel)
                rec = types.SimpleNamespace(option=sel.label, product_id=pid_, product_version=ver_, meets_hard_constraints=sel.hard_status != "fail", claim_ids=list(sel.claim_ids), strengths=[],
                                            risks=(["the hard constraints of this branch are unresolved"] if sel.hard_status == "unresolved" else []))
                others = []
                for n in nodes:
                    if n.depth != 1 or n.id == sel.id or n.id == sel.parent_id:
                        continue
                    p_, v_ = identity(n)
                    others.append(types.SimpleNamespace(option=n.label, product_id=p_, product_version=v_, meets_hard_constraints=not n.pruned, claim_ids=list(n.claim_ids), strengths=[],
                                                        risks=[n.reason] if n.pruned else [f"score {n.score} against the selected branch" if n.score is not None else "not evaluated (the call limit was reached)"]))
                out = types.SimpleNamespace(decision_subject=subject, options=[rec] + others, recommended=sel.label,
                                            rationale=f"exploration tree: {len(nodes)} branch(es), {len(pruned)} pruned, {tres['calls_used']} call(s)" + (f", partial ({tres['reason']})" if tres["partial"] else "") + f"; selected {sel.label} (score {sel.score}, hard constraints {sel.hard_status})",
                                            unresolved=(["the selected branch's hard constraints are unresolved: the selection is conditional"] if sel.hard_status == "unresolved" else []) + [f"pruned: {n.label} ({n.reason})" for n in pruned][:5])
                return {"status": "tree", "output": out, "entry": entries[-1] if entries else None, "tree": {"nodes": len(nodes), "pruned": len(pruned), "selected": tres["selected_id"], "partial": tres["partial"], "calls": tres["calls_used"], "version": version}}

            hard_all = list(hard)
            dcfg = ctx.settings.get("discovery") or {}
            if (state.get("change") or {}).get("kind") == "usage":
                dcfg = dict(dcfg, panel=False, currency=False, followup=False, simulate_panel=False)   # a usage correction recomputes on the evidence in the store: no panel, no currency read, no follow-up research
            run_started = (conn.execute("SELECT started_at FROM runs WHERE id=?", (rid,)).fetchone() or [None])[0]
            catalogue_cache: dict = {}

            def _catalogue() -> list[dict]:
                """The OpenRouter model catalogue, read once per run through the gateway's client (free); empty when it cannot be read."""
                if "rows" in catalogue_cache:
                    return catalogue_cache["rows"]
                rows = []
                try:
                    import httpx
                    client = ctx.gateway.clients.get(ctx.gateway.default_model)
                    if client is not None and not getattr(ctx.research, "simulated", False):
                        r0 = client.get("/models", cast_to=httpx.Response)
                        rows = list((r0.json() or {}).get("data") or [])
                except Exception as e:  # noqa: BLE001
                    ev.step(conn, rid, "compare_options", "Catalogue not read", f"{type(e).__name__}"[:120])
                catalogue_cache["rows"] = rows
                return rows

            def _followup(i: int, subject: str, sclass: str, cands: list[dict], questions: list[str], hard_s: list, rv: int) -> int:
                """One bounded follow-up for the products the panel named or the currency check found: a typed candidate row
                each, one Exa search each (auto) with the pages stored and linked to the subject, their claims extracted and verified through the
                existing contracts; a contradiction becomes a research question. Returns the number of new sources extracted."""
                frunner = ToolRunner(conn, ctx.ledger, rid, pid, "research_plan", ctx.research, worker=f"f{i}", branch=ctx.branch, stage_limit=int(ctx.settings["tools"]["stage_limit"]),
                                     run_limits={"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}, data_dir=ctx.data_dir, settings=ctx.settings, allowed=roles.WORKER_TOOLS)
                qs = [{"text": f"{c['label']} {sclass.replace('_', ' ')} pricing documentation {DS.run_date()[:4]}"[:200], "decision_subject": subject, "target_fact": f"candidates: {c['label']} ({sclass})"[:200], "freshness": "volatile", "language": "en"} for c in cands[:2]]
                qs += [{"text": q[:200], "decision_subject": subject, "target_fact": q[:200], "freshness": "technical", "language": "en"} for q in questions[:2]]
                if not qs:
                    return 0
                rq_ids = coverage.persist_research_questions(conn, pid, {"queries": qs}, run_id=rid)
                with ctx.tx():
                    for c in cands[:2]:
                        DS.insert_candidate(conn, pid, rid, AP.norm(subject), label=c["label"], vendor=c.get("vendor") or "", product=c.get("product") or "", product_class=AP.norm(c.get("product_class") or sclass), subject_class=sclass,
                                            status="kept", reason=c.get("reason") or "named by the panel check")   # the panel's vendor and product when it named them, the identifiers bare
                new_sources: list[str] = []
                for q, rq in zip(qs, rq_ids):
                    req = {"tool": "search", "args": {"query": q["text"], "freshness": q["freshness"], "num_results": 5, "language": "en"}}
                    try:
                        if not frunner.is_replay(req):
                            ctx.check_time("follow-up search")
                        obs = frunner.handle(req)
                        ids = [pg["source_id"] for pg in (obs.get("data") or []) if isinstance(pg, dict) and pg.get("source_id")]
                        coverage.link_sources(conn, pid, "research_question", rq, ids)
                        new_sources += [x for x in ids if x not in new_sources]
                        ev.tool(conn, rid, obs["tool_call_id"], "search", "completed", f"follow-up {rq}: {len(ids)} result(s)", engine="exa", query=q["text"][:120])
                    except ToolRejected as e:
                        ev.step(conn, rid, "compare_options", "Follow-up search refused", str(e)[:200]); break
                    except (BudgetExceeded, UnknownCallPending, RunStopped) as e:
                        raise StopRun("budget_reserve_reached" if isinstance(e, BudgetExceeded) else ("unknown_call" if isinstance(e, UnknownCallPending) else "run_stopped"), str(e)[:200]) from e
                if not new_sources:
                    return 0
                ex = ctx.extractor_cls(conn, ctx.gateway, rid, pid, ctx.settings, data_dir=ctx.data_dir, worker=f"f{i}", branch=ctx.branch, before_dispatch=lambda what: ctx.check_time(f"follow-up extract {what}"))
                subjects_all = coverage.subjects_for_extraction(conn, pid)
                done_n = 0
                for sid in new_sources[:4]:
                    res = ex.extract_source(sid, subjects_all)
                    if res.get("status") in ("budget", "stopped", "unknown", "headroom"):
                        break
                    if int(res.get("claims") or 0) > 0:
                        _attach_source_claims(sid)
                        _verify_source(sid, hard_s, rv, [])
                        done_n += 1
                with ctx.tx():
                    reconcile.derive(conn, pid, run_id=rid)                                     # derive runs inside a transaction; outside one the follow-up fails here and the final comparison never runs
                ev.step(conn, rid, "compare_options", "Follow-up researched", f"{subject[:60]}: {len(new_sources)} source(s) searched, {done_n} extracted and verified")
                return done_n

            reread_tried: set = set()                                                          # a page is read again at most once per comparison, at the draft or at the final
            reread_count: dict = {}                                                            # the reads attempted per pick (its canonical identity) across the draft and the final; the cap of two is counted over both

            def _currency_reread(i: int, rec, rel: list) -> list:
                """When the pick's priced claims rest on pages
                this run did not fetch, the vendor's own pages of those claims (discovery.currency_reread_targets: the vendor's domain or tier 1, the own
                pages first, at most two) are read once more through the read tool and the ledger (reserved, recorded, under the run's page limit and the
                stage limit, durable ids under the worker cur<i>, replayed on resume; the two are counted per pick across the draft and the
                final, so a pick read twice at the draft is read no more at the final); the store keeps the page and the fresh read beside it as any read
                (store_pages: the same bytes refresh the provenance and the derivation restores the support a live copy settles; new bytes are the
                changed-price path, see _recite). No vendor page among the pick's evidence, a refused or failed read, or a stub adapter not asked to read
                fresh ([research] simulate_fresh_reads): nothing is read and today's line stands. Returns the ids of the sources read."""
                if not dcfg.get("currency", True) or rec is None or not rec.product_id or not run_started:
                    return []
                if getattr(ctx.research, "simulated", False) and not (ctx.settings.get("research") or {}).get("simulate_fresh_reads"):
                    return []
                priced = [c0["id"] for c0 in rel if c0.get("kind") == "price" and _about_pick(rec, c0)]
                if not priced:
                    return []
                rows = [dict(zip(("id", "url", "retrieved_at", "tier"), r0)) for r0 in conn.execute(f"SELECT DISTINCT s.id, s.url, s.retrieved_at, s.tier FROM claims c JOIN sources s ON s.id=c.source_id WHERE c.project_id=? AND c.id IN ({','.join('?' * len(priced))})", (pid, *priced)).fetchall()]
                pick_key = coverage.canonical_identity(rec.product_id, rec.product_version)
                todo = [r0 for r0 in DS.currency_reread_targets({"product_id": rec.product_id, "product_version": rec.product_version, "option": rec.option}, [r0 for r0 in rows if r0["id"] not in reread_tried and r0.get("url")], run_started, limit=2 - reread_count.get(pick_key, 0))]   # the pages this pick has left under its cap of two
                if not todo:
                    return []
                crunner = ToolRunner(conn, ctx.ledger, rid, pid, "research_plan", ctx.research, worker=f"cur{i}", branch=ctx.branch, stage_limit=int(ctx.settings["tools"]["stage_limit"]),
                                     run_limits={"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}, data_dir=ctx.data_dir, settings=ctx.settings, allowed=roles.WORKER_TOOLS)
                read = []
                for r0 in todo:
                    reread_tried.add(r0["id"]); reread_count[pick_key] = reread_count.get(pick_key, 0) + 1   # every attempt counts, as every send is a charge
                    try:
                        ctx.check_time("currency read")
                        obs = crunner.handle({"tool": "read", "args": {"url": r0["url"], "freshness": "volatile"}})
                        ev.tool(conn, rid, obs["tool_call_id"], "read", "completed", f"{r0['id']} read again for the currency check of {rec.option[:60]} (D-A225)", engine="exa", query=r0["url"][:120])
                        read.append(r0["id"])
                    except ToolRejected as e:                                                     # the page limit, a refused or failed fetch, an unknown outcome: the page keeps its copy, today's line stands
                        ev.step(conn, rid, "compare_options", "Currency read refused", f"{r0['id']}: {str(e)[:160]}")
                        if "run page limit" in str(e).lower() or "run search limit" in str(e).lower():
                            break
                    except (BudgetExceeded, UnknownCallPending, RunStopped) as e:
                        raise StopRun("budget_reserve_reached" if isinstance(e, BudgetExceeded) else ("unknown_call" if isinstance(e, UnknownCallPending) else "run_stopped"), str(e)[:200]) from e
                if read:
                    ev.step(conn, rid, "compare_options", "Currency reads", f"{rec.option[:60]}: {len(read)} vendor page(s) of its priced claims read again in this run (D-A225)")
                return read

            def _recite(reread: list, cited: list, kept_why: list, memo: dict, out, rel: list, by_id: dict):
                """The changed-price path: after a re-read, a cited claim whose sentence the fresh copy no longer carries has lost its verdict
                and is cited no more, and its sentence is dropped with it. The claims of the re-read pages take their current
                status and scope from the store (the derivation ran inside the read), the citable set is computed again over them and handed back, so
                the gates, the cost table's eligibility (derived.cost_table) and the memo's option rows read the refreshed evidence, never the price the
                fresh copy cleared. Returns the citations, the kept sentences, the rationale (None: unchanged) and the citable set (None: no re-read)."""
                if not reread:
                    return cited, kept_why, None, None
                touched = [c0["id"] for c0 in rel if c0.get("source_id") in set(reread)]
                with coverage.derived(conn, pid, run_id=rid):                                     # a claim's status is derived: read inside the accessor's reads context
                    fresh = conn.execute(f"SELECT id, status, scope_status FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(touched)) or 'NULL'})", (pid, *touched)).fetchall()
                for row in fresh:
                    by_id[row[0]].update(status=row[1], scope_status=row[2])                    # the one dict per claim, shared by rel, price_claims and by_id
                still = citable_of(conn, pid, rel, rv_now)
                memo["options"] = [{**o0, "claim_ids": [c0 for c0 in o0.get("claim_ids", []) if c0 in still]} for o0 in memo.get("options", [])]   # an option row cites no claim the fresh copy cleared: every row, whether or not the pick's own citations survived (an alternative's row cites its own price)
                if all(x in still for x in cited):
                    return cited, kept_why, None, still
                cited = [x for x in cited if x in still]
                kept_why = [w0 for w0 in kept_why if all(c0 in cited or c0 not in by_id for c0 in w0.claim_ids)]
                memo["why"] = [{"text": w0.text, "claim_ids": [c0 for c0 in w0.claim_ids if c0 in cited]} for w0 in kept_why]
                memo["dropped_sentences"] = len(out.why) - len(kept_why)
                return cited, kept_why, _rationale_of(kept_why), still

            memo_extras: dict = {}                                                            # per subject: the panel and currency lines the final memo prints
            subjects_final: list = []

            def _identity_repair(key, msgs_r, r: dict, cite_set: set, by_id: dict) -> dict:
                """When the evidence identity rule would refuse
                the comparison's pick (every claim it cites is about another product, none about the pick; for example Render picked with Railway's claims), the comparison gets its one repair with the
                refusal reason, under the stage's one repair allowance (gateway.repair, as a failure shape and the panel's invalid answer get theirs); the
                repaired answer then meets the same rules. A flagged answer, a tree's own result (no comparison send) or a spent allowance leaves it as it is."""
                if not key or not msgs_r or r.get("status") == "flagged" or r.get("output") is None:
                    return r
                out0 = memo_of(r["output"]); rec0 = _pick_option(out0)
                if rec0 is None or not rec0.product_id:
                    return r
                wanted0 = list(dict.fromkeys(list(rec0.claim_ids) + [c0 for w0 in out0.why for c0 in w0.claim_ids]))
                cited0 = [x for x in wanted0 if x in cite_set and x in by_id]
                about_pick = any(_about_pick(rec0, by_id[y]) for y in cited0)
                mismatch0 = [x for x in cited0 if not _about_an_option(rec0, out0, by_id[x], about_pick)]
                if not mismatch0 or about_pick:
                    return r                                                                     # no refusal: the pick has evidence of its own (the citations about other products are dropped later) or none is about another product
                about0 = sorted({f"{by_id[x].get('product_id') or 'unnamed'} {by_id[x].get('product_version') or ''}".strip() for x in mismatch0})
                reason0 = (f"The evidence cited for {rec0.option} ({rec0.product_id} {rec0.product_version}) is about {', '.join(about0)}, not about it; evidence must be about the product it supports. "
                           "Recommend the product your cited claims are about, or cite verified claims about the product you recommend.").replace("  ", " ")
                r2 = ctx.gateway.repair(key, M.ComparisonOutV2, msgs_r + [prompts.repair_turn(reason0)], 0, before_dispatch=lambda what: ctx.check_time(f"compare_options {what}"))
                entry0 = r2.get("entry") or r.get("entry")
                if entry0:
                    react.record(conn, rid, key, entry0, f"compare {key[-60:]}", "identity refusal: the one repair with the reason (D-A142)", reason0[:300], f"repair {r2.get('status')}")
                if r2.get("status") == "repaired":
                    return dict(r, output=r2["output"], entry=entry0, identity_repair=True)
                return r

            def settling_of(hard_s: list, memo: dict, leading: list | None = None) -> dict:
                """An unresolved decision whose binding hard requirement lacks counting support states the check that settles it; a
                comparison that gave up states it from the subject's leading identities."""
                check = CS.settling_check(conn, pid, hard_s, rv_now, memo.get("options") or []) if leading is None else CS.gave_up_check(conn, pid, hard_s, rv_now, leading)
                return {"settling_check": check, "open": [check] + list(memo.get("open") or [])} if check else {}

            def leading_of(subject: str, ident: dict, citable: set, by_id: dict) -> list:
                """A subject's two leading identities, as options for the settling check: the two highest-scoring unpruned tree nodes when a tree
                ran, otherwise the two eligible identities with the most citable claims."""
                leading: list = []
                for label, ids_json in conn.execute("SELECT label, claim_ids_json FROM tree_nodes WHERE run_id=? AND decision_subject=? AND pruned=0 AND score IS NOT NULL ORDER BY score DESC, id", (rid, subject)).fetchall():
                    ids = [x for x in json.loads(ids_json or "[]") if x in by_id]
                    k0 = coverage.majority_identity([by_id[x] for x in ids], label)
                    if k0[0] and k0 not in [(o["product_id"], o["product_version"]) for o in leading]:
                        leading.append({"option": coverage.identity_label(*k0), "product_id": k0[0], "product_version": k0[1], "claim_ids": ids})
                if not leading:
                    mine = {k0: [x for x in sorted(citable) if x in by_id and (by_id[x].get("product_id") or "", by_id[x].get("product_version") or "") == k0] for k0 in ident}
                    leading = [{"option": ident[k0], "product_id": k0[0], "product_version": k0[1], "claim_ids": mine[k0]} for k0 in sorted((k1 for k1 in mine if mine[k1]), key=lambda k1: (-len(mine[k1]), k1))]
                return leading[:2]

            GAVE_UP_MEMO = {"recommendation": None, "runner_up": None, "why": [], "options": [], "change_mind": [], "open": [], "dropped_sentences": 0}   # the memo of a comparison flagged on the failure shape, so its settling check and panel lines have a record
            TAG = reuse_tag(ctx, "compare_options")                                               # the comparison's reuse keys name the prompt version and the model identity (v7:<model>), so a memo stored under one prompt version or model is never replayed for a send under another
            pcaps = EP.caps_of(ctx.settings)

            def _packs(subject: str, hard_s: list, rel: list, citable: set, ident: dict) -> list:
                """The subject's evidence packs, one per candidate of the pool, built by code from the stored rows (app/core/evidence_pack.py)."""
                return EP.build_packs(EP.load_inputs(conn, pid, subject, hard_s, rv_now, rel, citable, rq_claims, rq_subject, predecessor=predecessor_of(conn, pid, subject, subjects)), ident, pcaps)   # a joined line names where it came from

            def _class_options(subject: str, out, options: list, left: list) -> list:
                """The memo's options without the ones the store records under another decision's class (options_of_class); the pick and the
                runner-up are never removed here. The ones that left are appended to `left` for the memo's record."""
                keep = tuple(x for o0 in (out.recommendation, out.runner_up) if o0 is not None for x in ((coverage.canonical_identity(o0.product_id, o0.product_version) if o0.product_id else ""), label_identity(o0.option or "")) if x)
                stay, gone = options_of_class(conn, pid, subject, options, keep)
                left.extend(gone)
                return stay

            def _with_budget(subject: str, cands: list) -> None:
                """The memo's writer receives, for each candidate the budget was calculated for, the calculation's own sentence (the
                figure against the budget, or the reason no figure stands), as data on the candidate beside its coverage verdicts, not as prompt
                text. A candidate the calculation did not judge carries nothing."""
                for cand in cands:
                    j0 = calc_inputs.get((subject, cand.get("product_id") or "", cand.get("product_version") or ""))
                    if j0 is not None and j0.get("line"):
                        cand["budget_calculation"] = W.founder_text(j0["line"])

            def _candidates_of(pool: dict, excluded: dict) -> list:
                """The comparison's candidate list: the pool's identities with their labels; an excluded identity carries the pool rule's reason as data, not as
                prompt text; with no identity at all, a single placeholder candidate."""
                return [{"product_id": k[0], "product_version": k[1], "label": v, **({"excluded": excluded[k]} if k in excluded else {})} for k, v in sorted(pool.items())] or [{"product_id": "", "product_version": "", "label": "no candidate with an explicit product identity"}]

            def _with_checked(cands: list, packs: list, hard_s: list) -> list:
                """Per candidate the dates its evidence was checked, read from the records for the compare_options prompt: verdict_date
                (the newest of its counting hard-requirement verdicts, coverage_step.checked_at), newest_read and oldest_read (the read dates of the sources
                behind its pack's claim lines, evidence_pack's read_dates); None where the records hold none. The candidate dicts are completed in place."""
                by_key = {coverage.canonical_identity(p0["product_id"], p0["product_version"]): p0 for p0 in packs if p0.get("product_id")}
                for cand in cands:
                    p0 = by_key.get(coverage.canonical_identity(cand["product_id"], cand["product_version"] or "")) if cand.get("product_id") else None
                    rd = (p0 or {}).get("read_dates") or {}
                    cand["checked"] = {"verdict_date": CS.checked_at(conn, pid, hard_s, rv_now, cand["product_id"], cand["product_version"] or "") if cand.get("product_id") and hard_s else None,
                                       "newest_read": rd.get("newest"), "oldest_read": rd.get("oldest")}
                return cands

            def _excluded_open(memo, pool: dict, excluded: dict) -> None:
                """The memo's open list says which product the pool rule excluded and why: one code-composed line per reason naming the products it
                excluded (the first six by label and a count of the rest), instead of "no options"; the model's own lines are kept. The line is never
                cut: its bound is the six names and the reason, both whole."""
                if memo is None or not excluded:
                    return
                by_why: dict = {}
                for k, why in sorted(excluded.items()):
                    by_why.setdefault(why, []).append(pool[k])                                  # one line per reason (the memo's duplicate rule reads two lines that differ by a name alone as one): the products it excluded, by label
                lines = []
                for why, labels in by_why.items():
                    rest = len(labels) - 6
                    who = ", ".join(labels[:6]) + (f" and {rest} more" if rest > 0 else "")
                    lines.append(f"{who} {'is not an option' if len(labels) == 1 else 'are not options'} of this decision: {why}; a source stating what is missing would make "
                                 f"{'it' if len(labels) == 1 else 'one of them'} one.")        # no character cut (a cut at a fixed length could end inside the reason): six names of the candidates' length and the reason bound the line
                memo["open"] = list(dict.fromkeys(list(memo.get("open") or []) + lines))

            def _record_packs(subject: str, packs: list) -> None:
                """The trace of the packs: one step event with the included and omitted claim ids (and their reason) per pack."""
                s0 = EP.summary(packs)
                ev.step(conn, rid, "compare_options", "Evidence packs", f"{subject[:60]}: {s0['candidates']} pack(s), {s0['included']} claim(s) included, {s0['omitted']} omitted ({s0['omitted_cap']} by the cap, {s0['omitted_duplicate']} duplicate, {s0['omitted_superseded']} superseded, {s0.get('omitted_status', 0)} by status, {s0.get('omitted_scope', 0)} by scope, {s0.get('omitted_currency', 0)} by currency), {s0['contradictions']} material contradiction(s)"
                        + (f", {s0['insufficient_capacity']} pack(s) insufficient" if s0["insufficient_capacity"] else ""), packs=EP.event_packs(packs), caps=pcaps)

            def _store_packs(skey: str, did: str, subject: str, packs: list, change: str) -> None:
                """The packs as the comparison saw them, stored with the decision's draft, and the step's reasoning record. Inside the caller's transaction."""
                s0 = EP.summary(packs)
                conn.execute("UPDATE decisions SET evidence_pack_json=? WHERE id=?", (json.dumps(packs), did))
                react.record(conn, rid, skey, did, f"evidence packs of {subject[:80]}", "evidence packs (D-A184, D-A185)", f"{s0['candidates']} pack(s); {s0['included']} claim(s) included, {s0['omitted']} omitted ({s0['omitted_cap']} by the cap), {s0['contradictions']} material contradiction(s), {s0['insufficient_capacity']} insufficient", change)

            def _open_for_capacity(skey: str, subject: str, hard_s: list, req_links: list, packs: list, final: bool = False) -> str:
                """A pack that cannot hold one supporting claim per binding hard requirement beside its material contradictions: the comparison is not
                asked to decide; the decision stays open as an unresolved draft with the reason pack_capacity, surfaced like any other unresolved draft; a later
                pass under a larger cap in settings rebuilds the packs."""
                bad = [p0 for p0 in packs if p0["insufficient_capacity"]]
                detail = "; ".join(f"{p0['label']}: " + ", ".join(f"{d0['requirement_id']} ({', '.join(d0['claim_ids'])})" for d0 in p0["did_not_fit"]) for p0 in bad)
                rationale = (f"pack_capacity: the evidence pack of {len(bad)} candidate(s) cannot hold one supporting claim for every binding hard requirement beside its material contradictions "
                             f"under the cap of {pcaps['max_claims_per_pack']} claims and {pcaps['max_tokens_per_pack']} tokens ({detail}); a larger cap in settings [evidence_pack] rebuilds the pack.")
                memo = dict(GAVE_UP_MEMO, open=[rationale[:300]], pack_capacity=[{"product_id": p0["product_id"], "product_version": p0["product_version"], "label": p0["label"], "did_not_fit": p0["did_not_fit"]} for p0 in bad], pack_line=EP.pack_line(packs), **({"final": True} if final else {}))
                with ctx.tx():
                    did = repo.next_id(pid, "DEC")
                    repo.upsert_decision(M.Decision(id=did, project_id=pid, subject=subject[:200], choice="unresolved", rationale=rationale[:600], importance="high" if hard_s else "medium",
                                                    requirement_ids=req_links, claim_ids=[], rejected=[], status="unresolved", plan_version=next_plan_version(), run_id=rid))
                    conn.execute("UPDATE decisions SET memo_json=? WHERE id=?", (json.dumps(memo), did))
                    _store_packs(skey, did, subject, packs, "no comparison sent; the decision stays open with the reason pack_capacity")
                    for h0 in hard_s:
                        coverage.ensure_subject(conn, pid, "requirement", h0["id"])
                    reconcile.derive(conn, pid, run_id=rid)
                    ctx.mark(skey, "compare_options", {"decision": did, "status": "unresolved", "reason": "pack_capacity", "final": final})
                decided.append(did)
                ev.emit(conn, rid, "decision", {"decision_id": did, "status": "unresolved", "subject": subject[:120], "reason": "pack_capacity", **({"final": True} if final else {})})
                return did

            def _conditional(memo: dict, rec, label: str, lacking: list, rationale: str) -> str:
                """The decision is made as a conditional recommendation: the option the comparison recommends,
                recorded as recommended and not yet proven on each hard requirement the evidence is silent on (its label and value), with the check that
                would confirm it. The mark rides on the memo (conditional: the requirements, the sentence, the check; the state not_proven) and opens the
                rationale; the derived status rule (app/core/reconcile.py) keeps the decision active with the same requirements in its reason. Returns
                the rationale."""
                option0 = {"option": label, "product_id": rec.product_id, "product_version": rec.product_version or ""}
                line = CS.conditional_line(label, lacking, CS.calculation_reasons(conn, pid, option0, lacking, rv_now))
                check = CS.confirming_check(conn, pid, option0, lacking, rv_now)
                memo["evidence_state"] = "not_proven"
                memo["conditional"] = {"requirements": [{"id": h["id"], "key": h.get("key"), "label": W.label_of(h.get("key")), "value": W.value_words(h.get("value"))} for h in lacking], "line": line, "check": check}
                memo["settling_check"] = check
                memo["open"] = list(dict.fromkeys([line, check] + list(memo.get("open") or [])))
                return f"{line} {rationale}".strip()

            def _budget_support(memo: dict, rec, hard_s: list, cited: list, evidence_by_req: dict) -> list:
                """For an active pick whose hard budget was judged within by calculation, the calculation is the requirement's support: the price
                claims it used join the decision's citations (the derived status rule counts them there), and its sentence is recorded on the memo
                (budget: the state, the figure, the sentence, the claims) and stands among the reasons."""
                if not budget or not budget.get("hard") or not any(h["id"] == budget["requirement_id"] for h in hard_s):
                    return cited
                v0 = CS.verdict_of(conn, pid, budget["requirement_id"], rv_now, rec.product_id, rec.product_version or "")
                if v0 is None or not CS.is_calculated(v0.get("note")):
                    return cited
                line = CS.calculation_of(v0["note"])
                memo["budget"] = {"requirement_id": budget["requirement_id"], "state": {"entails": BG.WITHIN, "contradicts": BG.OVER}.get(v0["verdict"], BG.NOT_COMPUTABLE), "line": line, "claim_ids": list(v0["claim_ids"])}
                j0 = calc_inputs.get((memo.get("decision_subject") or "", rec.product_id, rec.product_version or "")) or next((j1 for (s1, p1, v1), j1 in calc_inputs.items() if p1 == rec.product_id and v1 == (rec.product_version or "") and BG.CALCULATED + j1["line"] == v0["note"]), None)
                if j0 is not None and j0.get("line") == line:                                     # the judgement's own inputs ride on the memo, so the plan budget and the option's monthly cost are computed by the same selection over the same claims
                    memo["budget"].update({"input_claim_ids": list(j0.get("input_claim_ids") or []), "option": j0.get("option"), "names": j0.get("names"), "monthly_usd": j0.get("monthly_usd"), **({"range": j0["range"]} if j0.get("range") else {})})
                if v0["verdict"] != "entails":
                    return cited
                evidence_by_req[budget["requirement_id"]] = list(v0["claim_ids"])
                short = TL.to_sentence_end(line, 400)[0]                                          # the figure and the ceiling (the first sentence); the rows behind it are on the memo's budget record and in the cost table
                if not any(w0.get("text") == short for w0 in memo.get("why") or []):
                    memo["why"] = list(memo.get("why") or []) + [{"text": short, "claim_ids": list(v0["claim_ids"])[:10], "kind": "calculation"}]   # the code's own sentence, marked: a reader's reason on every surface, never one of the comparison's quoted reasons the scorecard counts
                return list(dict.fromkeys(list(cited) + list(v0["claim_ids"])))

            def _reject_violating(memo: dict, rejected: list, out, rec, hard_s: list) -> None:
                """Every option of the memo with a proven violation of a binding hard requirement (a contradicts verdict: the evidence's,
                with its quote, or the calculation's, with its arithmetic) reads rejected with that line, whether or not it was the pick; never one with a
                proven violation is recommended."""
                for o in out.options:
                    if not o.product_id or (rec is not None and o is rec):
                        continue
                    vio = CS.violations(conn, pid, hard_s, rv_now, o.product_id, o.product_version or "")
                    if vio:
                        _reject_option(memo, rejected, o, CS.rejection_line(conn, pid, o.option, vio))

            def _preferred(out, rec, hard_s: list, ident: dict, pack_support: set, by_id: dict):
                """The order of preference among the memo's options, in code: an option proven on every binding hard requirement, then one not
                proven on some, never one with a proven violation. The comparison's pick stands when it is proven, or when no other option of the memo
                (in the memo's own order: runner-up, bake-off, watch) is; a pick with a proven violation gives way to the first option without one.
                The memo is changed in place (the recommendation, the verdicts, one code-written reason citing the new pick's own claims); returns
                the pick."""
                if rec is None or not hard_s or not rec.product_id:
                    return rec
                cites = lambda o: [x for x in o.claim_ids if x in pack_support and x in by_id and _about_pick(o, by_id[x])]
                violated = lambda o: bool(CS.violations(conn, pid, hard_s, rv_now, o.product_id, o.product_version or ""))
                proven = lambda o: not violated(o) and all(CS.counting_support(conn, pid, h, rv_now, o.product_id, o.product_version or "", cites(o)) for h in hard_s)
                mine_violated = violated(rec)
                if not mine_violated and proven(rec):
                    return rec
                others = sorted([o for o in out.options if o is not rec and o.product_id and o.verdict in ("recommended", "runner-up", "bake-off", "watch") and eligible_pick(ident, o) is not None and cites(o)],
                                key=lambda o: CS.OPTION_ORDER.index(o.verdict))
                alt = next((o for o in others if proven(o)), None) or (next((o for o in others if not violated(o)), None) if mine_violated else None)
                if alt is None:
                    return rec
                why = (f"{alt.option} is recommended: {rec.option} has a proven violation of a must-have and is rejected." if mine_violated
                       else f"{alt.option} is recommended: it is proven on every must-have, and {rec.option} is not yet proven on all of them.")
                out.why = [M.MemoWhy(text=why[:400], claim_ids=cites(alt)[:10])]
                out.recommendation = M.MemoPick(option=alt.option, product_id=alt.product_id, product_version=alt.product_version or "")
                out.runner_up = None if mine_violated else M.MemoPick(option=rec.option, product_id=rec.product_id, product_version=rec.product_version or "")
                rec.verdict = "rejected" if mine_violated else "runner-up"; alt.verdict = "recommended"
                ev.step(conn, rid, "compare_options", "Order of preference", f"{alt.option[:60]} over {rec.option[:60]}: " + ("the pick has a proven violation" if mine_violated else "proven on every hard requirement"))
                return alt

            def _decide_final(i: int, subject: str, hard: list, skey: str, claims: list):
                """The final comparison of a subject after a follow-up: the simple comparison over the enlarged candidate set and the new
                claims, the memo carrying the panel and currency lines; the same rules as the draft (citations, identity, support), one send."""
                req_links = [h["id"] for h in hard] or [r0["id"] for r0 in AP.binding(ctx.requirements(confirmed_only=True), subject, hard_only=False)]   # with no binding hard requirement, the decision links to the requirements that bind its subject
                by_id = {c["id"]: c for c in claims}
                rel = subject_claims(conn, pid, subject, claims, rq_claims, rq_subject, subjects)   # the subject's own claims and its predecessor's (a renamed subject), the one selection of the draft and the final
                citable = citable_of(conn, pid, rel, rv_now)                                     # the one citability rule of the draft and the final comparison
                el = eligible_candidates(conn, pid, subject, rel); ident = el["identities"]   # the same typed eligible set as the draft's
                pool, excluded = EP.pool_identities(ident, el["excluded"], rel)                 # the packs' identities: the eligible set, and the excluded ones beside it when no eligible identity carries a supported claim
                candidates = _candidates_of(pool, excluded)
                _coverage_step(i, subject, hard, rv_now, ident, rel, citable, by_id)
                for cand in candidates:
                    cand["coverage"] = CS.verdicts_for(conn, pid, hard, rv_now, cand["product_id"], cand["product_version"]) if cand["product_id"] else {}
                _with_budget(subject, candidates)                                                 # each candidate's calculated figure or its reason, for the memo's writer
                hard_in = [{"id": h["id"], "key": h["key"], "value": h["value"], "coverage": {cand["label"]: cand["coverage"].get(h["id"], "unchecked") for cand in candidates}} for h in hard]
                packs = _packs(subject, hard, rel, citable, pool)                                 # the final sees the packs built over the enlarged evidence, as the draft saw its own
                _with_checked(candidates, packs, hard)                                            # the dates the evidence was checked, per candidate
                pack_support = {c0["claim_id"] for c0 in EP.claim_lines(packs) if not c0.get("contradicts")}   # the set the final may cite (the packs' supporting lines), as the draft's
                _record_packs(subject, packs)
                if any(p0["insufficient_capacity"] for p0 in packs):                               # the comparison is not asked to decide
                    _open_for_capacity(skey, subject, hard, req_links, packs, final=True)
                    return
                msgs = prompts.messages("compare_options", {"decision_subject": subject, "hard_requirements": hard_in, "soft_preferences": soft, "candidates": candidates, "evidence_packs": [EP.prompt_view(p0) for p0 in packs],
                                                            "usage": _usage_numbers() or usage_answer or "none", "budget_ceiling": (budget or {}).get("ceiling_usd", "none")})
                r = call_stage(ctx, "compare_options", skey, M.ComparisonOutV2, msgs)
                r = _identity_repair(skey, msgs, r, pack_support, by_id)                         # a refused pick gets the comparison's one repair, judged on the packs' supporting lines
                extras = dict(memo_extras.get(subject, {}))
                if r["status"] == "flagged":
                    choice, rationale, cited, rejected, status, memo, rec, findings = "unresolved", f"comparison flagged: {r['detail']}", [], [], "unresolved", None, None, []
                    if r.get("gave_up"):
                        memo = dict(GAVE_UP_MEMO, final=True, pack_line=EP.pack_line(packs), **{k: v for k, v in extras.items() if k in ("panel", "currency", "panel_detail")})
                        memo.update(settling_of(hard, memo, leading_of(subject, ident, citable, by_id)))
                else:
                    out, merged_n = _merge_options(r["output"])                             # one row per canonical identity
                    rec = _pick_option(out)                                                  # the recommendation's text may differ from its option's (for example "KolWrite (API, Base model, uploaded recordings)" against "KolWrite (API)"); the pick must not be lost
                    rec = _preferred(out, rec, hard, ident, pack_support, by_id)              # the order of preference among the memo's options
                    wanted = list(dict.fromkeys(list(rec.claim_ids) + [c0 for w0 in out.why for c0 in w0.claim_ids])) if rec else []
                    cited = [x for x in wanted if x in pack_support]                                 # a citation is a supporting line of the packs
                    rejected = [{"option": o.option, "reason": (o.risk or o.verdict)[:200] or "not recommended"} for o in out.options if not rec or o.option != rec.option]
                    kept_why = _kept_why(out.why, pack_support)                              # non-empty citations only, inside the packs
                    rationale = _rationale_of(kept_why)
                    findings = list(out.open)
                    with coverage.derived(conn, pid, run_id=rid):                                     # a decision's status is derived: read inside the accessor's reads context
                        draft_pick = (conn.execute("SELECT choice FROM decisions WHERE project_id=? AND subject=? AND status='stale' ORDER BY id DESC LIMIT 1", (pid, subject[:200])).fetchone() or [None])[0]
                    if rec and draft_pick and AP.norm(ident.get((rec.product_id, rec.product_version), rec.option)) != AP.norm(draft_pick) and extras.get("panel"):
                        extras["panel"] = extras["panel"].replace("the pick held", f"changed to {rec.option}" + (f" because {kept_why[0].claim_ids[0]} {kept_why[0].text[:80]}" if kept_why and kept_why[0].claim_ids else ""))
                    other_class: list = []                                                        # the listed options the store records under another decision's class
                    memo = {"recommendation": out.recommendation.model_dump() if out.recommendation else None, "runner_up": out.runner_up.model_dump() if out.runner_up else None,
                            "why": [{"text": w0.text, "claim_ids": [c0 for c0 in w0.claim_ids if c0 in pack_support]} for w0 in kept_why],
                            "options": _class_options(subject, out, [{**o.model_dump(), "claim_ids": [c0 for c0 in o.claim_ids if c0 in EP.option_support(packs, o.product_id, o.product_version, pack_support)]} for o in out.options], other_class), "change_mind": list(out.change_mind), "open": list(out.open), **({"options_other_class": other_class} if other_class else {}),
                            "dropped_sentences": len(out.why) - len(kept_why), "coverage": {cand["label"]: cand.get("coverage", {}) for cand in candidates}, "final": True, "pack_line": EP.pack_line(packs), **({"merged_options": merged_n} if merged_n else {}), **{k: v for k, v in extras.items() if k in ("panel", "currency", "panel_detail")}}
                    product_mismatch = [x for x in cited if not _about_an_option(rec, out, by_id[x], any(_about_pick(rec, by_id[y]) for y in cited))] if rec else []   # a cited claim is about the pick, or (the pick having evidence of its own) about another option it is compared with
                    if product_mismatch and any(_about_pick(rec, by_id[y]) for y in cited):      # a citation about no option of the memo is refused and its sentence dropped; the decision stands on the pick's own evidence
                        refused_identity = list(product_mismatch)
                        cited = [x for x in cited if x not in refused_identity]
                        kept_why = [w0 for w0 in kept_why if all(c0 in cited or c0 not in by_id for c0 in w0.claim_ids)]
                        memo["why"] = [{"text": w0.text, "claim_ids": [c0 for c0 in w0.claim_ids if c0 in pack_support]} for w0 in kept_why]
                        memo["dropped_sentences"] = len(out.why) - len(kept_why)
                        memo["refused_identity"] = refused_identity
                        if "refused_cites" in locals():
                            refused_cites.extend(refused_identity)                                 # the step record names them beside the citations refused for scope
                        rationale = _rationale_of(kept_why)                                       # the kept sentences alone, never the removed one
                        product_mismatch = []
                    _reject_violating(memo, rejected, out, rec, hard)                           # every option with a proven violation reads rejected, as at the draft
                    cited, kept_why, rr, still = _recite(_currency_reread(i, rec, rel), cited, kept_why, memo, out, rel, by_id)   # as at the draft: a changed pick gets its own reads, a page read at the draft is not read again
                    rationale = rr if rr is not None else rationale
                    citable = still if still is not None else citable                               # the refreshed citable set after a re-read
                    if still is not None:
                        _budget_by_calculation(subject, hard, rv_now, ident, citable, by_id)        # the budget is calculated again on the prices as the re-read left them, as at the draft
                    _name_pick(subject, rec, ident, hard, citable, by_id, getattr(out, "recommendation", None))                          # the pick's budget on the rows of what the recommendation names, as at the final comparison
                    evidence_by_req = {}
                    hs = CS.hard_status(conn, pid, hard, rv_now, rec.product_id, rec.product_version) if rec and rec.product_id else None
                    failed = (not rec.product_id or product_mismatch or rec.verdict == "rejected" or hs == "fail" or not cited) if rec else False   # a none verdict does not fail the pick; the gate below reads it as not yet proven
                    outside = rec is not None and bool(rec.product_id) and eligible_pick(ident, rec) is None   # a pick outside the subject's eligible typed candidates is refused
                    evidence_refusal = rec is not None and bool(rec.product_id) and not product_mismatch and not outside and rec.verdict != "rejected" and hs == "fail"   # a proven violation speaks in its own words; missing support (not proven, or sources that disagree) is read at the gate below
                    if evidence_refusal and hs == "fail":                                             # a proven violation: rejected with the claim quoted
                        choice, status = "unresolved", "unresolved"
                        rationale = CS.rejection_line(conn, pid, rec.option, CS.violations(conn, pid, hard, rv_now, rec.product_id, rec.product_version))
                        _reject_option(memo, rejected, rec, rationale); memo["evidence_state"] = "proven_violation"
                        cited = []
                    elif rec is None or failed or outside or not kept_why:
                        choice, status = "unresolved", "unresolved"
                        cause = "no recommendation" if rec is None else "no product identity" if not rec.product_id else (f"the pick {rec.product_id} {rec.product_version} is not among the subject's eligible typed candidates ({', '.join(sorted(ident.values())) or 'none'})".replace("  ", " ") if outside and not product_mismatch else ("citations or hard constraints failed" if failed else f"{NO_RATIONALE}: no why sentence of the memo cites a supported, in-scope claim"))   # a pick without a retained reason is unresolved, the removed text never its rationale; no pick is "no recommendation", never a read of its product id; the eligible-set reason speaks before a missing citation, as in the draft (a pick outside the set can cite nothing: its claims are in no pack)
                        rationale = f"final comparison of '{rec.option}' could not be activated: {cause}" if rec else "no recommendation"
                        cited = []
                    else:
                        for h in hard:
                            evidence_by_req[h["id"]] = CS.counting_support(conn, pid, h, rv_now, rec.product_id, rec.product_version, cited)
                        lacking = [h for h in hard if not evidence_by_req.get(h["id"])]
                        if lacking:
                            disagree_line, memo["evidence_state"] = _evidence_state(conn, pid, hard, rv_now, rec, by_id, lacking)   # sources_disagree when the missing support sits in an open conflict, else not_proven
                            if memo["evidence_state"] == "sources_disagree":                       # sources disagree: the decision stays open
                                choice, status = "unresolved", "unresolved"
                                rationale = disagree_line
                                cited = []
                            else:                                                                  # a conditional recommendation, as at the draft
                                choice, status = ident[eligible_pick(ident, rec)], "active"
                                rationale = _conditional(memo, rec, choice, lacking, rationale)
                        else:
                            choice, status = ident[eligible_pick(ident, rec)], "active"        # the eligible candidate's label, never the model's text
                        if status == "active":
                            cited = _budget_support(memo, rec, hard, cited, evidence_by_req)     # the budget calculation is the budget's support, as at the draft
                    if status != "active" and memo is not None:
                        memo["open"] = list(dict.fromkeys(memo.get("open", []) + [f"what would settle it: {rationale}"[:300]]))
                        memo.update(settling_of(hard, memo, leading_of(subject, ident, citable, by_id) if rec is None and not out.options else None))
                if status == "active" and cited:
                    left = unprocessed_of(conn, pid, cited, rq_claims)                                 # the sources left unprocessed, as in the draft
                    if left:
                        memo["unprocessed_sources"] = left
                        memo["open"] = list(dict.fromkeys(memo.get("open", []) + [f"{len(left)} source(s) of the questions behind this decision were left unprocessed; the evidence cited here stands, and they could add to it."]))
                _excluded_open(memo, pool, excluded)                                                 # the open list names the excluded products of the pool with the reason
                with ctx.tx():
                    did = repo.next_id(pid, "DEC")
                    repo.upsert_decision(M.Decision(id=did, project_id=pid, subject=subject[:200], choice=choice[:300], rationale=rationale[:600], importance="high" if hard else "medium",
                                                    requirement_ids=req_links, claim_ids=cited, rejected=rejected, status=status, plan_version=next_plan_version(), run_id=rid))
                    conn.execute("UPDATE decisions SET unresolved_json=?, product_id=?, product_version=?, memo_json=? WHERE id=?", (json.dumps([f[:300] for f in findings][:20]), (rec.product_id if rec else None), (rec.product_version if rec else None), json.dumps(memo) if memo else None, did))
                    _store_packs(skey, did, subject, packs, "the final comparison saw the packs and nothing else")   # the packs stored with the decision
                    EX.record(conn, pid, rid, subject[:200], did, el["refused"])                 # the final's refusals, as the draft's
                    for h in hard:
                        coverage.ensure_subject(conn, pid, "requirement", h["id"])
                    reconcile.derive(conn, pid, run_id=rid)
                    with coverage.derived(conn, pid, run_id=rid):
                        status = conn.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0]
                    ctx.mark(skey, "compare_options", {"decision": did, "status": status, "final": True})
                decided.append(did)
                ev.emit(conn, rid, "decision", {"decision_id": did, "status": status, "subject": subject[:120], "final": True, **({"conditional": "not yet proven on " + "; ".join(q0["label"] for q0 in memo["conditional"]["requirements"])} if status == "active" and (memo or {}).get("conditional") else {})})   # the run's timeline says a conditional recommendation for what it is
            for i, subject in enumerate(subjects, start=1):
                skey = ctx.key("compare_options", f"{TAG}:d{i}")                                   # the reuse key names the prompt version and the model identity
                if ctx.done(skey) is not None:
                    continue
                hard = AP.binding(hard_all, subject)                                          # a decision tests only the hard requirements that bind its subject
                req_links = [h["id"] for h in hard] or [r0["id"] for r0 in AP.binding(ctx.requirements(confirmed_only=True), subject, hard_only=False)]   # a decision no hard requirement binds links to the requirements that bind it, so the plan's sweep never calls it unlinked
                subject_class = AP.norm((conn.execute("SELECT subject_class FROM candidates WHERE project_id=? AND subject=? LIMIT 1", (pid, AP.norm(subject))).fetchone() or [""])[0])
                rel = subject_claims(conn, pid, subject, claims, rq_claims, rq_subject, subjects)   # claims attached to this subject's own research questions or named for it by the extractor, and those of its predecessor name when a later plan renamed the subject
                price_claims = [c for c in rel if c.get("kind") == "price"]                      # a decision with a verified price claim among its evidence is cost-bearing
                if price_claims and usage_answer is None and not usage_asked:
                    usage_asked = True; usage_answer = _usage_answer()
                mine_rqs = [rq for rq, subj in rq_subject.items() if coverage._norm(subj) == coverage._norm(subject)]
                incomplete = sorted(rq for rq in mine_rqs if rq_processing.get(rq) == "in_progress")   # the subject's own questions whose extraction started and is not complete (needs_review included)
                not_covered = {rq: (f"evidence {rq_status[rq][0]}" if rq_status[rq][0] != "supported" else f"processing {rq_status[rq][1]}") for rq in mine_rqs if rq in rq_status and rq not in rq_covered}
                if not rel:                                                                       # no claim of its own, no comparison, no borrowed choice
                    why = "No page about this decision yielded a verified claim yet; a vendor page stating the price and the capability would settle it." if incomplete else "No evidence was found for this decision; a search of the vendors' pricing and documentation pages would settle it."
                    with ctx.tx():
                        did = repo.next_id(pid, "DEC")
                        repo.upsert_decision(M.Decision(id=did, project_id=pid, subject=subject[:200], choice="unresolved", rationale=why, importance="high" if hard else "medium",
                                                        requirement_ids=req_links, claim_ids=[], rejected=[], status="unresolved", plan_version=next_plan_version(), run_id=rid))
                        ctx.mark(skey, "compare_options", {"decision": did, "status": "unresolved", "reason": why})
                    decided.append(did)
                    ev.emit(conn, rid, "decision", {"decision_id": did, "status": "unresolved", "subject": subject[:120], "reason": why})
                    continue
                citable = citable_of(conn, pid, rel, rv_now)                                     # a supported, in-scope claim at the current versions is citable unless it is in an open conflict
                el = eligible_candidates(conn, pid, subject, rel); ident = el["identities"]   # the one typed eligible set (dropped labels out, spellings of one identity collapsed), shared with the final comparison and the activation
                pool, excluded = EP.pool_identities(ident, el["excluded"], rel)                 # the packs' identities: the eligible set, and the excluded ones beside it when no eligible identity carries a supported claim
                candidates = _candidates_of(pool, excluded)
                by_id = {c["id"]: c for c in claims}
                _coverage_step(i, subject, hard, rv_now, ident, rel, citable, by_id)          # the coverage step per product and binding hard requirement, before the comparison
                for cand in candidates:
                    cand["coverage"] = CS.verdicts_for(conn, pid, hard, rv_now, cand["product_id"], cand["product_version"]) if cand["product_id"] else {}
                _with_budget(subject, candidates)                                                 # each candidate's calculated figure or its reason, for the memo's writer
                hard_in = [{"id": h["id"], "key": h["key"], "value": h["value"], "coverage": {cand["label"]: cand["coverage"].get(h["id"], "unchecked") for cand in candidates}} for h in hard]
                packs = _packs(subject, hard, rel, citable, pool)                                 # the evidence packs, one per candidate of the pool, the one evidence input of the comparison, the tree and the panel
                _with_checked(candidates, packs, hard)                                            # the dates the evidence was checked, per candidate, for the memo
                pack_view = [EP.prompt_view(p0) for p0 in packs]; pack_lines = EP.claim_lines(packs)
                pack_ids = {c0["claim_id"] for c0 in pack_lines}; pack_support = {c0["claim_id"] for c0 in pack_lines if not c0.get("contradicts")}
                _record_packs(subject, packs)
                if any(p0["insufficient_capacity"] for p0 in packs):                               # the comparison is not asked to decide
                    _open_for_capacity(skey, subject, hard, req_links, packs)
                    continue
                cmp_key, cmp_msgs = None, None                                                   # the comparison send whose answer a refusal repairs
                use_tree = bool(hard) and len([k for k in ident if k[0]]) >= 2 and (subject in tree_used or len(tree_used) < max_tree)   # a high-importance decision (hard requirements apply) with alternatives to explore (two or more candidate identities) gets a tree, at most max_tree per run; the rest take the simple comparison
                r = None
                if use_tree:
                    tree_used.add(subject)
                    r = _tree_compare(i, skey, subject, rel, citable, by_id, ident, pack_lines, pack_ids, pack_support, usage_answer, budget)
                    if r["status"] == "flagged":                                               # a tree that selects no branch never leaves the decision without a memo: its outcome is recorded (the branch event, the reasoning record, this step) and the comparison decides on the packs as in the simple configuration
                        ev.step(conn, rid, "compare_options", "Tree selected no branch", f"{subject[:60]}: {str(r.get('detail') or '')[:160]}; the comparison decides on the packs")
                        r = None
                    elif r["status"] == "tree":                                                # the comparison model writes the tree's choice as a reader memo; the tree's own sentence stays in the provenance
                        tsel = r["output"]
                        tree_cands = []
                        for o in tsel.options:
                            if o.product_id:
                                cand = {"product_id": o.product_id, "product_version": o.product_version or "", "label": ident.get((o.product_id, o.product_version or ""), o.option)}
                                if o.option == tsel.recommended:
                                    cand["note"] = "selected by the bounded exploration of this decision; name it with its product and mode unless the evidence says otherwise"
                                if cand not in tree_cands:
                                    tree_cands.append(cand)
                        for cand in tree_cands:
                            cand["coverage"] = CS.verdicts_for(conn, pid, hard, rv_now, cand["product_id"], cand["product_version"]) if cand["product_id"] else {}
                        _with_checked(tree_cands, packs, hard)                                     # the dates the evidence was checked, per candidate
                        _with_budget(subject, tree_cands)                                          # each candidate's calculated figure or its reason
                        hard_in_t = [{"id": h["id"], "key": h["key"], "value": h["value"], "coverage": {cand["label"]: cand["coverage"].get(h["id"], "unchecked") for cand in tree_cands}} for h in hard]
                        msgs_t = prompts.messages("compare_options", {"decision_subject": subject, "hard_requirements": hard_in_t, "soft_preferences": soft, "candidates": tree_cands or candidates, "evidence_packs": pack_view,
                                                                      "usage": _usage_numbers() or usage_answer or "none", "budget_ceiling": (budget or {}).get("ceiling_usd", "none")})
                        r_memo = call_stage(ctx, "compare_options", f"{skey}:memo", M.ComparisonOutV2, msgs_t)
                        react.record(conn, rid, skey, r.get("entry") or f"{skey}:memo", f"compare {subject[:80]}", "the tree's memo", (tsel.rationale or "")[:300], "the reader memo written by the comparison model; the exploration sentence kept here")   # the code-composed string goes to provenance, never to a reader field
                        if r_memo["status"] != "flagged":
                            r = dict(r_memo, tree=r.get("tree"), entry=r_memo.get("entry") or r.get("entry"))
                            cmp_key, cmp_msgs = f"{skey}:memo", msgs_t
                if r is None:                                                                  # the simple comparison: no tree for this decision, or a tree that selected no branch
                    msgs = prompts.messages("compare_options", {"decision_subject": subject, "hard_requirements": hard_in, "soft_preferences": soft, "candidates": candidates, "evidence_packs": pack_view,
                                                                "usage": _usage_numbers() or usage_answer or "none", "budget_ceiling": (budget or {}).get("ceiling_usd", "none")})
                    r = call_stage(ctx, "compare_options", skey, M.ComparisonOutV2, msgs)
                    cmp_key, cmp_msgs = skey, msgs
                r = _identity_repair(cmp_key, cmp_msgs, r, pack_support, by_id)                  # a refused pick gets the comparison's one repair with the reason, judged on the packs' supporting lines
                if r["status"] != "flagged":
                    merged_out, merged_n = _merge_options(memo_of(r["output"]))               # one memo shape for the tree's result and the comparison's; one row per canonical identity before anything is stored
                    r = dict(r, output=merged_out, merged_options=merged_n)
                refused_cites = []
                evidence_by_req = {}
                if r["status"] == "flagged":
                    choice, rationale, cited, rejected, status = "unresolved", f"comparison flagged: {r['detail']}", [], [], "unresolved"
                    if r.get("gave_up"):
                        rec, memo = None, dict(GAVE_UP_MEMO, pack_line=EP.pack_line(packs))
                        memo.update(settling_of(hard, memo, leading_of(subject, ident, citable, by_id)))
                else:
                    out = r["output"]                                                          # the memo shape (ComparisonOutV2 or memo_of(the tree's result))
                    rec = _pick_option(out)                                                  # the recommendation's text may differ from its option's (for example "KolWrite (API, Base model, uploaded recordings)" against "KolWrite (API)"); the pick must not be lost
                    rec = _preferred(out, rec, hard, ident, pack_support, by_id)              # the order of preference among the memo's options
                    wanted = list(dict.fromkeys(list(rec.claim_ids) + [c0 for w0 in out.why for c0 in w0.claim_ids])) if rec else []
                    cited = [x for x in wanted if x in pack_support]; refused_cites = [x for x in wanted if x not in pack_support]   # a citation is a supporting line of the packs; a claim the cap omitted, or in no pack, is refused
                    rejected = [{"option": o.option, "reason": (o.risk or o.verdict)[:200] or "not recommended"} for o in out.options if not rec or o.option != rec.option]
                    kept_why = _kept_why(out.why, pack_support)                              # a sentence whose citation is outside the subject's supported, in-scope claims or outside the packs is dropped, never the decision; an uncited sentence too
                    dropped_why = len(out.why) - len(kept_why)
                    rationale = _rationale_of(kept_why)                                          # the kept sentences alone
                    findings = list(out.open)
                    other_class: list = []                                                        # the listed options the store records under another decision's class
                    memo = {"recommendation": out.recommendation.model_dump() if out.recommendation else None, "runner_up": out.runner_up.model_dump() if out.runner_up else None,
                            "why": [{"text": w0.text, "claim_ids": [c0 for c0 in w0.claim_ids if c0 in pack_support]} for w0 in kept_why],
                            "options": _class_options(subject, out, [{**o.model_dump(), "claim_ids": [c0 for c0 in o.claim_ids if c0 in EP.option_support(packs, o.product_id, o.product_version, pack_support)]} for o in out.options], other_class), "change_mind": list(out.change_mind), "open": list(out.open), **({"options_other_class": other_class} if other_class else {}),
                            "dropped_sentences": dropped_why, "coverage": {cand["label"]: cand.get("coverage", {}) for cand in candidates}, "pack_line": EP.pack_line(packs), **({"merged_options": r["merged_options"]} if r.get("merged_options") else {})}   # the rows merged away, counted; the packs' one line
                    memo.update({k: v for k, v in memo_extras.get(subject, {}).items() if k in ("panel", "currency", "panel_detail")})
                    product_mismatch = [x for x in cited if not _about_an_option(rec, out, by_id[x], any(_about_pick(rec, by_id[y]) for y in cited))] if rec else []   # a cited claim is about the pick, or (the pick having evidence of its own) about another option it is compared with
                    if product_mismatch and any(_about_pick(rec, by_id[y]) for y in cited):      # a citation about no option of the memo is refused and its sentence dropped; the decision stands on the pick's own evidence
                        refused_identity = list(product_mismatch)
                        cited = [x for x in cited if x not in refused_identity]
                        kept_why = [w0 for w0 in kept_why if all(c0 in cited or c0 not in by_id for c0 in w0.claim_ids)]
                        memo["why"] = [{"text": w0.text, "claim_ids": [c0 for c0 in w0.claim_ids if c0 in pack_support]} for w0 in kept_why]
                        memo["dropped_sentences"] = len(out.why) - len(kept_why)
                        memo["refused_identity"] = refused_identity
                        if "refused_cites" in locals():
                            refused_cites.extend(refused_identity)                                 # the step record names them beside the citations refused for scope
                        rationale = _rationale_of(kept_why)                                       # never the removed sentence
                        product_mismatch = []   # exact identity, never token overlap
                    _reject_violating(memo, rejected, out, rec, hard)                           # every option with a proven violation reads rejected, with the quote or the arithmetic
                    cited, kept_why, rr, still = _recite(_currency_reread(i, rec, rel), cited, kept_why, memo, out, rel, by_id)   # the vendor's pages of the pick's priced claims read again before the gate reads the support
                    rationale = rr if rr is not None else rationale
                    citable = still if still is not None else citable                               # the refreshed citable set is what the gates and the cost table's eligibility read
                    if still is not None:
                        _budget_by_calculation(subject, hard, rv_now, ident, citable, by_id)        # the budget is calculated again on the prices as the re-read left them
                    _name_pick(subject, rec, ident, hard, citable, by_id, getattr(out, "recommendation", None))                          # the pick's budget on the rows of what the recommendation names
                    if rec is None:
                        choice, status = "unresolved", "unresolved"
                    elif not citable and (incomplete or not_covered):                              # no supported claim yet, whatever the processing note; the reason in reader words, never a gate on review
                        choice, status = "unresolved", "unresolved"
                        whys = sorted({why.replace("evidence ", "the evidence is ").replace("processing ", "the reading is ") for why in not_covered.values()})
                        rationale = "No supported claim about these candidates was verified yet" + (f" ({'; '.join(whys)})" if whys else "") + "; a vendor page stating the price and the required capability would settle it."
                    elif not rec.product_id:                                                      # the option must name the product identity it selects
                        choice, status = "unresolved", "unresolved"
                        rationale = f"The option {rec.option} does not name a product; a decision must name the product and its plan or API."
                        cited = []
                    elif product_mismatch:                                                        # citations bind on exact product id and version
                        choice, status = "unresolved", "unresolved"
                        about = sorted({f"{by_id[x].get('product_id') or 'unnamed'} {by_id[x].get('product_version') or ''}".strip() for x in product_mismatch})
                        rationale = f"The evidence cited for {rec.option} ({rec.product_id} {rec.product_version}) is about {', '.join(about)}, not about it; evidence must be about the product it supports.".replace("  ", " ")
                        cited = []
                    elif eligible_pick(ident, rec) is None:                                       # a pick outside the subject's eligible typed candidates is refused with the reason (the evidence rule above speaks first when its evidence is another product's)
                        choice, status = "unresolved", "unresolved"
                        rationale = f"The pick {rec.option} ({rec.product_id} {rec.product_version}) is not among the subject's eligible typed candidates ({', '.join(sorted(ident.values())) or 'none'}); a decision names an eligible candidate.".replace("  ", " ")
                        cited = []
                    elif rec.verdict == "rejected":                                                 # the model's own verdict on its recommendation: never active, the constraints named
                        choice, status = "unresolved", "unresolved"
                        rationale = f"recommendation '{rec.option}' does not meet the hard constraints ({', '.join(h['key'] for h in hard) or 'none'}); risks: {(rec.risk or '')[:200]}"
                    elif (hs := CS.hard_status(conn, pid, hard, rv_now, rec.product_id, rec.product_version)) == "fail":   # a contradicts verdict on a binding hard requirement: a proven violation, the candidate rejected with the claim quoted
                        choice, status = "unresolved", "unresolved"
                        rationale = CS.rejection_line(conn, pid, rec.option, CS.violations(conn, pid, hard, rv_now, rec.product_id, rec.product_version))
                        _reject_option(memo, rejected, rec, rationale); memo["evidence_state"] = "proven_violation"
                    elif not cited:                                                               # no citation to store, nothing to activate
                        choice, status = "unresolved", "unresolved"
                        rationale = f"No verified claim supports {rec.option}; a vendor page confirming its price and capability would settle it."
                        memo["evidence_state"] = "not_proven"
                    elif not kept_why:                                                            # no why sentence of the memo survived the citation rule: unresolved, the removed text never its rationale
                        choice, status = "unresolved", "unresolved"
                        rationale = f"{NO_RATIONALE}: no why sentence of the memo cites a supported, in-scope claim about {rec.option}; a sentence citing one would settle it."
                    else:
                        for h in hard:                                                             # support is the persisted verification record naming the requirement at this version, or the coverage step's entails for the product
                            evidence_by_req[h["id"]] = CS.counting_support(conn, pid, h, rv_now, rec.product_id, rec.product_version, cited)
                        lacking = [h for h in hard if not evidence_by_req.get(h["id"])]
                        withdrawn = {h["id"]: {x: why for x, why in coverage.withdrawn_support(conn, pid, h["id"], rv_now).items() if x in cited} for h in hard}   # records withdrawn by the reconciliation (freshness by kind, eligibility, conflict)
                        for h in hard:
                            for x, why in sorted(withdrawn[h["id"]].items()):
                                findings.append(f"{x} does not count for {h['key']} ({h['id']}): {why}; check live before relying on it")
                        if lacking:                                                                # each applicable hard requirement needs its own counting support
                            gone = {h["id"]: {x: why for x, why in coverage.withdrawn_support(conn, pid, h["id"], rv_now).items() if x in by_id and _about_pick(rec, by_id[x])} for h in lacking}   # support out of the citable set (an open conflict) is not cited, and the reason still names it
                            disagree_line, memo["evidence_state"] = _evidence_state(conn, pid, hard, rv_now, rec, by_id, lacking)   # sources_disagree when the missing support sits in an open conflict, else not_proven
                            if memo["evidence_state"] == "sources_disagree":                       # sources that disagree on the requirement's attribute leave the decision open
                                choice, status = "unresolved", "unresolved"
                                rationale = disagree_line
                            else:                                                                  # the evidence is silent on a hard requirement: a conditional recommendation, the unproven requirement printed with it
                                choice, status = ident[eligible_pick(ident, rec)], "active"
                                notes = [f"{x} does not count for {W.label_of(h['key'])}: {why}." for h in lacking for x, why in sorted(gone[h["id"]].items())]   # the record's own reason for a claim that was offered as support and does not count, kept in the founder's sentence
                                rationale = _conditional(memo, rec, choice, lacking, " ".join(notes + [rationale]).strip())
                                findings.extend(n0.rstrip(".") for n0 in notes)
                        else:
                            choice, status = ident[eligible_pick(ident, rec)], "active"       # the label is the eligible candidate's, never the model's text
                        if status == "active":
                            cited = _budget_support(memo, rec, hard, cited, evidence_by_req)     # the calculation is the budget's support: its price claims are cited by the decision and its sentence stands among the reasons
                    if status != "active":
                        cited = []                                                                 # an unresolved decision cites nothing; its evidence is named in the rationale only
                        memo["open"] = list(dict.fromkeys(memo.get("open", []) + [f"what would settle it: {rationale}"[:300]]))   # the open shape keeps the memo with what would settle it
                        memo.update(settling_of(hard, memo, leading_of(subject, ident, citable, by_id) if rec is None and not out.options else None))
                    if refused_cites and r.get("entry"):
                        react.record(conn, rid, skey, r["entry"], f"compare {subject}", "citations refused", f"{len(refused_cites)} cited id(s) not citable (not a current, supported, in-scope claim of this subject)", "only citable claims kept")
                if r["status"] != "flagged" and cited:
                    left = unprocessed_of(conn, pid, cited, rq_claims)                             # the sources left unprocessed for the questions behind the citations are listed with the decision
                    if left:
                        memo["unprocessed_sources"] = left
                        memo["open"] = list(dict.fromkeys(memo.get("open", []) + [f"{len(left)} source(s) of the questions behind this decision were left unprocessed; the evidence cited here stands, and they could add to it."]))
                if r["status"] != "flagged" or r.get("gave_up"):
                    _excluded_open(memo, pool, excluded)                                             # the open list names the excluded products of the pool with the reason
                with ctx.tx():
                    did = repo.next_id(pid, "DEC")
                    repo.upsert_decision(M.Decision(id=did, project_id=pid, subject=subject[:200], choice=choice[:300], rationale=rationale[:600], importance="high" if hard else "medium",
                                                    requirement_ids=req_links, claim_ids=cited, rejected=rejected, status=status, plan_version=next_plan_version(), run_id=rid))   # the plan version this decision is made for (1 on the first run, n+1 on a revision)
                    conn.execute("UPDATE decisions SET unresolved_json=?, product_id=?, product_version=?, memo_json=? WHERE id=?", (json.dumps([f[:300] for f in (findings if r["status"] != "flagged" else [])][:20]),
                                 (rec.product_id if (r["status"] != "flagged" and rec) else None), (rec.product_version if (r["status"] != "flagged" and rec) else None), json.dumps(memo) if r["status"] != "flagged" or r.get("gave_up") else None, did))   # findings, the selected identity and the memo persisted
                    _store_packs(skey, did, subject, packs, "the comparison, the tree and the panel saw the packs and nothing else")   # the packs stored with the decision
                    EX.record(conn, pid, rid, subject[:200], did, el["refused"])                 # every identity the eligible set refused, one row with its rule and reason, for this decision
                    if price_claims:                                                           # the derived cost table of a cost-bearing decision, one row per option and price claim of its product, from the comparison's evidence and the usage answer; the model computed no figure
                        opts = [{"label": o.option, "product_id": o.product_id, "product_version": o.product_version} for o in out.options] if r["status"] != "flagged" else [dict(c0) for c0 in candidates]
                        table = derived.cost_table(subject, opts, price_claims, set(cited), usage_answer, budget, eligible=citable)   # a sourced monthly estimate needs a current, supported, in-scope price claim (the citable set); any other price evidence stays unresolved in the table
                        if table:
                            drv_id = f"{pid}.DRV-{next_number(conn, 'derived_tables', pid, 'DRV'):03d}"
                            conn.execute("INSERT INTO derived_tables (id, project_id, decision_id, run_id, title, fields_json, units_json, labels_json, rows_json, claim_ids_json, usage_question_id, budget_requirement_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                         (drv_id, pid, did, rid, table["title"], json.dumps(table["fields"]), json.dumps(table["units"]), json.dumps(table["labels"]), json.dumps(table["rows"]), json.dumps(table["claim_ids"]),
                                          (conn.execute("SELECT id FROM questions WHERE project_id=? AND kind='usage' AND status='answered' ORDER BY id DESC LIMIT 1", (pid,)).fetchone() or [None])[0] if usage_answer is not None else None, (budget or {}).get("requirement_id")))
                            react.record(conn, rid, skey, drv_id, f"derive the monthly cost of {subject[:80]}", "cost calculator (D-25, D-A28)", f"{len(table['rows'])} row(s) from {len(price_claims)} price claim(s); budget {'none' if budget is None else str(budget['ceiling_usd']) + ' USD ' + ('hard' if budget['hard'] else 'soft')}; usage {'answered' if usage_answer is not None else 'not answered'}", "derived rows stored with their labels; the model computed no figure")
                    stored = {r0[0] for r0 in conn.execute("SELECT from_id FROM trace_edges WHERE project_id=? AND to_id=? AND kind='claim_decision'", (pid, did))}
                    if status == "active" and (not stored or any(x not in stored for ev_ids in evidence_by_req.values() for x in ev_ids)):   # activation needs the stored citation edges
                        status = "unresolved"; choice = "unresolved"; rationale = "citation edges not stored; decision unresolved"
                        conn.execute("UPDATE decisions SET proposed_status='unresolved', choice='unresolved', rationale=? WHERE id=?", (rationale, did))   # the proposal is the base record; status is derived below
                    for h in hard:
                        coverage.ensure_subject(conn, pid, "requirement", h["id"])
                    drep = reconcile.derive(conn, pid, run_id=rid)                                 # the derivation in the same BEGIN IMMEDIATE transaction as the decision
                    status = conn.execute("SELECT status FROM decisions WHERE id=?", (did,)).fetchone()[0]
                    ctx.mark(skey, "compare_options", {"decision": did, "status": status, "refused_citations": refused_cites, "evidence_by_requirement": evidence_by_req, "incomplete_questions": incomplete, "tree": r.get("tree")})
                decided.append(did)
                ev.emit(conn, rid, "decision", {"decision_id": did, "status": status, "subject": subject[:120], **({"conditional": "not yet proven on " + "; ".join(q0["label"] for q0 in memo["conditional"]["requirements"])} if status == "active" and r["status"] != "flagged" and (memo or {}).get("conditional") else {})})   # the run's timeline says a conditional recommendation for what it is
                # after the draft memo, the currency check of the pick and the panel check; a new product or a contradiction
                # is researched in one bounded follow-up and the memo is finalised once (the draft decision superseded), never twice
                gave_up = bool(r.get("gave_up")) or (r["status"] != "flagged" and rec is None and not out.options)   # a comparison that gave up is unresolved like any other draft: the panel and the one bounded follow-up
                if (gave_up or (r["status"] != "flagged" and rec is not None)) and subject not in memo_extras:
                    extras: dict = {}
                    cur_line = None
                    if dcfg.get("currency", True) and rec is not None:
                        src_rows = [dict(zip(("id", "retrieved_at", "url", "tier"), r0)) for r0 in conn.execute(f"SELECT DISTINCT s.id, s.retrieved_at, s.url, s.tier FROM claims c JOIN sources s ON s.id=c.source_id WHERE c.project_id=? AND c.id IN ({','.join('?' * len(cited)) or 'NULL'})", (pid, *cited)).fetchall()]
                        claim_rows = [c0 for c0 in (dict(zip(("id", "source_id", "verdict", "scope_status", "text", "product_id", "product_version"), r0)) for r0 in conn.execute(f"SELECT id, source_id, verdict, scope_status, text, product_id, product_version FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(cited)) or 'NULL'})", (pid, *cited)).fetchall()) if _about_pick(rec, c0)]   # the supported claims about the pick, with their source's domain and tier, decide the currency line
                        cur = DS.currency_check({"product_id": rec.product_id, "product_version": rec.product_version, "option": rec.option}, _catalogue(), run_started, src_rows, claim_rows)
                        extras["currency"] = cur["line"]; cur_line = cur
                    reading = {"candidates": [], "questions": [], "models": 0}
                    if dcfg.get("panel", True) and not getattr(ctx.research, "simulated", False) or dcfg.get("simulate_panel"):
                        fusion = bool(dcfg.get("panel_server_tools", True))                 # false offers the provider no tool at all (the fusion tool's models search and fetch inside the provider, by the provider's documentation): the panel is then the route's one model reading the draft and the candidates' labels (for a component experiment on frozen evidence)
                        pmodel = dcfg.get("model") or None
                        if pmodel and pmodel not in (getattr(ctx.gateway, "prices", None) or {}):
                            pmodel = None
                        pkey = f"{rid}:panel:{ctx.worker}:{ctx.branch}:{reuse_tag(ctx, 'panel' if fusion else 'panel_direct', 'panel', pmodel)}:d{i}"   # the panel's reuse key names its prompt (panel or panel_direct) and the model identity
                        draft = {"recommendation": rec.option, "runner_up": (out.runner_up.option if out.runner_up else None), "why": [w0.text for w0 in kept_why][:4]} if rec is not None else {"recommendation": None, "runner_up": None, "why": []}
                        pmsgs = prompts.messages("panel" if fusion else "panel_direct", {"decision_subject": subject, "product_class": subject_class or "product", "binding_requirements": [{"id": h["id"], "key": h["key"], "value": h["value"]} for h in hard],
                                                           "usage": _usage_numbers() or usage_answer or "none", "candidates": [p0["label"] for p0 in packs] or [c0["label"] for c0 in candidates], "draft": draft})   # the panel names the packs' candidates; without the tool the instruction is panel_direct.v1, the one model's own reading, never the panel prompt's call of the fusion tool
                        ev.step(conn, rid, "panel", "The panel check", f"{subject[:80]}: " + (f"{len(dcfg.get('panel_models') or DS.PANEL_PRESET)} models, web search and fetch inside" if fusion else "one model, no tool offered, its own reading of the draft and the candidates' labels (panel_direct.v1)"))
                        pr = call_stage(ctx, "panel", pkey, M.PanelOut, pmsgs, model=pmodel, extra_body=DS.extra_body(f"{rid}:{AP.norm(subject)}:panel", 4, float(dcfg.get("panel_cap_usd", 0.75))) if fusion else None,   # the panel sends max_tool_calls as its one step bound, no stop_server_tools_when (a settings file naming a stop_shape does not put the cost condition on the wire)
                                        server_tools=[DS.fusion_tool(dcfg.get("panel_models") or None, 4)] if fusion else None, prompt_tokens_est=DS.panel_prompt_tokens(float(dcfg.get("panel_cap_usd", 0.75)), (getattr(ctx.gateway, "prices", {}) or {}).get(pmodel or ctx.gateway.default_model, (1.0, 5.0))) if fusion else 0)   # the whole per-decision cap reserved: the panel's models bill inside the one request; without the tool the gateway's own estimate of the one send
                        if pr["status"] == "flagged" or getattr(pr.get("output"), "status", "ok") != "ok":
                            extras["panel"] = f"Panel check: not completed ({(pr.get('detail') or getattr(pr.get('output'), 'failure_reason', None) or 'the fusion tool failed')[:120]}); " + ("the pick stands on our evidence" if rec is not None else "the draft had no pick")
                        else:
                            requested = list(dcfg.get("panel_models") or DS.PANEL_PRESET) if fusion else [pmodel or ctx.gateway.default_model]   # the models the request named
                            reading = DS.panel_reading(pr["output"].model_dump(), [{"label": c0["label"], "product_class": subject_class} for c0 in candidates], subject_class, requested=requested)
                            if not fusion:
                                reading["models"] = {"requested": 1, "answered": 1, "others": {}}   # the record says what ran, one model and no panel of three; its answer is the one substantive response
                            extras["panel_detail"] = {"consensus": pr["output"].consensus[:6], "blind_spots": reading["blind_spots"][:6], "new_candidates": [c0["label"] for c0 in reading["candidates"]], "questions": reading["questions"][:4], "dropped": [c0["label"] for c0 in reading["dropped"]], **({"notes": reading["notes"]} if reading.get("notes") else {})}   # a panel text shortened to its sentence end carries a note, kept with the record
                            extras["panel"] = DS.panel_line(reading, held=rec is not None)
                    memo_extras[subject] = extras
                    follow_cands = list(reading.get("candidates") or [])
                    if cur_line and cur_line["status"] == "superseded" and cur_line.get("current"):
                        follow_cands.append({"label": cur_line["current"], "product_class": subject_class, "reason": f"the current generation of {rec.option} (currency check)"})
                    new_n = 0
                    if not dcfg.get("followup", True) and (follow_cands or reading.get("questions")):   # the request is recorded, never researched (for a component experiment on frozen evidence)
                        extras["followup_withheld"] = {"candidates": [c0["label"] for c0 in follow_cands], "questions": list(reading.get("questions") or [])}
                        ev.step(conn, rid, "compare_options", "Follow-up withheld", f"{subject[:60]}: {len(follow_cands)} candidate(s) and {len(reading.get('questions') or [])} question(s) recorded, not researched (settings [discovery].followup off)",
                                candidates=extras["followup_withheld"]["candidates"], questions=extras["followup_withheld"]["questions"])
                    elif (follow_cands or reading.get("questions")) and not getattr(ctx.research, "simulated", False) or dcfg.get("simulate_panel"):
                        try:
                            new_n = _followup(i, subject, subject_class or "product", follow_cands, list(reading.get("questions") or []), hard, rv_now)
                        except StopRun:
                            raise
                        except Exception as e:  # noqa: BLE001
                            ev.step(conn, rid, "compare_options", "Follow-up failed", f"{type(e).__name__}: {str(e)[:160]}")
                    if new_n > 0:
                        with ctx.tx():
                            conn.execute("UPDATE decisions SET proposed_status='stale' WHERE id=?", (did,))   # the draft is superseded by the final memo; the derived status follows from derive below (a direct write to the derived column is refused)
                            reconcile.derive(conn, pid, run_id=rid)
                        decided.remove(did)
                        with ctx.tx():
                            conn.execute("DELETE FROM run_steps WHERE run_id=? AND step_key=?", (rid, skey))                 # the final comparison runs under its own key below
                        subjects_final.append((i, subject))
                    elif extras and memo is not None:                                            # the follow-up added no source, so the draft stands and carries its panel and currency lines as a final memo does
                        memo.update({k: v for k, v in extras.items() if k in ("panel", "currency", "panel_detail", "followup_withheld")})   # the withheld request rides on the memo
                        with ctx.tx():
                            conn.execute("UPDATE decisions SET memo_json=? WHERE id=?", (json.dumps(memo), did))
            for i, subject in subjects_final:                                                   # the one final comparison after a follow-up (never a second follow-up: memo_extras holds the subject)
                skey = ctx.key("compare_options", f"{TAG}:d{i}:final")
                if ctx.done(skey) is not None:
                    continue
                hard = AP.binding(hard_all, subject)
                req_links = [h["id"] for h in hard] or [r0["id"] for r0 in AP.binding(ctx.requirements(confirmed_only=True), subject, hard_only=False)]
                claims = ctx.run_claims(); rq_claims.clear()
                for (rq, cj) in conn.execute("SELECT subject_id, claim_ids_json FROM coverage WHERE project_id=? AND subject_type='research_question'", (pid,)).fetchall():
                    for cid in json.loads(cj):
                        rq_claims.setdefault(cid, set()).add(rq)
                rq_subject.update({r0[0]: r0[1] for r0 in conn.execute("SELECT id, decision_subject FROM research_questions WHERE project_id=?", (pid,))})
                rq_status.update({r0[0]: (r0[1], r0[2]) for r0 in conn.execute("SELECT subject_id, evidence_status, processing_status FROM coverage WHERE project_id=? AND subject_type='research_question'", (pid,)).fetchall()})
                rq_processing.update({k: v[1] for k, v in rq_status.items()}); rq_covered.clear(); rq_covered.update(rq_covered_of(rq_status))   # the same predicate as the draft's
                _decide_final(i, subject, hard, skey, claims)
            def _defer_unverified() -> None:
                """What the evidence allowance left of a revision's re-verification set is recorded as deferred,
                and every decision of this run whose candidates hold a deferred claim is marked unresolved with its settling check and the sentence
                that it is not yet re-verified against the changed requirement; a later revision run continues the sweep from the deferred claims
                (apply_change takes the pending remainder on the candidates). Once per run (a durable mark); a proven violation stands as it is."""
                dkey = ctx.key("compare_options", "deferred")
                if not state.get("change") or ctx.done(dkey) is not None:
                    return
                rev_ids = sorted(set((state.get("change") or {}).get("reverify") or []))
                deferred = [tuple(r0) for r0 in conn.execute(f"SELECT id, decision_subject FROM claims WHERE project_id=? AND verdict IS NULL AND invalidated_at IS NOT NULL AND id IN ({','.join('?' * len(rev_ids)) or 'NULL'}) ORDER BY id", (pid, *rev_ids)).fetchall()] if rev_ids else []
                affected: list[str] = []
                if deferred:
                    deferred_ids = {d0[0] for d0 in deferred}
                    by_subject: dict[str, set] = {}
                    for cid0, subj0 in deferred:
                        by_subject.setdefault(AP.norm(subj0 or ""), set()).add(cid0)
                    stale_ids = list(state.get("stale") or [])
                    for did0, subj0 in conn.execute(f"SELECT id, subject FROM decisions WHERE project_id=? AND id IN ({','.join('?' * len(stale_ids)) or 'NULL'})", (pid, *stale_ids)).fetchall():
                        for (cid0,) in conn.execute("SELECT from_id FROM trace_edges WHERE project_id=? AND kind='claim_decision' AND to_id=?", (pid, did0)).fetchall():
                            if cid0 in deferred_ids:
                                by_subject.setdefault(AP.norm(subj0 or ""), set()).add(cid0)       # a claim the re-opened decision cited affects its subject whatever the claim's own subject field
                    ch0 = state.get("change") or {}
                    with ctx.tx():
                        for did0 in decided:
                            row0 = conn.execute("SELECT subject, memo_json, proposed_status FROM decisions WHERE id=? AND project_id=?", (did0, pid)).fetchone()
                            if row0 is None or row0[2] == "stale":
                                continue
                            ids0 = sorted(by_subject.get(AP.norm(row0[0] or ""), set()))
                            if not ids0:
                                continue
                            memo0 = json.loads(row0[1]) if row0[1] else dict(GAVE_UP_MEMO)
                            if memo0.get("evidence_state") == "proven_violation":
                                continue
                            line0 = (f"Not yet re-verified against the changed requirement {ch0.get('requirement_key')} ({ch0.get('new_value')}): {len(ids0)} claim(s) of its candidates await re-verification ({', '.join(ids0[:6])}{', ...' if len(ids0) > 6 else ''}); "
                                     "the sweep stopped at the evidence allowance and a later revision run continues from them, so this decision is not shown as verified against the new requirement until it completes.")
                            hard0 = AP.binding(hard_all, row0[0] or "")
                            memo0.update(evidence_state="not_reverified", reverify_deferred=ids0)
                            if not memo0.get("settling_check"):
                                memo0.update(settling_of(hard0, memo0))                              # the check that settles it, from the memo's options
                            if not memo0.get("settling_check"):
                                memo0["settling_check"] = f"The check that settles this: the re-verification of the {len(ids0)} deferred claim(s) against {ch0.get('requirement_key')} ({ch0.get('new_value')}) in the next revision run."
                            memo0["open"] = [line0, memo0["settling_check"]] + [o0 for o0 in (memo0.get("open") or []) if o0 not in (line0, memo0["settling_check"])]
                            conn.execute("UPDATE decisions SET proposed_status='unresolved', choice='unresolved', rationale=?, memo_json=? WHERE id=? AND project_id=?", (line0[:600], json.dumps(memo0), did0, pid))
                            affected.append(did0)
                        if affected:
                            conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (pid,))
                            reconcile.derive(conn, pid, run_id=rid)
                        ctx.mark(dkey, "compare_options", {"deferred": sorted(deferred_ids), "decisions": affected})
                    ev.step(conn, rid, "verify", "Re-verification deferred", f"{len(deferred)} claim(s) of the change's impact set left unverified at the evidence allowance; {len(affected)} decision(s) marked not yet re-verified against {ch0.get('requirement_key')} ({', '.join(affected) or 'none'}); a later revision run continues the sweep from them",
                            deferred=sorted(deferred_ids), decisions=list(affected), criterion="I8")
                else:
                    with ctx.tx():
                        ctx.mark(dkey, "compare_options", {"deferred": [], "decisions": []})

            def _attributes_of(packs0: list) -> dict:
                """The attribute of every claim a must_address item names, read from the store once per decision (a verifier item names requirement and question ids)."""
                ids0 = sorted({str(x0) for p0 in packs0 if isinstance(p0, dict) for it0 in p0.get("must_address") or [] for x0 in it0.get("claim_ids") or [] if x0})
                return {r0[0]: r0[1] for k0 in range(0, len(ids0), 500) for r0 in conn.execute(f"SELECT id, attribute FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(ids0[k0:k0 + 500]))})", (pid, *ids0[k0:k0 + 500])).fetchall()} if ids0 else {}

            def _contradiction_block() -> None:
                """Criterion I7: a contradiction between sources does not pause the
                run. A decision of this run with a material contradiction unresolved, an open conflict of its packs the comparison did not address
                or sources that disagree on a binding hard requirement, is recorded open: unresolved, with the conflict in words (the
                option, the attribute, the values with their sources) and the check that settles it, on the decision's record; the other decisions and
                the plan go on. No question is asked and none waits. A claim the verifier refused is no contradiction between sources: it is noted on
                the decision (the source does not state it; it is not used as evidence) and opens nothing. Computed once per run (a durable mark)."""
                bkey = ctx.key("compare_options", "contradictions")
                if ctx.done(bkey) is not None:
                    return
                blocked: list[dict] = []
                with ctx.tx():
                    for did0 in decided:
                        row0 = conn.execute("SELECT subject, rationale, memo_json, evidence_pack_json, proposed_status FROM decisions WHERE id=? AND project_id=?", (did0, pid)).fetchone()
                        if row0 is None or row0[4] == "stale":
                            continue
                        memo0 = json.loads(row0[2]) if row0[2] else {}
                        packs0 = json.loads(row0[3]) if row0[3] else []
                        extra0 = [row0[1] or "sources disagree on a binding hard requirement"] if memo0.get("evidence_state") == "sources_disagree" else []
                        every0 = contradiction_items(packs0, memo0, _attributes_of(packs0))   # the option matched by identity, the explanation recognised by the difference it names and its effect
                        items0 = [it0 for it0 in every0 if it0.get("kind") == "conflict"]; refused0 = [it0 for it0 in every0 if it0.get("kind") != "conflict"]
                        for it0 in items0:
                            it0["sources"] = _sources_of(it0["claim_ids"]); it0["line"] = conflict_words(it0)
                        notes0 = [refused_words(it0) for it0 in refused0]
                        lines0 = extra0 + [it0["line"] for it0 in items0]
                        if not lines0 and not notes0:
                            continue
                        if notes0:
                            memo0["refused_claims"] = refused0                                # on the record, in its own words, never as a contradiction between sources
                            memo0["open"] = list(dict.fromkeys(list(memo0.get("open") or []) + notes0))
                        if not lines0:
                            conn.execute("UPDATE decisions SET memo_json=? WHERE id=? AND project_id=?", (json.dumps(memo0), did0, pid))
                            continue
                        memo0["contradictions"] = items0                                      # the items with their values, claim ids and sources ride on the decision's record (the card shows them)
                        reason0 = lines0[0] if extra0 else f"Open on a contradiction between sources: {'; '.join(lines0)}"
                        if row0[4] != "unresolved":                                            # an active decision is recorded open: unresolved with the conflict as its reason and its settling check
                            memo0.update(evidence_state="contradiction_unaddressed"); memo0.pop("conditional", None)
                            memo0["settling_check"] = CS.settling_check(conn, pid, AP.binding(hard_all, row0[0] or ""), rv_now, memo0.get("options") or []) or "The check that settles this: a current statement of the vendor on the attribute the sources disagree about settles it; until then the decision stays open."
                            memo0["open"] = list(dict.fromkeys([reason0, memo0["settling_check"]] + list(memo0.get("open") or [])))
                            conn.execute("UPDATE decisions SET proposed_status='unresolved', choice='unresolved', rationale=?, memo_json=? WHERE id=? AND project_id=?", (reason0[:600], json.dumps(memo0), did0, pid))
                        else:
                            if not extra0:
                                memo0["open"] = list(dict.fromkeys(list(memo0.get("open") or []) + [reason0]))
                            conn.execute("UPDATE decisions SET memo_json=? WHERE id=? AND project_id=?", (json.dumps(memo0), did0, pid))
                        blocked.append({"decision": did0, "lines": lines0, "items": items0})
                    if blocked:
                        conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (pid,))
                        reconcile.derive(conn, pid, run_id=rid)
                    ctx.mark(bkey, "compare_options", {"blocked": blocked})
                for b0 in blocked:
                    ev.emit(conn, rid, "decision", {"decision_id": b0["decision"], "status": "unresolved", "reason": "contradiction", "criterion": "I7",
                                                    "contradictions": [{"label": it0["label"][:120], "about": it0["about"][:80], "claim_ids": list(it0["claim_ids"]), "values": [str(v0)[:120] for v0 in it0["values"]], "line": it0.get("line", "")} for it0 in b0.get("items") or []]})   # the decision card carries the values and the claims
                if blocked:
                    ev.step(conn, rid, "compare_options", "Contradictions recorded", f"{len(blocked)} decision(s) left open on a contradiction between sources; the run goes on without a question", criterion="I7")

            def _sources_of(claim_ids: list) -> list:
                """Each claim of a conflict with its value and where it was read (the source's address), for the conflict in words."""
                ids0 = [x for x in claim_ids if x]
                rows0 = {r0[0]: r0 for r0 in conn.execute(f"SELECT c.id, c.value, COALESCE(s.url, c.source_id, '') FROM claims c LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=? AND c.id IN ({','.join('?' * len(ids0)) or 'NULL'})", (pid, *ids0)).fetchall()} if ids0 else {}
                return [{"claim_id": x, "value": str(rows0[x][1] or ""), "source": str(rows0[x][2] or "")} for x in ids0 if x in rows0]

            _defer_unverified()
            _contradiction_block()
            coverage.derived(conn, pid, run_id=rid)                                              # the guarded accessor
            if _after_compare[0] is not None:
                _after_compare[0]()                                                               # test hook: a crash after the decisions are stored and before the stage mark
            with ctx.tx():
                ctx.mark(key, "compare_options", {"decisions": decided})
            ev.step(conn, rid, "compare_options", "Comparison", f"{len(decided)} decision(s)")
            return {}

    # ---- plan_generate
    def _carry_drafts(reason: str, subjects: list) -> list[str]:
        """The
        version written after a stop during or before the comparisons carries every draft decision recorded before it: the latest row of each subject
        at this version goes back to what it decided (a draft's choice is "unresolved" exactly when it decided nothing), each is marked with the stop
        reason, and the plan's open issues name the drafts and the subjects not compared. A row with its final comparison stored is no draft. The rule covers, for example, finished drafts that a follow-up marked stale for final comparisons the cap then refused."""
        latest = {r0[1]: r0 for r0 in conn.execute("SELECT id, subject, choice, proposed_status, memo_json FROM decisions WHERE project_id=? AND plan_version=? ORDER BY rowid", (pid, next_plan_version())).fetchall()}
        drafts = []
        with ctx.tx():
            for did, subject, choice, proposed, memo_json in latest.values():
                memo = json.loads(memo_json) if memo_json else {}
                if memo.get("final"):
                    continue
                note = f"Draft: the run stopped ({reason.replace('_', ' ')}) before this decision was final."
                memo.update(draft_stop=reason, open=list(dict.fromkeys(list(memo.get("open") or []) + [note])))
                conn.execute("UPDATE decisions SET proposed_status=?, memo_json=? WHERE id=?", (proposed if proposed != "stale" else "unresolved" if choice == "unresolved" else "active", json.dumps(memo), did))
                drafts.append((AP.norm(subject).replace("_", " "), choice))
        return coverage.stop_issues(reason, drafts, [AP.norm(s0).replace("_", " ") for s0 in subjects if s0[:200] not in latest])

    def _preserve_draft(key: str, status: str, reason: str, plan=None) -> None:
        """The one permitted draft after a cap or time stop that cannot be stored as a
        version is preserved whole with the reason it could not be saved (app/core/snapshot.preserve_draft, plan_drafts): the plan's JSON when
        the stage holds a plan (a failure shape, an inconsistent plan, a snapshot that failed), else the answer text of the permitted send from its
        ledger row (raw_json of an invalid or truncated attempt), else nothing but the reason. The run's events name the record; nothing is resent."""
        row = conn.execute("SELECT id, outcome, state, raw_json FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? ORDER BY planned_at DESC, rowid DESC LIMIT 1", (rid, key + "%")).fetchone()
        content = plan.model_dump_json() if plan is not None else (row[3] if row else None)
        with ctx.tx():
            snapshot_mod.preserve_draft(conn, pid, rid, key, status, reason, content, ledger_entry=row[0] if row else None)
        ev.emit(conn, rid, "intervention", {"stage": "plan_generate", "signal": "draft_preserved", "step_key": key, "status": status, "content": content is not None,
                                            "message": f"the one permitted draft could not be stored as a version ({status}: {reason[:160]}); the response is preserved with the reason (plan_drafts, D-A267) and nothing is resent"})

    _editor_stop: list = [None]                                                           # the stop that kept the Editor's pass from running, handed from the store to the plan node

    def plan_generate(state: RunState) -> RunState:
        key = ctx.key("plan_generate")
        if (d := ctx.done(key)) is not None:                                                     # the durable replay returns the run outcome the mark recorded, nothing else; a mark without one (an older mark) reads as completed with reason complete, the only legacy rule
            return {"plan_version": d.get("plan_version"), **d.get("outcome", {"status": "completed", "stop_reason": "complete"})}
        stop_lines = _carry_drafts(state["stop_reason"], state.get("decision_subjects") or []) if state.get("stop_reason") in DRAFT_STOPS else []   # a stop reason on entry came from an earlier node, so the comparisons did not finish; the drafts are carried before the decisions are read
        reqs = ctx.requirements(confirmed_only=True)
        with coverage.derived(conn, pid, run_id=rid):
            decisions = [dict(zip(("id", "subject", "choice", "status", "product_id", "product_version"), r)) for r in conn.execute("SELECT id, subject, choice, status, product_id, product_version FROM decisions WHERE project_id=? ORDER BY id", (pid,)).fetchall()]   # planning receives the canonical identity
            decisions = [d for d in decisions if d["status"] not in ("stale", "rejected")]      # a stale decision (superseded by a revision) or a rejected one is not part of the plan
            supported = [{"claim_id": c["id"], "text": c["text"], "status": c["status"], "scope_status": c["scope_status"], "conditions": json.loads(c["conditions_json"] or "[]")} for c in ctx.run_claims() if c["status"] in ("supported", "partial")]
            values = {"confirmed_requirements": reqs, "active_decisions": decisions, "supported_claims": supported, "sections_to_regenerate": "none: the whole plan"}
            msgs = prompts.messages("plan_generate", values)
            if not state.get("stop_reason") and ctx.over_time():                                  # the limit is met at the plan's own first dispatch: this is the time stop, and the one draft follows it
                state = dict(state, **_stop(state, "plan_generate", StopRun("time_limit", f"{ctx.settings['budget']['max_minutes']} minutes of active run time reached before the plan; the one permitted draft follows")))
            if state.get("stop_reason") in ("time_limit", "call_limit"):
                ctx.grant_draft()                                                                 # durable, run-scoped; granted once per run, consumed at the first actual plan dispatch (the call limit earns the draft as the time limit does)

            def draft():
                try:
                    return generate_plan(ctx.gateway, key, msgs, prompts.section_messages(values), conn=conn, project_id=pid, run_id=rid, finishing=True, before_dispatch=lambda what: ctx.check_time(f"plan_generate {what}"))
                except BudgetExceeded as e:
                    raise StopRun("budget_reserve_reached", f"plan_generate: {str(e)[:160]}") from e
                except UnknownCallPending as e:
                    raise StopRun("unknown_call", str(e)[:200]) from e
                except RunStopped as e:
                    raise StopRun("run_stopped", str(e)[:200]) from e
            try:
                res = draft()
            except StopRun as s0:                                                                 # a time stop anywhere before the first actual plan dispatch is handled here
                sent_already = ctx.ledger.was_sent(key)                                           # persisted send evidence only: a planned attempt released unsent by recovery is not a send
                if s0.reason != "time_limit" or state.get("stop_reason") or sent_already:
                    if sent_already and (state.get("stop_reason") in DRAFT_STOPS or s0.reason == "time_limit"):   # the permitted draft was sent and the stage stops before its repair, fallback or sections: what came back is preserved with the reason, never lost
                        row0 = conn.execute("SELECT outcome, state FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key LIKE ? ORDER BY planned_at DESC, rowid DESC LIMIT 1", (rid, key + "%")).fetchone()
                        status0 = row0[0] if row0 and row0[0] else ("provider_error" if row0 and row0[1] == "failed" else s0.reason)   # the attempt's typed outcome (invalid, truncated), a provider failure, else the stop's reason
                        _preserve_draft(key, status0, f"the permitted draft's send settled without a usable plan and the stage stopped before a repair, a fallback or the sections: {s0.detail[:200]}")
                    raise                                                                         # credit, unknown call and stop refusals keep their meaning; a second time stop is final; once the draft request was sent, a time stop after its failure is the time stop (no repair, no fallback, no resend)
                state = dict(state, **_stop(state, "plan_generate", s0))                          # the stop with its pause event and coverage report
                if not (ctx.grant_draft() or ctx.draft_permit() == "permitted"):
                    raise                                                                         # the permit was already used: no further draft
                res = draft()                                                                     # the one retry; the permit is consumed at the actual dispatch
            if ctx.draft_consumed:
                entry = conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND step_key LIKE ? AND kind='model' ORDER BY planned_at DESC, rowid DESC LIMIT 1", (rid, key + "%")).fetchone()
                if entry:
                    react.record(conn, rid, key, entry[0], "plan after the time stop", "the one permitted draft", f"status {res['status']}", "no further send after expiry; the permit is used")
            if res["status"] in ("credit", "unknown", "stopped"):
                raise _stop_from_status(res["status"], res.get("detail"))
            if _after_plan_draft[0] is not None:
                _after_plan_draft[0]()                                                            # test hook: a crash after the draft is durably stored and before the plan records are written
            drafting = state.get("stop_reason") in DRAFT_STOPS                                  # this is the one permitted draft after a cap or time stop
            if res["status"] != "ok":
                ev.emit(conn, rid, "intervention", {"stage": "plan_generate", "message": f"plan not stored: {res['status']} ({str(res.get('detail') or res.get('issues'))[:200]})"})
                if drafting:
                    _preserve_draft(key, res["status"], f"{res['status']}: {str(res.get('detail') or res.get('issues'))[:300]}", plan=res.get("plan") or res.get("answer"))   # a permitted response that cannot form a valid draft is preserved whole with the reason (an inconsistent plan, or a failure shape's answer)
                outcome = {"status": "partial", "stop_reason": state.get("stop_reason") or f"plan_{res['status']}"}
                with ctx.tx():
                    ctx.mark(key, "plan_generate", {"status": res["status"], "plan_version": None, "outcome": outcome})   # the mark records the run outcome beside the plan's own token
                return outcome
            plan: M.PlanOut = res["plan"]
            unresolved = list(plan.unresolved_checks)
            coverage.derived(conn, pid, run_id=rid)                                              # the guarded accessor
            for line in stop_lines + coverage.plan_unresolved_lines(conn, pid) + coverage.settling_checks(conn, pid) + CS.conditional_lines(conn, pid):   # every stored plan carries the coverage report and the unresolved comparison findings, and what settles an unresolved decision; the drafts and the subjects not compared come first
                if line not in unresolved:
                    unresolved.append(line)
            skipped = ["the planner enrichment", "the verification plan", "the Editor's pass"]
            if drafting:                                                                         # the permitted draft is stored before and independently of the optional steps, as an explicitly partial draft (otherwise the enrichment's time check could raise a second stop before the plan records are written)
                partial_line = coverage.partial_draft_line(state["stop_reason"], skipped)
                if partial_line not in unresolved:
                    unresolved.insert(len(stop_lines), partial_line)                              # after the stop lines (the drafts and the subjects not compared stay first), before the coverage report
                extras = {"enrichment": None, "verification": None, "notes": [f"enrichment skipped: the one permitted draft after the {state['stop_reason'].replace('_', ' ')} stop is stored without the optional steps (D-A267)"]}
            else:
                extras = _enrich(key, plan, reqs, decisions) if bool((ctx.settings.get("extraction") or {}).get("enrichment", True)) else {"enrichment": None, "verification": None, "notes": ["enrichment off"]}
                closing = extras.pop("stopped", None)
                if closing is not None:                                                          # a limit of the run refused the enrichment or the verification plan after the plan was written: the plan is stored as the permitted draft is, explicitly partial, naming what did not run, with no further send
                    skipped = list(extras.pop("skipped")) + ["the Editor's pass"]
                    state = dict(state, **_stop(state, "plan_generate", closing)); drafting = True
                    partial_line = coverage.partial_draft_line(closing.reason, skipped)
                    if partial_line not in unresolved:
                        unresolved.insert(len(stop_lines), partial_line)
            _editor_stop[0] = None
            try:
                version = _store_plan(plan, unresolved, assembled=res.get("assembled", False), state=state, extras=extras, partial={"stop_reason": state["stop_reason"], "skipped": skipped} if drafting else None)
            except snapshot_mod.SnapshotError as e:                                            # a snapshot that does not validate fails the version creation (the transaction rolled back); the run ends on that reason
                if drafting:
                    _preserve_draft(key, "stage_failed", f"plan version not created, the snapshot failed: {str(e)[:200]}", plan=plan)
                raise StopRun("stage_failed", f"plan version not created, the snapshot failed: {str(e)[:200]}") from e
            except StopRun as e:
                if drafting:
                    _preserve_draft(key, e.reason, f"plan version not created, the stage stopped while storing it: {str(e.detail)[:200]}", plan=plan)
                raise
            editor_stop, _editor_stop[0] = _editor_stop[0], None
            if editor_stop is not None:                                                          # the Editor's pass did not run and the version is stored and signed without it; the run's record and the run screen say so in the founder's words
                ev.emit(conn, rid, "intervention", {"stage": "plan_generate", "signal": "editor_not_run", "reason": editor_stop.reason, "plan_version": version, "message": W.editor_not_run(editor_stop.reason, version), "detail": f"{editor_stop.reason}: {editor_stop.detail}"[:300]})
                with conn:
                    conn.execute("INSERT INTO errors (run_id, step_key, category, detail) VALUES (?,?,?,?)", (rid, ctx.key("editor"), "policy", f"editor not run, version {version} stored without it: {editor_stop.reason}: {editor_stop.detail}"[:500]))
                if editor_stop.reason not in CLOSING_STOPS and editor_stop.reason not in ("stage_failed", "editor_failed"):
                    state = dict(state, **_stop(state, "plan_generate", editor_stop))           # the provider account's credit or the founder's Stop ends the run as it does anywhere; the version it wrote is stored
            try:
                snapshot_mod.write_markdown(conn, pid, version, pathlib.Path(ctx.data_dir) / "exports")   # the Markdown export of the new version, rendered from its snapshot only
            except OSError as e:                                                                # the file is a convenience copy of the stored snapshot: a write error is recorded, never a run failure
                ev.emit(conn, rid, "intervention", {"stage": "plan_generate", "message": f"Markdown export not written: {str(e)[:160]}"})
            issues = blocking_issues(consistency_sweep(conn, pid))                          # unresolved decisions are reported in the plan's unresolved checks, not as inconsistencies
            if issues:
                ev.emit(conn, rid, "intervention", {"stage": "plan_generate", "message": f"consistency sweep: {len(issues)} issue(s)", "issues": issues[:10]})
            outcome = {"status": ("partial" if state["stop_reason"] in DRAFT_STOPS else "stopped") if state.get("stop_reason") else "completed", "stop_reason": state.get("stop_reason") or "complete"}   # the canonical run outcome of this node
            with ctx.tx():
                ctx.mark(key, "plan_generate", {"status": "complete", "plan_version": version, "assembled": res.get("assembled", False), "unresolved": len(unresolved), "outcome": outcome, "partial_draft": state.get("stop_reason") if drafting else None})   # the mark records it beside the plan's own token, with the partial draft's stop reason
            ev.step(conn, rid, "plan_generate", f"Plan version {version}" + (" (partial draft)" if drafting else ""), f"{len(plan.components)} component(s), {len(plan.tasks)} task(s), {len(plan.acceptance)} acceptance test(s), {len(unresolved)} unresolved check(s)" + ("; assembled by sections" if res.get("assembled") else "") + (f"; a partial draft after the {state['stop_reason'].replace('_', ' ')} stop, stored without the optional steps (D-A267)" if drafting else ""))
            return {"plan_version": version, **outcome}

    def _closing_stop(e: Exception) -> StopRun:
        """A stop raised by a closing call of the plan (the enrichment, the verification plan, the Editor's pass) as the stop the version
        is stored under: a limit of the run (the money, the time, the model calls) is returned and the plan is stored without that call; anything
        else (an interrupted call, the founder's pause or Stop, the provider account's credit, a failed stage) is raised as it was."""
        s0 = e if isinstance(e, StopRun) else StopRun("call_limit" if isinstance(e, CallLimitRefused) else "budget_reserve_reached", str(e)[:200])
        if s0.reason not in CLOSING_STOPS:
            raise e
        return s0

    def _enrich(key: str, plan: M.PlanOut, reqs: list, decisions: list) -> dict:
        """The planner enrichment call (entities, interfaces, component relations) and the verification plan call, each one
        stage call under the run's limits (a flagged call leaves its section empty with the note); the records are written with the version."""
        out = {"enrichment": None, "verification": None, "notes": []}
        comps = [{"key": c.key, "name": c.name, "responsibility": c.responsibility} for c in plan.components]
        from app.core import verification_plan as VP
        active = VP.decision_inputs([d for d in decisions if d["status"] == "active"], VP.memo_reader(conn, pid))   # each active decision with the memo's recommendation text, its runner-up and the code-derived access mode (app/core/modes.py), the inputs of the enrich and verify_plan prompts
        out["decisions"] = active
        hard_rows = [{"id": r["id"], "key": r["key"], "value": r["value"]} for r in reqs if r["hard"]]
        tasks_in = [{"key": t0.key, "title": t0.title, "effort_hours": [t0.effort_hours_low, t0.effort_hours_high]} for t0 in plan.tasks]
        with coverage.derived(conn, pid, run_id=rid):
            cl_rows = [{"id": c0["id"], "text": c0["text"][:200]} for c0 in ctx.run_claims() if c0["status"] == "supported" and c0["scope_status"] in ("in_scope", "conditional")][:60]
            vdec = [dict(zip(("id", "subject", "status", "memo"), (r0[0], r0[1], r0[2], json.loads(r0[3]) if r0[3] else None))) for r0 in conn.execute("SELECT id, subject, status, memo_json FROM decisions WHERE project_id=? AND status='active' ORDER BY id", (pid,)).fetchall()]
        budget_preview = _budget_of(vdec)
        bp = {"total_usd": budget_preview.get("total_usd"), "ceiling_usd": budget_preview.get("ceiling_usd"), "worst_case_total_usd": budget_preview.get("worst_case_total_usd")} if budget_preview else "none"
        try:                                                                                 # the enrichment closes the run after the comparisons, so its send is judged against the cap as the plan's is (finishing); a limit that refuses it leaves the plan whole
            r1 = call_stage(ctx, "enrich", ctx.key("enrich"), M.EnrichmentOutV2, prompts.messages("enrich", {"components": comps, "active_decisions": active, "confirmed_requirements": reqs, "user_flow": plan.user_flow, "tasks": tasks_in, "budget": bp, "claims": cl_rows}), finishing=True)
        except (StopRun, BudgetExceeded, CallLimitRefused) as e:
            out["stopped"] = _closing_stop(e); out["skipped"] = ["the planner enrichment", "the verification plan"]
            out["notes"].append(f"enrichment not run: {out['stopped'].reason.replace('_', ' ')}")
            return out
        if r1["status"] == "flagged":
            out["notes"].append(f"enrichment flagged: {r1['detail'][:160]}")
        else:
            out["enrichment"] = r1["output"]
            lead = getattr(r1["output"], "lead", None)
            if lead is not None:
                valid_ids = {c0["id"] for c0 in cl_rows}
                out["lead"] = _lead_record(lead.text, [x for x in lead.claim_ids if x in valid_ids])   # stored whole, or to its last whole sentence with the note
                if out["lead"].get("note"):
                    out["notes"].append(out["lead"]["note"])
            out["task_steps"] = {ts.task_key: [{"text": st.text, "hours": st.hours} for st in ts.steps] for ts in getattr(r1["output"], "task_steps", []) or []}
        risks = [{"text": x, "severity": "high" if n < 2 else "medium"} for n, x in enumerate(plan.risks)]   # the plan lists its risks first by weight; the first two are the high ones the verification plan must answer
        try:
            r2 = call_stage(ctx, "verify_plan", ctx.key("verify_plan"), M.VerificationPlanOut, prompts.messages("verify_plan", {"hard_requirements": hard_rows, "risks": risks, "active_decisions": active}), finishing=True)   # a closing call, judged against the cap as the enrichment's send is
        except (StopRun, BudgetExceeded, CallLimitRefused) as e:
            out["stopped"] = _closing_stop(e); out["skipped"] = ["the verification plan"]
            out["notes"].append(f"verification plan not run: {out['stopped'].reason.replace('_', ' ')}")
            r2 = {"status": "flagged", "detail": out["stopped"].reason}
        if r2["status"] == "flagged" and out.get("stopped"):
            pass
        elif r2["status"] == "flagged":
            out["notes"].append(f"verification plan flagged: {r2['detail'][:160]}")
        else:
            out["verification"] = r2["output"]
        ev.step(conn, rid, "plan_generate", "Planner enrichment", f"{len(out['enrichment'].entities) if out['enrichment'] else 0} entities, {len(out['enrichment'].interfaces) if out['enrichment'] else 0} interfaces, {len(out['verification'].checks) if out['verification'] else 0} checks" + ("; " + "; ".join(out["notes"]) if out["notes"] else ""))
        return out

    def _editor(plan: M.PlanOut, extras: dict, memos: list[dict], week: list[str], candidates: list[str], current: list[dict] | None = None) -> dict:
        """The Editor, responsible for the editorial quality of the whole document of
        each family: one call to the strongest route over the whole reader content of the version in reading order (the editor prompt), with the decision
        records, the newest checked date of every cited claim, the candidates and the budget's figures; the hard checks on every rewrite, merge and
        drop (app/core/editor.py); a version whose estimate exceeds the cap runs the pass in two calls (the body fields, then the memos and the
        verification), never silently on a subset. Returns the record stored with the version: rewrites, merged, dropped (applied or not), not_dropped,
        flags (kind, records, correction), assessment, summary, calls (the route, the gateway's reported model, the tokens, the cost) and the
        completeness line. current is the version's complete current decisions (the ones it wrote and the ones it carries from earlier
        versions), the set the whole-stack worst case waits for; the fields and the decision records stay the version's own memos."""
        from app.core import editor as ED
        current = memos if current is None else current
        fields = ED.collect_fields(plan, extras, memos, week)
        rec = dict(ED.new_record(fields, prompts.VERSIONS["editor"]), trimmed=list(extras.get("trimmed") or []))
        if not fields or not bool((ctx.settings.get("extraction") or {}).get("editor", True)):
            rec["call"] = "skipped" if fields else "no fields"; rec["completeness"] = f"not run: {rec['call']}"
            return rec
        decisions = ED.decision_records(memos)
        open_decisions = {d["id"] for d in memos if d.get("status") != "active"}
        recommended = [d["recommendation"] for d in decisions if d.get("recommendation")]
        cited = ED.cited_ids(fields, memos)                                                  # the ids inline and the memos' citations
        claim_values, claim_dates, claim_status = {}, {}, {}
        with coverage.derived(conn, pid, run_id=rid):
            for ch in (cited[i:i + 400] for i in range(0, len(cited), 400)):
                for r0 in conn.execute(f"SELECT id, value, text, checked_at, status FROM claims WHERE project_id=? AND id IN ({','.join('?' * len(ch))})", (pid, *ch)).fetchall():
                    claim_dates[r0[0]] = r0[3]; claim_status[r0[0]] = r0[4]                              # the cited claim's recorded status decides whether its sentence is evidence
                    if r0[4] == "supported":
                        claim_values[r0[0]] = {"value": r0[1] or "", "text": r0[2] or "", "checked_at": r0[3]}   # the date beside the value, for the newer-claim rule
        budget_block = ED.whole_stack(extras.get("budget_preview") or {}, current)                      # the worst case the Editor reads is the one the documents state: none while a decision of the version is open
        approved = {r0[0] for r0 in conn.execute("SELECT d.id FROM decisions d JOIN plan_versions v ON v.project_id=d.project_id AND v.version=d.plan_version WHERE d.project_id=? AND v.approved_at IS NOT NULL", (pid,)).fetchall()}   # a decision an approved version carries is a decision while its record is active; every other memo text is a proposal
        kinds = ED.provenance_of(fields, memos, approved, claim_status); rec["provenance"] = kinds     # the class of every path, read from the records (the approval, the decision's status, the cited claims' statuses), stored with the pass
        record_nums = ED.record_numbers(ED.whole_stack(extras.get("budget_block"), current), extras.get("usage"))                 # the figures the records hold, which a rewrite may add as the context of a figure it keeps
        context = {"decisions": decisions, "claim_dates": claim_dates, "candidates": candidates, "numbers": ED.numbers_with_kinds(budget_block, extras.get("usage")), "coverage": extras.get("coverage") or "none"}
        cap = float((ctx.settings.get("studio") or {}).get("editor_cap_usd", 0.80))
        prices = getattr(ctx.gateway, "prices", {}) or {}
        model = strong_model(ctx.settings, "editor") or ctx.gateway.default_model
        price = prices.get(model) or prices.get(ctx.gateway.default_model) or (1.0, 5.0)
        from app.core.ledger import estimate_model_usd, estimate_tokens
        from app.adapters.model_gateway import output_ceiling, reasoning_allowance
        def estimate(msgs0):
            try:
                return estimate_model_usd(estimate_tokens(msgs0), output_ceiling("editor", ctx.settings) + reasoning_allowance(ctx.settings), *price)
            except ValueError:
                return 0.0
        def messages_for(part_fields):
            return prompts.messages("editor", dict(context, fields=[{"path": k, "text": v, "kind": kinds.get(k, "proposal")} for k, v in part_fields.items()]))   # each field with its provenance class
        whole = messages_for(fields)
        if estimate(whole) <= cap:
            parts = [(ctx.key("editor"), fields, whole)]
        else:                                                                                   # over the cap at the strongest route the pass runs in two calls, each with the assessment context, never silently on a subset
            body = {k: v for k, v in fields.items() if not k.startswith(("memo:", "verification:"))}
            rest = {k: v for k, v in fields.items() if k.startswith(("memo:", "verification:"))}
            parts = [(ctx.key("editor", "body"), body, messages_for(body)), (ctx.key("editor", "memos"), rest, messages_for(rest))]
        ev.step(conn, rid, "editor", "The Editor is reading the version", f"{len(fields)} reader field(s) in {len(parts)} call(s)")
        spent = 0.0                                                                             # the cap is per version; every call after the first is reserved against the cap less what the earlier calls cost (an unknown cost counts as its estimate)
        for key0, part_fields, msgs in parts:
            est = estimate(msgs)
            call = {"key": key0, "fields": sorted(part_fields), "route": stage_model(ctx, "editor"), "estimate_usd": round(est, 6)}
            if est > cap - spent:
                call["call"] = f"skipped: the reservation {est:.4f} USD would pass the Editor's cap of {cap:.4f} USD per version" + (f" ({cap - spent:.4f} USD left after {spent:.4f} USD spent on the earlier call)" if spent else "")
                rec["calls"].append(call); continue
            r0 = call_stage(ctx, "editor", key0, M.EditorOutV2, msgs, finishing=True)        # the Editor's pass closes the run, so its send is judged against the cap as the plan's is, never against the cap less the completion reserve
            row0 = conn.execute("SELECT model_id, tokens_in, tokens_out, reported_usd FROM usage_ledger WHERE run_id=? AND kind='model' AND step_key=? AND state='completed' ORDER BY attempt DESC LIMIT 1", (rid, key0)).fetchone()
            call.update({"reported_model": row0[0] if row0 else None, "tokens_in": row0[1] if row0 else None, "tokens_out": row0[2] if row0 else None, "cost_usd": r0.get("cost_usd") if r0.get("cost_usd") is not None else (row0[3] if row0 else None)})
            spent += float(call["cost_usd"]) if call.get("cost_usd") is not None else est
            if r0["status"] == "flagged":
                call["call"] = f"flagged: {r0.get('detail', '')[:200]}"; rec["calls"].append(call); continue
            call["call"] = "ok"; rec["calls"].append(call)
            ED.absorb(r0["output"], fields, candidates, recommended, claim_values, open_decisions, rec, record_numbers=record_nums)   # the hard checks on every rewrite, merge, drop, flag and summary statement (shared with the maintenance entry); a figure the records hold may join as context
        rec["completeness"] = ED.completeness_line(rec["calls"])
        rec["call"] = "ok" if all(c0.get("call") == "ok" for c0 in rec["calls"]) else "; ".join(c0.get("call") or "" for c0 in rec["calls"])
        rec["cost_usd"] = round(sum(float(c0.get("cost_usd") or 0) for c0 in rec["calls"]), 6)
        applied_drops = [d0 for d0 in rec["dropped"] if d0.get("applied")]
        ev.step(conn, rid, "editor", "The Editor's pass", f"{len(rec['rewrites'])} field(s) rewritten, {len(rec['merged'])} merge(s), {len(applied_drops)} field(s) left out, {len(rec['not_dropped'])} kept against the Editor's drop, {len(rec['flags'])} flag(s), {len(rec['dropped']) - len(applied_drops)} item(s) refused by the checks; {rec['completeness']}")
        return rec

    def _budget_of(version_decisions: list[dict], version: int | None = None) -> dict | None:
        """The budget of the version from the active decisions' memos (the pick's and the runner-up's price claims among the subject's
        supported in-scope claims) and the four usage numbers; None when the usage or the picks are missing. With a version (the plan node): the carried
        decisions too, priced from their signed price claims with the date each was read."""
        row = conn.execute("SELECT answer_json, usage_json FROM questions WHERE project_id=? AND kind='usage' AND status='answered' ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
        numbers = (json.loads(row[1]) if row and row[1] else (derived.parse_usage_numbers(json.loads(row[0])) if row and row[0] else None))
        if not numbers:
            return None
        picks = []
        def _claim_row(r0) -> dict:
            """A price claim's record for the budget rules, its recorded conditions included (the billing model and the buyer-condition rule read them)."""
            d1 = dict(zip(("id", "text", "attribute", "value", "product_id", "product_version", "kind", "subject", "status", "scope_status", "conditions_json"), r0))
            d1["conditions"] = json.loads(d1.pop("conditions_json") or "[]") if d1.get("conditions_json") else []
            return d1
        def _pick_of(subject: str, memo: dict, cl: list[dict], carried: bool = False) -> dict:
            rec = memo.get("recommendation") or {}
            mine = [c0 for c0 in cl if coverage.same_product(rec["product_id"], rec.get("product_version"), c0["product_id"], c0["product_version"])]
            judged = memo.get("budget") if isinstance(memo.get("budget"), dict) and isinstance((memo.get("budget") or {}).get("input_claim_ids"), list) else None
            missing = []
            if judged:                                                                    # a pick whose hard budget was judged by calculation is priced from the judgement's own claims with the judgement's own words: one selection, one figure
                have = {c0["id"] for c0 in cl}
                missing = [x for x in judged["input_claim_ids"] if x not in have]
                mine = [c0 for c0 in cl if c0["id"] in set(judged["input_claim_ids"])]
            ru = memo.get("runner_up") or {}
            theirs = [c0 for c0 in cl if ru.get("product_id") and coverage.same_product(ru["product_id"], ru.get("product_version"), c0["product_id"], c0["product_version"])]
            return {"item": AP.norm(subject).replace("_", " "), "provider": rec.get("option") or rec["product_id"], "product_version": rec.get("product_version") or "", "claims": mine, "runner_up": ({"provider": ru.get("option") or ru["product_id"], "claims": theirs} if theirs else None), "carried": carried,
                    **({"provider": judged.get("option") or rec.get("option") or rec["product_id"], "names": judged.get("names"), "missing": missing, "judged": judged} if judged else {})}
        for d in version_decisions:
            memo = d.get("memo") or {}
            rec = memo.get("recommendation") or {}
            if d.get("status") != "active" or not rec.get("product_id"):
                continue
            with coverage.derived(conn, pid, run_id=rid):                                     # claim status is derived: read inside the accessor's reads context
                rows_c = conn.execute("SELECT id, text, attribute, value, product_id, product_version, kind, subject, status, scope_status, conditions_json FROM claims WHERE project_id=? AND kind='price' AND status='supported' AND scope_status IN ('in_scope','conditional') AND decision_subject=?", (pid, d["subject"])).fetchall()
            picks.append(_pick_of(d["subject"], memo, [_claim_row(r0) for r0 in rows_c]))
        if version is not None:
            # the carried decisions: the active decision of every subject without a row at this version (the latest per subject),
            # priced from their signed price claims. Each carried decision takes the price set of the newest earlier signed version that holds a claim about its pick
            # (the immediately previous version's manifest may omit the subject), never a union of every supported price;
            # of that set, the claims about the pick that are still supported in scope. They are not fetched in this run, and each claim keeps the date it was read
            own_subjects = sorted({d["subject"] for d in version_decisions})
            signed_sets = [_signed_price_set(json.loads(bj)) for (bj,) in conn.execute("SELECT budget_json FROM plan_versions WHERE project_id=? AND version<? AND snapshot_sig IS NOT NULL ORDER BY version DESC", (pid, version)).fetchall() if bj]
            with coverage.derived(conn, pid, run_id=rid):
                carried_rows = conn.execute(f"SELECT id, subject, memo_json FROM decisions WHERE project_id=? AND status='active' AND plan_version<?{' AND subject NOT IN (' + ','.join('?' * len(own_subjects)) + ')' if own_subjects else ''} ORDER BY subject, plan_version DESC, CAST(substr(id, instr(id, '.DEC-') + 5) AS INTEGER) DESC", (pid, version, *own_subjects)).fetchall()
                signed_claims_by_set = []
                for price_set in signed_sets:
                    rows_s = conn.execute(f"SELECT id, text, attribute, value, product_id, product_version, kind, subject, status, scope_status, conditions_json FROM claims WHERE project_id=? AND kind='price' AND status='supported' AND scope_status IN ('in_scope','conditional') AND id IN ({','.join('?' * len(price_set)) or 'NULL'})", (pid, *price_set)).fetchall() if price_set else []
                    signed_claims_by_set.append([dict(_claim_row(r0), accessed=price_set.get(r0[0])) for r0 in rows_s])
            seen_s = set()
            for _did, subject, memo_json in carried_rows:
                memo = json.loads(memo_json) if memo_json else {}
                rec_c = memo.get("recommendation") or {}
                if subject in seen_s or not rec_c.get("product_id"):
                    continue
                seen_s.add(subject)
                signed_claims = next((cs for cs in signed_claims_by_set if any(coverage.same_product(rec_c["product_id"], rec_c.get("product_version"), c0["product_id"], c0["product_version"]) for c0 in cs)), [])   # the newest signed set that priced this pick; none: the pick has no signed price (its row says so for a token-priced item)
                picks.append(_pick_of(subject, memo, [dict(c0) for c0 in signed_claims], carried=True))
        if not picks:
            return None
        # every price used in the budget is fetched in this run: a price claim whose source was fetched before the run is re-fetched live once
        # (the read tool, maxAgeHours 0, inside the run's page limit); a claim whose quote the fresh copy no longer carries is left out with the reason
        run_started = (conn.execute("SELECT started_at FROM runs WHERE id=?", (rid,)).fetchone() or [None])[0]
        refreshed, stale_out = [], []
        usage_only = bool(conn.execute("SELECT 1 FROM run_events WHERE run_id=? AND event='step' AND data_json LIKE ? LIMIT 1", (rid, '%"label": "Usage corrected"%')).fetchone())   # a usage correction re-fetches no price page; the prices are the verified ones in the store, each with the date it was read
        if run_started and not usage_only and not getattr(ctx.research, "simulated", False):
            prunner = ToolRunner(conn, ctx.ledger, rid, pid, "research_plan", ctx.research, worker="price", branch=ctx.branch, stage_limit=int(ctx.settings["tools"]["stage_limit"]),
                                 run_limits={"searches": int(ctx.settings["budget"]["max_searches"]), "pages": int(ctx.settings["budget"]["max_pages"])}, data_dir=ctx.data_dir, settings=ctx.settings, allowed=roles.WORKER_TOOLS)
            for pk in picks:
                if pk.get("carried"):
                    continue                                                                  # a carried pick's prices are its signed price claims, not fetched in this run
                for group in ([pk["claims"]] + ([pk["runner_up"]["claims"]] if pk.get("runner_up") else [])):
                    for c0 in list(group):
                        src = conn.execute("SELECT id, url, retrieved_at, content_hash FROM sources WHERE id=(SELECT source_id FROM claims WHERE id=?)", (c0["id"],)).fetchone()
                        if not src or (src[2] or "") >= run_started:
                            continue
                        try:
                            obs = prunner.handle({"tool": "read", "args": {"url": src[1], "freshness": "volatile"}})
                            refreshed.append(src[0])
                            ev.tool(conn, rid, obs["tool_call_id"], "read", "completed", f"price page of {src[0]} re-fetched for the budget", engine="exa", query=src[1][:120])
                        except Exception as e:  # noqa: BLE001
                            stale_out.append((c0["id"], f"not re-fetched in this run ({type(e).__name__})")); group.remove(c0); continue
                        valid = {row["id"] for row in coverage.claim_rows(conn, pid, [c0["id"]], ctx.requirement_version()) if row["valid"]}
                        if c0["id"] not in valid:
                            stale_out.append((c0["id"], "the fresh copy of its page no longer carries its quote")); group.remove(c0)
        # the signed budget carries the exact price set it rests on: every eligible claim with its source hash and access date and whether
        # that source was fetched in this run; the excluded claims with the reason; fetched_this_run derived from the rows actually used, never a constant.
        # every own claim carries the date its source was read as it stands after the re-fetch (the row shows it); a carried claim keeps its signed date and is not fetched
        eligible, seen_e = [], set()
        for pk in picks:
            for group in ([pk["claims"]] + ([pk["runner_up"]["claims"]] if pk.get("runner_up") else [])):
                for c0 in group:
                    src = conn.execute("SELECT id, content_hash, retrieved_at FROM sources WHERE id=(SELECT source_id FROM claims WHERE id=?)", (c0["id"],)).fetchone()
                    if not pk.get("carried") or not c0.get("accessed"):
                        c0["accessed"] = src[2] if src else None                            # an older signed block names no date: the source's own read date
                    if c0["id"] in seen_e:
                        continue
                    seen_e.add(c0["id"])
                    fetched = (not pk.get("carried")) and bool(src and run_started and ((src[2] or "") >= run_started or src[0] in refreshed))
                    eligible.append({"claim_id": c0["id"], "source_id": src[0] if src else None, "source_hash": src[1] if src else None, "accessed": c0.get("accessed"), "fetched_this_run": fetched})
        budget_req = derived.find_budget(ctx.requirements(confirmed_only=True))
        for pk in picks:                                                                  # a judged pick whose claims changed after its judgement (a price page re-fetched, a claim no longer supported) keeps one figure: the same figure on the claims that remain, or no figure with the reason
            judged = pk.pop("judged", None)
            if judged is None:
                continue
            have = {c0["id"] for c0 in pk["claims"]}
            gone = [x for x in judged["input_claim_ids"] if x not in have]
            if gone and "monthly_usd" in judged:
                again = BG.option_judgement(pk["item"], pk["provider"], pk.get("product_version") or "", pk["claims"], numbers, (budget_req or {}).get("ceiling_usd"), names=pk.get("names"))
                gone = [] if again.get("monthly_usd") == judged.get("monthly_usd") and again.get("state") == judged.get("state") else gone
            pk["missing"] = gone
        out = BG.budget_of(numbers, picks, (budget_req or {}).get("ceiling_usd"))
        if out is not None:
            used = {c0 for r0 in out["rows"] for c0 in r0["claims"]}
            out["eligible"] = eligible
            out["excluded"] = [{"claim_id": cid, "reason": why} for cid, why in stale_out]
            out["fetched_this_run"] = bool(used) and all(e["fetched_this_run"] for e in eligible if e["claim_id"] in used)
            out["refreshed_sources"] = refreshed
            if stale_out:
                out["note"] = (out.get("note") or "") + (" " if out.get("note") else "") + "Left out: " + "; ".join(f"{cid} ({why})" for cid, why in stale_out)
        return out

    def _sweep_unsigned(versions: list[int]) -> None:
        """Deletes the version-bound records and the row of a version this run wrote without its signed snapshot; a signed row is never touched."""
        with ctx.tx():
            for v0 in versions:
                if conn.execute("SELECT 1 FROM plan_versions WHERE project_id=? AND version=? AND snapshot_sig IS NULL", (pid, v0)).fetchone() is None:
                    continue
                for t0 in ("entities", "interfaces", "component_relations", "verification_checks"):
                    conn.execute(f"DELETE FROM {t0} WHERE project_id=? AND plan_version=?", (pid, v0))
                conn.execute("DELETE FROM plan_versions WHERE project_id=? AND version=? AND snapshot_sig IS NULL", (pid, v0))

    def _store_plan(plan: M.PlanOut, unresolved: list[str], assembled: bool, state: RunState | None = None, extras: dict | None = None, partial: dict | None = None) -> int:
        """The version row and its snapshot stand or fall together. The row is committed before the budget's fetch and the Editor's send
        (both commit ledger rows of their own), so a store that does not reach the signing, whatever raised, removes the row it wrote
        before the exception goes on: no run ends with a version row that has no snapshot. Only a process that dies between the two
        transactions leaves one, and the resumed node sweeps it."""
        written: list[int] = []
        try:
            return _write_plan(plan, unresolved, assembled, state, extras, partial, written)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            try:
                _sweep_unsigned(written)
            except Exception as e2:                                                          # noqa: BLE001 (the sweep must not replace the exception on its way out; the gap is recorded)
                ev.emit(conn, rid, "intervention", {"stage": "plan_generate", "message": f"version row not removed after the store stopped before signing: {type(e2).__name__}"})
            raise

    def _write_plan(plan: M.PlanOut, unresolved: list[str], assembled: bool, state: RunState | None, extras: dict | None, partial: dict | None, written: list) -> int:
        """partial: {"stop_reason", "skipped"} when the version is the one permitted draft after a cap or time stop: the Editor's send is
        skipped (no further send depends on the version) and the row's diff_json carries partial_draft, which the versions screen, the exports
        and the Markdown route read; None for a regular version."""
        run_started = (conn.execute("SELECT started_at FROM runs WHERE id=?", (rid,)).fetchone() or [None])[0]
        half = [r0[0] for r0 in conn.execute("SELECT version FROM plan_versions WHERE project_id=? AND snapshot_sig IS NULL AND created_at >= ? ORDER BY version", (pid, run_started or "")).fetchall()]   # this run's own rows only (written since it started); a row another run left without its snapshot is that run's record and stays
        if half:                                                                              # a version row without its signed snapshot is an earlier attempt of this node that stopped mid-way (the Editor's send and the budget's fetch commit their ledger rows, so the row survived); its version-bound records go and the number is written again
            _sweep_unsigned(half)
            ev.step(conn, rid, "plan_generate", "Half-written version swept", f"version {', '.join(map(str, half))}: no signed snapshot (an earlier attempt stopped before signing); written again under the same number")
        repo = ctx.repo; state = state or {}; extras = extras or {}
        with ctx.tx(), coverage.derived(conn, pid, run_id=rid):                                # the active-decision identities read below are derived state (reads context)
            version = snapshot_mod.next_version(conn, pid, rid)                                  # the half-written row of an earlier attempt of this run was swept above; the number comes above a row another run left without its snapshot
            previous = conn.execute("SELECT MAX(version) FROM plan_versions WHERE project_id=? AND snapshot_sig IS NOT NULL AND version<?", (pid, version)).fetchone()[0]   # the version before is the latest signed one
            acc_ids = []
            dec_ids = {r[0] for r in conn.execute("SELECT id FROM decisions WHERE project_id=?", (pid,))}
            req_ids = {r[0] for r in conn.execute("SELECT id FROM requirements WHERE project_id=?", (pid,))}
            comp_ids, task_ids = {}, {}
            dec_identity = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, product_id, product_version FROM decisions WHERE project_id=? AND status='active'", (pid,)).fetchall()}
            for c in plan.components:
                cid = repo.next_id(pid, "CMP"); comp_ids[c.key] = cid
                repo.upsert_component(M.Component(id=cid, project_id=pid, name=c.name[:200], responsibility=c.responsibility[:600], data_leaves_device=c.data_leaves_device))
                stored_under = next((dec_identity[d] for d in c.decision_ids if d in dec_identity), (None, None))
                conn.execute("UPDATE components SET product_id=?, product_version=? WHERE id=?", (stored_under[0], stored_under[1], cid))   # components are stored under the canonical identity of their decision
                for d in c.decision_ids:
                    if d in dec_ids:
                        repo.add_trace_edge(pid, d, cid, "decision_component")
            for t in plan.tasks:
                tid = repo.next_id(pid, "TSK"); task_ids[t.key] = tid
            for t in plan.tasks:
                repo.upsert_task(M.Task(id=task_ids[t.key], project_id=pid, component_id=comp_ids[t.component_key], title=t.title[:300], depends_on=[task_ids[d] for d in t.depends_on if d in task_ids],
                                        effort_low_h=t.effort_hours_low, effort_high_h=t.effort_hours_high))
                repo.add_trace_edge(pid, comp_ids[t.component_key], task_ids[t.key], "component_task")
            for a in plan.acceptance:
                aid = repo.next_id(pid, "ACC"); req = a.requirement_id if a.requirement_id in req_ids else None; acc_ids.append(aid)
                repo.upsert_acceptance_test(M.AcceptanceTest(id=aid, project_id=pid, task_id=task_ids.get(a.task_key), requirement_id=req, criterion=a.criterion[:600]))
                if task_ids.get(a.task_key):
                    repo.add_trace_edge(pid, task_ids[a.task_key], aid, "task_test")
                if req:
                    repo.add_trace_edge(pid, req, aid, "req_test")
                elif a.requirement_id:
                    unresolved.append(f"acceptance test {aid} names a requirement that does not exist in this project; the test is stored without a requirement")   # the plan's own text never writes an id the store does not hold (the model's reference is not a record)
            from app.core import modes as MODES                                             # the generator reads the pick's mode from the decision record; a criterion testing the other mode is an unresolved line, never rewritten
            pick_labels = {r0[0]: ((json.loads(r0[1]) if r0[1] else {}).get("recommendation") or {}).get("option") or "" for r0 in conn.execute("SELECT id, memo_json FROM decisions WHERE project_id=? AND status='active'", (pid,)).fetchall()}
            unresolved += MODES.acceptance_mode_notes([{"id": aid0, "task_id": task_ids.get(a0.task_key), "criterion": a0.criterion} for aid0, a0 in zip(acc_ids, plan.acceptance)],
                                                      {tid0: comp_ids.get(next((t0.component_key for t0 in plan.tasks if task_ids.get(t0.key) == tid0), None)) for tid0 in task_ids.values()},
                                                      {comp_ids[c0.key]: [d0 for d0 in c0.decision_ids if d0 in dec_ids] for c0 in plan.components}, pick_labels)
            diff = {"problem": plan.problem, "users": plan.users, "scope_in": plan.scope_in, "scope_out": plan.scope_out, "user_flow": plan.user_flow, "risks": plan.risks, "unresolved_checks": unresolved}
            if plan.user_flow_labels:
                diff["user_flow_labels"] = list(plan.user_flow_labels)                            # the steps' labels, stored only where the plan holds them
            summary = "initial plan" + (" (assembled by sections)" if assembled else "")
            if state.get("change"):                                                              # a revision's version carries the model's change summary and the decision diff against the previous version
                change = dict(state["change"]); change.pop("reverify", None); change.pop("reverify_cited", None); stale = list(state.get("stale") or [])   # the diff records the requirement change; the reverify ids stay in the state and the admission record
                redone = [{"stale": r[0], "subject": r[1], "new": r[2]} for r in conn.execute("SELECT s.id, s.subject, n.id FROM decisions s JOIN decisions n ON n.project_id=s.project_id AND n.subject=s.subject AND n.plan_version=? WHERE s.project_id=? AND s.id IN (%s) ORDER BY s.id" % (",".join("?" * len(stale)) or "NULL"), (version, pid, *stale))]
                unchanged = [r[0] for r in conn.execute("SELECT id FROM decisions WHERE project_id=? AND plan_version<? AND status='active' ORDER BY id", (pid, version))]
                diff["revision"] = {"requirement": change, "previous_version": int(previous) if previous is not None else version - 1, "decisions_redone": redone, "decisions_unchanged": unchanged, "stale_decisions": stale}
                summary = (state.get("change_summary") or f"the requirement {change.get('requirement_key')} changed")[:600] + (" (assembled by sections)" if assembled else "")
            repo.upsert_plan_version(M.PlanVersion(project_id=pid, version=version, requirement_version=ctx.requirement_version(), change_summary=summary, diff=diff))
            # the enrichment records, the verification plan, the first week and the budget of this version, before the snapshot is built
            enrich_out = extras.get("enrichment"); ver_out = extras.get("verification")
            comp_dec = {cid: [d for d in c.decision_ids if d in dec_ids] for c in plan.components for cid in [comp_ids[c.key]]}
            if enrich_out is not None:
                for e in enrich_out.entities[:8]:
                    eid = f"{pid}.ENT-{next_number(conn, 'entities', pid, 'ENT'):03d}"
                    conn.execute("INSERT INTO entities (id, project_id, plan_version, name, fields_json, relations_json) VALUES (?,?,?,?,?,?)", (eid, pid, version, e.name[:60], json.dumps([f.model_dump() for f in e.fields]), json.dumps([r0.model_dump() for r0 in e.relations])))
                for itf in enrich_out.interfaces[:8]:
                    fk, tk = comp_ids.get(itf.from_key), comp_ids.get(itf.to_key)
                    if not fk or not tk:
                        continue                                                               # an interface naming an unknown component key is dropped
                    iid = f"{pid}.IFC-{next_number(conn, 'interfaces', pid, 'IFC'):03d}"
                    steps = [{"order": s0.order, "from": comp_ids.get(s0.from_key, s0.from_key), "to": comp_ids.get(s0.to_key, s0.to_key), "message": s0.message, "condition": s0.condition} for s0 in itf.steps]
                    conn.execute("INSERT INTO interfaces (id, project_id, plan_version, name, from_key, to_key, direction, kind, description, steps_json) VALUES (?,?,?,?,?,?,?,?,?,?)", (iid, pid, version, itf.name[:80], fk, tk, itf.direction[:20], itf.kind[:40], itf.description[:400], json.dumps(steps)))
                for rel in enrich_out.component_relations[:12]:
                    fk, tk = comp_ids.get(rel.from_key), comp_ids.get(rel.to_key)
                    dids = list(dict.fromkeys((comp_dec.get(fk) or []) + (comp_dec.get(tk) or []))) if fk and tk else []
                    if not fk or not tk or not dids:
                        continue                                                               # an edge always has a supporting record (the components' decisions); none, no edge
                    rid_ = f"{pid}.REL-{next_number(conn, 'component_relations', pid, 'REL'):03d}"
                    conn.execute("INSERT INTO component_relations (id, project_id, plan_version, from_key, to_key, kind, label, decision_ids_json) VALUES (?,?,?,?,?,?,?,?)", (rid_, pid, version, fk, tk, rel.kind[:40], rel.label[:120], json.dumps(dids)))
            if ver_out is not None:
                from app.core import verification_plan as VP
                hard_ids = {r["id"] for r in ctx.requirements(confirmed_only=True) if r["hard"]}
                rows_v, notes_v = VP.check_rows(ver_out.checks, hard_ids, list(plan.risks), extras.get("decisions") or [])   # a check against a mode no decision chose is dropped with its note; a text over its limit, or at it without a sentence end, is dropped with its note, never cut
                for chk in rows_v:
                    vid = f"{pid}.VCK-{next_number(conn, 'verification_checks', pid, 'VCK'):03d}"
                    conn.execute("INSERT INTO verification_checks (id, project_id, plan_version, check_text, how, pass_text, cost, requirement_id, risk_text) VALUES (?,?,?,?,?,?,?,?,?)", (vid, pid, version, chk["check"], chk["how"], chk["pass"], chk["cost"], chk["requirement_id"], chk["risk"]))
                    extras.setdefault("verification_ids", []).append({"id": vid, "check": chk["check"], "how": chk["how"], "pass": chk["pass"]})
                if notes_v:
                    extras.setdefault("notes", []).extend(notes_v); diff["verification_notes"] = list(notes_v)
                    ev.step(conn, rid, "plan_generate", "Verification checks dropped", "; ".join(notes_v))
            ladder = [{"id": task_ids[t0.key], "title": t0.title[:300], "depends_on": [task_ids[d] for d in t0.depends_on if d in task_ids], "effort_low": t0.effort_hours_low, "effort_high": t0.effort_hours_high} for t0 in plan.tasks]
            steps_by_task = {task_ids[k]: v for k, v in (extras.get("task_steps") or {}).items() if k in task_ids}
            week = first_week_from_steps(ladder, steps_by_task) if steps_by_task else first_week(ladder)   # the days from the sub-steps
        written.append(version)                                                              # the row is committed; from here to the signing, an exception removes it (_store_plan)
        with coverage.derived(conn, pid, run_id=rid):                                     # the decisions' status is derived: read inside the accessor's reads context, outside the version's first transaction
            version_decisions = [dict(zip(("id", "subject", "status", "memo"), (r0[0], r0[1], r0[2], json.loads(r0[3]) if r0[3] else None))) for r0 in conn.execute("SELECT id, subject, status, memo_json FROM decisions WHERE project_id=? AND plan_version=? ORDER BY id", (pid, version)).fetchall()]
        budget_block = _budget_of(version_decisions, version)                             # may re-fetch a price page: its ledger rows commit on their own, so never inside the version's transaction; with the version, the carried decisions' rows too
        from app.core import editor as ED
        usage_now = (lambda row0: (json.loads(row0[1]) if row0 and row0[1] else None))(conn.execute("SELECT answer_json, usage_json FROM questions WHERE project_id=? AND kind='usage' AND status='answered' ORDER BY id DESC LIMIT 1", (pid,)).fetchone())
        with coverage.derived(conn, pid, run_id=rid):
            price_rows = [dict(dict(zip(("id", "text", "attribute", "value", "product_id", "product_version", "kind", "conditions_json"), r0)), conditions=json.loads(r0[7] or "[]") if r0[7] else []) for r0 in conn.execute("SELECT id, text, attribute, value, product_id, product_version, kind, conditions_json FROM claims WHERE project_id=? AND kind='price' AND status='supported' AND scope_status IN ('in_scope','conditional')", (pid,)).fetchall()]
        memo_changed: set[str] = set(); trims: list[dict] = []
        subject_class = {r0[0]: r0[1] for r0 in conn.execute("SELECT subject, subject_class FROM candidates WHERE project_id=? AND status='kept' ORDER BY created_at", (pid,)).fetchall()}   # the subject's typed class bounds the tariffs an option cost may use
        from app.core import textlimit as TL
        for d0 in version_decisions:                                                     # the per-option monthly cost from the option's price claims at the usage (code, never the model); duplicates by meaning out of every memo list
            if not d0.get("memo"):
                continue
            if ED.memo_monthly_costs(d0["memo"], price_rows, usage_now, service_class=subject_class.get(d0["subject"]) or d0["subject"]) | ED.dedupe_memo(d0["memo"]):
                memo_changed.add(d0["id"])                                                    # written in the second transaction below, after the Editor read the memo
            for i0, o0 in enumerate(d0["memo"].get("options") or []):                       # a memo cell the provider cut at the contract's limit (fit 300, price 200, risk 300) is kept to its last sentence end with the note, never printed cut
                for k0, lim in TL.OPTION_CELLS:                                             # the monthly cost sentence too; each cell is kept to a sentence end or a clause end, never to a dangling word
                    if k0 == "monthly_cost" and isinstance(o0.get("monthly_cost_calc"), dict):
                        continue                                                             # a figure code computed (memo_monthly_costs) is no sentence
                    t0, note0 = TL.to_statement_end(str(o0.get(k0) or ""), lim, f"memo:{d0['id']}:option[{i0}].{k0}")
                    if note0:
                        o0[k0] = t0; memo_changed.add(d0["id"]); trims.append({"path": f"memo:{d0['id']}:option[{i0}].{k0}", "note": note0})
        plan_risks = ED.dedupe_by_meaning(list(plan.risks))                             # the risks by meaning; the unresolved checks are the coverage report's lines, kept as recorded
        candidates_known = sorted({r0[0] for r0 in conn.execute("SELECT label FROM candidates WHERE project_id=?", (pid,))} | {o.get("option", "") for d0 in version_decisions for o in ((d0.get("memo") or {}).get("options") or [])} | {(d0.get("memo") or {}).get("recommendation", {}).get("option", "") for d0 in version_decisions if (d0.get("memo") or {}).get("recommendation")})
        extras["budget_preview"] = {k: budget_block.get(k) for k in ("total_usd", "ceiling_usd", "worst_case_total_usd")} if budget_block else {}
        extras.update({"requirements": [{"id": r0["id"], "key": r0["key"], "value": r0["value"]} for r0 in ctx.requirements(confirmed_only=True)],   # the whole reader content, in reading order
                       "acceptance": [{"id": aid0, "criterion": a0.criterion[:600]} for aid0, a0 in zip(acc_ids, plan.acceptance)],
                       "tasks": [{"id": task_ids[t0.key], "title": t0.title[:300]} for t0 in plan.tasks], "components": [{"id": comp_ids[c0.key], "responsibility": c0.responsibility[:600]} for c0 in plan.components],
                       "budget_assumptions": list((budget_block or {}).get("assumptions") or []), "open_items": list(unresolved), "trimmed": trims,
                       "budget_block": budget_block, "usage": usage_now})                                         # the budget's rows and figures and the founder's usage, the record numbers of the Editor's checks
        lead = extras.get("lead")
        if partial:
            editor_rec = {"rewrites": {}, "merged": [], "flags": [], "dropped": [], "fields": 0, "call": f"skipped: the partial draft after the {partial['stop_reason'].replace('_', ' ')} stop makes no further send (D-A267)", "completeness": "not run: the partial draft makes no further send (D-A267)", "trimmed": trims}
        else:
            try:
                with coverage.derived(conn, pid, run_id=rid):                                 # the decisions the version carries from earlier versions (the status is derived); with the version's own they are its complete current set, the one the document model reads (current_ids)
                    carried_decisions = [dict(zip(("id", "subject", "status", "memo"), (r0[0], r0[1], r0[2], json.loads(r0[3]) if r0[3] else None))) for r0 in conn.execute("SELECT id, subject, status, memo_json FROM decisions WHERE project_id=? AND plan_version<? ORDER BY id", (pid, version)).fetchall()]
                editor_rec = _editor(plan, dict(extras, risks=plan_risks), version_decisions, week, [c0 for c0 in candidates_known if c0], current=carried_decisions + version_decisions)   # the Editor over the reader fields, before the snapshot; its send outside the version's transaction
            except (StopRun, BudgetExceeded, CallLimitRefused) as e:                           # the Editor may only change wording, so a version without its pass is a valid version: a send the ledger refused, or one that stopped, leaves the plan as the writer, the enrichment and the check left it, and it is signed below
                s0 = e if isinstance(e, StopRun) else StopRun("call_limit" if isinstance(e, CallLimitRefused) else "budget_reserve_reached", str(e)[:200])
                if s0.reason == "unknown_call" or (s0.reason == "run_stopped" and _paused()):
                    raise                                                                    # an interrupted call or the founder's pause holds the run, it does not end it: the row is removed (_store_plan) and the resumed node writes the version again
                _editor_stop[0] = s0
                ev.step(conn, rid, "editor", "The Editor's pass", "not run: " + W.editor_not_run(s0.reason))
                editor_rec = {"rewrites": {}, "merged": [], "flags": [], "dropped": [], "fields": 0, "call": f"skipped: {s0.reason}: {s0.detail}"[:300], "completeness": "not run: " + W.editor_not_run(s0.reason), "trimmed": trims}
        if _editor_stop[0] is None and editor_rec.get("calls") and all(str(c0.get("call") or "").startswith("flagged") for c0 in editor_rec["calls"]):
            _editor_stop[0] = StopRun("editor_failed", str(editor_rec.get("call") or "")[:200])   # a pass whose every call failed did not run either; the version is stored without it and the run says so
        with ctx.tx(), coverage.derived(conn, pid, run_id=rid):                                # the second transaction: the Editor's result poured back, the version's records and the signed snapshot
            applied = ED.apply(editor_rec, plan, version_decisions, week, lead, plan_risks, verification=extras.get("verification_ids"))
            week = applied["week"]; lead = applied["lead"]; plan_risks = applied["risks"]
            for d0 in version_decisions:
                if d0.get("memo") and (d0["id"] in applied["memos_changed"] or d0["id"] in memo_changed):
                    conn.execute("UPDATE decisions SET memo_json=? WHERE id=?", (json.dumps(d0["memo"]), d0["id"]))
                    rep0 = {x.get("was"): x.get("now") for x in (d0["memo"].get("replaced_sentences") or []) if x.get("was")}   # the decision's unresolved list carries the same replacement as the memo's open list
                    if rep0:
                        ur0 = conn.execute("SELECT unresolved_json FROM decisions WHERE id=?", (d0["id"],)).fetchone()
                        ul0 = json.loads(ur0[0]) if ur0 and ur0[0] else []
                        if any(x in rep0 for x in ul0):
                            conn.execute("UPDATE decisions SET unresolved_json=? WHERE id=?", (json.dumps([rep0.get(x, x) for x in ul0]), d0["id"]))
            for vid, texts in applied["verification"].items():
                conn.execute("UPDATE verification_checks SET check_text=COALESCE(?, check_text), how=COALESCE(?, how), pass_text=COALESCE(?, pass_text) WHERE id=?", (texts.get("check"), texts.get("how"), texts.get("pass"), vid))
            for aid0, text0 in (applied.get("acceptance") or {}).items():                    # the Editor's rewrites of the reader-facing draft content beyond the plan body, under rewrite_ok
                conn.execute("UPDATE acceptance_tests SET criterion=? WHERE id=? AND project_id=?", (text0, aid0, pid))
            for tid0, text0 in (applied.get("tasks") or {}).items():
                conn.execute("UPDATE tasks SET title=? WHERE id=? AND project_id=?", (text0, tid0, pid))
            for cid0, text0 in (applied.get("components") or {}).items():
                conn.execute("UPDATE components SET responsibility=? WHERE id=? AND project_id=?", (text0, cid0, pid))
            diff.update({"problem": applied["problem"], "users": applied["users"], "scope_in": applied["scope_in"], "scope_out": applied["scope_out"], "user_flow": applied["user_flow"], "risks": plan_risks, "unresolved_checks": unresolved})
            if "user_flow_labels" in diff:                                                         # a step the Editor dropped takes its label with it
                kept = M.flow_labels(applied.get("user_flow_labels") or [], applied["user_flow"])
                diff.pop("user_flow_labels") if not kept else diff.update(user_flow_labels=kept)
            over = BG.ceiling_issue(budget_block)                                               # a total above the founder's ceiling is an open issue of the plan, naming the rows, never a figure beside the ceiling without comment
            if over and over not in (diff.get("unresolved_checks") or []):
                diff["unresolved_checks"] = list(diff.get("unresolved_checks") or []) + [over]
            if partial:
                diff["partial_draft"] = {"stop_reason": partial["stop_reason"], "skipped": list(partial["skipped"])}   # the explicit mark of the partial draft on the version row
            conn.execute("UPDATE plan_versions SET diff_json=? WHERE project_id=? AND version=?", (json.dumps(diff), pid, version))
            conn.execute("UPDATE plan_versions SET first_week_json=?, budget_json=?, lead_json=?, editor_json=? WHERE project_id=? AND version=?",
                         (json.dumps(week), json.dumps(budget_block) if budget_block else None, json.dumps(lead) if lead else None, json.dumps(editor_rec), pid, version))
            snap = snapshot_mod.build(conn, pid, version, run_id=rid, component_ids=list(comp_ids.values()), task_ids=list(task_ids.values()), test_ids=acc_ids, spend=ctx.ledger.spend_report())   # the signed export snapshot, validated, inside this transaction
            repo.set_snapshot(pid, version, snapshot_mod.canonical(snap), snapshot_mod.signature(snap))
        return version

    # ---- routers (deterministic)
    def after_clarify(state: RunState) -> str:
        if state.get("stop_reason"):
            return _route(state, END)                                                        # a budget or time stop at clarify still gets its one draft; every other stop ends the run
        return "ask" if state.get("questions_open", 0) > 0 else "research_plan"

    def after_research(state: RunState) -> str:
        return _route(state, "extract_evidence")

    def after_extract(state: RunState) -> str:
        return _route(state, "verify")

    def after_verify(state: RunState) -> str:
        return _route(state, "compare_options")

    def after_compare(state: RunState) -> str:
        return _route(state, "plan_generate")

    def _route(state: RunState, next_node: str) -> str:
        reason = state.get("stop_reason")
        if not reason:
            return next_node
        return "plan_generate" if reason in DRAFT_STOPS else END                           # a stop that earns the draft goes to the plan, drafted from the evidence gathered

    def after_revise(state: RunState) -> str:
        if state.get("stop_reason"):
            return _route(state, END)
        if state.get("questions_open", 0) > 0:
            return "ask"
        if (state.get("change") or {}).get("kind") == "usage":
            return "compare_options"                                                         # a usage correction goes straight to the comparison: nothing is read, extracted or verified anew
        return "research_plan" if state.get("rediscover") else "extract_evidence"          # the quality run with research goes through the planner and the discovery passes

    g = StateGraph(RunState)
    g.add_node("ask", ask)
    g.add_node("extract_evidence", guarded("extract_evidence", extract_evidence))
    g.add_node("verify", guarded("verify", verify))
    g.add_node("compare_options", guarded("compare_options", compare_options))
    g.add_node("plan_generate", guarded("plan_generate", plan_generate))
    if kind == "revision":                                                                 # the revision run starts at the revise node; the evidence, comparison and plan nodes are the initial run's
        g.add_node("revise", guarded("revise", revise))
        g.add_node("research_plan", guarded("research_plan", research_plan))               # the planner and the discovery passes of a quality run with research
        g.add_edge(START, "revise")
        g.add_conditional_edges("revise", after_revise, ["ask", "research_plan", "extract_evidence", "compare_options", "plan_generate", END])
        g.add_conditional_edges("research_plan", after_research, ["extract_evidence", "plan_generate", END])
        g.add_edge("ask", "revise")
    else:
        g.add_node("clarify", guarded("clarify", clarify))
        g.add_node("research_plan", guarded("research_plan", research_plan))
        g.add_edge(START, "clarify")
        g.add_conditional_edges("clarify", after_clarify, ["ask", "research_plan", "plan_generate", END])
        g.add_edge("ask", "clarify")
        g.add_conditional_edges("research_plan", after_research, ["extract_evidence", "plan_generate", END])
    g.add_conditional_edges("extract_evidence", after_extract, ["verify", "plan_generate", END])
    g.add_conditional_edges("verify", after_verify, ["compare_options", "plan_generate", END])
    g.add_conditional_edges("compare_options", after_compare, ["plan_generate", END])
    g.add_edge("plan_generate", END)
    return g.compile(checkpointer=checkpointer)
