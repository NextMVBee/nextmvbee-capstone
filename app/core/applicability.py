"""The requirement applicability map: which decision subjects each hard requirement binds.

A hard requirement binds the subjects the planner (the research planning call of the workflow) names for it
(requirements.applies_to, a JSON list of decision subjects; migration 056). A decision tests only the hard requirements that
bind its subject. Two rules hold in code whatever the planner wrote: a budget requirement binds every subject, and a hard
requirement the planner left unmapped (applies_to NULL) binds every subject, so an unmapped
requirement never goes untested.

A requirement that names a language (its key names languages or its value names one of the
languages of LANGUAGES) carries that language's code, the language its queries are bound to and of the search cache key.

Deterministic; no model. Tested offline in tests/test_concepts.py."""
from __future__ import annotations

import json
import re
import sqlite3

from app.core.sqlguard import require_guarded

LANGUAGES = {"hebrew": "he", "arabic": "ar", "english": "en", "spanish": "es", "french": "fr", "german": "de", "portuguese": "pt", "italian": "it", "dutch": "nl", "russian": "ru",
             "turkish": "tr", "polish": "pl", "japanese": "ja", "chinese": "zh", "korean": "ko", "hindi": "hi", "swedish": "sv", "greek": "el", "ukrainian": "uk", "czech": "cs"}
LANGUAGE_KEYS = ("language", "languages", "supported_languages", "locale", "locales")
BUDGET_KEYS = ("budget", "monthly_cost", "spend", "cost_ceiling")


def norm(subject: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(subject or "").lower()).strip("_")


def _text(value) -> str:
    return " ".join(str(x) for x in value) if isinstance(value, list) else str(value or "")


def is_budget(req: dict) -> bool:
    key = str(req.get("key") or "").lower()
    return any(w in key for w in BUDGET_KEYS)


def languages_of(req: dict) -> list[str]:
    """The language codes a requirement names (its key or its value), in the order named; [] when it names none."""
    key = str(req.get("key") or "").lower(); text = _text(req.get("value")).lower()
    names = re.findall(r"[a-z]+", text)
    found = [LANGUAGES[w] for w in names if w in LANGUAGES]
    if not found and any(k in key for k in LANGUAGE_KEYS):
        found = [c for c in re.findall(r"\b([a-z]{2})\b", text) if c in LANGUAGES.values()]
    return list(dict.fromkeys(found))


def language_of(req: dict) -> str | None:
    """The one language a hard requirement binds the queries to: the first named language that is not English (English is the
    plan's own language), else None."""
    codes = [c for c in languages_of(req) if c != "en"]
    return codes[0] if codes else None


def applies_to_of(req: dict) -> list[str] | None:
    raw = req.get("applies_to")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return [norm(x) for x in raw] if isinstance(raw, list) and raw else None                      # an empty list is no map (every subject), like NULL; the planner never writes one


def binds(req: dict, subject: str) -> bool:
    """Whether a requirement binds a decision subject: a budget requirement every subject; an unmapped one (NULL or an empty list)
    every subject; otherwise the subjects of its map (normalised)."""
    if is_budget(req):
        return True
    subjects = applies_to_of(req)
    if subjects is None:
        return True
    return norm(subject) in subjects


def binding(requirements: list[dict], subject: str, hard_only: bool = True) -> list[dict]:
    return [r for r in requirements if (not hard_only or r.get("hard")) and binds(r, subject)]


def map_of(conn: sqlite3.Connection, project_id: str) -> dict[str, list[str] | None]:
    """requirement id -> its mapped subjects (None when unmapped) for the project's confirmed hard requirements."""
    require_guarded(conn)
    out = {}
    for rid, raw in conn.execute("SELECT id, applies_to FROM requirements WHERE project_id=? AND status='confirmed' AND hard=1 ORDER BY id", (project_id,)).fetchall():
        out[rid] = applies_to_of({"applies_to": raw})
    return out


def set_applies_to(conn: sqlite3.Connection, project_id: str, requirement_id: str, subjects: list[str] | None) -> None:
    """Write the map of one requirement (normalised subjects, sorted, without duplicates); None clears it. The caller owns the transaction."""
    require_guarded(conn)
    value = None if subjects is None else json.dumps(sorted({norm(s) for s in subjects if norm(s)}))
    conn.execute("UPDATE requirements SET applies_to=? WHERE id=? AND project_id=?", (value, requirement_id, project_id))


def apply_planner_map(conn: sqlite3.Connection, project_id: str, hard_requirements: list[dict], subjects: list[str], applicability: list[dict]) -> dict:
    """The planner's map written to the confirmed hard requirements: each entry's subjects filtered to the named decision subjects;
    a budget requirement binds every subject; a hard requirement the planner left unmapped stays unmapped (binds every subject) and is
    reported. Returns {requirement id: subjects written or None} with the dropped names. The caller owns the transaction."""
    require_guarded(conn)
    known = {norm(s): s for s in subjects}
    by_req = {}
    for a in applicability:
        rid = a.get("requirement_id")
        if rid is None:
            continue
        by_req.setdefault(rid, []).extend(a.get("subjects") or [])
    report = {"written": {}, "unmapped": [], "dropped": {}}
    for r in hard_requirements:
        rid = r["id"]
        if is_budget(r):
            set_applies_to(conn, project_id, rid, list(known.values()))
            report["written"][rid] = sorted(known); continue
        named = by_req.get(rid)
        if not named:
            report["unmapped"].append(rid); continue
        kept = [s for s in named if norm(s) in known]; dropped = [s for s in named if norm(s) not in known]
        if dropped:
            report["dropped"][rid] = dropped
        if kept:
            set_applies_to(conn, project_id, rid, kept); report["written"][rid] = sorted({norm(s) for s in kept})
        else:
            report["unmapped"].append(rid)
    return report


def rows_for_screen(requirements: list[dict], subjects: list[str]) -> list[dict]:
    """The map as the screen shows it: one row per confirmed hard requirement with the subjects it binds (every subject when unmapped or a budget)."""
    out = []
    for r in requirements:
        if not r.get("hard") or r.get("status") not in (None, "confirmed"):
            continue
        bound = [s for s in subjects if binds(r, s)]
        mapped = applies_to_of(r)
        out.append({"id": r["id"], "key": r.get("key"), "value": _text(r.get("value")), "subjects": bound, "how": "every subject (budget)" if is_budget(r) else ("every subject (unmapped)" if mapped is None else "named by the planner"), "language": language_of(r)})
    return out
