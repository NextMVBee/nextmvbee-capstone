"""The Editor's pure parts and the content rules that code enforces before a version is stored. The Editor is the model pass
that edits the wording of a new plan version (app/graph/workflow.py runs it over the whole reader content); it is responsible
for the editorial quality of the whole document of each family.

Collecting the content:
  collect_fields takes the whole reader content of a version by path, in reading order (the lead, the plan body, the confirmed
  requirements' display texts, the memos, the budget's assumptions, the components, the tasks, the acceptance criteria, the
  risks, the verification plan, the first week, the open items).
  fields_from_snapshot builds the same paths from a stored snapshot (for the maintenance entry, which runs the Editor over a
  stored version; its caller is not part of this repository).

Checking and applying the Editor's answer:
  rewrite_ok is the hard checks on one rewritten sentence: the same claim ids, the same numbers, no product name outside the
  candidates, no new fact (no new proper noun at any position of the sentence), no em dash, at most the ceiling of 1.3 times
  the original's own length; a changed number or product name is accepted only when the new value is a current recommendation
  or a cited supported claim's value.
  drop_decision applies the Editor's drops under the write scope (never a requirement, an acceptance criterion, a verification
  check or an open item of an open decision; a field whose claim is cited by no kept field stays and is listed).
  apply pours the kept rewrites, merges and drops back into the records.
  completeness_line, warnings_of and correction_path make what the Editor could not settle visible, never silent.

Helpers:
  provenance_class, provenance_of and numbers_with_kinds read the provenance class of every reader path from the records'
  statuses, so the Editor words each field by its kind (the flag kind provenance).
  dedupe_by_meaning: duplicates by meaning out of a list ("what would settle it:" prefixes stripped).
  memo_monthly_costs: the per-option monthly cost at the founder's usage from the option's price claims of a tariff the
  subject's class consumes, the calculation stored beside the figure (code computes, never the model).
  machine_language: the words a reader field may not carry."""
from __future__ import annotations

import math
import re
from collections import Counter

EM_DASH = chr(0x2014)
RECORD_ID = re.compile(r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9_-]+\.)?(?:CLM|SRC|REQ|DEC|RQ|TSK|CMP|ACC|ENT|IFC|REL|VCK|ASM|Q|DRV|EX|PJ)-[0-9a-f]{3,}\b")   # with or without the project prefix (a bare id such as DEC-056 is a record id too); a claim id with its prefix is a citation, bare it is a record id
CLAIM_ID = re.compile(r"\b[A-Za-z0-9_-]+\.CLM-\d{3,}\b")
NUMBER = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")
MACHINE = [r"exploration tree", r"\bbranch\(es\)", r"\bbranch(?:es)?\b(?=.*\bscore\b)", r"supports_requirements", r"coverage verdict", r"\bneeds review\b", r"needs_review", r"continuation limit",
           r"names no product_id", r"product_id", r"\b[a-z]+_[a-z]+(?:_[a-z]+)*\b", r"\bflagged:", r"finish_reason", r"\bstage call\b"]
MACHINE_RE = [re.compile(p, re.I) for p in MACHINE]
SETTLE_PREFIX = re.compile(r"^\s*what would settle it:\s*", re.I)
EXCLUDED_LINE = re.compile(r" (is not an option|are not options) of this decision: ")       # the exclusion line that code composes for a memo's open list (app.graph.workflow _excluded_open)
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by", "at", "from", "as", "is", "are", "be", "that", "this", "it", "its", "we", "you", "your", "our",
        "day", "days", "hour", "hours", "usd", "month", "a month", "per", "under", "over", "after", "before", "first", "week", "one", "two", "three", "four", "five"}
OPENERS = {"each", "every", "all", "both", "some", "any", "no", "not", "none", "when", "if", "while", "because", "since", "then", "there", "here", "so", "but", "yet",
           "also", "even", "still", "what", "which", "who", "whom", "whose", "how", "why", "where", "than", "into", "onto", "out", "up", "down", "off", "about", "above",
           "below", "between", "through", "during", "without", "within", "until", "unless", "once", "only", "can", "could", "may", "might", "must", "shall", "should",
           "will", "would", "do", "does", "did", "has", "have", "had", "was", "were", "been", "being", "am", "they", "them", "their", "he", "she", "his", "her", "him",
           "us", "me", "my", "mine", "i", "these", "those", "such", "most", "many", "few", "more", "less", "other", "another", "same", "own", "very", "just", "now",
           "never", "always", "often", "again", "already", "too", "either", "neither", "nor", "whether", "although", "though", "however", "therefore", "thus", "hence",
           "yes", "nothing", "something", "everything", "anything", "whoever", "whatever", "whenever", "wherever", "today", "tomorrow", "yesterday", "later", "earlier"}
# the function words of English a sentence may open with: never a product name, so a rewrite may open with one the original lacks


def list_of(path: str) -> str:
    """The list a path belongs to (merges join fields of one list): "memo:devA.DEC-001:open[2]" is "memo:devA.DEC-001:open"; "risk[1]" is "risk"."""
    return re.sub(r"\[\d+\]$", "", path)


def _numbers(text: str) -> list[str]:
    """The numbers of a sentence, a claim id's digits aside ("(devA.CLM-001)" is a citation, not a figure)."""
    return sorted(n.replace(",", "").rstrip(".") for n in NUMBER.findall(CLAIM_ID.sub(" ", text or "")))


def _proper_nouns(text: str) -> set[str]:
    """Capitalised words at every position of a sentence (a product or vendor name the sentence introduces), the function words of English
    aside: the first word is checked like any other (in "Deepgram supports Hebrew." the opening word is a product name)."""
    out = set()
    for sent in re.split(r"(?<=[.!?:;])\s+", CLAIM_ID.sub(" ", text or "")):             # a claim id is a citation, never a name (a rewrite may cite a newer supported claim)
        for w in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*", sent):
            if w[0].isupper() and w.lower() not in STOP and w.lower() not in OPENERS and len(w) > 1:
                out.add(w.lower().rstrip("."))
    return out


def machine_language(text: str) -> list[str]:
    """The machine words a reader field carries: record ids outside claim ids, the tree's vocabulary, snake_case keys."""
    hits = []
    plain = CLAIM_ID.sub(" ", text or "")
    if RECORD_ID.search(plain):
        hits.append("a record id outside a claim id")
    for rx in MACHINE_RE:
        m = rx.search(plain)
        if m and not (rx.pattern.startswith(r"\b[a-z]+_") and m.group(0) in ("e_mail",)):
            hits.append(m.group(0))
    return hits


def _context_only(nums_o: list[str], nums_r: list[str], record_numbers: set | None) -> bool:
    """Whether the rewrite keeps every number of the original (as a multiset) and adds only figures the records hold (the
    budget's and the founder's usage), as the context of a figure it keeps; a changed or dropped number is never context."""
    if not record_numbers:
        return False
    co, cr = Counter(nums_o), Counter(nums_r)
    added = list((cr - co).elements())
    return bool(added) and not list((co - cr).elements()) and all(n in record_numbers for n in added)


def record_numbers(block: dict | None, usage: dict | None) -> set[str]:
    """The figures the records hold, normalised as _numbers reads them (50 and 50.00 both), that a rewrite or a summary statement may add as
    the context of a figure it keeps: the budget's total, ceiling and worst case, each cost row's monthly figure and each worst-case delta, the
    founder's usage numbers (meetings, minutes, users, recipients, the summary length). Never a figure of the Editor's own."""
    out: set[str] = set()

    def add(v) -> None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        for t in (f"{f:g}", f"{f:.2f}"):
            out.update(_numbers(t))
    block = block or {}
    for k in ("total_usd", "ceiling_usd", "worst_case_total_usd"):
        add(block.get(k))
    for r in block.get("rows") or []:
        add(r.get("monthly_usd"))
    for w in block.get("worst_case") or []:
        add(w.get("delta_usd"))
    for k in ("meetings_per_month", "minutes_per_meeting", "users", "recipients_per_summary", "summary_words"):
        add((usage or {}).get(k))
    return out


def rewrite_ok(original: str, rewrite: str, candidates: list[str], recommended: list[str] | None = None, claim_values: dict | None = None, record_numbers: set | None = None) -> tuple[bool, str]:
    """The hard checks on one rewritten sentence. With recommended (the decision records' current
    recommendation labels) a new product name is accepted only when it is one of them, never any candidate; with claim_values (supported claim
    id -> its value, text and checked date) a changed claim id or number is accepted only when every claim id of the rewrite is a supported claim, every
    number the rewrite adds appears in a claim it cites and that carrying claim was checked after the newest claim the original cited, so a statement
    superseded by a later decision or a later reading is replaced and nothing else changes a fact. With record_numbers
    (the budget's figures and the founder's usage, app.core.editor.record_numbers) a rewrite may add one of them as the context of a figure it keeps
    (the cap a price stands under, the usage a cost is priced at); it still may not change or drop a number, and never adds a figure the records do not hold."""
    o, r = (original or "").strip(), (rewrite or "").strip()
    if not r:
        return False, "empty"
    if EM_DASH in r:
        return False, "an em dash"
    if len(r) > math.ceil(1.3 * len(o)):                                                   # 1.3 times the original's actual length, with no floor for a short sentence
        return False, f"longer than 1.3 times the original ({len(r)} against {len(o)})"
    ids_o, ids_r = sorted(set(CLAIM_ID.findall(o))), sorted(set(CLAIM_ID.findall(r)))      # as sets: a merge of two fields citing one claim keeps the union
    if ids_o != ids_r:
        if not claim_values or any(c not in claim_values for c in ids_r):
            return False, "the claim ids differ"
    names_allowed = recommended if recommended is not None else candidates
    def _without_names(t: str) -> str:                                                      # a figure inside an allowed product name ("Orion Compute 1", "Claude Haiku 5.5") is the name's, not a number of the sentence
        for nm in sorted((n0 for n0 in names_allowed if n0), key=len, reverse=True):
            t = re.sub(re.escape(nm), " ", t, flags=re.I)
        return t
    nums_o, nums_r = _numbers(_without_names(o)), _numbers(_without_names(r))
    if nums_o != nums_r and not _context_only(nums_o, nums_r, record_numbers):            # a record figure added as context passes; anything else meets the claim rules below
        added = [n for n in nums_r if n not in nums_o]
        cited_text = " ".join(f"{(claim_values or {}).get(c, {}).get('value', '')} {(claim_values or {}).get(c, {}).get('text', '')}" for c in ids_r)
        if not claim_values or not ids_r or (ids_o == ids_r) or not added or any(n not in _numbers(cited_text) for n in added):
            return False, f"the numbers differ ({nums_o} against {nums_r})"
        newest_o = max((str((claim_values or {}).get(c, {}).get("checked_at") or "") for c in ids_o), default="")   # the new value must come from a claim checked after the newest claim the sentence cited; an older reading supersedes nothing
        def _newer(c: str) -> bool:                                                         # the claim that carries the added number is the one that must be newer; a retained older citation gains nothing from an unrelated newer citation added beside it
            d = str(claim_values[c].get("checked_at") or "")
            return not d or not newest_o or d > newest_o
        for n in added:
            carriers = [c for c in ids_r if n in _numbers(f"{claim_values[c].get('value', '')} {claim_values[c].get('text', '')}")]
            if not any(_newer(c) for c in carriers):
                return False, f"the numbers differ ({nums_o} against {nums_r}) and the claim cited for the new value is older than the sentence's own citation ({', '.join(carriers) or n} checked {claim_values[carriers[0]]['checked_at'] if carriers else 'nowhere'}, the original's newest {newest_o})"
    allowed = {w.lower() for c in names_allowed for w in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*", c or "")}
    new_names = _proper_nouns(r) - _proper_nouns(o) - allowed - {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9.+-]*", o)}
    if new_names:
        return False, f"a name the original does not carry: {', '.join(sorted(new_names)[:3])}"
    hits = machine_language(r)
    if hits:
        return False, f"machine language: {hits[0]}"
    return True, ""


def dedupe_by_meaning(items: list[str], threshold: float = 0.6) -> list[str]:
    """Duplicates by meaning out of a list: the "what would settle it:" prefix stripped, then the token-overlap rule of app.graph.workflow.same_meaning."""
    out: list[str] = []

    def toks(s):
        return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2 and w not in STOP}
    for raw in items or []:
        x = SETTLE_PREFIX.sub("", str(raw)).strip()
        if not x:
            continue
        if EXCLUDED_LINE.search(x):                                                        # the code's exclusion lines (one per pool rule's reason) are facts, not prose: a repeat is the same text, never a token overlap, so two reasons sharing their wording both stand
            if x not in out:
                out.append(x)
            continue
        tx = toks(x)
        dup = False
        for y in out:
            ty = toks(y)
            if not tx or not ty:
                continue
            j = len(tx & ty) / len(tx | ty)
            if j >= threshold or ((tx <= ty or ty <= tx) and min(len(tx), len(ty)) >= 4):
                dup = True; break
        if not dup:
            out.append(x)
    return out


def dedupe_memo(memo: dict) -> bool:
    changed = False
    for k in ("open", "change_mind"):
        if isinstance(memo.get(k), list):
            new = dedupe_by_meaning(memo[k])
            if new != memo[k]:
                memo[k] = new; changed = True
    return changed


# ---------------------------------------------------------------- price presence from the pricing record
PRICE_DENIAL = re.compile(r"\b(?:not found|were not found|was not found|no verified price|no price|unpriced|price unknown|not verified|could not be found|not in the evidence|not available|unavailable|not recorded)\b", re.I)
GENERIC_OPTION_WORDS = {"api", "the", "via", "model", "plan", "service", "tier", "cloud", "hosted", "self", "open", "source", "free", "paid", "pro", "on", "and", "or", "of", "a", "an", "with", "for", "by", "in", "to", "its", "their", "version", "edition"}
UNIT_SUFFIX = {"million_tokens_in": " (input)", "million_tokens_out": " (output)"}
def option_words(option_label) -> set:
    """The words of an option's label that name its product (the generic words dropped; the version's digits kept)."""
    from app.core.budget import words_of
    return {w for w in words_of(option_label) if w not in GENERIC_OPTION_WORDS}


def denies_price(sentence: str, option_label=None) -> bool:
    """Whether a sentence denies a price ("not found", "no verified price", "unpriced" beside the words price, cost or tariff); with an option
    label, only when every product word of the label is in the sentence (the sentence is about that product)."""
    text = str(sentence or "")
    if not PRICE_DENIAL.search(text) or not re.search(r"\b(?:price|prices|priced|pricing|cost|costs|tariff|tariffs)\b", text, re.I):
        return False
    if option_label is None:
        return True
    from app.core.budget import words_of
    words = option_words(option_label)
    return bool(words) and words <= set(words_of(text))


def _pricing_clause(rows: list[dict], figure: str | None, usage_words: str) -> str:
    """The tariffs with their claim ids, the billing model when the record holds one, and the figure at the founder's usage, as one clause."""
    tariffs = "; ".join(f"{str(r.get('unit_price') or '').strip()}{UNIT_SUFFIX.get(r.get('unit'), '')}" for r in rows if str(r.get("unit_price") or "").strip())
    ids = ", ".join(dict.fromkeys(r.get("claim") or (r.get("claims") or [None])[0] for r in rows if r.get("claim") or r.get("claims")))
    billing = next((r.get("billing") for r in rows if isinstance(r.get("billing"), dict)), None)
    from app.core.budget import allowance_text
    model = (f", a credit pack of {billing['pack_usd']:.2f} USD for {billing['pack_size']:,.0f} {billing['pack_unit']}" + (f" valid {float(billing['expiry_months']):g} months, allocated over its validity" if billing.get("expiry_months") else "")
             + (f", {allowance_text(billing)}" if billing.get("free") else "")) if billing else ""   # the allowance in the record's one sentence
    fig = f", about {figure} USD a month at {usage_words}" if figure else ""
    return f"{tariffs}{model}{fig} ({ids})"


def pricing_sentence(option_label: str, rows: list[dict], figure: str | None, usage_words: str = "your usage") -> str:
    """The pricing record's sentence for one option: the tariffs with their claim ids, the billing model when recorded, and the figure
    at the founder's usage labelled an estimate."""
    return f"Price sourced: {option_label} at {_pricing_clause(rows, figure, usage_words)}; the figure is an estimate."


def pricing_sentences(priced: list[tuple], usage_words: str = "your usage") -> str:
    """One sentence for every option a denial named (the plan on one page carries it, so it stays short)."""
    if len(priced) == 1:
        return pricing_sentence(priced[0][0], priced[0][1], priced[0][2], usage_words)
    return "Prices sourced: " + "; ".join(f"{label} at {_pricing_clause(rows, fig, usage_words)}" for label, rows, fig in priced) + "; the figures are estimates."


def price_presence(sentences: list, priced: list[tuple], usage_words: str = "your usage", allowance_questions: list[dict] | None = None) -> tuple[list, list[dict]]:
    """The sentences with every one that denies a price for a product whose option carries
    traced tariff rows replaced by the pricing record's sentence (one sentence for every option the denial named); priced: (option label, rows,
    figure, product id). A sentence that denies nothing is replaced only when it is the allowance
    question the costing code itself wrote, identified by its stored record (allowance_questions, the memo key the code writes under: {product_id,
    text}) and the product id of a priced option whose rows carry a free allowance, never by its words; a question the model or the founder wrote
    is never replaced; the replacement names the record and the claim that resolves it. Returns the new list and the replacements made
    ({was, now, claims} and, for a resolved record, record)."""
    records = [q for q in (allowance_questions or []) if isinstance(q, dict) and q.get("product_id") and isinstance(q.get("text"), str)]
    out, replaced = [], []
    for x in sentences:
        if not isinstance(x, str):
            out.append(x); continue
        hits = [(label, rows, fig) for label, rows, fig, _pid in priced if rows and denies_price(x, label)]
        record = None if hits else next((q for q in records if q["text"] == x), None)
        if record is not None:                                                                # the code's own allowance question, resolved by the record of the same product that holds the allowance
            hits = [(label, rows, fig) for label, rows, fig, pid in priced if rows and pid == record["product_id"] and any(isinstance(r.get("billing"), dict) and r["billing"].get("free") for r in rows)]
        if not hits:
            out.append(x); continue
        now = pricing_sentences(hits, usage_words)
        claims = sorted({r.get("claim") or (r.get("claims") or [None])[0] for _, rows, _ in hits for r in rows if r.get("claim") or r.get("claims")})
        rep = {"was": x, "now": now, "claims": claims}
        if record is not None:
            rep["record"] = {"key": "allowance_questions", "product_id": record["product_id"]}
        replaced.append(rep); out.append(now)
    return out, replaced


def memo_monthly_costs(memo: dict, price_rows: list[dict], usage: dict | None, service_class: str | None = None) -> bool:
    """Every memo option whose product has a supported price claim gets its monthly cost at the founder's usage, computed by code from the
    decision's typed service class: only a tariff whose unit the class consumes is used (audio hours or minutes for
    speech to text, tokens for a model, a flat plan fee for email or hosting; app.core.budget.units_for), the rows selected as the budget selects
    them (app.core.budget.select_rows), and the calculation (claims, unit, usage, amount) is stored beside the figure as monthly_cost_calc for the renderer.
    A tariff in audio tokens has no supported conversion from transcript tokens and is never used. Without a class the rules
    know, no figure is computed and the model's words stand. Once an option's figure rests on traced rows, a
    sentence of memo.open, memo.change_mind or the option's price field that denies a price for that product is replaced by the pricing record's
    sentence (the tariffs, their claim ids, the figure labelled an estimate), noted on memo.replaced_sentences and counted in dropped_sentences.
    Returns whether the memo changed."""
    if not usage:
        return False
    from app.core import budget as BG
    from app.core.coverage import same_product
    try:
        u = BG.usage_from_numbers(usage)
    except Exception:  # noqa: BLE001
        return False
    item = str(service_class or "")
    if BG.units_for(item) is None:
        return False
    changed = False
    priced: list[tuple] = []                                                                 # (option label, the calculation's rows, the figure, the product id) of every option with rows
    for o in memo.get("options") or []:
        pid, ver = o.get("product_id") or "", o.get("product_version") or ""
        if not pid:
            continue
        mine = [c for c in price_rows if same_product(pid, ver, c.get("product_id"), c.get("product_version"), o.get("option", ""), c.get("text") or "")]   # the option's label and the claim's words decide the identity as the memo does
        judged = memo.get("budget") if isinstance(memo.get("budget"), dict) and same_product(pid, ver, (memo.get("recommendation") or {}).get("product_id"), (memo.get("recommendation") or {}).get("product_version")) else None
        if judged and isinstance(judged.get("input_claim_ids"), list):                      # the recommended option's figure rests on the claims its budget judgement was given, so the option's monthly cost is the budget sentence's figure
            mine = [c for c in price_rows if c.get("id") in set(judged["input_claim_ids"])]
        sel = BG.option_rows(item, (judged or {}).get("option") or o.get("option", ""), mine, u, (judged or {}).get("names"))   # the one selection of an option's price rows, with the judgement's plan words
        if not sel:
            continue
        foreign = [r for r in sel if r.get("currency")]
        if foreign:                                                                          # a price in another currency is stated in its own currency and is no US dollar figure: no calculation is stored
            words = "; ".join(f"{float(r['monthly_amount']):.2f} {r['currency']} a month" for r in foreign) + " (in its own currency, not converted)"
            if o.get("monthly_cost") != words or "monthly_cost_calc" in o:
                o["monthly_cost"] = words; o.pop("monthly_cost_calc", None); changed = True
            continue
        amount = sum(float(r.get("exact") or 0) for r in sel)
        figure = f"{amount:.2f}"
        calc = {"service_class": item, "amount": figure, "claims": sorted({c for r in sel for c in r["claims"]}),
                "rows": [{"claim": r["claims"][0], "unit": r["unit"], "unit_price": r["unit_price"], "usage": r["usage"], "monthly_usd": r["monthly_usd"], **({"range": r["range"]} if r.get("range") else {})} for r in sel]}   # a range row keeps its sourced plan prices
        if (o.get("monthly_cost") or "").strip() != figure:
            o["monthly_cost"] = figure; changed = True
        if o.get("monthly_cost_calc") != calc:
            o["monthly_cost_calc"] = calc; changed = True
        for cid in calc["claims"]:
            if cid not in (o.get("claim_ids") or []):
                o.setdefault("claim_ids", []).append(cid); changed = True
        crow = [{**r, "billing": next((x.get("billing") for x in sel if x["claims"][0] == r["claim"] and x.get("billing")), None)} for r in calc["rows"]]
        priced.append((o.get("option", ""), crow, figure, pid))
        if denies_price(o.get("price") or ""):                                              # the option's own price field that denies a price is replaced by the record's sentence
            now = pricing_sentence(o.get("option", ""), crow, figure)
            memo.setdefault("replaced_sentences", []).append({"was": o["price"], "now": now, "claims": list(calc["claims"])}); o["price"] = now; changed = True
            memo["dropped_sentences"] = int(memo.get("dropped_sentences") or 0) + 1
    for k in ("open", "change_mind"):                                                         # a sentence of the memo's lists that denies a price for a priced option's product
        if isinstance(memo.get(k), list) and priced:
            new, rep = price_presence(memo[k], priced, allowance_questions=memo.get("allowance_questions"))   # the memo's own allowance-question records, when it holds any
            if rep:
                memo[k] = new; memo.setdefault("replaced_sentences", []).extend(rep); memo["dropped_sentences"] = int(memo.get("dropped_sentences") or 0) + len(rep); changed = True
    return changed


PROTECTED_PREFIXES = ("requirement:", "acceptance:", "verification:")   # never dropped (uncertainty and the records preserved); their display text may be rewritten under rewrite_ok
RECORD_ONLY_PREFIXES = ("requirement:", "open[", "budget:")                # outside the Editor's write scope: a rewrite is recorded, never applied (a requirement's value changes through the clarify and revision paths; the open items and the budget's assumptions are code-composed records read by the draft marks and the budget)
KIND_WORDS = ("contradiction", "outdated", "source_error", "presentation", "provenance")   # provenance: a text that states a proposal or an assumption as a fact, or a fact as a guess
PROVENANCE_CLASSES = ("evidence", "founder", "assumption", "calculation", "proposal", "decision", "open")   # the seven classes, each a read of an existing status; no new record, no new status
ASSESSMENT_ORDER = ("correctness", "faithfulness and gaps", "fit to the reader and the stage", "clarity", "presentation")   # the review order of the assessment paragraph, presentation last
MEMO_PATH = re.compile(r"^memo:([^:]+):(why\[\d+\]|option\[\d+\]\.(fit|risk|monthly_cost)|change_mind\[\d+\]|open\[\d+\])$")
INCOMPLETE = "incomplete"
COMPLETE = "complete"


def provenance_class(path: str, text: str = "", approved_decisions: set | frozenset = frozenset(), cited=(), claim_status: dict | None = None) -> str:
    """The provenance class of one reader path, read from the records' statuses and never from the
    words of the text: founder (a confirmed requirement's display text; the budget's ceiling is the founder's confirmed budget), assumption (the
    budget's assumption lines), calculation (the budget's total and worst case, a cost row, a memo option's monthly cost: code computed them), open
    (the plan's open lines the documents print and a memo's open items, of a decided and of an open decision alike; and a field citing a claim whose
    recorded status is not supported, inline or by the memo's stored citation: the claim's status is the open matter), evidence (a field that cites
    claims, inline or by the memo's stored citation beside a why sentence, every one of them recorded as supported in claim_status), decision (a
    memo's own texts when the record is active and an approved version carries the decision: approved_decisions holds the decided ids), proposal (a
    memo's own texts of a decision no approved version carries or whose record is unresolved, and the plan's own fields: the planner's proposal)."""
    p = path or ""
    if p.startswith("requirement:") or p == "budget:ceiling_usd":
        return "founder"
    if p.startswith("budget:assumption["):
        return "assumption"
    if p.startswith("budget:"):
        return "calculation"
    if p.startswith("open["):
        return "open"
    m = MEMO_PATH.match(p)
    if m and m.group(2).startswith("open["):
        return "open"
    if m and m.group(2).endswith(".monthly_cost"):
        return "calculation"
    ids = set(CLAIM_ID.findall(text or "")) | set(cited or ())
    if ids:
        return "evidence" if all((claim_status or {}).get(c) == "supported" for c in ids) else "open"   # a citation is evidence only when every cited claim's recorded status is supported; otherwise the claim's status is the open matter
    if m:
        return "decision" if m.group(1) in approved_decisions else "proposal"
    return "proposal"


def provenance_of(fields: dict, memos: list[dict], approved_decisions: set | frozenset = frozenset(), claim_status: dict | None = None) -> dict:
    """Every reader path with its provenance class; the memos' stored citations (why[i].claim_ids) count as citations of their sentence.
    A decision in approved_decisions is decided only while its record's status is active (an unresolved record under an
    approved version keeps its texts proposals), and claim_status (claim id to recorded status) decides evidence: every cited claim supported."""
    stored, decided = {}, set()
    for d in memos or []:
        m = d.get("memo") or {}
        if d.get("id") in approved_decisions and d.get("status") == "active":
            decided.add(d["id"])
        for i, w in enumerate(m.get("why") or []):
            if isinstance(w, dict) and w.get("claim_ids"):
                stored[f"memo:{d['id']}:why[{i}]"] = list(w["claim_ids"])
    return {p: provenance_class(p, t, decided, stored.get(p, ()), claim_status) for p, t in fields.items()}


def assessment_order(text: str) -> list[str]:
    """The review-order labels an assessment paragraph carries, in the order they appear (each label read where it opens a finding, followed
    by a colon); a paragraph without them has an empty order. Code reads the order and makes a deviation visible; it never rewrites the paragraph."""
    low = (text or "").lower()
    found = []
    for label in ASSESSMENT_ORDER:
        i = low.find(label + ":")
        if i >= 0:
            found.append((i, label))
    return [label for _, label in sorted(found)]


def whole_stack(block: dict | None, decisions: list[dict]) -> dict | None:
    """The budget block as the Editor reads it. The whole-stack worst case is decided once: the budget
    states none while a pick has no priced row (app.core.budget.budget_of), and a version's open decision is a part the stack waits for
    as well (the document model's rule): with a current decision that is not an active recommendation the block carries no worst
    case. Current as the model reads it: neither stale nor rejected, the latest record of its subject."""
    latest: dict = {}
    for d in decisions or []:
        n = int((re.search(r"(\d+)$", str(d.get("id") or "")) or [0, 0])[1])
        if d.get("status") not in ("stale", "rejected") and n >= latest.get(d.get("subject"), (0, None))[0]:
            latest[d.get("subject")] = (n, d)
    waits = any(d.get("status") != "active" or not ((d.get("memo") or {}).get("recommendation") or {}) for _, d in latest.values())
    return dict(block, worst_case_total_usd=None) if block and waits else block


def numbers_with_kinds(block: dict | None, usage: dict | None = None) -> dict:
    """The budget's figures the Editor reads beside the fields, each with its kind: the total and the worst case are calculations, the ceiling is
    the founder's; with usage, the founder's usage numbers (kind founder), the context a monthly figure is priced at."""
    block = block or {}
    out = {k: {"value": block.get(k), "kind": provenance_class(f"budget:{k}")} for k in ("total_usd", "ceiling_usd", "worst_case_total_usd")} if block else {}
    if usage:
        out["usage"] = {"value": {k: usage.get(k) for k in ("meetings_per_month", "minutes_per_meeting", "users", "recipients_per_summary", "summary_words") if usage.get(k) is not None}, "kind": "founder"}
    return out


def document_open_lines(lines: list) -> list[str]:
    """The open items the documents print from the plan's unresolved checks (the document model of the renderer, which is not part of this repository: the stop lines and the
    budget's ceiling issue); the coverage report's lines ("Conflicting evidence on ...", "Not covered: ...", the per-requirement checks) are
    machine records the appendix tables derive from, not reader sentences, and are not the Editor's input (one version can carry hundreds of them)."""
    from app.core.coverage import STOP_ISSUE_PREFIXES
    return [x for x in (lines or []) if isinstance(x, str) and (x.startswith(STOP_ISSUE_PREFIXES) or x.startswith("The plan's monthly total of "))]


def requirement_display(key: str, value) -> str:
    """The display text of a confirmed requirement as the documents print it: the key's words and the value."""
    words = str(key or "").replace("_", " ").strip()
    v = value if isinstance(value, str) else ("; ".join(str(x) for x in value) if isinstance(value, list) else ("" if value is None else str(value)))
    return f"{words[:1].upper() + words[1:]}: {v}".strip(": ") if v else (words[:1].upper() + words[1:])


def _memo_fields(f: dict, did: str, m: dict) -> None:
    for i, w in enumerate(m.get("why") or []):
        f[f"memo:{did}:why[{i}]"] = w.get("text", "") if isinstance(w, dict) else str(w)
    for i, o in enumerate(m.get("options") or []):
        for k in ("fit", "risk"):
            if o.get(k):
                f[f"memo:{did}:option[{i}].{k}"] = o[k]
    for i, x in enumerate(m.get("change_mind") or []):
        f[f"memo:{did}:change_mind[{i}]"] = x
    for i, x in enumerate(m.get("open") or []):
        f[f"memo:{did}:open[{i}]"] = x


def collect_fields(plan, extras: dict, memos: list[dict], week: list[str]) -> dict:
    """The reader fields of a version by path, in reading order (the whole reader content): the lead, the plan body, the
    confirmed requirements' display texts (extras["requirements"]: id, key, value), the memos, the budget's assumption lines
    (extras["budget_assumptions"]), the components' responsibilities (extras["components"]: id, responsibility), the tasks' titles
    (extras["tasks"]: id, title), the acceptance criteria (extras["acceptance"]: id, criterion), the risks, the verification plan, the first
    week and the open items the document derives (extras["open_items"])."""
    f: dict[str, str] = {}
    lead = extras.get("lead")
    if lead and lead.get("text"):
        f["lead"] = lead["text"]
    f["problem"] = plan.problem; f["users"] = plan.users if isinstance(plan.users, str) else "; ".join(plan.users or [])
    for i, x in enumerate(plan.scope_in):
        f[f"scope_in[{i}]"] = x
    for i, x in enumerate(plan.scope_out):
        f[f"scope_out[{i}]"] = x
    for i, x in enumerate(plan.user_flow):
        f[f"flow[{i}]"] = x
    for r in extras.get("requirements") or []:
        f[f"requirement:{r['id']}"] = requirement_display(r.get("key"), r.get("value"))
    for d in memos:
        m = d.get("memo") or {}
        if m:
            _memo_fields(f, d["id"], m)
    for i, x in enumerate(extras.get("budget_assumptions") or []):
        f[f"budget:assumption[{i}]"] = x
    for c in extras.get("components") or []:
        if c.get("responsibility"):
            f[f"component:{c['id']}.responsibility"] = c["responsibility"]
    for t in extras.get("tasks") or []:
        if t.get("title"):
            f[f"task:{t['id']}.title"] = t["title"]
    for a in extras.get("acceptance") or []:
        if a.get("criterion"):
            f[f"acceptance:{a['id']}"] = a["criterion"]
    for i, x in enumerate(extras.get("risks") if extras.get("risks") is not None else plan.risks):
        f[f"risk[{i}]"] = x
    for v in extras.get("verification_ids") or []:
        for k in ("check", "how", "pass"):
            f[f"verification:{v['id']}.{k}"] = v[k]
    for i, x in enumerate(week):
        f[f"week[{i}]"] = x
    for i, x in enumerate(document_open_lines(extras.get("open_items") or [])):
        f[f"open[{i}]"] = x
    return {k: v for k, v in f.items() if isinstance(v, str) and v.strip()}


def fields_from_snapshot(snap: dict) -> dict:
    """The same paths from a stored snapshot (the maintenance entry): the lead, the plan body, the confirmed requirements, the current decisions'
    memos, the budget's assumptions, the components, the tasks, the acceptance tests, the risks, the verification plan, the first week, the open items."""
    body = snap.get("plan_body") or {}
    f: dict[str, str] = {}
    lead = snap.get("lead") or {}
    if lead.get("text"):
        f["lead"] = lead["text"]
    f["problem"] = body.get("problem") or ""; users = body.get("users"); f["users"] = users if isinstance(users, str) else "; ".join(users or [])
    for key, prefix in (("scope_in", "scope_in"), ("scope_out", "scope_out"), ("user_flow", "flow")):
        for i, x in enumerate(body.get(key) or []):
            f[f"{prefix}[{i}]"] = x
    for r in snap.get("requirements") or []:
        if r.get("status") == "confirmed":
            f[f"requirement:{r['id']}"] = requirement_display(r.get("key"), r.get("value"))
    for d in snap.get("decisions") or []:
        if d.get("status") in ("active", "unresolved") and d.get("memo"):
            _memo_fields(f, d["id"], d["memo"])
    for i, x in enumerate(((snap.get("budget") or {}).get("assumptions")) or []):
        f[f"budget:assumption[{i}]"] = x
    for c in snap.get("components") or []:
        if c.get("responsibility"):
            f[f"component:{c['id']}.responsibility"] = c["responsibility"]
    for t in snap.get("tasks") or []:
        if t.get("title"):
            f[f"task:{t['id']}.title"] = t["title"]
    for a in snap.get("acceptance_tests") or []:
        if a.get("criterion"):
            f[f"acceptance:{a['id']}"] = a["criterion"]
    for i, x in enumerate(body.get("risks") or []):
        f[f"risk[{i}]"] = x
    for v in snap.get("verification") or []:
        for k in ("check", "how", "pass"):
            if v.get(k):
                f[f"verification:{v['id']}.{k}"] = v[k]
    for i, x in enumerate(snap.get("first_week") or []):
        f[f"week[{i}]"] = x
    for i, x in enumerate(document_open_lines(body.get("unresolved_checks") or [])):
        f[f"open[{i}]"] = x
    return {k: v for k, v in f.items() if isinstance(v, str) and v.strip()}


def cited_ids(fields: dict, memos: list[dict]) -> list[str]:
    """Every claim the reader content cites: the ids inline in the fields and the memos' why and option citations (stored beside the text)."""
    ids = set(CLAIM_ID.findall(" ".join(fields.values())))
    for d in memos or []:
        m = d.get("memo") or {}
        for w in m.get("why") or []:
            ids.update(w.get("claim_ids") or [] if isinstance(w, dict) else [])
        for o in m.get("options") or []:
            ids.update(o.get("claim_ids") or [])
    return sorted(ids)


def decision_records(decisions: list[dict]) -> list[dict]:
    """The decision records the Editor reads (id, status, the recommendation's option and its mode) from the version's decisions with their memos."""
    from app.core import modes as MO
    out = []
    for d in decisions:
        rec = ((d.get("memo") or {}).get("recommendation") or {})
        out.append({"id": d["id"], "status": d.get("status"), "recommendation": rec.get("option") or None, "mode": MO.pick_mode(rec.get("option") or "") or None})
    return out


def protected(path: str, open_decisions: set) -> str | None:
    """Why a path may never be dropped: a requirement, an acceptance criterion, a verification check, an open item of an open decision,
    and the plan's open lines the documents print (a stop line, the ceiling issue): an unresolved issue the Editor never deletes."""
    if path.startswith(PROTECTED_PREFIXES):
        return "a requirement, an acceptance criterion or a verification check is never dropped"
    if path.startswith("open["):
        return "an unresolved issue is never deleted: the plan's open lines stay"
    m = re.match(r"memo:([^:]+):open\[\d+\]$", path)
    if m and m.group(1) in open_decisions:
        return "an open item of an open decision is never dropped"
    return None


def drop_decision(path: str, text: str, kept_texts: list[str], open_decisions: set) -> tuple[bool, str]:
    """(applied, why) for one drop the Editor proposed: refused when the path is protected; applied when the field carries no claim id or every
    claim id it carries is cited by a kept field of the same document; otherwise kept in the body and listed in the assessment with its claims
    (nothing cited is lost)."""
    why = protected(path, open_decisions)
    if why:
        return False, why
    ids = sorted(set(CLAIM_ID.findall(text or "")))
    if not ids:
        return True, ""
    elsewhere = set(CLAIM_ID.findall(" ".join(kept_texts)))
    orphans = [c for c in ids if c not in elsewhere]
    if orphans:
        return False, f"its claim(s) {', '.join(orphans)} are cited by no other field: the field stays in the body"
    return True, ""


def correction_path(records: list[str]) -> str:
    """Where a source_error is corrected, by the record named: a requirement through a founder revision run, a claim or a quote through the
    research correction (the source read again), a decision through the comparison stage, a source through a fresh read; the Editor never patches it."""
    kinds = {re.sub(r"^.*\.", "", x).split("-")[0] for x in records or []}
    paths = []
    if "REQ" in kinds:
        paths.append("a founder revision run for the requirement")
    if kinds & {"CLM", "SRC"}:
        paths.append("the research correction (the source read again, the claim re-verified)")
    if "DEC" in kinds:
        paths.append("the comparison stage (the decision re-decided)")
    if "ACC" in kinds or "TSK" in kinds or "CMP" in kinds:
        paths.append("a new plan version")
    return "; ".join(paths) or "the record's own update path"


def completeness_line(parts: list[dict]) -> str:
    """The explicit completeness line of the pass: 'complete', or 'incomplete: <why>' when an answer was truncated, failed its schema after
    the repair, when the pass ran in two calls, or when the cap left fields unread (which ones). parts: [{"call": status word, "fields": [paths], "detail"}]."""
    reasons = []
    for p in parts:
        st = str(p.get("call") or "")
        if st.startswith("flagged") and ("truncated" in st or "finish_reason=length" in st):
            reasons.append("truncated")
        elif st.startswith("flagged"):
            reasons.append(f"schema failure after the repair call ({st[len('flagged: '):][:120]})" if "invalid" in st or "schema" in st or "validation" in st else st[:160])
        elif st.startswith("skipped: the reservation"):
            reasons.append(f"the cap would have been exceeded: {len(p.get('fields') or [])} field(s) not read ({', '.join((p.get('fields') or [])[:8])}{', ...' if len(p.get('fields') or []) > 8 else ''})")
    if len(parts) > 1 and all(str(p.get("call") or "") in ("ok",) for p in parts):
        reasons.append("the pass ran in two calls (the body fields, then the memos and the verification), each with the assessment context")
    elif len(parts) > 1:
        reasons.append("the pass ran in two calls")
    return COMPLETE if not reasons else f"{INCOMPLETE}: " + "; ".join(dict.fromkeys(reasons))


def warnings_of(rec: dict | None) -> list[str]:
    """What the founder sees where the version is reviewed: the flags with their kind, path, issue, records and correction path; the
    completeness line when incomplete; the drops the checks refused (listed in the assessment); the assessment per family; the trims of cut records."""
    if not rec:
        return []
    out = []
    comp = str(rec.get("completeness") or "")
    if comp and comp != COMPLETE:
        out.append(f"editor: {comp}")
    for fl in rec.get("flags") or []:
        kind = fl.get("kind") or "flag"; recs = fl.get("records") or []
        line = f"editor {kind}: {fl.get('path')}: {fl.get('issue')}"
        if recs:
            line += f" (records {', '.join(recs)})"
        if kind == "source_error":
            line += f"; correction: {fl.get('correction') or correction_path(recs)}"
        out.append(line)
    for d in rec.get("not_dropped") or []:
        out.append(f"editor: {d.get('path')} kept in the body ({d.get('why')}); the Editor's reason: {d.get('reason')}")
    for a in rec.get("assessment") or []:                                                      # the assessment per family is what the founder reviews beside the flags
        order = a.get("order")
        note = f" (the paragraph does not follow the review order: {', '.join(order)})" if order and order != [x for x in ASSESSMENT_ORDER if x in order] else ""   # a deviation from the review order is visible, never silent
        out.append(f"editor assessment ({a.get('family')}): {a.get('text')}{note}")
    for t in rec.get("trimmed") or []:
        out.append(f"editor: {t.get('path')}: {t.get('note')}")
    return out


def apply(rec: dict, plan, memos: list[dict], week: list[str], lead: dict | None, risks: list[str], verification: list[dict] | None = None) -> dict:
    """The kept rewrites, merges and drops poured back (an applied drop leaves the stored list exactly as a merge removes its
    merged-away paths, so the snapshot and every document built from it carry the consolidated content and nothing downstream prints a dropped
    field again; editor_json keeps the dropped text with its path and reason): returns the plan body fields, the week, the lead, the risks, the
    verification texts by id, the acceptance, task and component texts by id and the ids of the memos changed."""
    rw = dict(rec.get("rewrites") or {})
    removed: set[str] = set()
    for m in rec.get("merged") or []:
        rw[m["paths"][0]] = m["text"]
        removed |= set(m["paths"][1:])
    removed |= {d["path"] for d in rec.get("dropped") or [] if d.get("applied")}

    def lst(prefix, items):
        out = []
        for i, x in enumerate(items):
            p = f"{prefix}[{i}]"
            if p in removed:
                continue
            out.append(rw.get(p, x))
        return out
    out = {"problem": rw.get("problem", plan.problem), "users": rw.get("users", plan.users if isinstance(plan.users, str) else "; ".join(plan.users or [])),
           "scope_in": lst("scope_in", plan.scope_in), "scope_out": lst("scope_out", plan.scope_out), "user_flow": lst("flow", plan.user_flow),
           "user_flow_labels": [x for i, x in enumerate(getattr(plan, "user_flow_labels", None) or []) if f"flow[{i}]" not in removed],   # a step's label stays with its step; the label of a dropped step leaves with it
           "risks": lst("risk", risks), "week": lst("week", week), "lead": dict(lead, text=rw["lead"]) if lead and "lead" in rw else lead, "verification": {}, "memos_changed": set()}
    for d in memos:
        m = d.get("memo") or {}
        if not m:
            continue
        did = d["id"]; changed = False
        why = []
        for i, w in enumerate(m.get("why") or []):                                           # an applied drop of a why sentence removes it, as of any list field
            p = f"memo:{did}:why[{i}]"
            if p in removed:
                changed = True; continue
            if p in rw:
                w["text"] = rw[p]; changed = True
            why.append(w)
        if changed:
            m["why"] = why
        for i, o in enumerate(m.get("options") or []):
            for k in ("fit", "risk"):
                p = f"memo:{did}:option[{i}].{k}"
                if p in removed:                                                              # a dropped fit or risk cell is emptied (the option row stays)
                    o[k] = ""; changed = True
                elif p in rw:
                    o[k] = rw[p]; changed = True
        for k in ("change_mind", "open"):
            items = m.get(k) or []
            new = lst(f"memo:{did}:{k}", items)
            if new != items:
                m[k] = new; changed = True
        if changed:
            out["memos_changed"].add(did)
    out["acceptance"] = {}; out["tasks"] = {}; out["components"] = {}
    for path, text in rw.items():
        if path.startswith("verification:"):
            vid, k = path[len("verification:"):].rsplit(".", 1)
            out["verification"].setdefault(vid, {})[k] = text
        elif path.startswith("acceptance:"):
            out["acceptance"][path[len("acceptance:"):]] = text
        elif path.startswith("task:") and path.endswith(".title"):
            out["tasks"][path[len("task:"):-len(".title")]] = text
        elif path.startswith("component:") and path.endswith(".responsibility"):
            out["components"][path[len("component:"):-len(".responsibility")]] = text
    base_by = {v["id"]: v for v in (verification or []) if isinstance(v, dict) and v.get("id")}
    for vid, texts in list(out["verification"].items()):                                   # a check rewritten in part keeps its other texts (a missing key would be a KeyError at the write)
        base = base_by.get(vid, {})
        out["verification"][vid] = {k: texts.get(k, base.get(k)) for k in ("check", "how", "pass")}
    return out


def absorb(out, fields: dict, candidates: list[str], recommended: list[str], claim_values: dict, open_decisions: set, rec: dict, record_numbers: set | None = None) -> None:
    """One EditorOutV2 answer poured into the record under the hard checks: the rewrites (rewrite_ok; a record-only path is
    recorded, never applied), the merges (two or more fields of one reader list, the union of their claim ids and numbers), the drops
    (drop_decision: applied, or kept and listed), the flags with their kinds and the correction path of a source_error, the assessment and the
    summary statements (each under rewrite_ok against the field it summarises; a record figure may join as context). Shared by the workflow's Editor step and the maintenance entry."""
    proposed_drops = {d0.path for d0 in out.dropped if d0.path in fields}
    for f0 in out.fields:
        if f0.path not in fields:
            rec["dropped"].append({"path": f0.path, "why": "unknown path"}); continue
        if f0.path.startswith(RECORD_ONLY_PREFIXES):
            rec["dropped"].append({"path": f0.path, "why": "outside the Editor's write scope: the record changes only under its own update rule", "text": f0.text}); continue
        ok, why = rewrite_ok(fields[f0.path], f0.text, candidates, recommended=recommended or None, claim_values=claim_values, record_numbers=record_numbers)
        if ok:
            rec["rewrites"][f0.path] = f0.text
        else:
            rec["dropped"].append({"path": f0.path, "why": why})
    for m0 in out.merged:
        paths = [p0 for p0 in m0.paths if p0 in fields]
        if len(paths) < 2 or len({list_of(p0) for p0 in paths}) != 1 or any(p0.startswith(PROTECTED_PREFIXES + RECORD_ONLY_PREFIXES) for p0 in paths):
            rec["dropped"].append({"path": ",".join(m0.paths), "why": "a merge must join two or more fields of one reader list"}); continue
        ok, why = rewrite_ok(" ".join(fields[p0] for p0 in paths), m0.text, candidates, recommended=recommended or None, claim_values=claim_values, record_numbers=record_numbers)   # the merged text keeps the union of the merged fields' claim ids and numbers
        if ok:
            rec["merged"].append({"paths": paths, "text": m0.text})
        else:
            rec["dropped"].append({"path": ",".join(paths), "why": why})
    kept_texts = [rec["rewrites"].get(p0, t0) for p0, t0 in fields.items() if p0 not in proposed_drops] + [m0["text"] for m0 in rec["merged"]]
    for d0 in out.dropped:
        if d0.path not in fields:
            rec["dropped"].append({"path": d0.path, "why": "unknown path", "reason": d0.reason}); continue
        applied, why = drop_decision(d0.path, fields[d0.path], kept_texts, open_decisions)
        entry = {"path": d0.path, "reason": d0.reason, "text": fields[d0.path], "applied": applied}
        if applied:
            rec["dropped"].append(entry)
        else:
            rec["not_dropped"].append(dict(entry, why=why, claims=sorted(set(CLAIM_ID.findall(fields[d0.path])))))
    for fl in out.flags:
        kind = fl.kind if fl.kind in KIND_WORDS else "presentation"
        entry = {"path": fl.path, "kind": kind, "issue": fl.issue, "records": list(fl.records)}
        if kind == "source_error":
            entry["correction"] = correction_path(list(fl.records))
        rec["flags"].append(entry)
    rec["assessment"].extend({"family": a0.family, "text": a0.text, "order": assessment_order(a0.text)} for a0 in out.assessment)   # the review-order labels found, in their order
    for s0 in out.summary:
        if s0.path in fields:
            ok, why = rewrite_ok(fields[s0.path], s0.text, candidates, recommended=recommended or None, claim_values=claim_values, record_numbers=record_numbers)
            (rec["summary"].append({"path": s0.path, "text": s0.text}) if ok else rec["dropped"].append({"path": f"summary:{s0.path}", "why": why}))
        else:
            rec["dropped"].append({"path": f"summary:{s0.path}", "why": "unknown path"})


def new_record(fields: dict, prompt_version: int) -> dict:
    """The empty record of one pass."""
    return {"rewrites": {}, "merged": [], "flags": [], "dropped": [], "not_dropped": [], "assessment": [], "summary": [], "fields": len(fields), "call": None, "calls": [], "completeness": None, "prompt_version": prompt_version, "trimmed": []}


def apply_to_snapshot(snap: dict, rec: dict) -> dict:
    """The record's kept rewrites, merges and applied drops poured into a copy of a stored snapshot (the maintenance entry's new version): the
    plan body, the lead, the first week, the current decisions' memos, the verification plan, the acceptance criteria, the tasks' titles and the
    components' responsibilities; the editor record stored on the copy."""
    import copy as _copy
    out = _copy.deepcopy(snap)
    rw = dict(rec.get("rewrites") or {})
    removed: set[str] = set()
    for m in rec.get("merged") or []:
        rw[m["paths"][0]] = m["text"]; removed |= set(m["paths"][1:])
    removed |= {d["path"] for d in rec.get("dropped") or [] if d.get("applied")}

    def lst(prefix, items):
        return [rw.get(f"{prefix}[{i}]", x) for i, x in enumerate(items or []) if f"{prefix}[{i}]" not in removed]
    body = out.get("plan_body") or {}
    body["problem"] = rw.get("problem", body.get("problem"))
    if "users" in rw:
        body["users"] = rw["users"]
    if body.get("user_flow_labels"):                                                # the labels are filtered before the steps are rewritten, by the steps' own indices
        body["user_flow_labels"] = [x for i, x in enumerate(body["user_flow_labels"]) if f"flow[{i}]" not in removed]
    for key, prefix in (("scope_in", "scope_in"), ("scope_out", "scope_out"), ("user_flow", "flow"), ("risks", "risk")):
        body[key] = lst(prefix, body.get(key))
    out["plan_body"] = body
    if out.get("lead") and "lead" in rw:
        out["lead"] = dict(out["lead"], text=rw["lead"])
    out["first_week"] = lst("week", out.get("first_week"))
    for d in out.get("decisions") or []:
        m = d.get("memo")
        if not m:
            continue
        did = d["id"]
        why = []
        for i, w in enumerate(m.get("why") or []):                                           # the applied drops of why sentences and of fit and risk cells, as apply pours them
            if f"memo:{did}:why[{i}]" in removed:
                continue
            if f"memo:{did}:why[{i}]" in rw and isinstance(w, dict):
                w["text"] = rw[f"memo:{did}:why[{i}]"]
            why.append(w)
        m["why"] = why
        for i, o in enumerate(m.get("options") or []):
            for k in ("fit", "risk"):
                if f"memo:{did}:option[{i}].{k}" in removed:
                    o[k] = ""
                elif f"memo:{did}:option[{i}].{k}" in rw:
                    o[k] = rw[f"memo:{did}:option[{i}].{k}"]
        for k in ("change_mind", "open"):
            m[k] = lst(f"memo:{did}:{k}", m.get(k))
    for v in out.get("verification") or []:
        for k in ("check", "how", "pass"):
            if f"verification:{v['id']}.{k}" in rw:
                v[k] = rw[f"verification:{v['id']}.{k}"]
    for a in out.get("acceptance_tests") or []:
        if f"acceptance:{a['id']}" in rw:
            a["criterion"] = rw[f"acceptance:{a['id']}"]
    for t in out.get("tasks") or []:
        if f"task:{t['id']}.title" in rw:
            t["title"] = rw[f"task:{t['id']}.title"]
    for c in out.get("components") or []:
        if f"component:{c['id']}.responsibility" in rw:
            c["responsibility"] = rw[f"component:{c['id']}.responsibility"]
    out["editor"] = rec
    return out
