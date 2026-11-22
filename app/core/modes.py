"""The mode of a pick and the acceptance tests that exercise it.

A decision's pick names its mode inside the option label the memo records ("Soniox speech-to-text API (async mode for uploaded
recordings)", "Gladia Speech-to-Text API (Solaria-1, async mode)"): pick_mode reads that parenthetical when it carries a mode word, and
nothing else is read. mode_family classes a text as the asynchronous path (upload, pre-recorded, batch) or the real-time path (streaming,
live), the two families the product's picks name; a text naming neither has no family. An acceptance criterion of the task that integrates
the pick must test the pick's mode: mode_conflict says when a criterion names the other family, and acceptance_mode_notes writes the
plan node's unresolved line for every such criterion (the generator reads the pick's mode from the decision record; the criterion itself is
never rewritten). The document model of the renderer (not part of this repository) carries the same fields beside every acceptance row."""
from __future__ import annotations

import re

ASYNC_WORDS = ("async", "asynchronous", "upload", "uploaded", "pre-recorded", "prerecorded", "batch", "offline")
REALTIME_WORDS = ("real-time", "realtime", "real time", "streaming", "stream", "live")
MODE_WORDS = ASYNC_WORDS + REALTIME_WORDS + ("mode",)


def pick_mode(option_label: str) -> str:
    """The mode a pick's option label names in its parenthetical ("async mode for uploaded recordings"), or "" when the label carries none."""
    for m in re.finditer(r"\(([^()]*)\)", str(option_label or "")):
        inner = m.group(1).strip()
        low = inner.lower()
        if any(w in low for w in MODE_WORDS):
            parts = [p.strip() for p in inner.split(",")]
            named = [p for p in parts if any(w in p.lower() for w in MODE_WORDS)]
            return ", ".join(named) if named else inner
    return ""


def _names(words, low: str) -> bool:
    """A mode word is read at the start of a word only: 'deliver' is not 'live', 'alive' names no family; 'uploaded' and 'streaming' still carry 'upload' and 'stream'."""
    return any(re.search(r"(?<![a-z])" + re.escape(w), low) for w in words)


def mode_family(text: str) -> str | None:
    """'async' for the asynchronous path (upload, pre-recorded, batch), 'realtime' for the real-time path (streaming, live), None otherwise."""
    low = str(text or "").lower()
    if _names(REALTIME_WORDS, low):
        return "realtime"
    if _names(ASYNC_WORDS, low):
        return "async"
    return None


def mode_conflict(mode: str, criterion: str) -> bool:
    """True when the pick names a mode family and the criterion names the other one."""
    fm, fc = mode_family(mode), mode_family(criterion)
    return bool(fm and fc and fm != fc)


FAMILY_WORDS = {"async": "asynchronous", "realtime": "real-time"}


def conflict_note(test_id: str, criterion: str, mode: str, decision_id: str = "") -> str:
    """The note of a conflicting criterion: the test, the path it tests and the pick's mode; fixed words and the records' own."""
    return (f"acceptance test {test_id} tests the {FAMILY_WORDS[mode_family(criterion)]} path while the pick" + (f" of {decision_id}" if decision_id else "")
            + f" names {mode}; the criterion stays as written and the recommended path is the pick's")


def acceptance_mode_notes(acceptance: list, task_component: dict, component_decisions: dict, pick_labels: dict) -> list[str]:
    """The plan node's unresolved lines: for every acceptance test whose task's component is under a decision whose pick names a
    mode, and whose criterion names the other mode family, one line. acceptance: [{id, task_id, criterion}]; task_component: task id to
    component id; component_decisions: component id to its decision ids; pick_labels: decision id to the pick's option label."""
    out = []
    for a in acceptance or []:
        comp = task_component.get(a.get("task_id"))
        for did in component_decisions.get(comp) or []:
            mode = pick_mode(pick_labels.get(did) or "")
            if mode and mode_conflict(mode, a.get("criterion") or ""):
                out.append(conflict_note(a.get("id"), a.get("criterion") or "", mode, did))
                break
    return out
