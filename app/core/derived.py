"""Bounded derived data: a cost comparison computed from sourced prices and explicit usage assumptions. Every number carries a label:
sourced fact, assumption, calculated estimate or measurement. Application code computes every figure from the verified price claims and the founder's
usage answer; the model never computes a figure."""
import re

USAGE_WHY = "The monthly cost of every option is calculated from these figures and the sourced prices; they are stored as your assumptions."
FIELDS = ["option", "product", "price_claim", "unit_price_usd", "price_unit", "usage_per_month", "usage_label", "monthly_cost_usd", "monthly_label", "budget_usd", "budget_check"]
LABELS = {"option": "Option", "product": "Product", "price_claim": "Price claim", "unit_price_usd": "Unit price (USD, sourced fact)", "price_unit": "Per", "usage_per_month": "Usage per month",
          "usage_label": "Usage label", "monthly_cost_usd": "Monthly cost (USD)", "monthly_label": "Monthly cost label", "budget_usd": "Budget ceiling (USD per month)", "budget_check": "Budget check"}
UNITS = {"unit_price_usd": "USD", "monthly_cost_usd": "USD per month", "budget_usd": "USD per month"}


def cost_comparison(options: list[dict], usage: dict) -> list[dict]:
    """options: [{name, unit, unit_price_usd, claim_id}]; usage: {units_per_month, label}. Returns labelled rows."""
    rows = []
    for o in options:
        monthly = round(float(o["unit_price_usd"]) * float(usage["units_per_month"]), 2)
        rows.append({"option": o["name"], "unit_price_usd": o["unit_price_usd"], "unit_price_label": "sourced fact", "claim_id": o["claim_id"],
                     "units_per_month": usage["units_per_month"], "usage_label": "assumption", "monthly_cost_usd": monthly, "monthly_label": "calculated estimate",
                     "measured_usd": o.get("measured_usd"), "measured_label": "measurement" if o.get("measured_usd") is not None else "not measured"})
    return sorted(rows, key=lambda r: (r["monthly_cost_usd"], r["option"]))


# ---------------------------------------------------------------- the founder's usage answer, in words, to units per month
_NUM = r"(?<![\d,.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![,.]?\d)"   # a thousands separator is kept (1,000 is a thousand); a comma followed by one or two digits is an ambiguous form and matches nothing
_FOREIGN = re.compile(r"[\u20ac\u00a3\u00a5\u20aa]|\b(?:eur|euros?|gbp|pounds?|chf|jpy|yen|ils|nis|shekels?|cad|aud|inr|rupees?)\b")   # a non-USD currency marker; such a value is not computed as USD
_USAGE_WORDS = {"meetings": ("meeting", "meetings", "call", "calls", "sessions", "session", "recordings", "recording"), "minutes": ("minute", "minutes", "min", "mins"),
                "hours": ("hour", "hours", "hr", "hrs"), "users": ("user", "users", "seat", "seats", "people", "person", "members", "member"),
                "emails": ("email", "emails", "summaries", "summary", "messages", "message"), "gb": ("gb", "gigabyte", "gigabytes")}


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_usage(answer) -> dict:
    """The founder's answer as units per month, each unit with its label: a figure the founder gave is an assumption; a figure computed from
    the founder's figures (minutes a month from meetings and minutes each; hours from minutes; one email a meeting when no email figure was
    given) is a calculated estimate. Deterministic; no model."""
    text = str(answer or "").lower()
    found: dict[str, float] = {}; each: dict[str, float] = {}
    for m in re.finditer(_NUM + r"\s*([a-z]+)(\s+each|\s+per\s+(?:meeting|call|session))?", text):
        value, word, per = _num(m.group(1)), m.group(2), m.group(3)
        for key, words in _USAGE_WORDS.items():
            if word in words:
                (each if per else found)[key] = value
                break
    out: dict[str, dict] = {}
    for key in ("meetings", "users", "emails", "gb"):
        if key in found:
            out[key] = {"value": found[key], "label": "assumption"}
    if "minutes" in found and "minutes" not in each:
        out["minutes"] = {"value": found["minutes"], "label": "assumption"}
    elif "minutes" in each and "meetings" in found:
        out["minutes"] = {"value": round(found["meetings"] * each["minutes"], 2), "label": "calculated estimate"}
    if "hours" in found and "hours" not in each:
        out["hours"] = {"value": found["hours"], "label": "assumption"}
    elif "hours" in each and "meetings" in found:
        out["hours"] = {"value": round(found["meetings"] * each["hours"], 2), "label": "calculated estimate"}
    if "minutes" in out and "hours" not in out:
        out["hours"] = {"value": round(out["minutes"]["value"] / 60.0, 4), "label": "calculated estimate"}
    if "hours" in out and "minutes" not in out:
        out["minutes"] = {"value": round(out["hours"]["value"] * 60.0, 2), "label": "calculated estimate"}
    if "emails" not in out and "meetings" in out:
        out["emails"] = {"value": out["meetings"]["value"], "label": "calculated estimate"}       # one summary email a meeting (the product's flow): stated as calculated, never as the founder's figure
    return out


# ---------------------------------------------------------------- a verified price claim's value to a unit price
_PRICE = r"(?:\$|usd\s*)?" + _NUM + r"\s*(?:usd)?"
_UNIT_PATTERNS = [
    ("minute", re.compile(_PRICE + r"\s*(?:/|per)\s*(?:audio\s*)?(?:min(?:ute)?s?)\b")),
    ("hour", re.compile(_PRICE + r"\s*(?:/|per)\s*(?:audio\s*)?(?:hour|hr)s?\b")),
    ("thousand_emails", re.compile(_PRICE + r"\s*(?:/|per)\s*1[,.]?000\s*(?:emails?|messages?)")),
    ("user_month", re.compile(_PRICE + r"\s*(?:/|per)\s*(?:user|seat)(?:\s*(?:/|per|a)\s*(?:mo|month))?")),
    ("million_tokens", re.compile(_PRICE + r"\s*(?:/|per)\s*(?:1\s*m|1m|mtok|million)\s*(?:tok(?:ens?)?)?")),
    ("gb", re.compile(_PRICE + r"\s*(?:/|per)\s*gb\b")),
    ("month", re.compile(_PRICE + r"\s*(?:/|per|a)\s*(?:mo|month)\b")),
]


def unit_price(value) -> tuple[float, str] | None:
    """(amount in USD, the unit it is quoted per) read from a price claim's value, or None when the value states no unit price the
    calculator knows (a tier list, a free credit, a percentage), names a currency other than USD, or states more than one
    amount (a compound tariff such as a base plus an overage, or a tier list; only a complete, unambiguous
    selected tariff is calculated)."""
    text = str(value or "").lower().replace("\u00a0", " ")
    if _FOREIGN.search(text):
        return None
    amounts = [m for m in re.finditer(_NUM, text) if not re.search(r"(?:per|/|\bfor|\bevery)\s*$", text[max(0, m.start() - 8):m.start()])]   # the amounts of the value: a figure after "per", "/", "for" or "every" is part of the unit, not a price
    if len(amounts) != 1:
        return None
    for unit, pat in _UNIT_PATTERNS:
        m = pat.search(text)
        if m:
            return _num(m.group(1)), unit
    return None


def monthly_estimate(amount: float, unit: str, usage: dict) -> tuple[float | None, str, str | None]:
    """(the monthly cost, the label of the usage figure it used, the reason it is not computable) for one unit price under the usage."""
    if unit == "month":
        return round(amount, 2), "flat monthly price", None
    need = {"minute": "minutes", "hour": "hours", "thousand_emails": "emails", "user_month": "users", "gb": "gb"}.get(unit)
    if need is None or need not in usage:
        return None, "", f"usage per {unit.replace('_', ' ')} not in the answer"
    units = float(usage[need]["value"]) / (1000.0 if unit == "thousand_emails" else 1.0)
    return round(amount * units, 2), usage[need]["label"], None


# ---------------------------------------------------------------- the budget requirement and the check
_BUDGET = re.compile(r"(?:\$\s*|usd\s*)?" + _NUM + r"\s*(?:usd|\$|dollars?)?", re.I)


def find_budget(requirements: list[dict]) -> dict | None:
    """The project's monthly budget ceiling from its confirmed requirements: a requirement whose key names a budget (budget, cost, spend)
    or whose value names a budget with a figure; the first by id; None when none exists (said honestly as "no budget requirement")."""
    for r in sorted(requirements, key=lambda x: x["id"]):
        key = str(r.get("key") or "").lower(); value = r.get("value"); text = " ".join(str(x) for x in value) if isinstance(value, list) else str(value or "")
        if any(w in key for w in ("budget", "monthly_cost", "spend")) or ("budget" in text.lower() and re.search(r"\d", text)):
            m = re.search(r"budget[^\d]{0,40}" + _NUM, text, re.I) or _BUDGET.search(text)
            if m:
                return {"requirement_id": r["id"], "key": r.get("key"), "ceiling_usd": _num(m.group(1)), "hard": bool(r.get("hard"))}
    return None


def budget_check(monthly: float | None, budget: dict | None) -> str:
    if monthly is None:
        return "not computable"
    if budget is None:
        return "no budget requirement"
    if monthly <= float(budget["ceiling_usd"]) + 1e-9:
        return "within budget"
    return "above budget (hard requirement)" if budget.get("hard") else "above budget (soft preference)"


def cost_table(subject: str, options: list[dict], price_claims: list[dict], cited: set, usage_answer, budget: dict | None, eligible: set | None = None) -> dict | None:
    """The derived cost table of one cost-bearing decision: one row per option and price claim of its product (the comparison's evidence,
    cited by the decision or not), the unit price a sourced fact, the usage an assumption or a calculated estimate from the founder's
    answer, the monthly cost a calculated estimate, the budget check against the ceiling where a budget requirement exists. Returns None
    when no option has a price claim. The structure is schemas/export_models.DerivedTable's. eligible: the ids
    of the price claims a sourced estimate may rest on (current, supported, in scope); any other price claim's row stays not computable with
    its status and scope named."""
    usage = parse_usage(usage_answer) if usage_answer else {}
    rows, claim_ids = [], []
    for o in options:
        mine = [c for c in price_claims if (c.get("product_id") or "") == (o.get("product_id") or "") and (c.get("product_version") or "") == (o.get("product_version") or "")] if o.get("product_id") else []
        for c in mine:
            up = unit_price(c.get("value"))
            if eligible is not None and c["id"] not in eligible:
                monthly, usage_label, why = None, "", f"price evidence not eligible (status {c.get('status')}, scope {c.get('scope_status')}); a current, supported, in-scope claim is needed"
                amount, unit = None, ""
            elif up is None:
                monthly, usage_label, why = None, "", "no unit price in the claim's value (none the calculator knows, more than one amount, or a currency other than USD)"
                amount, unit = None, ""
            else:
                amount, unit = up
                monthly, usage_label, why = (None, "", "usage not answered") if not usage else monthly_estimate(amount, unit, usage)
            used = None
            if unit and unit != "month" and usage:
                key = {"minute": "minutes", "hour": "hours", "thousand_emails": "emails", "user_month": "users", "gb": "gb"}.get(unit)
                used = usage.get(key, {}).get("value") if key else None
            check = budget_check(monthly, budget) if why is None else f"not computable: {why}"
            rows.append([o.get("label") or o.get("option") or "", f"{o.get('product_id') or ''} {o.get('product_version') or ''}".strip(), f"{c['id']}{' (cited)' if c['id'] in cited else ''}",
                         amount, unit, used, usage_label if used is not None else ("flat" if unit == "month" else ""), monthly, "calculated estimate" if monthly is not None else "not computable",
                         float(budget["ceiling_usd"]) if budget else None, check])
            claim_ids.append([c["id"]])
    if not rows:
        return None
    return {"title": f"Monthly cost estimate: {subject}", "fields": list(FIELDS), "units": dict(UNITS), "labels": dict(LABELS), "rows": rows, "claim_ids": claim_ids,
            "usage": usage, "budget": budget}


# ---------------------------------------------------------------- the usage question's four numbers
USAGE_QUESTION = ("How much will the product be used in a month? Give the figures the prices are quoted in, in your own words. For a meeting product: meetings a month, "
                  "minutes a meeting, users, and recipients of a summary (for example: 40 meetings a month, 45 minutes each, 3 users, 3 recipients). For any other workload: "
                  "the items a month in your own word for them, stated as the total, and what one item takes (for example: 100 packs a month in total, 110 image attempts, 20 revisions, "
                  "50 users, 400 words a pack, 20,000 tokens in and 1,500 tokens out a pack, 5 searches and 10 page reads a pack, 2 GB of storage). One answer serves every cost-bearing decision of the project.")
USAGE_FIELDS = ("meetings_per_month", "minutes_per_meeting", "users", "recipients_per_summary")
_RECIPIENT_WORDS = ("recipient", "recipients", "people per summary", "readers")


def parse_usage_numbers(answer) -> dict | None:
    """The four numbers of the usage answer: meetings a month, minutes a meeting, users, recipients a summary (default: the users).
    None when the answer names no meetings figure. The founder's words stay as the answer; this is the structured record beside it."""
    text = str(answer or "").lower()
    found: dict[str, float] = {}
    for m in re.finditer(_NUM + r"\s*([a-z][a-z ]*?)(?=\s*(?:,|;|\.|$|\d))", text):
        value, words = _num(m.group(1)), m.group(2).strip()
        head = words.split()[0] if words else ""
        if head in _USAGE_WORDS["meetings"] and "meetings_per_month" not in found:
            found["meetings_per_month"] = value
        elif head in _USAGE_WORDS["minutes"] and "minutes_per_meeting" not in found:
            found["minutes_per_meeting"] = value
        elif head in _USAGE_WORDS["users"] and "users" not in found:
            found["users"] = value
        elif any(words.startswith(w) for w in _RECIPIENT_WORDS) and "recipients_per_summary" not in found:
            found["recipients_per_summary"] = value
    if "meetings_per_month" not in found:
        return None
    out = {k: found.get(k) for k in USAGE_FIELDS}
    if out["recipients_per_summary"] is None:
        out["recipients_per_summary"] = out["users"]
    return out


# ---------------------------------------------------------------- the summary's length, one founder usage question
SUMMARY_LENGTH_QUESTION = "How long is one meeting summary, in words? (for example: 500 words)"
SUMMARY_LENGTH_WHY = "A price quoted per output token needs the length of one output; the words are converted at 4/3 tokens a word, never a guessed quantity."
LENGTH_QUESTION_FORM = "How long is one {item}, in words? (for example: 500 words)"      # the same question asked in the founder's own item word (pack, report, document)
LENGTH_QUESTION_LIKE = "How long is one %, in words? (for example: 500 words)"         # the SQL shape that finds the length question whatever its item word
_LENGTH_QUESTION_RE = re.compile(r"How long is one [a-z][a-z -]*, in words\? \(for example: 500 words\)")


def length_question(item) -> str:
    """The length question in the item's word ("How long is one pack, in words?"); the meeting summary's own text without an item."""
    return LENGTH_QUESTION_FORM.format(item=str(item).strip()) if item else SUMMARY_LENGTH_QUESTION


def is_length_question(text) -> bool:
    return _LENGTH_QUESTION_RE.fullmatch(str(text or "")) is not None


SUMMARY_LENGTH_FORMS = tuple(re.compile(f.replace("N", _NUM)) for f in (                 # the scalar forms a length answer may take
    r"N", r"N words?", r"about N words?", r"up to N words?", r"a N-word summary", r"summary length - N words?", r"N words? per summary"))


def parse_summary_words(answer) -> float | None:
    """The words of one summary from the founder's answer, read by an allow-list, not a deny-list. The length is read only when the whole
    answer, trimmed, case-folded, its spaces
    collapsed and a final full stop dropped, is one of SUMMARY_LENGTH_FORMS ("500", "500 words", "about 500 words", "1,500 words", "up to 500
    words", "a 500-word summary", "Summary length - 500 words", "500 words per summary"); anything else, a second number, a number word, a
    range or a negative figure included, names no length (the workflow then stores nothing and the next run asks the question again)."""
    text = " ".join(str(answer or "").casefold().strip().rstrip(".").split())
    for form in SUMMARY_LENGTH_FORMS:
        m = form.fullmatch(text)
        if m:
            value = _num(m.group(1))
            return value if value and value > 0 else None
    return None


# ---------------------------------------------------------------- the scope of the meetings figure
USAGE_SCOPE_QUESTION = "Is the meetings figure the team's total or per user?"
USAGE_SCOPE_WHY = "The budget multiplies a per-user figure by the users; a monthly figure beside a users figure with no word for its scope can be read both ways."
_SCOPE_PER_USER = re.compile(r"\b(?:per|each|every|a|for each|for every)\s+(?:user|seat|person|member)\b|\bper[- ]user\b")
_SCOPE_TOTAL = re.compile(r"\b(?:in all|in total|total|altogether|overall|combined|team-wide|across the team|the team|the team's|for the team|whole team)\b")


def _meetings_clause(text: str, item_word: str | None = None) -> str:
    """The clause of the answer that carries the meetings figure (split at commas, semicolons,
    full stops and "and"), so a scope word of another quantity ("1 summary email per user") never settles the meetings figure; the whole
    text when no clause names a meetings figure (a clarification answer). With an item word, the clause that carries the items figure."""
    words = [re.escape(item_word)] if item_word else sorted(_USAGE_WORDS["meetings"], key=len, reverse=True)
    for clause in re.split(r"[,;.]|\band\b", text):
        if re.search(_NUM + r"\s*(?:[a-z]+\s+){0,3}?(" + "|".join(words) + r")\b", clause):
            return clause
    return text


def usage_scope(answer, item_word: str | None = None) -> str | None:
    """per_user when the words say per user, each user, a user (or seat, person, member); total when they say in all, total, the team; None otherwise.
    The words are read from the meetings figure's own clause, or from the items figure's clause when an item word is given."""
    text = _meetings_clause(str(answer or "").lower().replace("\u2019", "'"), item_word)
    if _SCOPE_PER_USER.search(text):
        return "per_user"
    if _SCOPE_TOTAL.search(text):
        return "total"
    return None


def usage_record(answer) -> dict | None:
    """The stored usage record: the four numbers of parse_usage_numbers and a scope field read from the answer's own words (per_user or
    total); None scope when the answer names users and no scope word (ambiguous: the clarification question is asked, never an invented total);
    total when no users figure is given (nothing to multiply). An answer that names no meetings figure but items a month in the founder's
    own word (packs, documents, reports, jobs) is the items record of parse_items_record with the same scope rule read from the items figure's
    clause. None when the answer names neither."""
    numbers = parse_usage_numbers(answer)
    if numbers is None:
        items = parse_items_record(answer)
        if items is None:
            return None
        scope = usage_scope(answer, items["item_word"])
        if scope is None and not items.get("users"):
            scope = "total"
        return {**items, "scope": scope}
    scope = usage_scope(answer)
    if scope is None and not numbers.get("users"):
        scope = "total"
    return {**numbers, "scope": scope}


def usage_scope_open(record) -> bool:
    """Whether the record's scope is still to be settled (a scope field present and None); a record without the field (stored before the rule) is read as given."""
    return bool(record) and isinstance(record, dict) and "scope" in record and record.get("scope") is None


def settle_usage_scope(record: dict | None, answer) -> dict:
    """The clarification answer settles the scope: its own words (usage_scope), else 'user' says per user and 'team', 'total' or 'all' says the total."""
    scope = usage_scope(answer)
    if scope is None:
        low = str(answer or "").lower()
        scope = "per_user" if re.search(r"\buser", low) else ("total" if re.search(r"\b(?:team|total|all)\b", low) else None)
    return {**(record or {}), "scope": scope}


# ---------------------------------------------------------------- a workload stated in items, in the founder's own word
ITEMS_FIELDS = ("items_per_month", "illustrations_per_month", "revisions_per_month", "revision_share", "users", "words_per_item", "tokens_in_per_item", "tokens_out_per_item",
                "searches_per_item", "page_reads_per_item", "storage_gb")
_KNOWN_NOUNS = {w for words in _USAGE_WORDS.values() for w in words} | {"word", "words", "token", "tokens", "search", "searches", "query", "queries", "read", "reads", "page", "pages", "fetch", "fetches",
                                                                       "revision", "revisions", "correction", "corrections", "edit", "edits", "rewrite", "rewrites", "illustration", "illustrations", "image", "images",
                                                                       "picture", "pictures", "attempt", "attempts", "generation", "generations", "recipient", "recipients", "percent", "mb", "megabyte", "megabytes",
                                                                       "storage", "month", "months", "user", "users", "job", "jobs"} - {"job", "jobs"}   # jobs is a founder's item word; the rest name a quantity of their own
_MONTHLY = r"(?:(?:a|per|each|every)\s+month\b|\bmonthly\b|/\s*(?:month|mo)\b)"
_ITEMS = re.compile(_NUM + r"\s+((?:[a-z]+\s+){0,3}?)([a-z]+)\s+" + _MONTHLY)
_ILLUS = re.compile(_NUM + r"\s+(?:[a-z]+\s+){0,2}?(?:illustration|image|picture|artwork)s?\b")
_REVIS = re.compile(_NUM + r"\s+(?:[a-z]+\s+){0,2}?(?:revision|correction|edit|rewrite)s?\b")
_USERS_RE = re.compile(_NUM + r"\s+(?:[a-z]+\s+)?(?:user|users|seat|seats|people|person|member|members)\b")
_WORDS_RE = re.compile(_NUM + r"\s*words?\b")
_TOK_A = re.compile(_NUM + r"\s*(?:input\s+|output\s+)?tokens?\s+(in|out|input|output)\b")                                                        # 20,000 tokens in
_TOK_B = re.compile(r"\b(input|output)\b(?:\s+[a-z]+){0,3}?\s+(?:of\s+)?(?:about\s+|around\s+|roughly\s+|up to\s+)?" + _NUM + r"\s*tokens?\b")   # input of about 20,000 tokens
_TOK_C = re.compile(_NUM + r"\s*(input|output)\s+tokens?\b")                                                                                   # 20,000 input tokens
_SEARCH_RE = re.compile(_NUM + r"\s+(?:[a-z]+\s+)?(?:searches|search|queries|query)\b")
_READS_RE = re.compile(_NUM + r"\s+(?:[a-z]+\s+)?(?:(?:page\s+)?(?:reads?|fetch(?:es)?)|pages?)\b")
_STORAGE_RE = re.compile(_NUM + r"\s*(gb|gigabytes?)\b")
_SHARE_RE = re.compile(r"revisions?\b[^.;]*?\b(?:a\s+|one\s+)?(quarter|half|third|three quarters|" + _NUM + r"\s*(?:%|percent|per cent))")
_SHARES = {"quarter": 0.25, "half": 0.5, "third": 1.0 / 3.0, "three quarters": 0.75}


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "ches", "shes")) and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        return word[:-1]
    return word


def _monthly_after(text: str, end: int) -> bool:
    return re.match(r"\s*(?:[a-z]+\s+){0,2}?" + _MONTHLY, text[end:end + 40]) is not None


def parse_items_record(answer) -> dict | None:
    """The founder's answer as an items record: the noun that carries the monthly figure is the item word (packs, documents, reports,
    jobs, generations: any noun that is not a quantity of its own), kept for the labels; illustration or image attempts a month, revisions a
    month (with a stated share of an item: a quarter, half, 25%), users, words an item, tokens in and out an item, searches and page reads an
    item, storage in GB. Parenthesised sub-figures (40 Hebrew, 30 Arabic) are not read. Every figure is the founder's (an assumption); nothing
    is multiplied here. None when no clause names items a month."""
    text = re.sub(r"\([^)]*\)", " ", str(answer or "").lower().replace("\u2019", "'"))
    found: dict = {}
    for m in _ITEMS.finditer(text):
        adjectives, noun = m.group(2), m.group(3)
        if noun in _KNOWN_NOUNS or any(w in _KNOWN_NOUNS for w in adjectives.split()):
            continue
        found["items_per_month"] = _num(m.group(1)); found["item_word"] = noun; found["item"] = _singular(noun)
        break
    if "items_per_month" not in found:
        return None
    rec = {k: None for k in ITEMS_FIELDS}
    rec.update({"kind": "items", **found})
    m = _ILLUS.search(text)
    if m:
        rec["illustrations_per_month"] = _num(m.group(1))
    m = _REVIS.search(text)
    if m:
        rec["revisions_per_month"] = _num(m.group(1))
        ms = _SHARE_RE.search(text)
        if ms:
            word = ms.group(1)
            rec["revision_share"] = _SHARES[word] if word in _SHARES else _num(re.match(_NUM, word).group(1)) / 100.0
    m = _USERS_RE.search(text)
    if m:
        rec["users"] = _num(m.group(1))
    m = _WORDS_RE.search(text)
    if m:
        rec["words_per_item"] = _num(m.group(1))
    for pat, group_first in ((_TOK_A, False), (_TOK_C, False), (_TOK_B, True)):
        for m in pat.finditer(text):
            direction = (m.group(1) if group_first else m.group(2)).strip(); value = _num(m.group(2) if group_first else m.group(1))
            key = "tokens_in_per_item" if direction in ("in", "input") else "tokens_out_per_item"
            if rec[key] is None:
                rec[key] = value
    m = _SEARCH_RE.search(text)
    if m:
        rec["searches_per_item"] = _num(m.group(1))
    m = _READS_RE.search(text)
    if m:
        rec["page_reads_per_item"] = _num(m.group(1))
    m = _STORAGE_RE.search(text)
    if m:
        rec["storage_gb"] = _num(m.group(1))
    return rec


def is_items(record) -> bool:
    """Whether a stored usage record is an items record rather than the four meeting numbers."""
    return isinstance(record, dict) and (record.get("kind") == "items" or record.get("items_per_month") is not None)


def record_length(record) -> float | None:
    """The recorded length of one output in words: the meeting summary's or the item's."""
    if not isinstance(record, dict):
        return None
    return record.get("words_per_item") if is_items(record) else record.get("summary_words")


def with_length(record: dict, words: float) -> dict:
    """The record with the founder's length answer written in its own field: words_per_item on an items record, summary_words on the meetings record."""
    return {**record, ("words_per_item" if is_items(record) else "summary_words"): words}


def item_of(record) -> str | None:
    """The founder's item word in the singular (pack, report), for the length question; None on the meetings record."""
    return record.get("item") if is_items(record) else None


# ---------------------------------------------------------------- what an answer was read as, said back to the founder
_SEEN = (("words", _WORDS_RE, "{:g} words"), ("users", _USERS_RE, "{:g} users"), ("illustrations", _ILLUS, "{:g} image attempts"), ("revisions", _REVIS, "{:g} revisions"), ("storage", _STORAGE_RE, "{:g} GB of storage"))
USAGE_REASK = "Your last answer was not stored: no monthly volume was recognised in it."
USAGE_NEEDED = "the items a month in your own word for them, stated as the total (for example: 200 packs a month in total), or the meetings a month for a meeting product"


def usage_read(answer) -> dict:
    """What the usage answer was read as, in the founder's words: record (usage_record, None when the answer names no monthly volume),
    recognised (each figure read, in words) and missing (the figures the cost calculation can use that the answer does not state). An answer
    without a monthly volume recognises whatever other figures it holds (600 words) and names the volume as missing."""
    rec = usage_record(answer)
    recognised, missing = [], []
    if rec is None:
        text = re.sub(r"\([^)]*\)", " ", str(answer or "").lower())
        for _name, pat, form in _SEEN:
            m = pat.search(text)
            if m:
                recognised.append(form.format(_num(m.group(1))))
        return {"record": None, "recognised": recognised, "missing": [USAGE_NEEDED]}
    if is_items(rec):
        item, word = rec.get("item") or "item", rec.get("item_word") or "items"
        have = lambda k: rec.get(k) is not None
        recognised.append(f"{rec['items_per_month']:g} {word} a month" + (" in total" if rec.get("scope") == "total" else " per user" if rec.get("scope") == "per_user" else ""))
        for k, form, lack in (("illustrations_per_month", "{:g} image attempts a month", "the image attempts a month"), ("revisions_per_month", "{:g} revisions a month", "the revisions a month"), ("users", "{:g} users", "the users"),
                              ("tokens_in_per_item", "{:g} tokens in a " + item, f"the tokens in a {item}"), ("storage_gb", "{:g} GB of storage", "the storage in GB")):
            (recognised if have(k) else missing).append(form.format(rec[k]) if have(k) else lack)
        if have("words_per_item"):
            recognised.append(f"{rec['words_per_item']:g} words a {item}")
        if have("tokens_out_per_item"):
            recognised.append(f"{rec['tokens_out_per_item']:g} tokens out a {item}")
        if not have("words_per_item") and not have("tokens_out_per_item"):
            missing.append(f"the words or the tokens out of one {item}")
        if have("searches_per_item") or have("page_reads_per_item"):
            recognised.append(f"{float(rec.get('searches_per_item') or 0):g} searches and {float(rec.get('page_reads_per_item') or 0):g} page reads a {item}")
    else:
        for k, form, lack in (("meetings_per_month", "{:g} meetings a month", "the meetings a month"), ("minutes_per_meeting", "{:g} minutes a meeting", "the minutes a meeting"), ("users", "{:g} users", "the users"),
                              ("recipients_per_summary", "{:g} recipients a summary", "the recipients of a summary")):
            (recognised if rec.get(k) is not None else missing).append(form.format(rec[k]) if rec.get(k) is not None else lack)
    return {"record": rec, "recognised": recognised, "missing": missing}


def usage_read_line(read: dict) -> str:
    """The sentence that says which figures were recognised and which are missing, after an answer."""
    seen = "Recognised: " + ", ".join(read["recognised"]) + "." if read["recognised"] else "No figure was recognised."
    if read["record"] is None:
        return f"{USAGE_REASK} {seen} Still needed: {'; '.join(read['missing'])}."
    return seen + (f" Not stated (a price quoted in one of them stays not computable): {', '.join(read['missing'])}." if read["missing"] else "")
