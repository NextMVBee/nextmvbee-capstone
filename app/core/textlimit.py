"""No text the pipeline stores is cut mid-word (for example a verification check ending "in the wrong s", memo cells ending "the database s",
panel notes at their cap, a project title cut at 80 characters).

A text at its contract's limit without a sentence end is the sign of a cut (the provider enforces the schema's maxLength by cutting the string;
code sliced at the store write). The rules, by what the text is: whole_or_dropped keeps a text whole when it fits its limit and ends at a
sentence end (or is shorter than the limit: a short field is never a cut) and otherwise returns None with the note (the verification plan's
fields: a cut check is dropped and flagged, never stored cut); to_sentence_end keeps a text at its cap to its last sentence end, else to its last
word boundary, with the note (memo cells, panel notes); shorten_words shortens a display at a word boundary (the running title of the document renderer, which is not part of this repository). None
of them slices inside a word."""
from __future__ import annotations

import re

from app.core import docrules

SENTENCE_END = re.compile(r"""[.!?](?:["')\]]+)?$""")
CUT_WORD = "the stored record would end mid-sentence"
ABBREVIATION = re.compile(r"""(?:\b(?:[A-Za-z]\.){2,}|\b(?:cf|vs|viz|incl|excl|approx|ca|etc)\.)(?:["')\]]+)?$""", re.IGNORECASE)   # a point that ends an abbreviation (e.g., i.e., a dotted abbreviation of letters, cf., vs., viz., incl., excl., approx., ca., etc.) where a text was cut at its limit


def ends_at_sentence(text: str) -> bool:
    """True when the text ends with a sentence mark (a closing quote or bracket may follow it)."""
    return bool(SENTENCE_END.search((text or "").rstrip()))


def whole_or_dropped(text: str, limit: int, what: str) -> tuple[str | None, str | None]:
    """(the text whole, None) when it is under its limit, or at the limit and ends at a sentence end; (None, the note) when it is over the limit
    or reaches it without a sentence end (a cut): the record is dropped and the note says so. Never a slice."""
    t = (text or "").strip()
    if len(t) > limit:
        return None, f"{what} dropped: {len(t)} characters, over the stored limit of {limit}; never cut"
    if len(t) == limit and not ends_at_sentence(t):
        return None, f"{what} dropped: it reaches the limit of {limit} characters without a sentence end ({CUT_WORD}); never cut"
    return t, None


def to_sentence_end(text: str, limit: int, what: str = "the text") -> tuple[str, str | None]:
    """A text at or over its limit without a sentence end is kept to its last sentence end, else to its last word boundary within the limit,
    with the note; a text under its limit, or at it with a sentence end, is returned whole with no note."""
    t = (text or "").strip()
    if len(t) < limit or (len(t) == limit and ends_at_sentence(t)):
        return t, None
    head = t[:limit]
    ends = [m.end() for m in re.finditer(r"""[.!?](?:["')\]]+)?(?=\s|$)""", head)]
    if ends:
        kept = head[:ends[-1]].rstrip()
        return kept, f"{what} kept to its last sentence end ({len(kept)} of {len(t)} characters; the record reached its limit of {limit})"
    kept = shorten_words(t, limit) if len(t) > limit else shorten_words(t, limit - 1)   # a text at exactly its cap is a cut whose last word is the cut one ("additional te"), so that word goes too; over the cap the next character tells whether the word at the limit is whole
    return kept, f"{what} kept to its last whole word ({len(kept)} of {len(t)} characters; no sentence end before the limit of {limit})"


def to_statement_end(text: str, limit: int, what: str = "the text", clause: bool = True) -> tuple[str, str | None]:
    """A prose cell at or over its limit without a sentence end (a cut: for example a cost estimate that stops inside its last word) is kept to
    its last sentence end; else to the end of its last whole clause (the words before its last semicolon); else it is dropped ("", the note says
    so). It never ends inside a word and never inside a sentence or a clause because of the limit. A text that ends at a sentence end, or stops
    two characters or more under its limit, is returned whole with no note. A text one character short of its limit without a sentence end is a
    cut too: the provider cut at the limit, the cut landed on a space, and the reading of the contract stripped it (for example a risk cell of
    299 characters that ended "... except for resale as a"). A text at its limit that ends with an abbreviation's point is a cut inside its
    sentence as well, and such a point is no sentence end to retreat to. clause=False is the rule of a text printed as sentences (the lead of
    the plan on one page): it ends at a sentence end or is dropped, never at a clause. From generation 4 of the document rules on
    (app.core.docrules), the point after a digit is read as a cut decimal one character short of the limit as well, the boundary of the other
    cuts the contract's reading shortened (for example a lead of 699 characters ending "charges at 0.")."""
    t = (text or "").strip(); n = len(text or "")                                    # the length as received: a cut that lands on a space is a cut too
    later = docrules.generation() >= 4                                               # a version of an earlier generation is composed under its own rules, as it was approved
    decimal = n >= (limit - 1 if later else limit) and re.search(r"\d\.$", t[:limit]) is not None   # a point after a digit at the limit may be a decimal point whose digits were cut ("billed at 0."): not taken as a sentence end
    if n < limit - 1 or (n <= limit and ends_at_sentence(t) and not decimal and not ABBREVIATION.search(t)):
        return t, None
    head = (t[:limit][:-1] if later else t[:limit - 1]) if decimal else t[:limit]    # without the point of the cut decimal, wherever it stands
    ends = [m.end() for m in re.finditer(r"""[.!?](?:["')\]]+)?(?=\s|$)""", head) if not ABBREVIATION.search(head[:m.end()])]   # the point of an abbreviation is no sentence end, neither at the limit nor before it (for example "in several standard formats, e.g." followed by "CSV and JSON.")
    if ends:
        kept = head[:ends[-1]].rstrip()
        return kept, f"{what} kept to its last sentence end ({len(kept)} of {len(t)} characters; the record reached its limit of {limit})"
    semi = head.rfind(";") if clause else -1
    if semi > 0 and head[:semi].strip():
        kept = head[:semi].rstrip()
        return kept, f"{what} kept to the end of its last whole clause ({len(kept)} of {len(t)} characters; no sentence end before the limit of {limit})"
    return "", f"{what} dropped: it reaches the limit of {limit} characters without a sentence end{' or a clause end' if clause else ''} ({CUT_WORD}); never cut"


LEAD_LIMIT = 700                                  # the contract's limit of the lead of the plan on one page (schemas/models.py LeadDraft.text)


def lead_whole(text: str) -> tuple[str, str | None]:
    """The lead of the plan on one page as whole sentences. A lead that ends at a sentence end stands whole whatever
    its length (the Editor may lengthen a lead past the planner's limit, app/core/editor.py rewrite_ok). A lead at its contract's limit (or one
    character short of it) or longer that ends inside a sentence arrived cut: it is kept to its last whole sentence, never to a clause, and
    with no whole sentence it is dropped (""); the note says what was left out. A shorter lead is returned as it is."""
    t = (text or "").strip()
    kept, note = to_statement_end(text, max(LEAD_LIMIT, len(text or "")), "the lead", clause=False)
    if note is None:
        return kept, None
    if kept:
        return kept, f"the lead kept to its last whole sentence ({len(kept)} of {len(t)} characters): the text ended inside a sentence, left out: {len(t) - len(kept)} characters"
    return "", f"the lead dropped: its {len(t)} characters hold no whole sentence and end inside one ({CUT_WORD}); never cut"
OPTION_CELLS = (("fit", 300), ("price", 200), ("monthly_cost", 120), ("risk", 300))   # the prose cells of a memo's options table with the contract's limits (schemas/models.py MemoOption)


def shorten_words(text: str, limit: int) -> str:
    """The text whole when it fits, else its first words up to the limit at a word boundary (no cut inside a word)."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else t[:limit]).rstrip(" ,;:")


def title_of(idea: str, limit: int = 200) -> str:
    """A project's title from its idea: the first sentence whole up to the schema's maximum, shortened at a word boundary where it must be,
    never cut inside a word (for example a title stored cut at 80 characters, "...city tri")."""
    first = (idea or "").strip().splitlines()[0].lstrip("# ") if (idea or "").strip() else ""
    sentence = first.split(".")[0].strip() or first.strip()
    return shorten_words(sentence, limit) or shorten_words((idea or "").strip(), limit)
