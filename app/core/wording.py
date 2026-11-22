"""The founder's words: a requirement is named to the founder by its label and its value, never by its key;
a question, its why line and a pause message carry no internal name or id (a key with an underscore, a decision, research-question or claim
id, an id of the development's own records, a phase name, an intervention criterion id, a call id).

The ids stay in the records. INTERNAL is the one list of the patterns, each defined once, with its flags, and matching the
whole name; founder_text removes what the patterns match."""
import re

LABELS = {"budget_monthly_usd": "Monthly budget (USD)", "languages": "Languages", "supported_languages": "Supported languages", "platform": "Platform", "users": "Users",
          "timeline": "Timeline", "privacy": "Privacy", "processing": "Processing", "region": "Region", "deployment_region": "Deployment region", "nutrition_data": "Nutrition data",
          "household_size": "Household size", "reminders": "Reminders", "offline": "Offline use", "accounts": "Accounts"}

INTERNAL = (("a call id", re.compile(r"\b[A-Za-z0-9_]+-R\d+(?::[\w.~-]*\w){2,}")),   # a durable id built on a run id (<project>-R<n>:<stage>:<worker>:<branch>[:more], a step key, a call's ledger id <step key>:model:<n>, a tool call's id), whole; first, so the removal takes the whole id before a part of it
            ("a key with an underscore", re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")), ("a D id", re.compile(r"\bD-\d+\b")), ("a D-A id", re.compile(r"\bD-A\d+\b")),
            ("a phase name", re.compile(r"\bPhase \d\w*", re.I)), ("a criterion id", re.compile(r"\bcriterion I\d+\b|\bI\d{1,2}\b")),
            ("a decision id", re.compile(r"\w*\.DEC-\d\w*")), ("a claim id", re.compile(r"\w*\.CLM-\d\w*")), ("a research question id", re.compile(r"\w*\.RQ-[0-9a-f]\w*")))   # a research question id's suffix is hexadecimal (app/core/coverage.py)
# each pattern takes the whole name (the project part and the suffix of a record id, the suffix of a phase name), so the one pattern both detects and removes


def label_of(key: str | None) -> str:
    """A requirement key as a label: the known keys by name, any other key in words."""
    if not key:
        return ""
    return LABELS.get(key) or str(key).replace("_", " ").strip().capitalize()


def value_words(value) -> str:
    """A requirement's value as the founder wrote it: a list joined by commas, anything else as its text."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(x) for x in value)
    return "" if value is None else str(value).strip()


def requirement_words(requirement: dict) -> str:
    """The requirement in the founder's words: its label and its value ("Languages: Hebrew, Arabic, English"), whole."""
    label, value = label_of(requirement.get("key")), value_words(requirement.get("value"))
    return f"{label}: {value}" if label and value else (label or value)


def internal_names(text) -> list[str]:
    """The internal names and ids a founder's text carries, by pattern name with the match; [] for a text in the founder's language."""
    out = []
    for name, pat in INTERNAL:
        m = pat.search(str(text or ""))
        if m:
            out.append(f"{name}: {m.group(0)}")
    return out


def _scoped(pat: re.Pattern) -> str:
    """A pattern of INTERNAL as a part of a larger pattern, under its own flags."""
    return f"(?{'i' if pat.flags & re.I else ''}:{pat.pattern})"


# the removal is built from the compiled patterns of INTERNAL, never written apart from them. A key is put in words, a record id is
# dropped, and every other pattern of the list is dropped with its parenthesis (_ID_NOTE) or with the word that joins it to the sentence (_BARE_ID).
_KEY = dict(INTERNAL)["a key with an underscore"]
_RECORDS = tuple(pat for name, pat in INTERNAL if name in ("a decision id", "a claim id", "a research question id"))
_CITED = "|".join(_scoped(pat) for _, pat in INTERNAL if pat is not _KEY and pat not in _RECORDS)
_ID_NOTE = re.compile(r"\s*\((?:[^()]*?)(?:" + _CITED + r")(?:[^()]*?)\)")
_BARE_ID = re.compile(r"(?:\s*,)?\s*(?:\b(?i:under|per|by|in|of|from|see|for|with|and|or|as in|according to)\s+)?(?:\bthe\s+)?(?:" + _CITED + ")")
_OTHER_RECORD_ID = re.compile(r"\b[A-Za-z0-9]+\.(?:REQ|Q|SRC)-[0-9a-f]+\b")   # the record ids INTERNAL does not list (a requirement, a question, a source), removed too
HARDNESS_WHY = "A must-have rejects every option that fails it."
CONFIRMATION_WHY = "Research starts only on a requirement you confirmed."
REVISION_WHY = "A change of a requirement re-opens the decisions it binds, and the re-run is asked before it spends money."


def hardness_question(key: str, value=None) -> str:
    """The hardness question in the founder's words: the requirement by its label and value."""
    label, words = label_of(key), value_words(value)
    named = f"{label} ({words})" if words else label
    return f"Is {named} a must-have, so that every option that fails it is rejected? Answer yes or no."


def confirmation_question(key: str, value) -> str:
    return f"{label_of(key)} was recorded as your statement with the value {value_words(value)}. Answer yes to confirm it, no to reject it, or answer with the corrected value."


CALL_KINDS = {"model": "a model", "search": "the search service", "contents": "the page reader"}   # who a request was sent to, in the founder's words
_LATE = ("SendDeadline", "Timeout", "still in flight after the timeout")                            # the causes that are a reply not arriving in time (the send bound, the client's timeout, a wait on a send in flight)


def interrupted_call(kind: str, cause: str, retry_usd: float, cap_usd: float) -> dict:
    """The pause of one interrupted call in the founder's words: what happened, what each of the three choices does
    and costs, and the usual choice. The call id, the step and the exception's name are not part of these words; they stand in the one
    details line (app/runner/recovery.call_details) and in the records. Wording only: the choice stays the founder's, nothing is resent on
    its own."""
    late = any(w in str(cause or "") for w in _LATE)
    amount = f"${float(retry_usd or 0.0):.4f}"
    return {"headline": f"One request to {CALL_KINDS.get(kind, 'a service')} " + ("did not answer in time" if late else "was interrupted before its answer was saved"),
            "saved": f"Everything else is saved and will not run again. The first request may already have been charged, at most {amount}; that amount stays counted beside the spend until the provider's figure is known.",
            "retry_label": "Retry (the usual choice)", "continue_label": "Continue without it", "reconcile_label": "Use the provider's figure",
            "retry": f"Retry sends the request once more and the run goes on from where it paused. This is the usual choice. It may be charged once more, at most {amount}, within the run's cap of ${float(cap_usd):.2f}.",
            "continue": "Continue without it: the run goes on without this answer, and the part that needed it is marked as not completed. Nothing more is sent for it and nothing more is charged.",
            "reconcile": "Use the provider's figure: if you looked up what the provider charged for the first request, enter the amount and it replaces the possible charge; left blank, the request counts as not charged unless the provider "
                         f"holds a record of it. The request is then sent once more, as with a retry (at most {amount} again).",
            "retry_usd": float(retry_usd or 0.0)}


def interrupted_pause(calls: list[dict], cap_usd: float, saved: dict | None = None, opening: str = "Run paused") -> str:
    """The pause message of a run that waits on interrupted calls, in the founder's words; calls carry their words (recovery.call_details)."""
    if not calls:
        return f"{opening}."
    total = sum(float(c["words"]["retry_usd"]) for c in calls)
    what = (calls[0]["words"]["headline"][:1].lower() + calls[0]["words"]["headline"][1:]) if len(calls) == 1 else f"{len(calls)} requests were interrupted before their answers were saved"
    kept = "Everything else is saved and will not run again" + (f". Saved: {saved['completed_calls']} completed call(s), {saved['steps']} recorded step(s), {saved['sources']} source(s), {saved['claims']} claim(s)" if saved else "")
    it = "it" if len(calls) == 1 else "each"
    possible = sum(float(c.get("possible_charge_usd") or 0.0) for c in calls)
    kept += f". The interrupted request{'' if len(calls) == 1 else 's'} may already have been charged, at most ${possible:.4f}" if possible else ""
    one = len(calls) == 1                                                             # the message itself says what each choice does and costs, as the run screen does (interrupted_call)
    return (f"{opening}: {what}. {kept}. The usual choice is to retry {it}: {'the request is' if one else 'each request is'} sent once more and the run goes on from where it paused. {'It' if one else 'They'} may be charged once more, at most ${total:.4f}, "
            f"within the run's cap of ${float(cap_usd):.2f}. You can also continue without {it}: the run goes on without {'this answer' if one else 'these answers'}, the {'part that needed it is' if one else 'parts that needed them are'} marked as not completed, "
            f"nothing more is sent for {'it' if one else 'them'} and nothing more is charged. Or you can use the provider's figure for {it}: the amount you enter replaces the possible charge{'' if one else ' of that request'} (left blank, the request counts as "
            f"not charged unless the provider holds a record of it), and {'the' if one else 'each'} request is then sent once more, as with a retry (at most ${total:.4f} again). Nothing is sent again until you choose.")


def _in_words(text: str, keys) -> str:
    """One pass of founder_text over a text."""
    out = _BARE_ID.sub("", _ID_NOTE.sub("", text))
    for k in sorted({str(k) for k in keys if k and "_" in str(k)}, key=len, reverse=True):
        out = re.sub(r"\b" + re.escape(k) + r"\b", label_of(k), out)
    for pat in _RECORDS:
        out = pat.sub("", out)
    out = _KEY.sub(lambda m: m.group(0).replace("_", " "), _OTHER_RECORD_ID.sub("", out))
    out = re.sub(r"\(\s*(?:(?:[,;]|\b(?:see|and|or)\b)\s*)*\)", "", out)                      # a parenthesis that held ids alone, however many and however joined, leaves nothing
    out = re.sub(r"\(\s*[,;]\s*", "(", re.sub(r"(?:\s*[,;])+\s*\)", ")", out))                # an id beside other words in a parenthesis leaves the words and no dangling separator
    out = re.sub(r"\s+([,.;:?])", r"\1", re.sub(r"[ \t]{2,}", " ", out)).strip(" ,;:")
    return out[:1].upper() + out[1:] if text[:1] != out[:1] else out   # a sentence whose first word was an id starts with a capital


def founder_text(text, keys=()) -> str:
    """A text shown to the founder, in the founder's language: a parenthetical that cites a decision id, a criterion or a phase is
    dropped, so is one named outside a parenthesis with the word that joins it, a requirement key the project holds is replaced by its label, a record id is dropped, and any other word joined by underscores is
    written in words. The stored row keeps its text; this is the display and the form new rows are written in. Every removal is a pattern of
    INTERNAL itself, and the pass is repeated while internal_names still reports a match (a name that two removals bring together, a capital the
    pass gave), so no match internal_names reports on a text is left in what this returns."""
    out = _in_words(str(text or ""), keys)
    while internal_names(out):
        out = _in_words(out, keys)
    return out


# ---------------------------------------------------------------- the words of a limit, of a recalculation and of a version a run did not finish
LIMIT_ENDS = {"budget_reserve_reached": "its spending limit", "budget": "its spending limit", "time_limit": "its time limit", "call_limit": "its limit of model calls"}   # a run's limits in the founder's words; the stop's internal name stays in the records and on the details line
_EDITOR_WHY = {"budget_reserve_reached": "the run's spending limit left no room for it", "budget": "the run's spending limit left no room for it", "time_limit": "the run reached its time limit first",
               "call_limit": "the run reached its limit of model calls first", "credit_exhausted": "the provider account had no credit left", "run_stopped": "the run was stopped first", "founder_stop": "the run was stopped first"}
_OTHER_ENDS = {"credit_exhausted": "the provider account had no credit left", "run_stopped": "you stopped it", "founder_stop": "you stopped it", "stage_failed": "one of its stages failed", "server_restart": "the server restarted"}
RUN_OPEN = ("running", "awaiting_user", "interrupted")


def editor_not_run(reason: str | None, version: int | None = None) -> str:
    """Why the editorial pass did not run and what was stored without it, in the founder's words."""
    why = _EDITOR_WHY.get(str(reason or ""), "its request did not complete")
    return f"The editorial pass did not run: {why}. " + (f"Version {int(version)} is" if version else "The version is") + " stored and signed as it was written and checked; the editorial pass only changes wording."


def unfinished_version(u: dict) -> str:
    """A version row a run began and did not store, in the founder's words: which run state left it, which version stands, how the next
    version comes about. u: one row of app/core/snapshot.unfinished_versions."""
    v, standing, reason = int(u["version"]), u.get("standing"), str(u.get("stop_reason") or "")
    stands = f"Version {int(standing)} stands" if standing else "No version is stored yet"
    if u.get("run_status") in RUN_OPEN:
        return f"Version {v} is being written by the run in progress and is not stored yet. {stands} until it is."
    how = f"stopped at {LIMIT_ENDS[reason]}" if reason in LIMIT_ENDS else ("stopped because " + _OTHER_ENDS[reason] if reason in _OTHER_ENDS else "ended")
    return f"The run that was writing version {v} {how} before the version was stored. {stands}. A recalculation or a requirement change writes the next version."


def run_noun(recalculation: bool) -> str:
    """What a run is called on the run screen: research, or, for a revision run that searches and reads nothing, a recalculation."""
    return "The recalculation of the decisions and costs" if recalculation else "Research"


def run_end(status: str | None, stop_reason: str | None, plan_version=None) -> dict:
    """The banner of a run's end in the founder's words: which limit ended it and what was kept; the stop's internal name stands on
    the details line only (None when the run ended without one). Returns {"line", "details", "ok"}."""
    reason = str(stop_reason or ""); status = str(status or "")
    details = f"Details: {reason}" if reason and reason != "complete" else None
    if plan_version and status == "completed":
        return {"line": f"Plan version {plan_version} is ready.", "details": None, "ok": True}
    if plan_version and status == "partial":
        return {"line": f"Plan version {plan_version} is ready as a partial plan: the run ended at {LIMIT_ENDS.get(reason, 'a limit')}. Everything decided and verified before the stop is in it.", "details": details, "ok": True}
    kept = "The decisions it made and everything it verified are kept; the last stored version stands."
    if status == "partial":
        return {"line": f"The run ended at {LIMIT_ENDS.get(reason, 'a limit')} before a new version was stored. {kept}", "details": details, "ok": False}
    why = _OTHER_ENDS.get(reason) or (f"it reached {LIMIT_ENDS[reason]}" if reason in LIMIT_ENDS else "")
    return {"line": "The run ended without a new version" + (f": {why}." if why else ".") + (f" Plan version {plan_version} was stored before it ended." if plan_version else f" {kept}"), "details": details, "ok": False}


def limit_pause(reason: str | None) -> str:
    """The pause banner of a run stopped by one of its limits, in the founder's words; the recorded message stands on the details line."""
    return f"The run reached {LIMIT_ENDS.get(str(reason or ''), 'a limit')}: nothing more is researched or compared. What was decided and verified so far is kept, and the plan is written from it where the limit allows."


INTERVENTIONS = {   # the safety rules' signals (app/core/safety.INTERVENTION_RULES) in the founder's words, what happened and what the run does; one sentence for a research run and for a recalculation
    "unsupported_hard_requirements": "A must-have has no supporting quote yet: the decisions it binds are recommended as not yet proven on it, and the plan says so.",
    "unresolved_contradictions": "Sources disagree on a fact a decision depends on: both are kept and the disagreement is listed under the unresolved checks.",
    "ambiguous_scope": "A requirement change does not say where it applies: the run pauses on that question for your answer.",
    "cap_reached": "The run reached one of its limits: nothing more is researched or compared, and the plan is written from what was decided and verified so far.",
    "interrupted_calls": "A request was interrupted and may have been charged: the run pauses for your choice to retry it, continue without it or reconcile it.",
    "loop_limit": "A step reached its limit of searches, page reads or model requests: it ends with what it had gathered.",
}


def intervention_words(data: dict) -> str:
    """An intervention event as the run screen's row says it: the signal's sentence of INTERVENTIONS; an event of a signal the table
    does not hold (the editorial pass that did not run, whose message is written in the founder's words; a signal of a later release) is its
    recorded message through founder_text. The recorded message stays in the event and stands on the row's details line."""
    return INTERVENTIONS.get(str((data or {}).get("signal") or "")) or founder_text((data or {}).get("message"))


def subject_words(subject) -> str:
    return str(subject or "").replace("_", " ").strip()


def correction_groups(priced: list, carried: list) -> str:
    """The two groups of a usage correction in the founder's words: the decisions decided again on the corrected figures and the
    decisions carried unchanged because the usage figures do not price them."""
    a = [subject_words(s) for s in priced]; b = [subject_words(s) for s in carried]
    first = (f"Decided again on the corrected figures ({len(a)}): {', '.join(a)}." if a else "No decision is priced from the usage figures, so none is decided again.")
    second = (f" Carried unchanged, because the usage figures do not price them ({len(b)}): {', '.join(b)}." if b else " No decision is carried unchanged.")
    return first + second + " Nothing new is searched or read."


# ---------------------------------------------------------------- a stored version without a decision, a run that stored no plan, an approval refused
def empty_version(version, stop_reason: str | None, requirements: int, checks: int) -> dict:
    """A stored, signed version that holds no decision, named as what it is: the label of the comb
    and of the approval screen, what the version holds, and the next step that exists (a new plan; the version can be read and stays)."""
    reason = str(stop_reason or "")
    draft = reason in LIMIT_ENDS
    why = f"the run reached {LIMIT_ENDS[reason]} before any part of the plan was compared" if draft else "the run that wrote it decided no part of the plan"
    holds = f"your brief with {requirements} requirement{'s' if requirements != 1 else ''}" + (f" and {checks} open check{'s' if checks != 1 else ''}" if checks else "")
    return {"label": "DRAFT · NO DECISION" if draft else "NO DECISION", "head": f"Version {version} is stored as a draft with no decision." if draft else f"Version {version} is stored and holds no decision.",
            "line": f"It is signed and can be read: {why}. It holds {holds}, and no recommendation, no quote and no cost.",
            "next": "To get a plan with decisions, start a new plan: a new run researches the idea again under its own limits.", "action": {"label": "Start a new plan", "href": "/new"}}


def no_plan_stored(noun: str, stop_reason: str | None) -> str:
    """The outcome line of a run that ended at a limit before any version of the project was stored."""
    return f"{noun} stopped at {LIMIT_ENDS.get(str(stop_reason or ''), 'a limit')}: no plan was stored."


def approval_refusal(kind: str, version, cause: str = "") -> dict:
    """An approval that was refused, on the approval screen in the founder's words: what was refused, why, the next step; the
    refusal's own text stands on the details line."""
    v = f"Version {version}"
    line, step = {"draft": (f"{v} is a draft, and a draft is approved only with your explicit acceptance.", "Tick the box that accepts it as a draft, then press Approve again."),
                  "no_snapshot": (f"{v} was not stored whole, so it cannot be approved.", "Make a new version: change a requirement, or correct the usage on the brief."),
                  "missing": (f"This project has no version {version}.", "Open the versions of the project and choose one that exists."),
                  "signature": (f"The stored record of {v.lower()} does not match its signature, so nothing was approved.", "Nothing you did caused this. Write to Founder@NextMVBee.ai with the project and the version.")
                  }.get(kind, (f"{v} was not approved.", "Reload this page: it shows the version as it stands now."))
    return {"head": f"{v} was not approved", "line": line, "next": step, "details": f"Details: {cause}" if cause else None}


FORM_REFUSALS = {409: ("That action is not available now", ["Go back to the page you came from and reload it: it shows where things stand now.", "If the page still offers the same action, press it once more; if it is refused again, write to Founder@NextMVBee.ai with the time."]),
                 422: ("The form was not complete", ["Go back to the form, fill what is missing and send it again."])}
FORM_REFUSAL_OTHER = ("The request was refused", ["Go back to the page you came from and reload it: it shows where things stand now."])


def form_refusal(status: int, detail) -> dict:
    """A refused form submission of a founder screen as a page: a title for the status, the refusal's reason through the founder
    wording path, and what to do next."""
    title, steps = FORM_REFUSALS.get(int(status), FORM_REFUSAL_OTHER)
    reason = founder_text(str(detail or "")).strip()
    return {"title": title, "message": (reason[:1].upper() + reason[1:] + ("" if reason.endswith((".", "?", "!")) else ".")) if reason else "The request could not be carried out.", "steps": list(steps)}
