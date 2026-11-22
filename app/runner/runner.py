"""Runner: starts a run under this instance's id, keeps the heartbeat every 10 s on its own connection,
builds the gateway, research adapter and workflow from settings, drives the graph through its interrupts and records the
run's end. One active run per project (runs.one_active_run). Decision summaries come from the gateway and the nodes;
the runner only emits events and keeps the run row honest. Keys are read from config/.env and never logged."""
import json, logging, os, pathlib, sqlite3, threading, time
from langgraph.types import Command

from app.adapters.model_gateway import gateway_from_settings, load_settings
from app.adapters.research import ExaResearch, data_root
from app.core.ledger import BudgetLedger
from app.core import coverage, coverage_step as CS, react
from app.core import wording as W
from app.core.sqlguard import require_guarded
from app.graph.workflow import RunContext, build_workflow, InterruptedCall, RunPaused
from app.repo import connect, init_db, open_checkpointer
from app.runner import events as ev
from app.runner.recovery import start_run, heartbeat, new_instance_id, answer_question, resume_run, recover_on_startup, is_locked, ActiveRunExists, interrupt_for_unknown

ENV_PATH = pathlib.Path("config/.env")
log = logging.getLogger("app.runner")                                                      # ids, counts and errors only


def load_env(path: pathlib.Path = ENV_PATH) -> dict:
    """KEY=VALUE lines of config/.env; values are returned to the caller only, never printed. A key the file does not hold is taken
    from the process environment when present (a key loaded into the environment for one command is read the same way, and a
    checkout without config/.env runs)."""
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("OPENROUTER_API_KEY", "EXA_API_KEY"):
        if k not in out and os.environ.get(k):
            out[k] = os.environ[k]
    return out


OUTCOME_TARGET = {"completed": "completed", "partial": "partial", "stopped": "failed"}   # graph outcome to the close target; "stopped" is the response word kept for a failed run


def usage_priced(conn, project_id: str) -> dict:
    """The current decisions of a project in two groups, by whether the founder's usage record prices them.
    A decision is priced from the usage record when its store rows say so, any of: its memo carries a budget judgement by calculation (
    memo.budget, written for the recommended option whether the figure was computed or not), an option of its memo carries a monthly cost
    computed by code at the usage (monthly_cost_calc), it has a cost table (derived_tables), or the budget of the latest stored version
    holds a row of its part that rests on a price claim. Every other current decision is carried unchanged by a usage correction.
    Returns {"priced": [{"id", "subject"}], "carried": [{"id", "subject"}]}, each by id."""
    require_guarded(conn)
    from app.core import applicability as AP
    with coverage.derived(conn, project_id):
        rows = conn.execute("SELECT d.id, d.subject, d.memo_json, EXISTS (SELECT 1 FROM derived_tables t WHERE t.project_id=d.project_id AND t.decision_id=d.id) FROM decisions d "
                            "WHERE d.project_id=? AND d.status IN ('active','unresolved') ORDER BY d.id", (project_id,)).fetchall()
    bj = conn.execute("SELECT budget_json FROM plan_versions WHERE project_id=? AND snapshot_sig IS NOT NULL AND budget_json IS NOT NULL ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
    items = {str(r.get("item") or "") for r in ((json.loads(bj[0]) or {}).get("rows") or []) if r.get("claims")} if bj and bj[0] else set()
    out = {"priced": [], "carried": []}
    for did, subject, memo_json, has_table in rows:
        memo = json.loads(memo_json) if memo_json else {}
        priced = bool(has_table) or isinstance(memo.get("budget"), dict) or any(isinstance(o, dict) and o.get("monthly_cost_calc") for o in memo.get("options") or []) or AP.norm(subject).replace("_", " ") in items
        out["priced" if priced else "carried"].append({"id": did, "subject": subject})
    return out


class Heartbeat:
    """Daemon thread: heartbeat_at every interval on its own connection until stopped, as the run's liveness. A
    locked or busy store on one beat is a missed beat (counted, logged once per episode, retried at the next tick, the
    resumption logged); any other exception is kept on the object (error) and ends the thread, and the runner records it.
    Leaving the context stops the thread and waits for it longer than its SQLite timeout, so the report taken afterwards
    is final."""
    def __init__(self, db_path, run_id: str, interval_s: float, timeout_s: float = 5):
        self.db_path, self.run_id, self.interval_s, self._stop = db_path, run_id, interval_s, threading.Event()
        self.timeout_s = float(timeout_s)
        self.beats = 0; self.missed = 0; self.episodes = 0                                    # written beats; beats lost to a locked store; locked episodes (one log line each)
        self.error = None                                                                     # the exception that ended the thread
        self.last_written = None; self._in_episode = False
        self._t = threading.Thread(target=self._loop, name=f"heartbeat-{run_id}", daemon=True)

    def report(self) -> dict:
        """What the runner reads after a drive: counts and the ending error, as text (no handle, no trace)."""
        return {"beats": self.beats, "missed": self.missed, "episodes": self.episodes, "alive": self._t.is_alive(),
                "error": f"{type(self.error).__name__}: {self.error}" if self.error is not None else None}

    def _missed_beat(self, exc) -> None:
        self.missed += 1
        if not self._in_episode:
            self._in_episode = True; self.episodes += 1
            log.warning("run %s: heartbeat missed, the store is locked (%s); retrying every %.3g s", self.run_id, exc, self.interval_s)

    def _loop(self):
        """The thread: the beats on the heartbeat's own connection; anything but a transient lock ends it with the reason kept."""
        from app.core.sqlguard import open_connection
        try:
            self._beats(open_connection)
        except Exception as e:                                                                # noqa: BLE001 (never swallowed: kept on the object, logged, recorded by the runner)
            self.error = e
            log.error("run %s: heartbeat stopped: %s: %s", self.run_id, type(e).__name__, e)

    def _beats(self, open_connection):
        conn = open_connection(self.db_path, timeout=self.timeout_s)                         # a plain connection: the journal mode is a property of the file; no pragma that needs a lock while the run writes
        try:
            try:
                heartbeat(conn, self.run_id); self.last_written = time.monotonic(); self.beats += 1   # the first beat at once, so a run resumed after a wait for the founder never shows the beat of before the wait
            except sqlite3.OperationalError as e:
                if not is_locked(e):
                    raise
                self._missed_beat(e)
            while not self._stop.wait(self.interval_s):
                try:
                    heartbeat(conn, self.run_id)
                except sqlite3.OperationalError as e:
                    if is_locked(e):
                        self._missed_beat(e); continue                                       # a transient lock: this beat is lost, the next tick tries again
                    raise
                self.last_written = time.monotonic(); self.beats += 1
                if self._in_episode:
                    self._in_episode = False
                    log.warning("run %s: heartbeat resumed after %d missed beat(s)", self.run_id, self.missed)
        finally:
            conn.close()

    def __enter__(self):
        self._t.start(); return self

    def __exit__(self, *a):
        self._stop.set(); self._t.join(timeout=self.timeout_s + self.interval_s + 1)       # a beat blocked on the lock returns within the SQLite timeout, then the loop sees the stop


class Runner:
    """start(project_id, idea) runs the workflow until it ends or pauses; resume(run_id, answers) continues a paused or
    interrupted run. gateway_factory(ledger) and research let tests inject stub providers; the live run builds both from
    settings and config/.env."""
    def __init__(self, db_path, settings: dict | None = None, gateway_factory=None, research=None, instance_id: str | None = None, checkpoint_path=None, data_dir=None, extractor_cls=None, production: bool = False):
        self.db_path, self.settings = db_path, settings or load_settings()
        self.instance_id = instance_id or new_instance_id()
        from app.core import sqlguard
        self._open_kwargs = {"mode": sqlguard.mode_from(settings), "production": production}   # a worker's handle opens as this one did
        self.conn = init_db(db_path, mode=sqlguard.mode_from(settings), production=production)   # production=True (the CLI, the web app): refused while NMB_TEST_FIXTURES is set; the handle's mode comes from settings [guard].sql_mode, raise by default
        try:
            self.checkpointer = open_checkpointer(checkpoint_path or pathlib.Path(db_path).with_name("checkpoints.db"))
        except BaseException:
            self.conn.close(); raise                                             # the first handle never outlives a failed second open
        self.data_dir = data_dir or data_root(None, self.conn)
        self._gateway_factory, self._research = gateway_factory, research
        self._extractor_cls = extractor_cls

    def close(self) -> None:
        """Close both owned handles (the application store and the checkpointer's store); idempotent, a second call is a no-op."""
        for owner in (getattr(self, "checkpointer", None), self):
            handle = getattr(owner, "conn", None)
            if handle is not None:
                try:
                    handle.close()
                except Exception:                                                # noqa: BLE001 (closing never raises past the owner)
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # ---- providers
    def _gateway(self, ledger: BudgetLedger):
        if self._gateway_factory is not None:
            return self._gateway_factory(ledger)
        env = load_env()
        return gateway_from_settings(self.settings, ledger, api_key=env["OPENROUTER_API_KEY"])

    def _research_adapter(self):
        if self._research is not None:
            return self._research
        env = load_env()
        return ExaResearch(api_key=env["EXA_API_KEY"], search_type=self.settings["research"].get("type", "auto"))   # the search and contents timeouts are the code's SEND_TIMEOUT_S per class

    def _ledger(self, run_id: str, conn=None) -> BudgetLedger:
        conn = conn if conn is not None else self.conn                                     # a worker's ledger runs over its own handle, with the same bounds
        b = json.loads(conn.execute("SELECT budget_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        bound = "max_model_calls" in b and "closing_reserve_calls" in b                  # the two limit values are bound at start_run; a run row without them gets max_model_calls 0 and every model dispatch is refused with the reason budget_json without call limits
        t = self.settings.get("tree", {})
        return BudgetLedger(conn, run_id, float(b["cap_usd"]), float(b.get("completion_reserve_usd", 0.0)), max_model_calls=int(b["max_model_calls"]) if bound else 0, closing_reserve_calls=int(b["closing_reserve_calls"]) if bound else 0, owner=self.instance_id,
                            tree_calls_per_decision=int(t.get("max_model_calls", 7)), tree_decisions_per_run=int(t.get("max_tree_decisions_per_run", 0)))   # the tree bounds of settings [tree], enforced by the ledger at the tree's dispatch point; the tree is off as shipped (0 decisions per run; the file's value and this default), a positive value opts in

    def _context(self, project_id: str, run_id: str) -> RunContext:
        ctx = RunContext(conn=self.conn, settings=self.settings, gateway=self._gateway(self._ledger(run_id)), research=self._research_adapter(), project_id=project_id, run_id=run_id, data_dir=self.data_dir,
                         db_path=self.db_path, make_ledger=lambda c, r: self._ledger(r, c), make_gateway=self._gateway, open_kwargs=dict(self._open_kwargs))   # a worker's context forks from this one
        if self._extractor_cls is not None:
            ctx.extractor_cls = self._extractor_cls
        return ctx

    # ---- lifecycle
    def recover(self) -> list[dict]:
        """Startup recovery before any dispatch: runs owned by another instance or with a stale heartbeat are
        reconciled (planned attempts released, sent attempts unknown, every row summarised) and marked interrupted."""
        out = recover_on_startup(self.conn, self.instance_id, int(self.settings.get("runner", {}).get("stale_after_seconds", 60)))
        from app.core.reconcile import reconcile_outdated
        reconcile_outdated(self.conn)                                                         # the derived evidence state of every project whose rules_version is older
        return out

    def open_run(self, project_id: str, kind: str = "initial", run_id: str | None = None) -> str:
        """The run row, the counted run, written on its own: the hosted runner of the web application (not part of this repository) writes it in the request that admitted the
        run, before the background drive is dispatched, so the allowance gate of the next request counts it; the
        drive (start, revise) finds the row and does not write it again. The two limit values are read from settings here and
        bound in budget_json. A revision run takes the revision cap and the project's latest run as its parent. The id."""
        n = self.conn.execute("SELECT COUNT(*) FROM runs WHERE project_id=?", (project_id,)).fetchone()[0] + 1
        run_id = run_id or f"{project_id}-R{n}"
        b = self.settings["budget"]
        parent = (self.conn.execute("SELECT id FROM runs WHERE project_id=? ORDER BY started_at DESC, id DESC LIMIT 1", (project_id,)).fetchone() or [None])[0] if kind == "revision" else None
        start_run(self.conn, run_id, project_id, run_id, self.instance_id, kind=kind, parent_run_id=parent, cap_usd=float(b["revision_cap_usd" if kind == "revision" else "cap_usd"]), completion_reserve_usd=float(b["completion_reserve_usd"]),
                  max_model_calls=int(b["max_model_calls"]), closing_reserve_calls=int(self.settings["extraction"]["closing_reserve_calls"]))
        return run_id

    def _opened(self, project_id: str, run_id: str | None) -> bool:
        """Whether run_id names a row of the project written before this drive (open_run at admission)."""
        return run_id is not None and self.conn.execute("SELECT 1 FROM runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone() is not None

    def start(self, project_id: str, idea: str, run_id: str | None = None, decisions: list | None = None) -> dict:
        """decisions: the project's decisions loaded from its decisions file (subject, question, candidate pool rule, research questions,
        criteria); the research planner receives them as the decisions to support; None: the planner proposes them."""
        self.recover()
        if not self._opened(project_id, run_id):
            run_id = self.open_run(project_id, run_id=run_id)
        b = self.settings["budget"]
        ev.emit(self.conn, run_id, "step", {"time": time.strftime("%H:%M:%S"), "glyph": ">", "stage": "run", "label": "Run started", "detail": f"instance {self.instance_id}; cap ${float(b['cap_usd']):.2f}, reserve ${float(b['completion_reserve_usd']):.2f}" + (f"; {len(decisions)} decision(s) loaded" if decisions else ""), "cost": ""})
        return self._drive(project_id, run_id, {"project_id": project_id, "run_id": run_id, "idea": idea, "round": 1, **({"decisions": list(decisions)} if decisions else {})})

    def revise(self, project_id: str, requirement_id: str, new_value, run_id: str | None = None, research: bool = False, hard: bool | None = None) -> dict:
        """The founder's change of one requirement opens a revision run. The change is applied first (app/core/invalidation.apply_change:
        the preview, the superseding row at the next requirement version, the stale set), then the run starts with kind revision under
        settings [budget].revision_cap_usd (its own cap, never the research cap of the run that produced the plan), the completion
        reserve and the call limits bound as for an initial run, the latest run of the project as its parent, and the graph starts at
        the revise node. Returns the drive's result with the change record.
        hard states the changed requirement's hardness (None carries the flag); a stated hardness
        opens a hardness question and a change that re-opens decisions or re-verifies claims opens the revision question, both
        persisted before the drive, so the run pauses at the ask node before its first send; the scope of the change (what the requirement
        binds, the decisions re-opened, the claims to re-verify) is stated in the run events."""
        self.recover()
        from app.core.invalidation import apply_change
        from app.core import interventions as IV
        opened = self._opened(project_id, run_id)                                             # the hosted runner wrote the row at admission; the active-run check excludes it
        if self.conn.execute("SELECT 1 FROM runs WHERE project_id=? AND status IN ('running','awaiting_user') AND id != ?", (project_id, run_id if opened else "")).fetchone():
            raise ActiveRunExists(f"project {project_id} already has an active run")        # checked before the change is applied: a change never lands under a running run
        try:
            change = apply_change(self.conn, project_id, requirement_id, new_value, hard=hard)
        except Exception:
            if opened:
                from app.runner.recovery import finish_run
                finish_run(self.conn, run_id, "failed", "change_not_applied")                 # the row written at admission never stays running without its change
            raise
        if not opened:
            run_id = self.open_run(project_id, kind="revision", run_id=run_id)
        b = self.settings["budget"]
        stale = change["stale"]["DEC"]
        questions = self._revision_questions(project_id, run_id, change, float(b["revision_cap_usd"]))   # the questions the run answers before its first send
        subjects = sorted({r[0] for r in self.conn.execute(f"SELECT DISTINCT subject FROM decisions WHERE project_id=? AND id IN ({','.join('?' * len(stale)) or 'NULL'})", (project_id, *stale)).fetchall()})
        scope = "every subject (applies_to empty)" if change.get("applies_to") is None else ", ".join(change["applies_to"]) or "no subject"
        ev.emit(self.conn, run_id, "step", {"time": time.strftime("%H:%M:%S"), "glyph": ">", "stage": "run", "label": "Revision run started",
                                            "detail": f"instance {self.instance_id}; cap ${float(b['revision_cap_usd']):.2f}, reserve ${float(b['completion_reserve_usd']):.2f}; requirement {change['requirement_key']} {change['old_id']} -> {change['new_id']} (version {change['requirement_version']}), "
                                                      f"{'hard' if change['hard'] else 'not hard'}{' as stated' if change.get('hard_stated') else ' (the flag carried)'}; binds {scope}; {len(stale)} decision(s) re-opened"
                                                      + (f" ({', '.join(subjects)}: {len(change.get('bound') or [])} by the binding, the rest by the recorded edges or carried stale)" if stale else "")
                                                      + f"; {len(change['reverify'])} claim(s) to re-verify on their candidates, the {len(change.get('reverify_cited') or [])} cited by the re-opened decisions first; {len(questions)} question(s) before the first send",
                                            "cost": "", "criterion": "I8", "stale": list(stale), "bound": list(change.get("bound") or []), "reverify": len(change["reverify"]), "reverify_cited": len(change.get("reverify_cited") or []), "candidates": change.get("candidates") or {}})
        payload = {"project_id": project_id, "run_id": run_id, "round": 1, "change": {**{k: change[k] for k in ("requirement_key", "old_id", "new_id", "old_value", "new_value", "requirement_version")}, "reverify": list(change["reverify"]), "reverify_cited": list(change.get("reverify_cited") or [])}, "stale": stale, "rediscover": bool(research)}   # with research the planner and the discovery passes run for the stale subjects as the quality run does; the impact set's claim ids travel with the change, durably in the state
        res = self._drive(project_id, run_id, payload)
        res["change"] = change
        return res

    def _revision_questions(self, project_id: str, run_id: str, change: dict, cap_usd: float) -> list[str]:
        """The questions a revision run answers before its first send, persisted here, asked by the ask node
        (app/graph/workflow.py revise returns to it while one is open): the hardness question of a changed requirement whose hardness the
        founder stated (the clarify node's wording; yes or no is the flag that stands), and the revision question when the change re-opens
        decisions or re-verifies claims (yes starts the re-run under the revision cap, no keeps the change and ends the run without spending).
        A change that re-opens nothing asks nothing. The ids, in the order asked."""
        from app.repo import Repo
        from app.core import wording as W
        repo = Repo(self.conn); stale = change["stale"]["DEC"]; qids = []
        with self.conn:
            if change.get("hard_stated"):
                qid = repo.next_id(project_id, "Q"); k = change["requirement_key"]
                self.conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status, kind) VALUES (?,?,?,?,?,1,'open','hardness')",
                                  (qid, project_id, f"The changed requirement {W.label_of(k)} was recorded as {'a must-have' if change['hard'] else 'not a must-have'}, as you stated. " + W.hardness_question(k, change.get("new_value")), W.HARDNESS_WHY + " The change records what you state here.", k)); qids.append(qid)   # the question shows the requirement's label and value, no id
            if stale or change["reverify"]:
                qid = repo.next_id(project_id, "Q")
                subjects = sorted({r[0] for r in self.conn.execute(f"SELECT DISTINCT subject FROM decisions WHERE project_id=? AND id IN ({','.join('?' * len(stale)) or 'NULL'})", (project_id, *stale)).fetchall()})
                self.conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status, kind) VALUES (?,?,?,?,?,1,'open','revision')",
                                  (qid, project_id, f"The change of {W.label_of(change['requirement_key'])} re-opens {len(stale)} decision(s) ({', '.join(str(x).replace('_', ' ') for x in subjects) or 'none'}) and re-verifies {len(change['reverify'])} claim(s) of their candidates; the re-run spends under the revision cap of {cap_usd:.2f} USD. Answer yes to start the re-run, or no to keep the change and end this run without spending.",
                                   W.REVISION_WHY, None)); qids.append(qid)   # no requirement key and no id in the question
        return qids

    def requality(self, project_id: str, note: str, run_id: str | None = None, research: bool = False) -> dict:
        """The quality run: a revision run with no requirement change. Its change is the research method (the new prompt versions,
        the strong route and the applicability map, named in `note`); its stale set is every decision of the latest plan version (re-decided
        with the new comparison), proposed stale inside one transaction before the run starts; the coverage checks and the enrichment are
        new work, nothing of them is reused. The run takes the revision cap and limits like a requirement revision."""
        self.recover()
        from app.core.reconcile import derive
        if self.conn.execute("SELECT 1 FROM runs WHERE project_id=? AND status IN ('running','awaiting_user')", (project_id,)).fetchone():
            raise ActiveRunExists(f"project {project_id} already has an active run")
        latest = int((self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM plan_versions WHERE project_id=? AND snapshot_sig IS NOT NULL", (project_id,)).fetchone() or [0])[0])   # the latest signed version; a half-written row (no snapshot) is swept by the next store
        rv = int((self.conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [1])[0])
        stale = [r[0] for r in self.conn.execute("SELECT id FROM decisions WHERE project_id=? AND plan_version=? ORDER BY id", (project_id, latest)).fetchall()]
        with coverage.derived(self.conn, project_id):                                     # a decision the latest version carried unchanged from an earlier version (the latest row of a subject with no row at the latest version) is a current decision of that version and is re-decided too; the latest version's own rows alone would leave the carried subjects out
            own_subjects = {r[0] for r in self.conn.execute("SELECT DISTINCT subject FROM decisions WHERE project_id=? AND plan_version=?", (project_id, latest)).fetchall()}
            carried = {}
            for did, subj, pv in self.conn.execute("SELECT id, subject, plan_version FROM decisions WHERE project_id=? AND plan_version<? ORDER BY plan_version, CAST(substr(id, instr(id, '.DEC-') + 5) AS INTEGER)", (project_id, latest)).fetchall():   # whatever the row's status: a carried row may be stale after a partial revision attempt, and a status filter would leave its subject out
                if subj not in own_subjects:
                    carried[subj] = did                                                    # the latest row of the subject wins (by plan version, then the numeric suffix of the id)
        stale += sorted(carried.values(), key=lambda d: int(d.rsplit("-", 1)[1]))
        if not stale:
            raise ValueError(f"project {project_id} has no decision of plan version {latest} to re-decide")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for did in stale:
                self.conn.execute("UPDATE decisions SET proposed_status='stale' WHERE id=? AND project_id=?", (did, project_id))
            self.conn.execute("UPDATE decisions SET proposed_status='stale' WHERE project_id=? AND plan_version>? AND COALESCE(proposed_status, 'active') IN ('active', 'unresolved')", (project_id, latest))   # the decisions of a run that stored no version are orphans, never active beside this run's (an unlinked orphan left active refuses the next plan)
            self.conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (project_id,))
            derive(self.conn, project_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK"); raise
        n = self.conn.execute("SELECT COUNT(*) FROM runs WHERE project_id=?", (project_id,)).fetchone()[0] + 1
        run_id = run_id or f"{project_id}-R{n}"
        parent = (self.conn.execute("SELECT id FROM runs WHERE project_id=? ORDER BY started_at DESC, id DESC LIMIT 1", (project_id,)).fetchone() or [None])[0]
        b = self.settings["budget"]
        start_run(self.conn, run_id, project_id, run_id, self.instance_id, kind="revision", parent_run_id=parent, cap_usd=float(b["revision_cap_usd"]), completion_reserve_usd=float(b["completion_reserve_usd"]),
                  max_model_calls=int(b["max_model_calls"]), closing_reserve_calls=int(self.settings["extraction"]["closing_reserve_calls"]))
        ev.emit(self.conn, run_id, "step", {"time": time.strftime("%H:%M:%S"), "glyph": ">", "stage": "run", "label": "Quality revision run started", "detail": f"instance {self.instance_id}; cap ${float(b['revision_cap_usd']):.2f}; {len(stale)} decision(s) of version {latest} re-decided"})
        change = {"requirement_key": "(quality revision)", "old_id": None, "new_id": None, "old_value": "the research method before Phase 9a", "new_value": note[:300], "requirement_version": rv, "kind": "quality"}
        payload = {"project_id": project_id, "run_id": run_id, "round": 1, "change": {**change, "reverify": []}, "stale": stale, "rediscover": bool(research)}   # with research, the planner and the discovery passes run again for the stale subjects before the evidence is re-read
        res = self._drive(project_id, run_id, payload)
        res["change"] = {**change, "stale": {"DEC": stale}, "preview": {"decisions": stale}}
        return res

    def correct_usage(self, project_id: str, answer: str, run_id: str | None = None) -> dict:
        """The founder corrects the recorded usage answer. The corrected words are read as the usage question reads
        them (app/core/derived.usage_read); an answer without a monthly volume is refused with what was recognised and what is needed, and nothing
        is stored. Otherwise the correction is a new answered usage row (the earlier answer stays as history), the length of one output and the
        scope carried from the earlier record when the correction does not state them, and the decisions the usage record prices (usage_priced: the
        priced group) are re-decided in a revision run under the revision cap: the existing revision path with no requirement change, no question
        and no new research (the revise node sends nothing; no search, no page read, no panel, no follow-up, no re-read of a vendor page), so
        the cost tables and the budget judgements follow the corrected record. With no priced decision the record is stored and the run
        ends at once."""
        self.recover()
        from app.core import derived
        from app.core.reconcile import derive
        from app.repo import Repo
        from app.runner.recovery import finish_run
        opened = self._opened(project_id, run_id)
        if self.conn.execute("SELECT 1 FROM runs WHERE project_id=? AND status IN ('running','awaiting_user') AND id != ?", (project_id, run_id if opened else "")).fetchone():
            raise ActiveRunExists(f"project {project_id} already has an active run")
        read = derived.usage_read(answer)
        if read["record"] is None:
            if opened:
                finish_run(self.conn, run_id, "failed", "change_not_applied")
            raise ValueError(derived.usage_read_line(read))
        prev_row = self.conn.execute("SELECT answer_json, usage_json FROM questions WHERE project_id=? AND kind='usage' AND status='answered' AND usage_json IS NOT NULL ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        prev = json.loads(prev_row[1]) if prev_row else {}
        record = dict(read["record"])
        if derived.is_items(record) == derived.is_items(prev):
            if not derived.record_length(record) and derived.record_length(prev):
                record = derived.with_length(record, derived.record_length(prev))          # the length of one output answered apart stays the founder's answer
            if record.get("scope") is None and prev.get("scope"):
                record["scope"] = prev["scope"]
        groups = usage_priced(self.conn, project_id)                                       # every current decision the usage record prices, not only those with a cost table
        stale = [d["id"] for d in groups["priced"]]
        rv = int((self.conn.execute("SELECT requirement_version FROM projects WHERE id=?", (project_id,)).fetchone() or [1])[0])
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            qid = Repo(self.conn).next_id(project_id, "Q")
            self.conn.execute("INSERT INTO questions (id, project_id, text, why_blocking, round, status, answer_json, usage_json, kind) VALUES (?,?,?,?,1,'answered',?,?,'usage')",
                              (qid, project_id, derived.USAGE_QUESTION, derived.USAGE_WHY, json.dumps(answer), json.dumps(record)))
            for did in stale:
                self.conn.execute("UPDATE decisions SET proposed_status='stale' WHERE id=? AND project_id=?", (did, project_id))
            if stale:
                self.conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (project_id,))
                derive(self.conn, project_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK"); raise
        if not opened:
            run_id = self.open_run(project_id, kind="revision", run_id=run_id)
        b = self.settings["budget"]
        ev.emit(self.conn, run_id, "step", {"time": time.strftime("%H:%M:%S"), "glyph": ">", "stage": "run", "label": "Usage corrected", "detail": f"instance {self.instance_id}; cap ${float(b['revision_cap_usd']):.2f}; {qid}: {derived.usage_read_line(read)} {W.correction_groups([d['subject'] for d in groups['priced']], [d['subject'] for d in groups['carried']])}", "cost": "",
                                            "priced": [d["id"] for d in groups["priced"]], "carried": [d["id"] for d in groups["carried"]]})   # the run's first step names both groups in the founder's words
        change = {"kind": "usage", "requirement_key": "(usage figures)", "old_id": None, "new_id": None, "old_value": json.loads(prev_row[0]) if prev_row and prev_row[0] else None, "new_value": answer, "requirement_version": rv}
        if not stale:
            finish_run(self.conn, run_id, "completed", "usage_recorded")
            return {"run_id": run_id, "status": "completed", "change": {**change, "stale": {"DEC": []}}, "question_id": qid, "record": record}
        res = self._drive(project_id, run_id, {"project_id": project_id, "run_id": run_id, "round": 1, "change": {**change, "reverify": []}, "stale": stale, "rediscover": False})
        res["change"] = {**change, "stale": {"DEC": stale}}; res["question_id"] = qid; res["record"] = record
        return res

    def open_questions(self, run_id: str) -> dict:
        """The question ids an answer may address on this run: the project's open clarify questions, and the question of
        the run's paused ask_user call. Everything else is foreign."""
        project_id = self.conn.execute("SELECT project_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        ids = {r[0]: "clarify" for r in self.conn.execute("SELECT id FROM questions WHERE project_id=? AND status='open' AND tool_call_id IS NULL", (project_id,))}
        for (qid,) in self.conn.execute("SELECT q.id FROM questions q JOIN tool_calls t ON t.id=q.tool_call_id WHERE t.run_id=? AND t.state='paused' AND q.status='open'", (run_id,)):
            ids[qid] = "ask_user"
        return ids

    def resume(self, run_id: str, answers: dict | None = None) -> dict:
        self.recover()
        row = self.conn.execute("SELECT project_id, status FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError(f"run {run_id} does not exist")
        project_id, status = row[0], row[1]
        if status not in ("awaiting_user", "interrupted"):
            raise ValueError(f"run {run_id} is {status}; only an awaiting_user or interrupted run can be resumed")
        from app.core import interventions as IV
        retired = [r[0] for r in self.conn.execute("SELECT id FROM questions WHERE project_id=? AND kind='contradiction' AND status='open'", (project_id,)).fetchall()]
        if retired:                                                                        # no question of kind contradiction waits: an open one is retired, and an answer sent for it is not an answer to anything open
            with self.conn:
                IV.retire_contradiction_questions(self.conn, project_id)
            answers = {q: a for q, a in (answers or {}).items() if q not in retired}
        allowed = self.open_questions(run_id)
        foreign = [q for q in (answers or {}) if q not in allowed]
        if foreign:                                                                        # validated before any write: nothing of a foreign question is stored
            raise ValueError(f"answer(s) for question(s) not open on run {run_id}: {', '.join(sorted(foreign))}")
        pending = self._ledger(run_id).unknown_calls()
        if pending:                                                                        # nothing is resent on its own; the founder's choice on every interrupted call comes first (resolve)
            raise ValueError(f"run {run_id} has {len(pending)} interrupted call(s) awaiting the founder's choice: {', '.join(c['id'] for c in pending)}; retry, continue or reconcile each before resuming")
        if status == "interrupted":
            resume_run(self.conn, run_id, self.instance_id)
        for qid, a in (answers or {}).items():
            if allowed[qid] == "ask_user":
                answer_question(self.conn, qid, a)                                        # an ask_user question: the exact paused tool call gets the answer
        return self._drive(project_id, run_id, Command(resume=answers or {}) if (answers or status == "awaiting_user") else None)

    def resolve(self, run_id: str, entry_id: str, choice: str, reported_usd: float | None = None) -> dict:
        """The founder's choice on one interrupted call (retry, continue, reconcile). reconcile without a figure asks the
        provider for the generation's cost when the row carries a generation id (GET /generation through the same OpenRouter client, no
        charge); the key is read as for every call and never printed."""
        from app.runner.recovery import resolve_call
        self.recover()
        def provider_figure(generation_id: str):
            from openai import OpenAI
            from app.adapters.model_gateway import OPENROUTER
            client = OpenAI(base_url=OPENROUTER, api_key=load_env()["OPENROUTER_API_KEY"], max_retries=0, timeout=30.0)
            try:
                data = client.get("/generation", options={"params": {"id": generation_id}}, cast_to=dict)
            except Exception as e:                                                         # noqa: BLE001 (no record, or no answer: the figure stays unknown to the product; the founder gives it or zero stands)
                log.warning("run %s: no provider figure for %s: %s", run_id, generation_id, type(e).__name__); return None
            cost = ((data or {}).get("data") or {}).get("total_cost")
            return float(cost) if isinstance(cost, (int, float)) else None
        return resolve_call(self.conn, run_id, entry_id, choice, reported_usd=reported_usd, provider_figure=provider_figure)

    def _drive(self, project_id: str, run_id: str, payload) -> dict:
        config = {"configurable": {"thread_id": run_id}}
        ctx = self._context(project_id, run_id)
        kind = (self.conn.execute("SELECT kind FROM runs WHERE id=?", (run_id,)).fetchone() or ["initial"])[0]
        graph = build_workflow(ctx, self.checkpointer, kind=kind)                              # a revision run drives the revision graph, on its start and on every resume
        from app.core import interventions as IV
        with self.conn:
            IV.retire_contradiction_questions(self.conn, project_id)                           # a contradiction question of an earlier run never holds this one at its ask node
        hb = Heartbeat(self.db_path, run_id, float(self.settings.get("runner", {}).get("heartbeat_seconds", 10)))
        interrupted = None; pause_exc = None
        try:
            with hb:
                try:
                    out = graph.invoke(payload, config)
                except InterruptedCall as e:                                               # an unknown call pauses the run for the founder's choice; the graph keeps its checkpoint
                    interrupted = e; out = {}
                except RunPaused as e:                                                     # the founder's pause; the row is already interrupted (founder_pause), the graph keeps its checkpoint
                    pause_exc = e; out = {}
        finally:
            CS.use_model(None)                                                             # the run's coverage route is in use only while this drive runs; outside a drive no model is in use and a stored verdict is read as it stands
            ctx.tick()                                                                     # the remainder of this drive is persisted like every dispatch tick; paused time is never driven
            with self.conn:
                self.conn.execute("UPDATE runs SET active_ticked_at=NULL WHERE id=?", (run_id,))
            heartbeat_report = hb.report()                                                 # the thread is stopped and joined by the context's exit: this report is final
            if hb.error is not None:                                                       # a heartbeat that ended with an error is never silent: an errors row the trace carries, on every exit of the drive
                try:
                    with self.conn:
                        self.conn.execute("INSERT INTO errors (run_id, step_key, category, detail) VALUES (?,?,?,?)", (run_id, f"{run_id}:heartbeat:{self.instance_id}", "internal", f"heartbeat stopped: {heartbeat_report['error']}"[:500]))
                except Exception as e:                                                     # noqa: BLE001 (the record must not replace a graph exception on its way out)
                    log.error("run %s: the heartbeat error could not be recorded: %s: %s", run_id, type(e).__name__, e)
        ev.announce_missing(self.conn, run_id)
        searches, pages = ctx.counts()
        ev.budget(self.conn, run_id, ctx.ledger, searches, pages)
        if pause_exc is not None:
            row = self.conn.execute("SELECT status, stop_reason FROM runs WHERE id=?", (run_id,)).fetchone()
            return {"run_id": run_id, "status": row[0], "stop_reason": row[1], "stage": pause_exc.stage, "spend": ctx.ledger.spend_report(), "beats": hb.beats, "heartbeat": heartbeat_report, "trace": react.trace_complete(self.conn, run_id)}
        if interrupted is not None:
            paused = interrupt_for_unknown(self.conn, run_id, self.instance_id, f"{interrupted.stage}: {interrupted.detail}"[:300])
            status = paused["status"] if paused["changed"] else (self.conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone() or ["interrupted"])[0]
            return {"run_id": run_id, "status": status, "stop_reason": "unknown_call", "stage": interrupted.stage, "unknown_calls": paused["unknown_calls"] or self._ledger(run_id).unknown_calls(), "possible_usd": paused.get("possible_usd", 0.0),
                    "spend": ctx.ledger.spend_report(), "beats": hb.beats, "heartbeat": heartbeat_report, "trace": react.trace_complete(self.conn, run_id)}
        if "__interrupt__" in out:
            return {"run_id": run_id, "status": "awaiting_user", "interrupt": [i.value for i in out["__interrupt__"]], "beats": hb.beats, "heartbeat": heartbeat_report}
        status = out.get("status") or ("completed" if out.get("plan_version") else "partial")
        reason = out.get("stop_reason")
        from app.runner.recovery import finish_run                                         # the one path that closes a run; its done event is written in its transaction
        target = OUTCOME_TARGET[status]                                                    # the closed vocabulary of a run outcome; any other word is a programming error (KeyError), never a failed run
        closed = finish_run(self.conn, run_id, target, reason or ("complete" if status == "completed" else status), {"plan_version": out.get("plan_version")}, worker_id=self.instance_id)   # another worker's run is never closed by this one
        reason = closed["stop_reason"]                                                     # the response carries the stored reason on every close
        if not closed["changed"] or closed["status"] != target:                            # the close lost to a competing transition, or an open call downgraded it: the stored status class is the response ("stopped" for a failed run is the only wording kept)
            status = closed["status"]
        trace = react.trace_complete(self.conn, run_id)
        return {"run_id": run_id, "status": status, "stop_reason": reason, "plan_version": out.get("plan_version"), "trace": trace, "tool_events_missing": ev.tool_events_missing(self.conn, run_id),
                "spend": ctx.ledger.spend_report(), "beats": hb.beats, "heartbeat": heartbeat_report, "active_seconds": round(float(self.conn.execute("SELECT COALESCE(active_seconds,0) FROM runs WHERE id=?", (run_id,)).fetchone()[0]), 3)}   # the persisted total only

    def trace_export(self, run_id: str) -> dict:
        """The run's trace: events, decision summaries, ledger rows (ids, costs, states) and the coverage report. No prompts, no page text."""
        pid = self.conn.execute("SELECT project_id FROM runs WHERE id=?", (run_id,)).fetchone()[0]
        return {"run_id": run_id, "events": [{"id": i, "event": e, "data": d} for i, e, d in ev.events(self.conn, run_id)],
                "reasoning_records": [dict(zip(("step_key", "action_id", "purpose", "action", "observation", "change"), r)) for r in self.conn.execute("SELECT step_key, action_id, purpose, action, observation, change FROM reasoning_records WHERE run_id=? ORDER BY id", (run_id,))],
                "ledger": [dict(zip(("id", "kind", "state", "outcome", "reserved_usd", "reported_usd", "model_id", "attempt_kind", "tokens_in", "tokens_out", "reasoning_tokens"), r)) for r in self.conn.execute("SELECT id, kind, state, outcome, reserved_usd, reported_usd, model_id, attempt_kind, tokens_in, tokens_out, reasoning_tokens FROM usage_ledger WHERE run_id=? ORDER BY planned_at, rowid", (run_id,))],
                "tool_calls": [dict(zip(("id", "tool", "state", "args"), (r[0], r[1], r[2], json.loads(r[3])))) for r in self.conn.execute("SELECT id, tool, state, args_json FROM tool_calls WHERE run_id=? ORDER BY created_at, n", (run_id,))],
                "coverage_report": coverage.coverage_report(self.conn, pid), "trace_complete": react.trace_complete(self.conn, run_id), "spend": self._ledger(run_id).spend_report()}
