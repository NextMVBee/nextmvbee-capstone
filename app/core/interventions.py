"""The intervention criteria: the one list, in code, of every place the product stops for the founder,
each with an id, the condition, the enforcement point, the resolution the founder can give and the test that proves the row.

The Runner (app/runner/runner.py, which drives a run) raises interrupts only through this list: every pause event and every
graph interrupt names its criterion (criterion_of) and is raised through interrupt, the one entry point. open_interrupts
lists a run's open interrupts by criterion.

Blocking criteria pause the run (status awaiting_user or interrupted) until the founder's resolution resumes it; nothing is sent while it
waits (the ledger's dispatch claim refuses a run that is not running, and an unknown call is never resent). The
non-blocking criteria never pause it: a cap stop ends the run with the one draft, the approval acts are given on a finished
version, a flagged source is excluded until the founder decides while the run goes on, a delivered
document awaits its acceptance, and a contradiction between sources is a recorded reason on the decision
that depends on it: that decision is left open with the conflict in words and the check that settles it, and the rest of the run
continues. The founder is turned to when he holds the information or the authority."""
import json
import sqlite3
from dataclasses import dataclass

from app.core.sqlguard import require_guarded

DRAFT_STOPS = ("budget_reserve_reached", "time_limit", "call_limit", "skipped_call")   # stop research, draft the plan from the evidence gathered (skipped_call: the founder continued without a stage's interrupted send); the workflow's one definition


@dataclass(frozen=True)
class Criterion:
    id: str
    name: str
    condition: str
    enforced: str                 # file and function
    options: str                  # the founder's options, as the command line takes them
    blocking: bool                # the run waits (awaiting_user or interrupted) until the resolution
    test: str                     # the test that proves the enforcement


CRITERIA = (
    Criterion("I1", "hard-requirement confirmation",
              "a hardness question (the clarify model proposed a hard requirement, a stored project carries a legacy flag, or a changed requirement's hardness was stated, 8a), a confirmation question (a requirement the model marked as the founder's statement), a clarifying question of the clarify round, or the planner's ask_user question is open",
              "app/graph/workflow.py clarify (the questions persisted) and ask (the run awaiting_user, one interrupt, yes or no sets the flag); research_plan with app/core/tools.py ToolRunner.run (ask_user pauses the stage); app/runner/runner.py Runner.revise (the stated hardness opens the question)",
              "resume --run RUN --answer QID=yes|no (a confirmation: yes, no or the corrected value; a clarifying question: the answer)", True, "tests/test_interventions.py i1; tests/test_milestone1.py T7; tests/test_guarantees.py C12"),
    Criterion("I2", "usage questions",
              "a cost-bearing decision is compared and the project's usage figures, the usage scope or the summary's length are not answered (D-A28, D-A107, D-A130)",
              "app/graph/workflow.py compare_options (_usage_answer, _usage_scope, _length_question: the question persisted, the run awaiting_user, one interrupt)",
              "resume --run RUN --answer QID=<the usage in the founder's words>", True, "tests/test_interventions.py i2; tests/test_quality.py (the usage, scope and length questions)"),
    Criterion("I3", "an unknown call",
              "a send ended unknown (a timeout or a lost reply after the provider received it): the call and everything depending on its result wait, independent work continues, the reservation stays as a possible charge (D-A182)",
              "app/graph/workflow.py guarded (InterruptedCall) and app/runner/runner.py Runner._drive with app/runner/recovery.py interrupt_for_unknown (the run interrupted with stop_reason unknown_call); app/core/ledger.py claim_dispatch refuses a resend",
              "resolve --run RUN --call ENTRY --choice retry|continue|reconcile [--usd X], then resume --run RUN", True, "tests/test_interventions.py i3; tests/test_recovery.py T10, T17"),
    Criterion("I4", "a cap stop",
              "the money cap less the completion reserve, the active minutes or the model-call limit of D-A183 is reached: research stops and the one draft version carries every draft decision marked with the stop reason (D-A178)",
              "app/graph/workflow.py guarded and _stop (DRAFT_STOPS), plan_generate (_carry_drafts); app/core/ledger.py BudgetLedger (budget_reserve_reached, call_limit); RunContext.check_time (time_limit)",
              "the run ended partial with the draft version: approve it explicitly (I6) or open a new run with higher caps (--cap-usd, --max-minutes, --max-calls)", False, "tests/test_interventions.py i4; tests/test_quality.py T38"),
    Criterion("I5", "version approval",
              "a signed plan version exists without the owner's approval act; nothing is produced from it before the act (D-A13)",
              "app/export/approval.py approve_version (the act bound to the snapshot signature); app/runner/cli.py approve; app/web/live.py approve",
              "approve --project P --version N --by WHO --ref TEXT", False, "tests/test_interventions.py i5; tests/test_snapshot.py; tests/test_exports.py"),
    Criterion("I6", "approval of a draft version",
              "the version to approve is marked as a draft (written after a cap stop: its checks name the drafts or the subjects not compared, or its run ended with a stop reason of DRAFT_STOPS)",
              "app/export/approval.py draft_marks and approve_version (refused without accept_draft); app/runner/cli.py approve --accept-draft; app/web/live.py approve (the acceptance box of the approve screen)",
              "approve --project P --version N --by WHO --ref TEXT --accept-draft", False, "tests/test_interventions.py i6"),
    Criterion("I7", "a material contradiction unresolved in a decision",
              "a decision of the run ends with a material contradiction unresolved: an open conflict of its evidence packs the comparison did not address (D-A185), or sources that disagree on a binding hard requirement's attribute (D-A190 state c). Since Phase 19 package 19.1 (D-A409, the owner's decision of 13 November 2026) this is a recorded reason on the decision, not an interrupt: the decision that depends on the conflict is left open and the run continues",
              "app/graph/workflow.py compare_options (_contradiction_block: the decision recorded open, unresolved, with the conflict in words, the option, the attribute and the values with their sources, and the check that settles it; no question, no pause; a claim the verifier refused is noted on the decision and opens nothing)",
              "none to give during the run: the decision card and the plan state the conflict and the settling check; the conflict is settled by evidence (a later run, or a requirement change)", False, "tests/test_interventions.py i7; tests/test_evidence_states.py es4"),
    Criterion("I8", "a hard-requirement change",
              "a revision changes a requirement: the decisions it binds are re-opened (every decision of the project when applies_to is empty, an empty list or none, 8b, D-A219), its hardness is as the founder stated it (8a), and the run asks before any money is spent on the re-run; the revision's scope questions belong here too",
              "app/core/invalidation.py apply_change (the stale set by binding, the bounded reverify set, the hard flag); app/runner/runner.py Runner.revise (the revision question, the scope stated in the run events); app/graph/workflow.py revise (no send while a question is open) and ask (yes starts the re-run, no ends it without spending)",
              "resume --run RUN --answer QID=yes|no (the revision question), QID=<the scope answer> (a scope question); revise --hard yes|no states the changed requirement's hardness", True, "tests/test_interventions.py i8, i8b, i8c, i8d; tests/test_milestone1.py T7"),
    Criterion("I9", "a source that tries to change instructions",
              "the extractor flagged instruction-like text in any batch of a page (sources.injection_flag): the page is excluded from claims as a whole until the founder decides (the claims its other batches stored are removed, its remaining batches are not read, a refreshed version is not read; D-A217); the run goes on without it",
              "app/core/extraction.py (_exclude_flagged: the flag, the page's claims removed, its batches closed; extract_source reads no flagged undecided page); app/graph/workflow.py extract_evidence (the intervention event); app/core/interventions.py decide_source (include: read again whole and kept at the next extraction; exclude: never read again)",
              "source --run RUN --source SID --choice include|exclude", False, "tests/test_interventions.py i9, i9b"),
    Criterion("I10", "acceptance of a delivered document",
              "a document group of an approved version was produced (the rows checked, shown as delivered, with the report's warnings and page count) and awaits the founder's decision; each file opens read-only for the inspection before it (D-A239), nothing is published before it and no new request of the key starts while it waits (D-A36, D-A307)",
              "app/export/store.py decide (approve: the acceptance record with the warnings as accepted notes, the rows available; reject: rejected with the note; a file that no longer hashes to its row refuses the decision, B-02); app/web/exports.py decide (the route: the owner, CSRF, 409 before delivery); the approve screen's Accept and Reject controls (app/templates/partials/daylight/doc_family.html, app/static/s8.js)",
              "the approve screen: Accept the documents (the confirmation names the warnings count; a note) or Reject (the note kept as the reason); the command line: python -m app.export.store decide --export ID --decision approve|reject --by WHO --note TEXT; not blocking", False, "tests/test_exports_web.py (c), (e); tests/test_exports.py (the store's decision)"),
)

BY_ID = {c.id: c for c in CRITERIA}
QUESTION_KINDS = {"hardness": "I1", "confirmation": "I1", "usage": "I2", "contradiction": "I7", "revision": "I8"}   # a value question is I1 (clarify, ask_user) in an initial run and I8 (a scope question) in a revision run; contradiction: the kind of the questions earlier runs asked (none is asked now, and none waits: retire_contradiction_questions)


def retire_contradiction_questions(conn: sqlite3.Connection, project_id: str) -> int:
    """No question of kind contradiction waits. A question an earlier run asked and left open is closed as skipped (the row and
    its text stay as history); the decision it named is open on its own record. Returns the rows closed. The caller owns the transaction."""
    require_guarded(conn)
    return conn.execute("UPDATE questions SET status='skipped' WHERE project_id=? AND kind='contradiction' AND status='open'", (project_id,)).rowcount


def criterion(cid: str) -> Criterion:
    return BY_ID[cid]


def criterion_of(kind: str | None, revision: bool = False, tool_call: bool = False) -> str:
    """The criterion of a question by its kind: the value questions of a revision run are its scope questions (I8); a paused ask_user
    question and the clarify round's questions are the founder's answers before research (I1)."""
    if kind in QUESTION_KINDS:
        return QUESTION_KINDS[kind]
    return "I8" if revision and not tool_call else "I1"


def interrupt(payload: dict):
    """The one raising entry point of a graph interrupt. The payload names its criterion (criterion, or criteria for the ask node's
    questions of several kinds); every id is a blocking criterion of CRITERIA, else the payload is refused before the graph is touched.
    Returns the resume value as the graph returns it."""
    ids = list(payload.get("criteria") or ([payload["criterion"]] if payload.get("criterion") else []))
    if not ids:
        raise ValueError("an interrupt names its criterion (D-A191)")
    unknown = [c for c in ids if c not in BY_ID]
    if unknown:
        raise ValueError(f"criterion {', '.join(unknown)} is not in the list")
    passive = [c for c in ids if not BY_ID[c].blocking]
    if passive:
        raise ValueError(f"criterion {', '.join(passive)} does not pause the run; it is listed, never raised")
    from langgraph.types import interrupt as graph_interrupt
    return graph_interrupt(payload)


def entry(cid: str, ref: str, detail: str, resolution: str | None = None) -> dict:
    c = BY_ID[cid]
    return {"criterion": cid, "name": c.name, "ref": ref, "detail": detail[:300], "blocking": c.blocking, "resolution": resolution or c.options}


def draft_marks(conn: sqlite3.Connection, project_id: str, version: int) -> list[str]:
    """I6: what marks a version as a draft: the lines of its signed checks that name the drafts or the subjects not compared after a stop
    (app/core/coverage.stop_issues) and the stop reason of the run that wrote it when it is one of DRAFT_STOPS. [] for a version
    written by a run that finished, or one without a snapshot."""
    require_guarded(conn)
    from app.core import snapshot as SN
    from app.core.coverage import STOP_ISSUE_PREFIXES
    try:
        stored = SN.read(conn, project_id, version)
    except SN.SnapshotError:
        return []                                                                           # a signature that does not recompute: no mark read here; the approval refuses the version on its own check
    if stored is None:
        return []
    snap = stored["snapshot"]
    marks = [line for line in ((snap.get("plan_body") or {}).get("unresolved_checks") or []) if line.startswith(STOP_ISSUE_PREFIXES)]   # the partial-draft line marks the version too
    runs = snap.get("run") or []
    if runs:
        row = conn.execute("SELECT stop_reason FROM runs WHERE id=? AND project_id=?", (runs[0]["id"], project_id)).fetchone()
        if row and row[0] in DRAFT_STOPS:
            marks.append(f"written by run {runs[0]['id']} after its stop ({row[0].replace('_', ' ')})")
    return marks


def open_interrupts(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    """The interrupts open on a run, each with its criterion id, what it refers to and the resolution the founder can give: the open
    questions by kind (I1, I2, I7, I8), the unresolved unknown calls (I3), a cap stop of this run with its draft version (I4), the
    newest signed version of the project awaiting approval (I5, or I6 when it is marked as a draft), and the project's flagged sources
    without a decision (I9). Read only."""
    require_guarded(conn)
    from app.core.ledger import BudgetLedger
    row = conn.execute("SELECT project_id, status, stop_reason, kind, budget_json FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"run {run_id} does not exist")
    pid, status, stop_reason, kind, budget_json = row
    out = []
    for qid, text, qkind, key in conn.execute("SELECT id, text, kind, requirement_key FROM questions WHERE project_id=? AND status='open' AND tool_call_id IS NULL ORDER BY id", (pid,)).fetchall():
        cid = criterion_of(qkind, revision=(kind == "revision"))
        out.append(entry(cid, qid, f"{qkind} question" + (f" on {key}" if key else "") + f": {text}", f"resume --run {run_id} --answer {qid}=" + ("yes|no" if qkind in ("hardness", "confirmation", "revision") else "open|<note>" if qkind == "contradiction" else "<answer>")))
    for qid, text in conn.execute("SELECT q.id, q.text FROM questions q JOIN tool_calls t ON t.id=q.tool_call_id WHERE t.run_id=? AND t.state='paused' AND q.status='open' ORDER BY q.id", (run_id,)).fetchall():
        out.append(entry("I1", qid, f"the research planner's question: {text}", f"resume --run {run_id} --answer {qid}=<answer>"))
    b = json.loads(budget_json or "{}")
    for c in BudgetLedger(conn, run_id, float(b.get("cap_usd", 2.5)), float(b.get("completion_reserve_usd", 0.0))).unknown_calls():
        out.append(entry("I3", c["id"], f"{c.get('kind')} call of step {c.get('step_key')} ended unknown; possibly charged up to {float(c.get('possible_charge_usd') or 0.0):.4f} USD", f"resolve --run {run_id} --call {c['id']} --choice retry|continue|reconcile, then resume --run {run_id}"))
    version = None
    done = conn.execute("SELECT data_json FROM run_events WHERE run_id=? AND event='done' ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    if done:
        version = (json.loads(done[0]) or {}).get("plan_version")
    if stop_reason in DRAFT_STOPS and status in ("partial", "failed", "completed"):
        out.append(entry("I4", run_id, f"the run stopped ({stop_reason.replace('_', ' ')}) and ended {status}" + (f" with the draft version {version}" if version else " with no version"),
                         (f"approve --project {pid} --version {version} --by WHO --ref TEXT --accept-draft, or " if version else "") + "a new run with higher caps (--cap-usd, --max-minutes, --max-calls)"))
    newest = conn.execute("SELECT version, approved_at FROM plan_versions WHERE project_id=? AND snapshot_sig IS NOT NULL ORDER BY version DESC LIMIT 1", (pid,)).fetchone()
    if newest and newest[1] is None:
        marks = draft_marks(conn, pid, int(newest[0]))
        if marks:
            out.append(entry("I6", f"version {newest[0]}", f"version {newest[0]} is marked as a draft: " + "; ".join(marks), f"approve --project {pid} --version {newest[0]} --by WHO --ref TEXT --accept-draft"))
        else:
            out.append(entry("I5", f"version {newest[0]}", f"version {newest[0]} awaits the owner's approval", f"approve --project {pid} --version {newest[0]} --by WHO --ref TEXT"))
    has_col = "injection_decision" in {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    if has_col:
        for sid, url in conn.execute("SELECT id, url FROM sources WHERE project_id=? AND injection_flag=1 AND injection_decision IS NULL ORDER BY id", (pid,)).fetchall():
            out.append(entry("I9", sid, f"{sid} ({url[:120]}) carries instruction-like text and is excluded from claims until you decide", f"source --run {run_id} --source {sid} --choice include|exclude"))
    return out


def decide_source(conn: sqlite3.Connection, run_id: str, source_id: str, choice: str) -> dict:
    """I9: the founder's decision on a flagged source of the run's project. include: the flag is cleared and the decision recorded, so the
    next extraction reads the page and keeps its claims (and never flags it again); exclude: the decision recorded, the source stays out
    and is not read again. Refused for a source that is not flagged, or a word outside include and exclude. One transaction; a run event."""
    require_guarded(conn)
    if choice not in ("include", "exclude"):
        raise ValueError("the choice is include or exclude")
    pid = (conn.execute("SELECT project_id FROM runs WHERE id=?", (run_id,)).fetchone() or [None])[0]
    if pid is None:
        raise ValueError(f"run {run_id} does not exist")
    row = conn.execute("SELECT injection_flag, injection_decision FROM sources WHERE id=? AND project_id=?", (source_id, pid)).fetchone()
    if row is None or not row[0]:
        raise ValueError(f"source {source_id} is not a flagged source of project {pid}")
    from app.runner import events as ev
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE sources SET injection_decision=?, injection_flag=? WHERE id=? AND project_id=?", (choice, 0 if choice == "include" else 1, source_id, pid))
        if choice == "include":
            conn.execute("DELETE FROM extraction_batches WHERE project_id=? AND source_id=?", (pid, source_id))   # the batches the flag closed without a claim are planned again by the next extraction of the page
        ev.emit(conn, run_id, "intervention", {"message": f"{source_id}: the founder decided {choice} on the flagged source" + (" (its claims are kept at the next extraction)" if choice == "include" else " (it stays out and is not read again)"), "signal": "injection", "source_id": source_id, "criterion": "I9", "choice": choice})
        conn.commit()
    except Exception:
        conn.rollback(); raise
    return {"source_id": source_id, "choice": choice, "previous": row[1]}
