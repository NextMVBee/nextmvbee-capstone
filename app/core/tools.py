"""Bounded, model-requested tool calling. The application validates and executes every request; the model never grants
itself permissions. Allowlist per stage, per-stage limit, run quotas reserved transactionally across workers, durable
tool ids replayed only on an exact request match, billing attempts separate from logical tool identity, ask_user that
persists the question and pauses the run, and a run-status guard at dispatch. Tested offline (tests/test_concepts.py,
tests/test_audit.py)."""
import hashlib, json, sqlite3, time
from pydantic import ValidationError
from schemas.models import TOOL_ARGS
from app.core.ledger import BudgetLedger, estimate_exa_search_usd, estimate_exa_contents_usd, EXA_SAFETY, DispatchRefused, UnknownCallPending, SEND_TIMEOUT_S, wait_out_lock
from app.core.ids import make_id, next_number
from app.runner.recovery import assert_run_active, cancel_run, RunStopped
from app.adapters.research import ExaHTTPError, ExaTimeout, store_pages, source_text
from app.core.sqlguard import require_guarded

ALLOWED = {"research_plan": {"search", "read", "ask_user"}, "verify": {"re_fetch"}}   # every other stage: no tools
STAGE_LIMIT = 8
RUN_LIMITS = {"searches": 30, "pages": 50}
QUOTA_KIND = {"search": "searches", "read": "pages", "re_fetch": "pages"}
OBSERVATION_CHARS, OBSERVATION_TOTAL_CHARS = 6000, 18000     # defaults mirrored by settings [tools] observation_chars and observation_total_chars
SENTENCE_ENDS = (". ", ".\n", ".\r", "? ", "?\n", "?\r", "! ", "!\n", "!\r")   # LF, CR and CRLF endings all count as a sentence boundary (text is never normalised)
TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "search", "description": "Search current sources with Exa (at most 10 results). Each result page comes back bounded: the first part of its text with source_id, source_version, the character range shown and next_offset.",
                                      "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "freshness": {"type": "string", "enum": ["volatile", "policy", "technical", "stable"]}, "num_results": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read", "description": "Read a page: by url (fetches it; page quota) or by source_id and source_version of a page already stored (free): offset continues from next_offset, query returns the most relevant stored passages with their ranges. Exactly one of url or source_id.",
                                      "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "freshness": {"type": "string"}, "source_id": {"type": "string"}, "source_version": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "query": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {"name": "re_fetch", "description": "Fetch a page live again to confirm a volatile fact.",
                                      "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "ask_user", "description": "Ask the founder one blocking question; the run pauses until they answer.",
                                      "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "why_blocking": {"type": "string"}, "requirement_key": {"type": "string"}}, "required": ["text", "why_blocking", "requirement_key"]}}},
]


def tools_for(stage: str) -> list[dict]:
    return [t for t in TOOL_SCHEMAS if t["function"]["name"] in ALLOWED.get(stage, set())]


MARKER = "…"


def cut_window(text: str, start: int, limit: int) -> tuple[str, int, str, bool]:
    """The window of stored text shown to the model from start: at most limit characters of source text, cut at a sentence
    boundary when one exists within the last 20% of the window, otherwise one character short of the window so the ellipsis
    marker fits inside it. Returns (chars, range_end, shown, hard_cut): chars are source characters only (range_end - start
    of them); the caller appends the marker when hard_cut is true, and ranges never count it."""
    total = len(text)
    if start >= total:
        return "", start, "full", False
    end = min(start + limit, total)
    if end >= total:
        return text[start:end], end, "full", False
    seg = text[start:end]
    floor = int(len(seg) * 0.8)
    best = max(seg.rfind(m, floor) for m in SENTENCE_ENDS)
    if best >= 0:
        cut = best + 1
        return seg[:cut], start + cut, "partial", False
    return seg[:-1], start + len(seg) - 1, "partial", True


def observation_for_model(obs: dict, observation_chars: int = OBSERVATION_CHARS, observation_total_chars: int = OBSERVATION_TOTAL_CHARS) -> dict:
    """The structured, always-valid observation a tool result becomes in the model's context.
    Per page: source_id, source_version, title, url, fetch (full, partial, blocked), shown (full, partial), content (the
    first observation_chars characters, sentence-aware), chars_total, range_start, range_end, more_available, next_offset.
    observation_total_chars caps the sum of content across pages; pages beyond it carry metadata only with more_available
    true. A page that already carries a range (a stored read at an offset, a passage from a query, a refresh notice) is
    shown as it is. The full result stays in tool_calls.result_json."""
    data = obs.get("data")
    out = {"ok": bool(obs.get("ok", True)), "tool_call_id": obs.get("tool_call_id"), "tool": obs.get("tool")}
    if not isinstance(data, list):
        out["data"] = data
        return out
    pages, remaining = [], int(observation_total_chars)
    for p in data:
        if not isinstance(p, dict):
            pages.append({"content": str(p)[:observation_chars]}); continue
        text = p.get("text") or ""
        base = {"source_id": p.get("source_id"), "source_version": p.get("source_version") or p.get("content_hash"), "title": p.get("title"), "url": p.get("url"),
                "fetch": "blocked" if p.get("fetch_status") == "blocked" else ("full" if (text or p.get("range_start") is not None) else "partial")}
        if p.get("refreshed"):
            base.update(refreshed=True, requested_version=p.get("requested_version"), invalid_ranges=True, chars_total=p.get("chars_total"), content="", shown="partial",
                        range_start=0, range_end=0, more_available=True, next_offset=0, note="the stored source was refreshed; ranges of the previous version are invalid; read again from offset 0 with this source_version")
            pages.append(base); continue
        if p.get("range_start") is not None:                     # already a bounded window or a passage: text holds source characters only, ranges are the adapter's
            total = int(p.get("chars_total") or len(text)); a = int(p["range_start"]); b = int(p.get("range_end", a + len(text)))
            limit = min(observation_chars, remaining)
            if limit <= 0:
                base.update(content="", shown="partial", chars_total=total, range_start=a, range_end=a, more_available=True, next_offset=a); pages.append(base); continue
            hard = bool(p.get("hard_cut"))
            if len(text) + (len(MARKER) if hard else 0) > limit:    # the cap counts the marker: never more than limit characters of content
                chars, end, hard = text[:limit - len(MARKER)], a + limit - len(MARKER), True
            else:
                chars, end = text, b
            content = chars + (MARKER if hard else "")
            remaining -= len(content)
            base.update(content=content, shown="full" if end >= total else "partial", chars_total=total, range_start=a, range_end=end, more_available=end < total, next_offset=end)
            for k in ("query", "end_of_source", "version_matches"):
                if k in p:
                    base[k] = p[k]
            pages.append(base); continue
        total = len(text); limit = min(observation_chars, remaining)
        if limit <= 0:
            base.update(content="", shown="partial", chars_total=total, range_start=0, range_end=0, more_available=total > 0, next_offset=0); pages.append(base); continue
        chars, end, shown, hard = cut_window(text, 0, limit)
        content = chars + (MARKER if hard else "")
        remaining -= len(content)
        base.update(content=content, shown=shown, chars_total=total, range_start=0, range_end=end, more_available=end < total, next_offset=end)
        pages.append(base)
    out["pages"] = pages; out["pages_total"] = len(pages)
    return out


def fingerprint(tool: str, args: dict) -> str:
    return hashlib.sha256((tool + "|" + json.dumps(args, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()


class ToolRejected(Exception):
    def __init__(self, message, tool_call_id=None):
        super().__init__(message); self.tool_call_id = tool_call_id


class UnknownToolPending(ToolRejected):
    """An interrupted attempt of this stage awaits the founder: the stage cannot continue on its own (never fed back to the model as a retry)."""


class ToolReplay(Exception):
    """Inside the claim: an identical request already completed; replay it without reserving or dispatching."""
    def __init__(self, row):
        super().__init__("replay"); self.row = row


class ToolInFlight(Exception):
    """Inside the claim: an identical request is claimed or in flight; wait for its result instead of dispatching again."""
    def __init__(self, tool_call_id):
        super().__init__(f"{tool_call_id} in flight"); self.tool_call_id = tool_call_id


class LimitReached(Exception):
    """Raised inside a slot transaction when a quota or the stage limit is reached; carries the rejected row id."""
    def __init__(self, message, tool_call_id):
        super().__init__(message); self.tool_call_id = tool_call_id


class Paused(Exception):
    def __init__(self, tool_call_id, question_id):
        super().__init__(f"paused for the founder: {question_id}"); self.tool_call_id, self.question_id = tool_call_id, question_id


def step_key_for(run_id: str, stage: str, worker: str = "w0", branch: str = "b0") -> str:
    return f"{run_id}:{stage}:{worker}:{branch}"


class ToolRunner:
    """Logical tool identity is the stage plus the fingerprint of the validated request, never a position in a sequence.
    A resumed stage finds completed or paused requests by fingerprint, so prior tools, answered questions and partly
    completed batches keep their identity. Rejections always get a fresh row and never touch an existing one."""

    def __init__(self, conn: sqlite3.Connection, ledger: BudgetLedger, run_id: str, project_id: str, stage: str, research,
                 worker: str = "w0", branch: str = "b0", stage_limit: int = STAGE_LIMIT, run_limits: dict | None = None,
                 data_dir=None, settings: dict | None = None, allowed=None):
        require_guarded(conn)
        self.conn, self.ledger, self.run_id, self.project_id, self.stage = conn, ledger, run_id, project_id, stage
        self.allowed = set(ALLOWED.get(stage, set())) & set(allowed) if allowed is not None else set(ALLOWED.get(stage, set()))   # a caller may narrow the stage allowlist (a worker: search and read), never widen it
        self.research, self.worker, self.branch, self.stage_limit = research, worker, branch, stage_limit
        self.run_limits = dict(RUN_LIMITS, **(run_limits or {}))
        self.step_key = step_key_for(run_id, stage, worker, branch)
        self.data_dir = data_dir                                      # None: next to the database file (app/adapters/research.data_root)
        tools_cfg = (settings or {}).get("tools") or {}
        self.observation_chars = int(tools_cfg.get("observation_chars", OBSERVATION_CHARS))

    _before_reservation = None    # test hook: called inside the claim transaction after the slot work, before the reservation
    _after_claim = None           # test hook: called after the claim committed, before the send
    _after_dispatch = None        # test hook: called after the adapter returned, before the settlement
    _after_sent = None            # test hook: called after the sent pair committed, before the adapter call
    _after_complete = None        # test hook: called after the completion pair committed

    def _insert_rejected_after_rollback(self, e, tool, args, fp):
        """A limit inside the claim rolled the transaction back; record the rejection on its own row (never a slot)."""
        rn = self._next_n(); rid = f"{self.step_key}:tool:{rn}"
        with self.conn:
            self.conn.execute("INSERT INTO tool_calls (id, run_id, step_key, stage, worker, branch, n, tool, args_json, fingerprint, state, error, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,'rejected',?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                              (rid, self.run_id, self.step_key, self.stage, self.worker, self.branch, rn, tool, json.dumps(args), fp, str(e)))
        e.tool_call_id = rid

    def _next_n(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(n),0) FROM tool_calls WHERE step_key=?", (self.step_key,)).fetchone()[0]) + 1

    def _insert(self, tc_id, n, tool, args, state, fp="", result=None, error=None, entry=None):
        with self.conn:
            self.conn.execute("INSERT INTO tool_calls (id, run_id, step_key, stage, worker, branch, n, tool, args_json, fingerprint, ledger_entry, state, result_json, error, finished_at) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?, CASE WHEN ? IN ('completed','failed','rejected','paused','unknown') THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END)",
                              (tc_id, self.run_id, self.step_key, self.stage, self.worker, self.branch, n, tool, json.dumps(args), fp, entry, state, json.dumps(result) if result is not None else None, error, state))

    def _update_sql(self, tc_id, state, result=None, error=None, entry=None) -> tuple:
        return ("UPDATE tool_calls SET state=?, result_json=COALESCE(?, result_json), error=?, ledger_entry=COALESCE(?, ledger_entry), "
                "finished_at=CASE WHEN ? IN ('completed','failed','rejected','paused','unknown') THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') ELSE finished_at END WHERE id=?",
                (state, json.dumps(result) if result is not None else None, error, entry, state, tc_id))

    def _update(self, tc_id, state, result=None, error=None, entry=None):
        self.ledger.atomically(self._update_sql(tc_id, state, result, error, entry))

    def _reject(self, tool, args, error) -> ToolRejected:
        n = self._next_n(); tc_id = f"{self.step_key}:tool:{n}"
        self._insert(tc_id, n, tool if tool in TOOL_ARGS else "search", args, "rejected", error=error)
        return ToolRejected(error, tc_id)

    def _reserve_slot(self, tc_id, n, tool, args, fp, existing: bool, state: str = "planned", result=None, extra=(), counts_quota: bool = True) -> None:
        """Take the run-level quota slot (search and page tools) and the stage slot inside one BEGIN IMMEDIATE transaction
        (safe across workers), with the run-status check inside it: a cancellation that lands before the transaction makes
        the dispatch fail. ask_user takes a stage slot the same way, written in state paused together with its question row
        and the run pause (extra statements), so an interruption can never leave a paused tool without its question."""
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            status = self.conn.execute("SELECT status, worker_id FROM runs WHERE id=?", (self.run_id,)).fetchone()
            if not status or status[0] != "running":
                raise RunStopped(f"run {self.run_id} is {status[0] if status else 'missing'}; no new request may start")
            owner = getattr(self.ledger, "owner", None)
            if owner is not None and status[1] != owner:
                raise RunStopped(f"run {self.run_id} is owned by worker {status[1]}, not {owner}; no new request may start")   # reclaimed and resumed by another worker
            self._slot_body(self.conn, tc_id, n, tool, args, fp, existing, state, result, extra, counts_quota=counts_quota)
            self.conn.commit()
        except LimitReached as e:
            self.conn.commit(); raise ToolRejected(str(e), e.tool_call_id)
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
            raise

    def _slot_body(self, conn, tc_id, n, tool, args, fp, existing: bool, state: str = "planned", result=None, extra=(), entry=None, counts_quota: bool = True) -> None:
        """Quota and stage checks plus the tool row, written inside whichever transaction is open (the ledger's
        dispatch claim for search, read and re_fetch; the slot reservation for ask_user and for stored reads, which take a
        stage slot but no page quota and no ledger row). Raises ToolRejected at a limit after writing the rejected row,
        which the caller must commit. Quota rows are the ones with a ledger entry: a stored read never counts as a page."""
        kind = QUOTA_KIND.get(tool) if counts_quota else None; cols = "('search')" if kind == "searches" else "('read','re_fetch')"
        label = "search" if kind == "searches" else "page"
        if True:
            used = self.conn.execute(f"SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND tool IN {cols} AND state IN ('planned','sent','completed','unknown') AND ledger_entry IS NOT NULL AND id<>?", (self.run_id, tc_id)).fetchone()[0] if kind else 0
            executed = self.conn.execute("SELECT COUNT(*) FROM tool_calls WHERE run_id=? AND step_key=? AND state IN ('planned','sent','completed','unknown','failed','paused') AND id<>?", (self.run_id, self.step_key, tc_id)).fetchone()[0]
            if (kind and used >= self.run_limits[kind]) or executed >= self.stage_limit:
                err = f"run {label} limit reached" if (kind and used >= self.run_limits[kind]) else f"stage limit of {self.stage_limit} tool calls reached"
                rn = self._next_n(); rid = f"{self.step_key}:tool:{rn}"
                self.conn.execute("INSERT INTO tool_calls (id, run_id, step_key, stage, worker, branch, n, tool, args_json, fingerprint, state, error, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,'rejected',?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                                  (rid, self.run_id, self.step_key, self.stage, self.worker, self.branch, rn, tool, json.dumps(args), fp, err))
                raise LimitReached(err, rid)                       # the caller commits the rejected row and raises ToolRejected
            if existing:
                self.conn.execute("UPDATE tool_calls SET state=?, error=NULL, ledger_entry=COALESCE(?, ledger_entry), finished_at=NULL WHERE id=?", (state, entry, tc_id))   # a retry re-links the row to its new billing attempt
            else:
                self.conn.execute("INSERT INTO tool_calls (id, run_id, step_key, stage, worker, branch, n, tool, args_json, fingerprint, ledger_entry, state, result_json, finished_at) "
                                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, CASE WHEN ? IN ('paused','completed') THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END)",
                                  (tc_id, self.run_id, self.step_key, self.stage, self.worker, self.branch, n, tool, json.dumps(args), fp, entry, state, json.dumps(result) if result is not None else None, state))   # the ledger link is persisted in the claim transaction
            for sql, params in extra:
                self.conn.execute(sql, params)

    def is_replay(self, request: dict) -> bool:
        """Whether this request would replay a completed observation (same stage instance, same fingerprint): a replay sends
        nothing, so the caller's dispatch checks (deadline, permit, allowance, budget) do not apply to it."""
        tool = request.get("tool"); raw_args = request.get("args", {}) or {}
        if tool not in TOOL_ARGS or tool not in self.allowed:
            return False
        try:
            args = TOOL_ARGS[tool].model_validate(raw_args)
        except ValidationError:
            return False
        fp = fingerprint(tool, args.model_dump(mode="json"))
        row = self.conn.execute("SELECT id, state FROM tool_calls WHERE step_key=? AND fingerprint=? AND state IN ('completed','paused') ORDER BY n LIMIT 1", (self.step_key, fp)).fetchone()
        if not row:
            return False
        if row[1] == "completed":
            return True
        return tool == "ask_user" and self.conn.execute("SELECT 1 FROM questions WHERE tool_call_id=? AND status='answered'", (row[0],)).fetchone() is not None   # the founder's stored answer: delivering it sends nothing

    def handle(self, request: dict) -> dict:
        """Validate, account, execute and store one tool request; returns the observation for the model."""
        tool = request.get("tool"); raw_args = request.get("args", {}) or {}
        if tool not in self.allowed:
            raise self._reject(tool, raw_args, f"{tool!r} is not allowed in stage {self.stage}" + ("" if tool not in ALLOWED.get(self.stage, set()) else f" for worker {self.worker}"))   # refused and recorded before any side effect
        try:
            args = TOOL_ARGS[tool].model_validate(raw_args)
        except ValidationError as e:
            raise self._reject(tool, raw_args, "invalid arguments: " + "; ".join(f"{'/'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5]))
        canon = args.model_dump(mode="json"); fp = fingerprint(tool, canon)
        row = self.conn.execute("SELECT id, state, result_json, n FROM tool_calls WHERE step_key=? AND fingerprint=? AND state<>'rejected' ORDER BY n LIMIT 1", (self.step_key, fp)).fetchone()
        if row and row[1] == "completed":
            return {"tool_call_id": row[0], "tool": tool, "ok": True, "summary": "replayed stored observation", "data": json.loads(row[2])}
        if tool == "read" and args.source_id is not None:
            return self._stored_read(args, canon, fp)
        if tool == "ask_user":
            if row:                                                             # paused earlier: deliver the answer or pause again on the same question
                q = self.conn.execute("SELECT id, status, answer_json FROM questions WHERE tool_call_id=?", (row[0],)).fetchone()
                if q and q[1] == "answered":
                    self._update(row[0], "completed", result={"question_id": q[0], "answer": json.loads(q[2])})
                    return {"tool_call_id": row[0], "tool": tool, "ok": True, "summary": "founder answered", "data": {"question_id": q[0], "answer": json.loads(q[2])}}
                qid = q[0] if q else json.loads(row[2] or "{}").get("question_id")   # the tool row carries the question id too; both are written in one transaction
                if not qid:
                    raise ToolRejected(f"{row[0]} is paused without a question; the record is inconsistent", row[0])
                raise Paused(row[0], qid)
            n = self._next_n(); tc_id = f"{self.step_key}:tool:{n}"
            qid = make_id(self.project_id, "Q", next_number(self.conn, "questions", self.project_id, "Q"))
            self._reserve_slot(tc_id, n, tool, canon, fp, existing=False, state="paused", result={"question_id": qid}, extra=[   # tool row, question and pause: one transaction
                ("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status, tool_call_id) VALUES (?,?,?,?,?,1,'open',?)",
                 (qid, self.project_id, args.text, args.why_blocking, args.requirement_key, tc_id)),
                ("UPDATE runs SET status='awaiting_user' WHERE id=? AND status='running'", (self.run_id,))])
            raise Paused(tc_id, qid)
        kind = "search" if tool == "search" else "contents"
        price = estimate_exa_search_usd(args.num_results) if tool == "search" else estimate_exa_contents_usd(1)   # the request price
        est = round(price * EXA_SAFETY, 6)                                                                      # reserved with the 15% margin like model calls
        chosen = {}

        def within(conn, entry_id):
            """Inside the claim transaction: fingerprint lookup, replay or in-flight decision, tool id allocation, slot and row with its ledger link."""
            require_guarded(conn)
            r = conn.execute("SELECT id, state, result_json, n, ledger_entry FROM tool_calls WHERE step_key=? AND fingerprint=? AND state<>'rejected' ORDER BY n LIMIT 1", (self.step_key, fp)).fetchone()
            if r and r[1] == "completed":
                raise ToolReplay(r)
            if r and r[1] in ("planned", "sent"):
                raise ToolInFlight(r[0])
            if r and r[1] == "unknown":
                res = conn.execute("SELECT resolution FROM usage_ledger WHERE id=?", (r[4],)).fetchone() if r[4] else None
                if res and res[0] == "skipped":                                                   # the founder continued without it: an error observation for the model, nothing resent
                    raise ToolRejected(f"{r[0]} was interrupted and the founder continued without it; no result", r[0])
                if not res or res[0] not in ("retried", "reconciled"):                           # a reconciled attempt is settled at the provider's figure; the request is made again as a new attempt
                    raise UnknownToolPending(f"{r[0]} was interrupted; the founder retries, skips or reconciles it first", r[0])
            existing = bool(r); n = r[3] if r else self._next_n(); tc_id = r[0] if r else f"{self.step_key}:tool:{n}"
            chosen.update(tc_id=tc_id, n=n)
            self._slot_body(conn, tc_id, n, tool, canon, fp, existing, entry=entry_id)
            if self._before_reservation is not None:
                self._before_reservation()
        deadline = time.monotonic() + float(getattr(self.research, "timeout_s", None) or SEND_TIMEOUT_S[kind])   # the in-flight wait is bounded by the class's send timeout
        while True:
            try:                                                 # run status, unknown check, lookup, quota and stage slot, tool row, attempt, reservation: one BEGIN IMMEDIATE
                entry = self.ledger.claim_dispatch(self.step_key, kind, est, within=within, request_scoped=kind == "contents")   # an unknown read blocks itself alone (within refuses it), never the instance's other pages
                break
            except ToolReplay as rp:
                return {"tool_call_id": rp.row[0], "tool": tool, "ok": True, "summary": "replayed stored observation", "data": json.loads(rp.row[2])}
            except ToolInFlight as fl:                           # wait for the identical request to finish, then the next claim replays it
                if time.monotonic() > deadline:
                    raise ToolRejected(f"{fl.tool_call_id} is still in flight after the timeout", fl.tool_call_id)
                time.sleep(0.05)
            except LimitReached as e:
                self._insert_rejected_after_rollback(e, tool, canon, fp); raise ToolRejected(str(e), e.tool_call_id)
            except DispatchRefused as e:
                raise RunStopped(str(e)) from e
            except UnknownCallPending as e:                      # an interrupted attempt of this step awaits the founder: never resent on its own
                raise UnknownToolPending(f"{e}; the founder retries, skips or reconciles it first", None) from e
        tc_id = chosen["tc_id"]
        if self._after_claim is not None:
            self._after_claim()                                  # test hook: a crash between the claim and the send
        try:
            self.ledger.mark_sent(entry, self._update_sql(tc_id, "sent", entry=entry))   # both sent markers in one transaction; the stop check at the wire: a stop after the claim releases the row and nothing is sent
        except DispatchRefused as e:
            raise RunStopped(str(e)) from e
        if self._after_sent is not None:
            self._after_sent()                                   # test hook: a crash right after the sent markers, before the request
        try:
            if tool == "search":
                if args.search_type or args.additional_queries:                                     # a deep search with the planner's variants; a stub adapter keeps the three-argument call
                    data = self.research.search(args.query, args.freshness.value, args.num_results, search_type=args.search_type or "auto", additional_queries=args.additional_queries)
                else:
                    data = self.research.search(args.query, args.freshness.value, args.num_results)
            elif tool == "read":
                data = self.research.contents([args.url], args.freshness.value)
            else:
                data = self.research.contents([args.url], "volatile")
        except ExaHTTPError as e:                               # refused before any content: nothing billed
            statements = [self.ledger.fail_sql(entry, charged=False, provider_status=e.status), self._update_sql(tc_id, "failed", error=str(e)[:200], entry=entry)]
            if e.status == 402:                                  # credits exhausted: the shared run stops for every worker, in the same transaction
                statements.append(("UPDATE runs SET status='cancelled', stop_reason='credit_exhausted', ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND status IN ('running','awaiting_user')", (self.run_id,)))
            self.ledger.atomically(*statements)
            if e.status == 402:
                raise ToolRejected("Exa credits exhausted (HTTP 402); the run is stopped", tc_id)
            raise ToolRejected(f"tool {tool} failed: {e}", tc_id)
        except (ExaTimeout, Exception) as e:                     # no answer: the request may have reached the provider
            self.ledger.atomically(self.ledger.fail_sql(entry, charged=None), self._update_sql(tc_id, "unknown", error=str(e)[:200], entry=entry))
            raise UnknownToolPending(f"tool {tool} failed; the call {tc_id} is marked unknown with its possible charge; the founder retries, skips or reconciles it first", tc_id)   # a fresh unknown tool outcome pauses the run before another send; it is never an error observation the model continues past
        if self._after_dispatch is not None:
            self._after_dispatch()                               # test hook: a crash after the request was sent, before the settlement
        if isinstance(data, list):                               # every page is stored and versioned before the observation is written (source_id, source_version on each page);
            wait_out_lock(store_pages, self.conn, self.project_id, data, self.data_dir, found_by=["exa"] if tool == "search" else None, refresh=self.stage != "verify")   # an interruption here leaves the pair sent, which recovery turns into an unknown call (the fetch may have been billed); a search result is found by Exa; the store lock is waited out (each page is one transaction, a stored page is stored once); the verify stage's re_fetch observes the live page and never moves the source version (the research plan's search and read refresh it)
        if getattr(self.research, "simulated", False):
            settle = self.ledger.settle_sql(entry, 0.0, provider="stub")      # a stub observation is never a charge
        else:
            reported = getattr(self.research, "last_cost_usd", None)                   # the request price is an estimate; only a provider figure settles
            settle = self.ledger.settle_sql(entry, float(reported) if isinstance(reported, (int, float)) else None, provider="exa")   # no figure: cost_uncertain stays set
        self.ledger.atomically(settle, self._update_sql(tc_id, "completed", result=data, entry=entry))   # settlement and observation in one transaction
        if self._after_complete is not None:
            self._after_complete()                               # test hook: a crash after the completion pair committed
        return {"tool_call_id": tc_id, "tool": tool, "ok": True, "summary": f"{tool} returned {len(data) if isinstance(data, list) else 1} item(s)", "data": data}

    def _stored_read(self, args, canon: dict, fp: str) -> dict:
        """read by source_id and source_version: the next range of the stored text from offset, or the most
        relevant stored passages for query (non-contiguous; no offset advances). Costs nothing: no Exa call, no ledger row, no
        page quota; it takes a stage slot. A source refreshed since (hash differs) returns the new version id and the fact that
        the previous ranges are invalid, never bytes of one version under the identity of another. A source that does not
        exist in this project (deleted or foreign) is a clear rejection. Identical requests replay by fingerprint."""
        from app.core.retrieval import FTS5Retriever
        from app.adapters.research import SourceVersionMismatch
        try:
            found = source_text(self.conn, self.project_id, args.source_id, self.data_dir)
        except SourceVersionMismatch as e:                                     # the bytes do not hash to the recorded version: nothing is served under it
            raise self._reject("read", canon, f"stored text of {args.source_id} failed its version check; nothing returned ({e})")
        if found is None:
            raise self._reject("read", canon, f"source {args.source_id} is not stored in this project (deleted or belongs to another project)")
        current, text = found
        meta = self.conn.execute("SELECT title, url FROM sources WHERE id=?", (args.source_id,)).fetchone()
        head = {"source_id": args.source_id, "source_version": current, "title": meta[0], "url": meta[1], "chars_total": len(text), "untrusted": True, "version_matches": True}   # the bytes hashed to the recorded version (source_text verified them)
        if current != args.source_version:
            data = [dict(head, refreshed=True, requested_version=args.source_version, invalid_ranges=True, text="", range_start=0, range_end=0)]
            summary = f"{args.source_id} was refreshed: version {current[:15]}… replaces {args.source_version[:15]}…; previous ranges invalid"
        elif args.query is not None:
            hits = FTS5Retriever(self.conn).retrieve(self.project_id, args.query, k=8, source_id=args.source_id)
            data = []
            for pid, score in hits:
                r = self.conn.execute("SELECT range_start, range_end, text FROM passages WHERE id=? AND project_id=?", (pid, self.project_id)).fetchone()
                if r:
                    data.append(dict(head, range_start=r[0], range_end=r[1], text=r[2], query=args.query, score=round(score, 4)))
            summary = f"{len(data)} stored passage(s) of {args.source_id} match the query"
        else:
            chars, end, shown, hard = cut_window(text, args.offset, self.observation_chars)
            data = [dict(head, range_start=args.offset, range_end=end, text=chars, shown=shown, hard_cut=hard, end_of_source=args.offset >= len(text))]   # source characters only; the marker is added when shown
            summary = f"stored text of {args.source_id} range {args.offset}-{end} of {len(text)}"
        n = self._next_n(); tc_id = f"{self.step_key}:tool:{n}"
        self._reserve_slot(tc_id, n, "read", canon, fp, existing=False, state="completed", result=data, counts_quota=False)   # stage slot and the stored observation, one transaction, no ledger row
        return {"tool_call_id": tc_id, "tool": "read", "ok": True, "summary": summary, "data": data, "stored": True}
