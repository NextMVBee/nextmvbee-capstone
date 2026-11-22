"""Extraction of claims from a source in batches, with in-batch continuation, per-claim condition continuation, explicit
truncation handling and section-based plan assembly.

Batches: a source is extracted over passage batches of at most research.batch_chars characters, in order, overlapping by
research.batch_overlap_chars so a claim at the end of one batch and its qualification at the start of the next are
seen together. Each batch has a persisted state (extraction_batches: pending, in_progress, completed, needs_review,
claims_found, continuations, pending work units).

Continuation: within one batch, a full response (12 claims) or more_claims true continues the same batch with the identities
already recorded, until a response returns fewer than the cap with more_claims false (completed) or
research.max_continuations_per_batch is reached (needs_review); two continuations in a row that add no new identity stop the
batch (no-progress guard).

Conditions: a claim with more_conditions gets follow-up calls for its remaining conditions against the same source_version and
range, at most research.max_condition_continuations; a quota reached with more_conditions still true keeps the conditions
received and marks the claim and its batch needs_review, so coverage never counts the subject as covered.

Truncation: provider truncation is its own outcome: the position does not advance, the unit is split at a passage boundary, a
single passage gets one ceiling bump within [output].max_bump, then the range is marked needs_review; every response to a
truncation is a decision summary.

Merging and limits: claims merge on the extraction identity of app/core/roles.py with provenance (source_id, source_version,
range, batch_no). Every send goes through the gateway's atomic claim and is bounded by the settings above; the run-level limit
on model calls is enforced outside this module.

Prompt text lives in prompts/runtime/extract_evidence.v5.md; this module only fills its placeholders."""
import json, pathlib, sqlite3
from app.core import react
from app.core.ledger import BudgetExceeded, UnknownCallPending, CallLimitRefused, estimate_tokens
from app.adapters.model_gateway import stage_of
from app.core.retrieval import chunk_ranges, aliases_of
from app.core.roles import extraction_identity
from app.adapters.research import source_text, SourceVersionMismatch
from app.runner.recovery import RunStopped
from schemas.models import EvidenceExtractionOutV2, ConditionContinuationOut, PlanOut, PLAN_SECTIONS, Claim as ClaimRecord
from app.core.discovery import bare_identifier
from app.core.sqlguard import require_guarded

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROMPT_V2 = ROOT / "prompts" / "runtime" / "extract_evidence.v5.md"   # the current version of the extraction prompt (the subject list and subject_ids, the access route as a facet of the identity, the identity kind, bare identifiers); the constant's name is kept for the callers
CLAIM_CAP = 12            # EvidenceExtractionOut.claims max_length: a response at the cap continues the batch
DONE = ("ok", "replayed", "repaired", "fallback")
STOPS = ("budget_stop", "stopped", "credit", "unknown", "failed", "allowance_used", "provider_error", "invalid", "headroom")   # headroom: the call allowance of the stage is spent


def batch_ranges(total_chars: int, batch_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """Consecutive ranges of at most batch_chars, each starting overlap_chars before the previous end."""
    if total_chars <= 0:
        return []
    step = max(1, batch_chars - overlap_chars)
    out, start = [], 0
    while True:
        end = min(start + batch_chars, total_chars)
        out.append((start, end))
        if end >= total_chars:
            return out
        start += step


def load_templates(path: pathlib.Path = PROMPT_V2) -> dict:
    """The two templates of the prompt file: {"batch": {"system", "user"}, "condition": {"system", "user"}}."""
    blocks, text = [], path.read_text(encoding="utf-8")
    parts = text.split("```")
    for i in range(1, len(parts), 2):
        block = parts[i].strip("\n")
        sys_lines, user_lines, mode = [], [], None
        for line in block.splitlines():
            if line.startswith("System: "):
                mode = "s"; sys_lines.append(line[len("System: "):]); continue
            if line.startswith("User: "):
                mode = "u"; user_lines.append(line[len("User: "):]); continue
            (sys_lines if mode == "s" else user_lines).append(line)
        blocks.append({"system": "\n".join(sys_lines), "user": "\n".join(user_lines)})
    return {"batch": blocks[0], "condition": blocks[1]}


def fill(template: str, values: dict) -> str:
    """Replace {name} placeholders for the given keys only; braces inside page text are left alone."""
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def default_batch_messages(ctx: dict, templates: dict | None = None) -> list[dict]:
    t = (templates or load_templates())["batch"]
    vals = dict(ctx, target_facts="; ".join(ctx.get("target_facts") or []), already_recorded=("; ".join(ctx.get("already_recorded") or []) or "none"), subjects=subject_lines(ctx.get("subjects") or []))
    return [{"role": "system", "content": fill(t["system"], vals)}, {"role": "user", "content": fill(t["user"], vals)}]


def subject_lines(subjects: list[dict]) -> str:
    """The subject list as the prompt's user turn carries it: one line per subject with id, kind, priority
    class, evidence status, processing state and key facts; built by the application from coverage, never by prompt text."""
    if not subjects:
        return "none"
    return "\n".join(f"- {x['id']} | {x['type']} | class {x['priority']} | evidence {x.get('evidence_status', 'pending')} | processing {x.get('processing_status', 'not_started')} | {x['label']}: {x['key_facts']}" for x in subjects)


def facts_of(subjects: list[dict]) -> tuple[str, list[str]]:
    """(decision_subject, target_facts) for the prompt's first two lines, derived from the subject list: the research questions'
    decision subjects joined, and every subject's key facts."""
    rq = []
    for x in subjects:
        if x.get("type") == "research_question" and x["label"] not in rq:
            rq.append(x["label"])
    return ("; ".join(rq) or "the idea"), [x["key_facts"] for x in subjects]


def default_condition_messages(ctx: dict, templates: dict | None = None) -> list[dict]:
    t = (templates or load_templates())["condition"]
    vals = dict(ctx, conditions_so_far=("; ".join(ctx.get("conditions_so_far") or []) or "none"), subjects=subject_lines(ctx.get("subjects") or []))   # the full subject list goes in every send, condition follow-ups included
    return [{"role": "system", "content": fill(t["system"], vals)}, {"role": "user", "content": fill(t["user"], vals)}]


def merge_conditions(existing: list[str], new: list[str]) -> list[str]:
    seen = {" ".join(c.lower().split()) for c in existing}
    out = list(existing)
    for c in new:
        k = " ".join(c.lower().split())
        if k and k not in seen:
            seen.add(k); out.append(c.strip())
    return out


class Extractor:
    """Runs the batches of one source version through the gateway. Every model send has its own step key under
    <run>:extract_evidence:<worker>:<branch>:<source>:<version>:b<n>..., so a completed response replays after an
    interruption and the stage ceiling comes from output_ceiling('extract_evidence')."""
    _after_findings = None     # test hook: called after the findings committed and before the batch position advances

    def __init__(self, conn: sqlite3.Connection, gateway, run_id: str, project_id: str, settings: dict, data_dir=None, worker: str = "w0", branch: str = "b0",
                 build_messages=None, build_condition_messages=None, before_dispatch=None):
        require_guarded(conn)
        self.conn, self.gateway, self.run_id, self.project_id, self.data_dir = conn, gateway, run_id, project_id, data_dir
        self.worker, self.branch = worker, branch
        self.before_dispatch = before_dispatch                 # the caller's shared deadline, called before every send (batches and condition follow-ups); it may raise
        r = settings["research"]
        self.batch_chars, self.overlap = int(r["batch_chars"]), int(r["batch_overlap_chars"])
        self.max_cont, self.max_cond = int(r["max_continuations_per_batch"]), int(r["max_condition_continuations"])
        self.bump = float((settings.get("output") or {}).get("max_bump", 1.0))
        self._templates = None
        self.build_messages = build_messages or (lambda ctx: default_batch_messages(ctx, self._tpl()))
        self.build_condition_messages = build_condition_messages or (lambda ctx: default_condition_messages(ctx, self._tpl()))

    def _tpl(self):
        if self._templates is None:
            self._templates = load_templates()
        return self._templates

    # ------------------------------------------------------------ batches
    def plan_batches(self, source_id: str) -> list[dict]:
        """Persist the pending batches of the current source version (idempotent) and return every batch row of that version."""
        from app.repo import Repo
        with Repo(self.conn)._tx():                                                             # the version read and the batch rows of that version in one BEGIN IMMEDIATE (a refresh on another connection waits or lands after)
            found = source_text(self.conn, self.project_id, source_id, self.data_dir)           # raises SourceVersionMismatch when the bytes fail their version check
            if found is None:
                raise ValueError(f"source {source_id} is not stored in project {self.project_id}")
            version, text = found
            ranges = batch_ranges(len(text), self.batch_chars, self.overlap)
            for i, (a, b) in enumerate(ranges, start=1):
                self.conn.execute("INSERT INTO extraction_batches (project_id, source_id, source_version, batch_no, range_start, range_end) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                                  (self.project_id, source_id, version, i, a, b))
            return self.batches(source_id, version)

    def batches(self, source_id: str, version: str) -> list[dict]:
        cur = self.conn.execute("SELECT project_id, source_id, source_version, batch_no, range_start, range_end, status, claims_found, continuations, units_json, no_progress, review_reason, responses_json "
                                "FROM extraction_batches WHERE project_id=? AND source_id=? AND source_version=? ORDER BY batch_no", (self.project_id, source_id, version))
        cols = ("project_id", "source_id", "source_version", "batch_no", "range_start", "range_end", "status", "claims_found", "continuations", "units_json", "no_progress", "review_reason", "responses_json")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def extract_source(self, source_id: str, subjects: list[dict]) -> dict:
        """Run every unfinished batch of the current version in order with the run's full subject list as the input of every
        send ([{"id", "type", "label", "key_facts", "hard", "priority", "evidence_status", "processing_status"}]).
        Stops at the first batch that cannot finish (budget, run stop, unknown call, headroom) and reports where it stopped;
        completed batches stay completed."""
        decision = self._injection_decision(source_id)
        if decision == "exclude":                                                                # the founder excluded the flagged source: not read again, nothing sent
            return {"status": "excluded", "source_id": source_id, "detail": "excluded by the founder's decision on its instruction-like text", "batches": [], "claims": 0}
        if decision is None and self._flagged(source_id):                                        # a flagged page the founder has not decided on is not read, at this version or a refreshed one
            return {"status": "flagged", "source_id": source_id, "detail": "flagged for instruction-like text; excluded as a whole until the founder decides; not read", "batches": [], "claims": 0}
        try:
            rows = self.plan_batches(source_id)
        except SourceVersionMismatch as e:
            return {"status": "failed", "source_id": source_id, "detail": str(e), "batches": [], "claims": 0}
        self.sync_aliases(source_id)                                                            # aliases that arrived after the last extraction join the stored claims' provenance; no send
        statuses = []
        for b in rows:
            if b["status"] in ("completed", "needs_review"):
                statuses.append(b["status"]); continue
            st = self.run_batch(b, subjects)
            statuses.append(st)
            if st == "flagged":                                                                  # the page is excluded as a whole; the flag closed its remaining batches, none is read
                return {"status": "flagged", "source_id": source_id, "source_version": b["source_version"], "detail": "flagged for instruction-like text; excluded as a whole until the founder decides", "batches": statuses, "claims": 0}
            if st in STOPS:
                return {"status": st, "source_id": source_id, "source_version": b["source_version"], "batches": statuses, "stopped_at_batch": b["batch_no"], "claims": self._claims_of(source_id, b["source_version"])}
        final = "needs_review" if "needs_review" in statuses else "completed"
        return {"status": final, "source_id": source_id, "source_version": rows[0]["source_version"] if rows else None, "batches": statuses, "claims": self._claims_of(source_id, rows[0]["source_version"]) if rows else 0}

    def _injection_decision(self, source_id: str) -> str | None:
        """The founder's decision on a flagged source (sources.injection_decision, migration 071), None while undecided or on a store without the column."""
        if "injection_decision" not in {r[1] for r in self.conn.execute("PRAGMA table_info(sources)")}:
            return None
        row = self.conn.execute("SELECT injection_decision FROM sources WHERE id=? AND project_id=?", (source_id, self.project_id)).fetchone()
        return row[0] if row else None

    def _flagged(self, source_id: str) -> bool:
        row = self.conn.execute("SELECT injection_flag FROM sources WHERE id=? AND project_id=?", (source_id, self.project_id)).fetchone()
        return bool(row and row[0])

    def _exclude_flagged(self, b: dict, reason: str) -> None:
        """The flagged page is excluded as a whole, in one transaction: the source flagged; the claims of this version that the
        page alone holds removed (a claim also found on another page keeps its row and is judged there); the current batch closed
        needs_review with the reason and the version's other unfinished batches closed with the exclusion named, every batch's claim count 0.
        The founder's include deletes the batches so the page is read again whole; exclude keeps it out."""
        sid, version = b["source_id"], b["source_version"]
        with self.conn:
            self.conn.execute("UPDATE sources SET injection_flag=1 WHERE id=? AND project_id=?", (sid, self.project_id))
            for cid, pj in self.conn.execute("SELECT id, provenance_json FROM claims WHERE project_id=? AND source_id=? AND source_version=?", (self.project_id, sid, version)).fetchall():
                if all(p.get("source_id") == sid for p in json.loads(pj or "[]")):
                    self.conn.execute("DELETE FROM claims_fts WHERE claim_id=?", (cid,)); self.conn.execute("DELETE FROM claims WHERE id=? AND project_id=?", (cid, self.project_id))
            self.conn.execute("UPDATE extraction_batches SET status='needs_review', units_json='[]', review_reason=?, claims_found=0, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND source_id=? AND source_version=? AND batch_no=?",
                              (reason, self.project_id, sid, version, b["batch_no"]))
            self.conn.execute("UPDATE extraction_batches SET status=CASE WHEN status='completed' THEN status ELSE 'needs_review' END, units_json=CASE WHEN status='completed' THEN units_json ELSE '[]' END, review_reason=COALESCE(review_reason, ?), claims_found=0, "
                              "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND source_id=? AND source_version=? AND batch_no<>?",
                              (f"excluded: instruction-like text elsewhere in the page (batch {b['batch_no']}); not read until the founder decides", self.project_id, sid, version, b["batch_no"]))

    def _claims_of(self, source_id, version) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM claims WHERE project_id=? AND source_id=? AND source_version=?", (self.project_id, source_id, version)).fetchone()[0])

    def _base(self, b) -> str:
        return f"{self.run_id}:extract_evidence:{self.worker}:{self.branch}:{b['source_id']}:{b['source_version'][7:15]}:b{b['batch_no']}"

    def _recorded(self, b) -> list[str]:
        """Identities already recorded from this batch (provenance carries the batch number), as short labels for the prompt."""
        out = []
        for ident, prov in self.conn.execute("SELECT identity_key, provenance_json FROM claims WHERE project_id=? AND source_id=? AND source_version=? ORDER BY id", (self.project_id, b["source_id"], b["source_version"])):
            if any(p.get("batch_no") == b["batch_no"] for p in json.loads(prov or "[]")):
                body = ident.split("sav:", 1)[1] if "sav:" in (ident or "") else (ident.split("tq:", 1)[1] if "tq:" in (ident or "") else (ident or ""))   # the label after the identity's product head and tag
                out.append(body.replace("|", " / "))
        return [x for x in out if x]

    def _persist(self, b, units, c, no_progress, status=None, reason=None):
        with self.conn:
            self.conn.execute("UPDATE extraction_batches SET units_json=?, continuations=?, no_progress=?, status=COALESCE(?, status), review_reason=COALESCE(?, review_reason), "
                              "claims_found=(SELECT COUNT(*) FROM claims WHERE project_id=? AND source_id=? AND source_version=? AND provenance_json LIKE ?), updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                              "WHERE project_id=? AND source_id=? AND source_version=? AND batch_no=?",
                              (json.dumps(units), c, no_progress, status, reason, self.project_id, b["source_id"], b["source_version"], f'%"batch_no": {b["batch_no"]}%',
                               self.project_id, b["source_id"], b["source_version"], b["batch_no"]))

    def _split_point(self, text: str, a: int, e: int) -> int | None:
        """The passage boundary nearest the middle of the unit, or None when the unit is a single passage."""
        starts = [s for s, _ in chunk_ranges(text) if a < s < e]
        return min(starts, key=lambda s: abs(s - (a + e) / 2)) if starts else None

    def _entry_of(self, step: str) -> str:
        """The real ledger entry id of the attempt of this step that was settled truncated (resume paths summarise against it)."""
        r = self.conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND step_key=? AND kind='model' AND outcome='truncated' ORDER BY attempt DESC LIMIT 1", (self.run_id, step)).fetchone()
        if not r:
            raise ValueError(f"no truncated attempt recorded for {step}")
        return r[0]

    def _rec(self, b, step, entry, purpose, action, observation, change):
        react.record(self.conn, self.run_id, step, entry, purpose, action, observation, change)   # entry is always a real ledger id; react.record refuses an empty one

    def _apply_truncation(self, b, step, text, units, a, e, bumped, reasons, entry, detail):
        """The deterministic strategy for a truncated unit, derived from the unit itself: split at a passage boundary when
        the unit spans several passages, otherwise one ceiling bump, otherwise needs_review. Used both for a fresh truncated
        response and, on resume, for a unit whose step was settled truncated before the crash (the ledger outcome is the
        durable fact; the cursor cache in units_json is derived from it, never the other way round)."""
        mid = self._split_point(text, a, e)
        if mid is not None:
            units = [[a, mid, False], [mid, e, False]] + units[1:]
            self._rec(b, step, entry, f"extract batch {b['batch_no']} range {a}-{e}", "truncated: split the unit at a passage boundary", detail, f"re-extract {a}-{mid} and {mid}-{e}; position not advanced")
        elif not bumped:
            units = [[a, e, True]] + units[1:]
            self._rec(b, step, entry, f"extract batch {b['batch_no']} single passage {a}-{e}", "truncated: raise the stage ceiling once", detail, f"retry the passage at {self.bump}x the ceiling (settings [output].max_bump); position not advanced")
        else:
            reasons.append(f"passage {a}-{e} still truncated after the split and one ceiling bump")
            units = units[1:]
            self._rec(b, step, entry, f"extract batch {b['batch_no']} single passage {a}-{e}", "truncated after split and bump: needs review", detail, "passage marked needs_review; extraction continues with the next unit")
        return units

    def _pending_conditions(self, b) -> list[str]:
        """Claims of this batch whose condition extraction is still in progress (persisted; resumed before the batch may complete)."""
        return [cid for cid, prov in self.conn.execute("SELECT id, provenance_json FROM claims WHERE project_id=? AND source_id=? AND source_version=? AND conditions_status='in_progress' ORDER BY id",
                                                        (self.project_id, b["source_id"], b["source_version"])) if any(p.get("batch_no") == b["batch_no"] for p in json.loads(prov or "[]"))]

    def _subjects_now(self, subjects: list[dict]) -> list[dict]:
        """The evidence status and processing state of every listed subject re-read from coverage before a
        send; the ids and key facts are the list's. A subject without a coverage row keeps the list's values."""
        if not subjects:
            return subjects
        from app.core import coverage
        with coverage.derived(self.conn, self.project_id):
            rows = {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT subject_id, evidence_status, processing_status FROM coverage WHERE project_id=?", (self.project_id,)).fetchall()}
        return [dict(x, evidence_status=rows[x["id"]][0], processing_status=rows[x["id"]][1]) if x["id"] in rows else x for x in subjects]

    def sync_aliases(self, source_id: str) -> int:
        """An alias that arrives after the canonical's extraction joins the provenance
        entries of the same content version only (the entry's source_version is the hash the alias shares); alias ids are added,
        never removed, and the entries of earlier versions are never rewritten. No send. Returns the claims updated."""
        from app.repo import Repo
        n = 0
        with Repo(self.conn)._tx():                                                             # the version read, the alias set and the provenance read share the write's BEGIN IMMEDIATE; a refresh on another connection is refused while they are open or lands after the commit
            cur = self.conn.execute("SELECT content_hash FROM sources WHERE id=? AND project_id=?", (source_id, self.project_id)).fetchone()
            if not cur:
                return 0
            aliases = set(aliases_of(self.conn, self.project_id, source_id))                   # the rows sharing the source's current content
            rows = self.conn.execute("SELECT id, provenance_json FROM claims WHERE project_id=? AND provenance_json LIKE ?", (self.project_id, f'%"source_id": "{source_id}"%')).fetchall()
            for cid, pj in rows:
                provs = json.loads(pj or "[]"); changed = False
                for e in provs:
                    if e.get("source_id") == source_id and e.get("source_version") == cur[0]:
                        merged = sorted(set(e.get("aliases") or []) | aliases)
                        if merged != sorted(e.get("aliases") or []):
                            e["aliases"] = merged; changed = True
                if changed:
                    self.conn.execute("UPDATE claims SET provenance_json=? WHERE id=?", (json.dumps(provs), cid)); n += 1
        return n

    def _finish_conditions(self, b, text, decision_subject, target_facts, reasons, subjects=()) -> str | None:
        """Run every pending condition follow-up of the batch; a stop status is returned, needs_review joins the reasons."""
        for cid in self._pending_conditions(b):
            st = self._conditions(b, cid, text, decision_subject, target_facts, subjects)
            if st in STOPS:
                return st
            if st == "needs_review":
                reasons.append(f"{cid}: condition extraction incomplete (more_conditions still true after {self.max_cond} follow-up(s), or a failure shape)")
        return None

    @staticmethod
    def _no_progress_from(responses: list[dict]) -> int:
        """Consecutive most-recent full responses that added nothing, from the persisted outcomes."""
        n = 0
        for r in reversed(responses):
            if r.get("full") and r.get("new", 0) == 0:
                n += 1
            else:
                break
        return n

    def run_batch(self, b: dict, subjects: list[dict]) -> str:
        decision_subject, target_facts = facts_of(subjects)
        valid_ids = {x["id"] for x in subjects}                                           # the alias ids of the batch's content version are read inside _store's write transaction
        try:
            found = source_text(self.conn, self.project_id, b["source_id"], self.data_dir)
        except SourceVersionMismatch as e:
            self._persist(b, json.loads(b["units_json"] or "[]"), b["continuations"], b["no_progress"], "needs_review", f"stored bytes failed their version check ({e}); nothing extracted from them")
            return "needs_review"
        if found is None or found[0] != b["source_version"]:
            self._persist(b, [], b["continuations"], b["no_progress"], "needs_review", "source refreshed or deleted before this batch finished; re-extract the new version")
            return "needs_review"
        text = found[1]
        units = [[b["range_start"], b["range_end"], False]] if b["status"] == "pending" else json.loads(b["units_json"] or "[]")   # a started batch with no unit left has only condition work left; it never re-extracts its range
        c, no_progress, reasons = int(b["continuations"]), int(b["no_progress"]), ([b["review_reason"]] if b["review_reason"] else [])
        if b["status"] == "pending":
            self._persist(b, units, c, no_progress, "in_progress")
        base = self._base(b)
        meta = dict(source_id=b["source_id"], source_version=b["source_version"], batch_no=b["batch_no"], decision_subject=decision_subject, target_facts=target_facts, subjects=subjects)
        stopped = self._finish_conditions(b, text, decision_subject, target_facts, reasons, subjects)       # condition work left pending by an earlier stop runs first
        if stopped:
            self._persist(b, units, c, no_progress, None, "; ".join(reasons) if reasons else None); return stopped
        while units:
            a, e, bumped = units[0]
            step = f"{base}:c{c}:u{a}-{e}" + (":bump" if bumped else "")
            if self.gateway.ledger.truncated(step):                                        # settled truncated before an interruption: apply the strategy, never repeat the send
                units = self._apply_truncation(b, step, text, units, a, e, bumped, reasons, self._entry_of(step), "truncated outcome found in the ledger on resume")
                self._persist(b, units, c, no_progress, None, "; ".join(reasons) if reasons else None)
                continue
            ctx = dict(meta, range_start=a, range_end=e, text=text[a:e], already_recorded=self._recorded(b), continuation=c, subjects=self._subjects_now(subjects))   # the subject list as coverage reads it before this send
            msgs = self.build_messages(ctx)
            if self.before_dispatch is not None and self.gateway.ledger.completed_result(step, "model") is None:   # a stored result replays before any check
                self.before_dispatch("extract")
            try:
                r = self.gateway.call(step, EvidenceExtractionOutV2, msgs, estimate_tokens(msgs), bump=self.bump if bumped else None, before_dispatch=self.before_dispatch)   # the deadline runs again inside the gateway before repairs and fallbacks
            except BudgetExceeded:
                self._persist(b, units, c, no_progress); return "budget_stop"           # findings so far stay; the batch resumes from this unit
            except CallLimitRefused:
                self._persist(b, units, c, no_progress); return "headroom"             # the stage's call allowance is spent; the remaining work stays explicit
            except RunStopped:
                self._persist(b, units, c, no_progress); return "stopped"
            except UnknownCallPending:
                self._persist(b, units, c, no_progress); return "unknown"              # an interrupted send of this unit awaits the founder; never resent on its own
            if r["status"] == "truncated":                                                 # the outcome is already durable with the settlement; the strategy follows from the unit
                units = self._apply_truncation(b, step, text, units, a, e, bumped, reasons, r.get("entry"), r.get("detail", "output stopped at the ceiling"))
                self._persist(b, units, c, no_progress, None, "; ".join(reasons) if reasons else None)
                continue
            if r["status"] not in DONE:
                self._persist(b, units, c, no_progress)
                return r["status"] if r["status"] in STOPS else "failed"
            out = r["output"]
            if out.injection_suspected and self._injection_decision(b["source_id"]) == "include":   # the founder read the page and included it; its claims are kept and it is never flagged again
                self._rec(b, step, r.get("entry"), f"extract batch {b['batch_no']} range {a}-{e}", "injection suspected, source included by the founder", "the model flagged instruction-like text; the founder's decision stands", "claims kept; the source stays unflagged")
            elif out.injection_suspected:                                                    # the page is data that tried to instruct: excluded from evidence as a whole: flagged, the claims this version stored before the flag removed, every batch of the version closed, nothing more read until the founder decides
                reasons.append(f"range {a}-{e}: injection suspected; the page excluded as a whole, nothing extracted from it")
                self._exclude_flagged(b, "; ".join(reasons))
                self._rec(b, step, r.get("entry"), f"extract batch {b['batch_no']} range {a}-{e}", "injection suspected", "the model flagged instruction-like text; no claim stored", "source flagged; the page excluded whole: its stored claims removed, its remaining batches closed; no further read until the founder decides")
                return "flagged"
            if out.failure_reason:
                reasons.append(f"range {a}-{e}: model could not comply ({out.failure_reason[:120]})"); units = units[1:]
                self._rec(b, step, r.get("entry"), f"extract batch {b['batch_no']} range {a}-{e}", "failure shape returned", out.failure_reason[:200], "unit marked needs_review; next unit")
                self._persist(b, units, c, no_progress, None, "; ".join(reasons)); continue
            full = len(out.claims) >= CLAIM_CAP or out.more_claims
            outcome, claim_ids, rejected, responses = self._store(b, a, e, step, out.claims, text, decision_subject, full, valid_ids)
            new_ids = ["persisted"] * outcome["new"]
            if self._after_findings is not None:
                self._after_findings()                                                   # test hook: a crash between the stored findings and the advanced position
            c += 1
            if full:
                no_progress = self._no_progress_from(responses)                          # from the persisted outcomes: a replayed response never increments the guard
                if no_progress >= 2:
                    reasons.append(f"no progress: two continuations in a row returned only already-recorded identities (range {a}-{e})"); units = units[1:]
                    change = "no-progress guard: unit stops with needs_review; no further send"
                elif c - 1 >= self.max_cont:
                    reasons.append(f"continuation limit {self.max_cont} reached with more claims reported (range {a}-{e})"); units = units[1:]
                    change = "continuation limit reached: unit stops with needs_review"
                else:
                    change = "continue the same unit with the recorded identities"
            else:
                no_progress = 0; units = units[1:]
                change = "unit done: fewer than the cap and more_claims false"
            self._rec(b, step, r.get("entry"), f"extract batch {b['batch_no']} response {c} range {a}-{e}", "store findings" + (" (replayed)" if r["status"] == "replayed" else ""),
                      f"{len(out.claims)} claim(s), {len(new_ids)} new, {len(rejected)} rejected (quote not in text), {outcome.get('refused_unlinked', 0)} refused (no valid subject id), more_claims={out.more_claims}", change)
            self._persist(b, units, c, no_progress, None, "; ".join(reasons) if reasons else None)           # the position advances only after the findings are durable
            stopped = self._finish_conditions(b, text, decision_subject, target_facts, reasons, subjects)
            if stopped:
                self._persist(b, units, c, no_progress, None, "; ".join(reasons) if reasons else None); return stopped
            if reasons:
                self._persist(b, units, c, no_progress, None, "; ".join(reasons))
        stopped = self._finish_conditions(b, text, decision_subject, target_facts, reasons, subjects)       # a batch never completes while a claim's conditions are in progress
        if stopped or self._pending_conditions(b):
            self._persist(b, [], c, no_progress, None, "; ".join(reasons) if reasons else None); return stopped or "failed"
        final = "needs_review" if reasons else "completed"
        self._persist(b, [], c, no_progress, final, "; ".join(reasons) if reasons else None)
        return final

    def _store(self, b, a, e, step, claims, text, decision_subject, full, valid_ids=frozenset()):
        """Store the claims of one response: quote located in the stored text (rejected when absent), merged on the extraction
        identity with provenance, conditions accumulated; new claims get CLM ids through the repository. The response's
        outcome (claims, new identities, more) is written into the batch's responses_json in the same transaction, and a
        response already recorded there (a replay after a crash) reuses its outcome instead of counting zero new.
        A claim re-found in a newer source version moves to that version and loses its verdict until re-verified."""
        from app.repo import Repo
        repo, new, ids, rejected, refused = Repo(self.conn), [], [], [], 0
        with repo._tx():
            aliases = [r[0] for r in self.conn.execute("SELECT id FROM sources WHERE project_id=? AND content_hash=? AND id<>? ORDER BY id", (self.project_id, b["source_version"], b["source_id"])).fetchall()]   # the rows sharing this batch's content version, read inside the write transaction
            responses = json.loads(self.conn.execute("SELECT responses_json FROM extraction_batches WHERE project_id=? AND source_id=? AND source_version=? AND batch_no=?",
                                                     (self.project_id, b["source_id"], b["source_version"], b["batch_no"])).fetchone()[0] or "[]")
            prior = next((x for x in responses if x.get("step") == step), None)
            for cl in claims:
                sids = [x for x in (getattr(cl, "subject_ids", None) or []) if x in valid_ids]
                if not sids:                                                              # a claim with no valid subject id is refused before storage and counted; nothing else about it is kept
                    refused += 1; continue
                q = (cl.quote or "").strip()
                pos = text.find(q, a, e) if q else -1
                if pos < 0 and q:
                    pos = text.find(q)                                                    # the overlap of a neighbouring batch
                if not q or pos < 0:
                    rejected.append(cl.text[:80]); continue
                cl = cl.model_copy(update={k: bare_identifier(getattr(cl, k, "")) for k in ("subject", "vendor", "product", "product_version", "access_route")})   # an identifier field is bare at the write (no backticks or quotes), before the identity and the merge key read it
                ident = extraction_identity(cl.model_dump())
                prov = {"source_id": b["source_id"], "source_version": b["source_version"], "range_start": pos, "range_end": pos + len(q), "batch_no": b["batch_no"], "subject_ids": sids}
                if aliases:
                    prov["aliases"] = list(aliases)                                        # the same content under these other addresses, never counted as independent evidence
                row = self.conn.execute("SELECT id, conditions_json, provenance_json, conditions_status, source_version FROM claims WHERE project_id=? AND identity_key=?", (self.project_id, ident)).fetchone()
                if row:
                    conds = merge_conditions(json.loads(row[1]), list(cl.conditions))
                    provs = json.loads(row[2] or "[]")
                    if prov not in provs:
                        provs.append(prov)
                    cstatus = "in_progress" if (cl.more_conditions and row[3] == "complete") else row[3]
                    self.conn.execute("UPDATE claims SET conditions_json=?, provenance_json=?, conditions_status=? WHERE id=?", (json.dumps(conds), json.dumps(provs), cstatus, row[0]))
                    if row[4] != b["source_version"]:                                        # re-found in a newer version, or in a later source (which may attach a condition): the quote now lives there; the old verdict no longer applies
                        self.conn.execute("UPDATE claims SET source_id=?, source_version=?, quote=?, range_start=?, range_end=?, batch_no=?, verdict=NULL, scope_status='unchecked', verified_requirement_version=NULL, checked_at=NULL, invalidated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                                          (b["source_id"], b["source_version"], q[:600], pos, pos + len(q), b["batch_no"], row[0]))   # the verdict is cleared (a base write); the status is derived by the reconciliation, requested by the derivation_pending flag below; the later source's exact quote travels with its range
                        self.conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (self.project_id,))
                    ids.append(row[0]); continue
                cid = repo.next_id(self.project_id, "CLM")
                subject_label = (cl.decision_subject or decision_subject or "")[:200]           # the model names the decision subject of each claim; the batch's subject is the fallback
                from app.core.coverage import product_identity
                product_id, product_version = product_identity(cl.vendor, cl.product, cl.product_version, cl.subject)   # explicit identity, normalised by code
                repo.upsert_claim(ClaimRecord(id=cid, project_id=self.project_id, text=cl.text, kind=cl.kind, source_id=b["source_id"], quote=q[:600],
                                              subject=cl.subject or None, attribute=cl.attribute or None, value=cl.value or None, conditions=list(cl.conditions)))
                self.conn.execute("UPDATE claims SET identity_key=?, source_version=?, range_start=?, range_end=?, batch_no=?, provenance_json=?, conditions_status=?, decision_subject=?, product_id=?, product_version=?, access_route=?, identity_kind=? WHERE id=?",
                                  (ident, b["source_version"], pos, pos + len(q), b["batch_no"], json.dumps([prov]), "in_progress" if cl.more_conditions else "complete", subject_label, product_id, product_version,
                                   (getattr(cl, "access_route", "") or "")[:120], getattr(cl, "identity_kind", "product") or "product", cid))   # the access route as a facet of the identity, and the identity kind
                new.append(ident); ids.append(cid)
            if new or ids:
                self.conn.execute("UPDATE projects SET derivation_pending=1 WHERE id=?", (self.project_id,))   # a base write without the derivation in this transaction: the derivation is marked pending
            if prior is None:
                prior = {"step": step, "range": [a, e], "claims": len(claims), "new": len(new), "rejected": len(rejected), "refused_unlinked": refused, "full": bool(full)}
                responses.append(prior)
                self.conn.execute("UPDATE extraction_batches SET responses_json=? WHERE project_id=? AND source_id=? AND source_version=? AND batch_no=?",
                                  (json.dumps(responses), self.project_id, b["source_id"], b["source_version"], b["batch_no"]))
        return prior, ids, rejected, responses

    def _conditions(self, b, claim_id: str, text: str, decision_subject: str, target_facts: list[str], subjects=()) -> str:
        """Follow-up calls for the remaining conditions of one claim against the same source_version and range, at most
        research.max_condition_continuations. Conditions accumulate on the stored claim; the limit reached with
        more_conditions still true leaves the claim needs_review (coverage then never counts its subject as covered)."""
        row = self.conn.execute("SELECT text, conditions_json, range_start, range_end, conditions_status FROM claims WHERE id=? AND project_id=?", (claim_id, self.project_id)).fetchone()
        if not row or row[4] != "in_progress":
            return row[4] if row else "complete"
        base = self._base(b); num = claim_id.rsplit("-", 1)[-1]
        a, e = b["range_start"], b["range_end"]
        last_entry = None
        for k in range(1, self.max_cond + 1):
            step = f"{base}:cl{num}:k{k}"
            if self.gateway.ledger.truncated(step):                                        # settled truncated before an interruption: no resend at the same range and ceiling
                with self.conn:
                    self.conn.execute("UPDATE claims SET conditions_status='needs_review' WHERE id=?", (claim_id,))
                self._rec(b, step, self._entry_of(step), f"conditions of {claim_id} follow-up {k}", "truncated outcome found in the ledger on resume", "the follow-up was cut at the ceiling before the interruption", "claim marked needs_review; conditions received are kept; no resend")
                return "needs_review"
            conds = json.loads(self.conn.execute("SELECT conditions_json FROM claims WHERE id=?", (claim_id,)).fetchone()[0])
            ctx = dict(claim_id=claim_id, claim_text=row[0], conditions_so_far=conds, source_id=b["source_id"], source_version=b["source_version"], range_start=a, range_end=e, text=text[a:e],
                       decision_subject=decision_subject, target_facts=target_facts, follow_up=k, subjects=self._subjects_now(list(subjects)))   # the full subject list in a condition send too
            msgs = self.build_condition_messages(ctx)
            if self.before_dispatch is not None and self.gateway.ledger.completed_result(step, "model") is None:
                self.before_dispatch("conditions")
            try:
                r = self.gateway.call(step, ConditionContinuationOut, msgs, estimate_tokens(msgs), before_dispatch=self.before_dispatch)
            except BudgetExceeded:
                return "budget_stop"
            except CallLimitRefused:
                return "headroom"
            except RunStopped:
                return "stopped"
            except UnknownCallPending:
                return "unknown"
            if r["status"] == "truncated":
                with self.conn:
                    self.conn.execute("UPDATE claims SET conditions_status='needs_review' WHERE id=?", (claim_id,))
                self._rec(b, step, r.get("entry"), f"conditions of {claim_id} follow-up {k}", "truncated", r.get("detail", "output stopped at the ceiling"), "claim marked needs_review; conditions received are kept")
                return "needs_review"
            if r["status"] not in DONE:
                return r["status"] if r["status"] in STOPS else "failed"
            last_entry = r.get("entry")
            out = r["output"]
            if out.failure_reason:                                                           # a failure shape is a failure, never a completed extraction
                with self.conn:
                    self.conn.execute("UPDATE claims SET conditions_status='needs_review' WHERE id=?", (claim_id,))
                self._rec(b, step, r.get("entry"), f"conditions of {claim_id} follow-up {k}", "failure shape returned", out.failure_reason[:200], "claim marked needs_review; conditions received are kept")
                return "needs_review"
            merged = merge_conditions(conds, list(out.conditions))
            done = not out.more_conditions
            with self.conn:
                self.conn.execute("UPDATE claims SET conditions_json=?, conditions_status=? WHERE id=?", (json.dumps(merged), "complete" if done else "in_progress", claim_id))
            self._rec(b, step, r.get("entry"), f"conditions of {claim_id} follow-up {k}", "store conditions", f"{len(out.conditions)} received, {len(merged)} total, more_conditions={out.more_conditions}",
                      "condition extraction complete" if done else "ask again for the remaining conditions")
            if done:
                return "complete"
        with self.conn:
            self.conn.execute("UPDATE claims SET conditions_status='needs_review' WHERE id=?", (claim_id,))
        self._rec(b, f"{base}:cl{num}:k{self.max_cond}", last_entry, f"conditions of {claim_id}", "condition continuation limit reached", f"more_conditions still true after {self.max_cond} follow-up(s)", "claim and batch needs_review; subject not covered until reviewed")   # linked to the last real attempt
        return "needs_review"


# ---------------------------------------------------------------- truncation outcomes
def truncated_entry(conn: sqlite3.Connection, run_id: str, step_key: str) -> str | None:
    """The ledger entry id of the attempt of this step that was settled with the typed outcome truncated (written in the
    settlement transaction by the gateway), or None. The strategy is derived from it: sections for a whole plan,
    needs_review for a section, split, bump or needs_review for an extraction unit."""
    require_guarded(conn)
    r = conn.execute("SELECT id FROM usage_ledger WHERE run_id=? AND step_key=? AND kind='model' AND outcome='truncated' ORDER BY attempt DESC LIMIT 1", (run_id, step_key)).fetchone()
    return r[0] if r else None


def truncated_outcome(conn: sqlite3.Connection, run_id: str, step_key: str) -> bool:
    require_guarded(conn)
    return truncated_entry(conn, run_id, step_key) is not None


def record_strategy(conn: sqlite3.Connection, run_id: str, step_key: str, entry: str, purpose: str, action: str, observation: str, change: str) -> bool:
    """The caller's strategy for a truncated call as a decision summary against the original ledger entry id, written once:
    the fresh branch and every resume branch call this, and a summary with the same action for the same entry is never
    duplicated. Distinct from the gateway's own summary of the send, which is written with the
    settlement. Returns True when a record was written."""
    require_guarded(conn)
    if conn.execute("SELECT 1 FROM reasoning_records WHERE run_id=? AND action_id=? AND action=? LIMIT 1", (run_id, entry, action)).fetchone():
        return False
    react.record(conn, run_id, step_key, entry, purpose, action, observation, change)
    return True


# ---------------------------------------------------------------- section-based plan assembly
SECTION_ORDER = ("scope", "components", "tasks", "acceptance")


def assemble_plan(sections: dict) -> PlanOut:
    """One PlanOut from the four section outputs (models or dicts)."""
    d = {}
    for name in SECTION_ORDER:
        sec = sections[name]
        sec = sec.model_dump() if hasattr(sec, "model_dump") else dict(sec)
        sec.pop("failure_reason", None)
        d.update(sec)
    return PlanOut(**d)


def validate_assembled_plan(plan: PlanOut, conn=None, project_id: str | None = None) -> list[dict]:
    """The Reviewer's consistency sweep over an assembled plan before it is stored: internal references between sections
    (tasks to components, acceptance to tasks, dependencies, duplicate keys) plus the stored-record sweep of
    app/core/invalidation.py when a connection is given. Empty list: consistent."""
    if conn is not None:
        require_guarded(conn)
    issues = []
    comp_keys = [c.key for c in plan.components]; task_keys = [t.key for t in plan.tasks]
    for keys, what in ((comp_keys, "component"), (task_keys, "task")):
        for k in sorted({k for k in keys if keys.count(k) > 1}):
            issues.append({"id": k, "issue": f"duplicate {what} key"})
    for t in plan.tasks:
        if t.component_key not in comp_keys:
            issues.append({"id": t.key, "issue": f"task names component {t.component_key!r} that no section defines"})
        for dep in t.depends_on:
            if dep not in task_keys:
                issues.append({"id": t.key, "issue": f"task depends on {dep!r}, which no section defines"})
    for i, a in enumerate(plan.acceptance):
        if a.task_key not in task_keys:
            issues.append({"id": f"acceptance[{i}]", "issue": f"acceptance test names task {a.task_key!r} that no section defines"})
    for c in plan.components:
        if c.key not in {t.component_key for t in plan.tasks}:
            issues.append({"id": c.key, "issue": "component has no task"})
    if conn is not None and project_id:
        from app.core.invalidation import consistency_sweep, blocking_issues
        issues += blocking_issues(consistency_sweep(conn, project_id))
    return issues


def plan_in_sections(gateway, step_key_base: str, build_section_messages, conn=None, project_id: str | None = None, run_id: str | None = None, finishing: bool = True, before_dispatch=None) -> dict:
    """Produce the plan section by section (scope, components, tasks, acceptance), each under the plan_generate ceiling,
    assemble it and validate it with the consistency sweep. build_section_messages(name, sections_so_far) returns the
    messages for a section. Planning is a finishing step: finishing=True lets the reservation use the completion reserve.
    A section whose truncation was already recorded is not sent again. The caller stores the plan only
    when issues is empty."""
    if conn is not None:
        require_guarded(conn)
    sections = {}
    for name in SECTION_ORDER:
        step = f"{step_key_base}:{name}"
        prior = truncated_entry(conn, run_id, step) if (conn is not None and run_id) else None
        if prior:                                                                      # settled truncated before an interruption: the strategy is derived, recorded once, nothing resent
            record_strategy(conn, run_id, step, prior, f"plan section {name}", "needs_review", "section output stopped at the ceiling (typed outcome found on resume)", "section returned for review; plan not stored; no resend")
            return {"status": "truncated", "section": name, "sections": sections, "detail": "section settled truncated earlier; not repeated"}
        msgs = build_section_messages(name, sections)
        if before_dispatch is not None and gateway.ledger.completed_result(step, "model") is None:   # a stored section replays before any check
            before_dispatch(f"section {name}")
        r = gateway.call(step, PLAN_SECTIONS[name], msgs, estimate_tokens(msgs), finishing=finishing, before_dispatch=before_dispatch)
        if r["status"] == "truncated":
            if conn is not None and run_id:
                record_strategy(conn, run_id, step, r["entry"], f"plan section {name}", "needs_review", r.get("detail", "output stopped at the ceiling"), "section returned for review; plan not stored; no identical retry")
            return {"status": "truncated", "section": name, "sections": sections, "detail": r.get("detail")}
        if r["status"] not in DONE:
            return {"status": r["status"], "section": name, "sections": sections, "detail": r.get("detail")}
        if r["output"].failure_reason:
            return {"status": "failed", "section": name, "sections": sections, "detail": r["output"].failure_reason}
        sections[name] = r["output"]
    plan = assemble_plan(sections)
    issues = validate_assembled_plan(plan, conn, project_id)
    return {"status": "ok" if not issues else "inconsistent", "plan": plan, "issues": issues, "sections": list(SECTION_ORDER)}


def generate_plan(gateway, step_key: str, messages: list[dict], build_section_messages, conn=None, project_id: str | None = None, run_id: str | None = None, finishing: bool = True, before_dispatch=None) -> dict:
    """The Planner's call: one whole-plan send under output_ceiling('plan_generate'), with finishing=True so the completion
    reserve is usable; when the provider truncates it, the plan is
    produced in sections instead of retried identically. A re-run after recovery finds the typed ledger outcome
    truncated, written in the settlement transaction, derives the strategy sections from it and sends nothing for the
    whole plan again. A failure shape is a failure. Every truncation is
    a decision summary."""
    if conn is not None:
        require_guarded(conn)
    prior = truncated_entry(conn, run_id, step_key) if (conn is not None and run_id) else None
    if prior:                                                                           # settled truncated earlier: the strategy is sections, derived, recorded once against the original entry, nothing resent
        record_strategy(conn, run_id, step_key, prior, "plan_generate whole plan", "section_assembly", "whole-plan output stopped at the ceiling (typed outcome found on resume)", "sections scope, components, tasks, acceptance under the same ceiling; no resend of the whole plan")
        out = plan_in_sections(gateway, step_key, build_section_messages, conn, project_id, run_id, finishing, before_dispatch)
        out["assembled"] = True
        return out
    if before_dispatch is not None and gateway.ledger.completed_result(step_key, "model") is None:   # a stored draft replays before the deadline or permit check
        before_dispatch("plan")
    r = gateway.call(step_key, PlanOut, messages, estimate_tokens(messages), finishing=finishing, before_dispatch=before_dispatch)
    if r["status"] == "truncated":
        if conn is not None and run_id:
            record_strategy(conn, run_id, step_key, r["entry"], "plan_generate whole plan", "section_assembly", r.get("detail", "output stopped at the ceiling"), "sections scope, components, tasks, acceptance under the same ceiling; no identical retry")
        out = plan_in_sections(gateway, step_key, build_section_messages, conn, project_id, run_id, finishing, before_dispatch)
        out["assembled"] = True
        return out
    if r["status"] in DONE:
        plan = r["output"]
        if plan.failure_reason:
            return {"status": "failed", "detail": plan.failure_reason, "assembled": False, "answer": plan}   # the answer travels with the failure (never under "plan": a failure is no plan), so a draft's failure shape is preserved whole
        issues = validate_assembled_plan(plan, conn, project_id)
        return {"status": "ok" if not issues else "inconsistent", "plan": plan, "issues": issues, "assembled": False}
    return {"status": r["status"], "detail": r.get("detail"), "assembled": False}
