"""The generation of the rules a plan version's figures and documents are composed under.

A correction of a rule that composes what a founder reads (a price reading of the budget, the PRD's count of unresolved checks, the end of
a cell's text) changes what an approved version's documents say, and an approval covers the documents as they were composed when the
founder approved them. So every version carries the generation it was written under (the snapshot's document_rules, written by
app.core.snapshot.build; a version that carries none is generation 1), and the document model (part of the document renderer, which is not in this repository) composes a version
under its own generation: an approved version's content signatures recompute byte for byte, its documents are produced as approved, and
the corrected rules reach a founder with the next version the product writes (a run, a requirement change, a usage correction).

Generation 2: a figure that counts a unit is no price; the PRD's unresolved checks count every decision's own conditional line; a cost
sentence that arrived cut at its limit ends at a sentence or a clause.

Generation 3: the lead of the plan on one page ends at a sentence end and the notes say what was left out; the reason of a part without
a price names its state (an open decision, or a recommended pick whose monthly cost is not established); a run that has ended is never
printed as running; every row of the questions and answers states what the founder gave, and a correction is named as one.

Generation 4: a point after a digit one character short of a text's limit is a cut decimal too; the bound of a recommended pick's own
figure travels to its rows and to the totals that hold it.

Generation 5: the line on a version's decisions counts recommended picks, not settled decisions; a day's line of the first week carries
its task's title only where the task changes, and the line of what follows the week names each task with its effort.

Generation 6: a day of the first week that holds several steps carries a task's title once, where the task changes, within the day
and across days.

Generation 7: among an option's sourced flat monthly prices, the row of the plan that the pick's own words identify, by its monthly
price or by its quantities with their units, stands before the lowest fee (app/core/budget.py, identified_rows); the generations
before read the plan's words alone. An amount in the pick's words names a plan's monthly price only where its own words give it the
month as its cadence, or no cadence and no other role (once, a year, per user, a deposit, a setup payment name no plan); a quantity
with its unit names its plan whatever preposition stands before it (app/core/budget.py, _monthly_price and plan_marks).

Code that runs outside a version's composition (the workflow writing a new version) runs under the current generation."""
import contextlib
import contextvars

CURRENT = 7
_generation: contextvars.ContextVar[int] = contextvars.ContextVar("nmb_document_rules", default=CURRENT)


def of(snapshot: dict | None) -> int:
    """The generation a snapshot was written under: its document_rules, 1 when it carries none (a version written before generations were recorded)."""
    return int((snapshot or {}).get("document_rules") or 1)


def generation() -> int:
    """The generation in force: the composed version's own inside composing(), else the current one."""
    return _generation.get()


@contextlib.contextmanager
def composing(snapshot: dict | None):
    """Compose under the snapshot's own generation: every rule that asks generation() inside reads the version's, not the code's."""
    token = _generation.set(of(snapshot))
    try:
        yield
    finally:
        _generation.reset(token)
