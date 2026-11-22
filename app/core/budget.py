"""The budget of a plan version at the founder's usage, calculated in code from the sourced prices. The model computes no figure. Deterministic.

Units: code converts the structured usage (meetings a month, minutes a meeting, users, recipients a summary) into the units the
sourced prices are quoted in with the stated rules:
  minutes to hours;
  audio minutes to transcript tokens at 150 words a minute and 1.33 tokens a word;
  summaries to emails by recipients;
  minutes to storage at 1 MB a minute with three months kept;
  minutes to egress at 1 MB a minute.

Rows: every row names its sourced price claim and its calculated usage and is labelled "sourced price, calculated usage" (a flat fee
"sourced price, flat"); every row carries the date its price was read (accessed). The total is set against the ceiling.

Worst cases: they come from the runner-up prices (the runner-up's monthly cost minus the pick's) and from a flat plan with included
usage at twice the included amount (the fee again).

What cannot be priced: a token-priced pick the rules cannot price gets a row that says not computable and names what is missing,
never a guessed quantity, and no worst-case total is stated while a pick is not priced.

Usage in items: a usage record stated in items (packs, documents, reports) feeds the parts apart from the founder's own figures
(usage_from_items).

Billing model: the billing model of a priced part is read from its claim (billing_model): a credit pack is priced as its purchase
spread over its validity, never as a flat monthly fee."""
from __future__ import annotations

import re

from app.core import docrules

WORDS_PER_MINUTE = 150.0
TOKENS_PER_WORD = 1.33
MB_PER_MINUTE = 1.0
MONTHS_KEPT = 3
LABEL = "sourced price, calculated usage"
LABEL_FLAT = "sourced price, flat"
LABEL_UNPRICED = "not computable"
TOKEN_UNITS = {"million_tokens_in", "million_tokens_out"}
TOKENS_PER_SUMMARY_WORD = 4.0 / 3.0                                               # a summary's output tokens are its words times 4/3, a fixed conversion
OUTPUT_NOT_RECORDED = "the summary's output length is not a recorded quantity (the founder's numbers are meetings a month, minutes a meeting, users and recipients a summary), so an output-token tariff cannot be applied"   # said on the row of an output-token tariff when the summary's length is not recorded: the output quantity is never guessed
COMPOUND_NOT_ONE_PRICE = "a compound tariff (more than one price in one value) is on record and is not read as one unit price"
_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
EGRESS_WORDS = ("egress", "bandwidth", "transfer", "traffic", "outgoing")        # the words of an outgoing-data tariff, in the attribute or the value
BUYER_CONDITION = re.compile(r"^\s*(?:if|when|unless|only)\b", re.I)            # a recorded condition written as a clause on the buyer's situation, which the plan's records do not establish
ADD_ON = re.compile(r"(?<![a-z])add[-_ ]?ons?(?![a-z])", re.I)                   # the word add-on in any of its spellings, inside an underscore attribute too (summarization_add_on_pricing)
NO_OVERAGE = re.compile(r"\bno overages?\b|\bnot permit(?:ted)? overages?\b|\bdoes not permit overages?\b|\b(?:sending )?stops at\b", re.I)
OVERAGE = re.compile(r"\boverages?\b", re.I)


FOUR = ("meetings_per_month", "minutes_per_meeting", "users", "recipients_per_summary")
BUDGET_CURRENCY = "USD"                                                           # the budget's currency (the ceiling is recorded in US dollars)
_SIGN = r"(?:\$|usd|\u20ac|\u00a3|eur|gbp)"                                        # a currency mark that stands before an amount ($, USD, the euro and pound signs, EUR, GBP)
_AFTER = r"(?:usd|cents|dollars|eur|euros?|gbp|pounds?)"                           # a currency word that stands after an amount
CURRENCIES = (("EUR", "euros", re.compile(r"\u20ac|\beur\b|\beuros?\b")), ("GBP", "pounds sterling", re.compile(r"\u00a3|\bgbp\b|\bpounds? sterling\b")), ("CHF", "Swiss francs", re.compile(r"\bchf\b")),
              ("JPY", "yen", re.compile(r"\u00a5|\bjpy\b|\byen\b")), ("ILS", "shekels", re.compile(r"\u20aa|\bils\b|\bnis\b|\bshekels?\b")), ("CAD", "Canadian dollars", re.compile(r"\bcad\b|\bca?\$")),
              ("AUD", "Australian dollars", re.compile(r"\baud\b|\bau?\$")), ("INR", "rupees", re.compile(r"\u20b9|\binr\b|\brupees?\b")))   # the marks of a currency other than the budget's
STARTING = re.compile(r"\b(?:from|starts?(?:\s+\w+)?\s+at|starting\s+(?:at|from)|as\s+low\s+as)\s+(?:about\s+|around\s+|just\s+|only\s+)?" + _SIGN + r"?\s*\d", re.I)   # the words of a starting price, directly before its amount


def currency_of(claim: dict) -> tuple[str, str] | None:
    """The currency of a price claim when it is not the budget's: (code, the currency in words), read from the value's own mark (the euro
    sign, EUR, GBP), else from the claim's text when the value carries no currency mark at all; None for a US dollar price or a value with no
    mark anywhere. A value marked in US dollars is a dollar price whatever its text says."""
    for field in ("value", "text"):
        low = str(claim.get(field) or "").lower().replace("\u00a0", " ")
        hit = next(((code, words) for code, words, pat in CURRENCIES if pat.search(low)), None)
        if hit and not (code_is_dollar(low) and hit[0] not in ("CAD", "AUD")):
            return hit
        if hit or "$" in low or "usd" in low or "cents" in low:
            return None
    return None


def code_is_dollar(low: str) -> bool:
    return "usd" in low or re.search(r"(?<![a-z])\$", low) is not None


def is_starting(claim: dict) -> bool:
    """Whether a price claim states a starting price (from, starts at, starting at, as low as, directly before the amount), in its value or its text."""
    return bool(STARTING.search(str(claim.get("value") or "")) or STARTING.search(str(claim.get("text") or "")))


def is_items(n) -> bool:
    """Whether a usage record is stated in items rather than the four meeting numbers."""
    return isinstance(n, dict) and (n.get("kind") == "items" or n.get("items_per_month") is not None)


def numbers_of(usage) -> dict | None:
    """The budget's numbers from a stored usage record: an items record as it is; else the four meeting numbers with their scope and
    summary words when all four are present; None when the record cannot feed the conversion."""
    if not isinstance(usage, dict) or not usage:
        return None
    if is_items(usage):
        return dict(usage)
    numbers = {k: usage.get(k) for k in FOUR}
    if not all(v for v in numbers.values()):
        return None
    return {**numbers, "scope": usage.get("scope"), "summary_words": usage.get("summary_words")}


QUANTITY_WORDS = {"hours": "audio hours (meetings a month and minutes a meeting)", "minutes": "audio minutes (meetings a month and minutes a meeting)", "emails": "emails a month",
                  "tokens_in": "input tokens an item (the final document length alone never prices research, verification or writing: the figure comes from the founder's stated allowance)",
                  "tokens_out": "output tokens an item (words an item or tokens out an item)", "illustrations": "illustration or image attempts a month", "searches": "searches and page reads an item",
                  "storage_gb_month": "storage in GB", "egress_gb": "outgoing traffic in GB"}   # what an unpriced row says the founder's answer does not quantify


def usage_from_items(n: dict) -> dict:
    """The converted usage of an items record, each part fed apart from the founder's own figures: the items (the stated total, multiplied
    only by an explicit per-user scope, never by languages); the model input tokens (tokens in an item times the items and the revisions at the
    founder's stated share, a revision counted as a whole item when no share is stated); the model output tokens (the stated tokens out an item,
    else the words an item at 4/3 tokens a word); the illustration attempts; the searches and page reads (both an item, times the items);
    the storage in GB-months; the users. A quantity the founder did not state is None, never zero, so a tariff in that unit stays unpriced with
    the reason. Every stated figure is an assumption, every derived figure a calculated estimate with its formula among the assumptions."""
    stated = float(n.get("items_per_month") or 0); users = float(n.get("users") or 0)
    items = stated * users if (n.get("scope") == "per_user" and users) else stated
    item = str(n.get("item") or "item"); word = str(n.get("item_word") or item + "s")
    revisions = float(n.get("revisions_per_month") or 0); share = n.get("revision_share")
    rev_items = revisions * (float(share) if share is not None else 1.0)
    counted = items + rev_items
    tin, tout, words = n.get("tokens_in_per_item"), n.get("tokens_out_per_item"), n.get("words_per_item")
    sp, rp = n.get("searches_per_item"), n.get("page_reads_per_item")
    out = {"mode": "items", "item": item, "item_word": word, "items": items, "items_stated": stated, "scope": n.get("scope") or "total", "users": users, "revisions": revisions, "revision_share": share,
           "counted_items": counted, "tokens_in_per_item": tin, "tokens_out_per_item": tout, "words_per_item": words, "searches_per_item": sp, "page_reads_per_item": rp,
           "meetings": None, "meetings_stated": None, "minutes": None, "hours": None, "recipients": None, "egress_gb": None,
           "emails": float(n["emails_per_month"]) if n.get("emails_per_month") is not None else None,
           "illustrations": float(n["illustrations_per_month"]) if n.get("illustrations_per_month") is not None else None,
           "tokens_in": round(counted * float(tin)) if tin else None,
           "tokens_out": round(counted * float(tout)) if tout else (round(counted * float(words) * TOKENS_PER_SUMMARY_WORD) if words else None),
           "searches": items * (float(sp or 0) + float(rp or 0)) if (sp is not None or rp is not None) else None,
           "storage_gb_month": float(n["storage_gb"]) if n.get("storage_gb") is not None else None,
           "assumptions": [f"{stated:g} {word} a month" + (f" per user ({items:g} in all)" if n.get("scope") == "per_user" and users else ", the total across users and languages") + " (the founder's figure, an assumption)"]}
    if users:
        out["assumptions"].append(f"{users:g} users (the founder's figure, an assumption)")
    if revisions:
        out["assumptions"].append(f"{revisions:g} revisions a month" + (f", each at {float(share):g} of a {item}'s tokens (the founder's stated share)" if share is not None else f", each counted as a whole {item} (no share stated)") + " (calculated estimate)")
    if out["tokens_in"] is not None:
        out["assumptions"].append(f"Tokens in: {float(tin):g} a {item} (the founder's stated allowance) times {counted:g} {word} and revisions = {out['tokens_in']:,} (calculated estimate)")
    if out["tokens_out"] is not None:
        out["assumptions"].append((f"Tokens out: {float(tout):g} a {item} (the founder's stated figure)" if tout else f"Tokens out: {float(words):g} words a {item} (the founder's answer) at 4/3 tokens a word (D-A130)") + f" times {counted:g} {word} and revisions = {out['tokens_out']:,} (calculated estimate)")
    if out["illustrations"] is not None:
        out["assumptions"].append(f"{out['illustrations']:g} illustration attempts a month (the founder's figure, an assumption)")
    if out["searches"] is not None:
        out["assumptions"].append(f"Searches and page reads: {float(sp or 0):g} searches and {float(rp or 0):g} page reads a {item} times {items:g} {word} = {out['searches']:g} requests (calculated estimate)")
    if out["storage_gb_month"] is not None:
        out["assumptions"].append(f"{out['storage_gb_month']:g} GB of storage a month (the founder's figure, an assumption)")
    return out


def usage_from_numbers(n: dict) -> dict:
    """The converted usage from the four numbers: minutes, hours, tokens in and out, emails, storage GB-months, egress GB; every figure a
    calculated estimate with its rule, in the order the rows read them. An items record takes usage_from_items."""
    if is_items(n):
        return usage_from_items(n)
    stated = float(n.get("meetings_per_month") or 0); minutes_each = float(n.get("minutes_per_meeting") or 0); users = float(n.get("users") or 0)
    recipients = float(n.get("recipients_per_summary") or users or 0)
    meetings = stated * users if (n.get("scope") == "per_user" and users) else stated     # only an explicit per-user figure is multiplied by the users; the arithmetic below runs on the resolved total
    minutes = meetings * minutes_each
    words = float(n.get("summary_words") or 0)                                          # the founder's answer to the summary length question, one summary a meeting
    out = {"meetings": meetings, "meetings_stated": stated, "scope": n.get("scope") or "total", "minutes": minutes, "hours": round(minutes / 60.0, 4), "users": users, "recipients": recipients,
            "tokens_in": round(minutes * WORDS_PER_MINUTE * TOKENS_PER_WORD),                 # no tokens_out: the summary's output length is not among the founder's four numbers and is never a guessed quantity
            "emails": round(meetings * recipients), "storage_gb_month": round(minutes * MB_PER_MINUTE * MONTHS_KEPT / 1024.0, 4), "egress_gb": round(minutes * MB_PER_MINUTE / 1024.0, 4),
            "assumptions": [f"Speech at about {WORDS_PER_MINUTE:g} words a minute; {TOKENS_PER_WORD:g} tokens a word (calculated estimate)",
                            f"Compressed audio at about {MB_PER_MINUTE:g} MB a minute, kept for {MONTHS_KEPT} months (calculated estimate)",
                            f"One summary email to {recipients:g} recipients per meeting (calculated estimate)"]}
    if words > 0:
        out["summary_words"] = words; out["tokens_out"] = round(meetings * words * TOKENS_PER_SUMMARY_WORD)
        out["assumptions"].append(f"One summary of {words:g} words a meeting (the founder's answer); 4/3 tokens a word (D-A130)")
    return out


def _amounts(text: str) -> list[float]:
    """The money amounts of a value: a figure with a currency sign or word, or a bare figure before a unit word; a figure after per, for or
    every is part of the unit, not a price."""
    low = text.lower().replace("\u00a0", " ")
    out = []
    marked = ("$" in low) or ("usd" in low) or ("cents" in low) or re.search(r"\u20ac|\u00a3|\beur\b|\bgbp\b|\beuros?\b", low) is not None   # the euro and pound marks are currency marks: only the marked figures are prices ("EUR 26/month, 250 credits, 10 templates" states one price)
    for m in re.finditer(_NUM, low):
        before = low[max(0, m.start() - 10):m.start()]; after = low[m.end():m.end() + 8]
        if re.search(r"(?:per|/|\bfor|\bevery|\bup to|\bincludes?|\bincluding)\s*$", before):
            continue
        if marked and not (re.search(_SIGN + r"\s*$", before) or re.match(r"\s*" + _AFTER + r"\b", after)):
            continue                                                             # a value with a currency sign: only the marked figures are prices (in "5 GB on Hobby, then $0.15 per GB" the 5 is no price)
        out.append(float(m.group(1).replace(",", "")))
    return out


def _unit_from(text: str, attr: str) -> str | None:
    s = text
    if re.search(r"(?:/|per)\s*(?:audio\s*)?(?:hour|hr)s?\b", s):
        return "hour"
    if re.search(r"(?:/|per)\s*(?:audio\s*)?min(?:ute)?s?\b", s):
        return "minute"
    if re.search(r"(?:/|per)\s*(?:1\s*m|1m|m|million|mtok)\b", s):
        return "million_tokens_out" if ("output" in attr or "completion" in attr or "output" in s) else "million_tokens_in"
    if re.search(r"(?:/|per)\s*10[,.]?000\s*(?:emails?|messages?)|(?:/|per)\s*10k\b\s*(?:emails?|messages?)?", s):
        return "ten_thousand_emails"                                             # "per 10,000 emails" and "per 10K emails" are a thousand-emails tariff at a tenth of the amount
    if re.search(r"(?:/|per)\s*1[,.]?000\s*(?:emails?|messages?)", s):
        return "thousand_emails"
    if re.search(r"(?:/|per)\s*1[,.]?000\s*(?:searches|queries|requests|lookups)|(?:/|per)\s*1k\s*(?:searches|queries|requests|lookups)", s):
        return "thousand_searches"                                               # "per 1,000 searches", "per 1K queries"
    if re.search(r"(?:/|per)\s*(?:search|query|lookup)\b", s):
        return "search"                                                          # "per search"
    if re.search(r"(?:/|per)\s*(?:generated\s+|output\s+)?(?:image|picture|illustration)s?\b", s):
        return "image"                                                           # "per image", "per generated image"
    if re.search(r"(?:/|per)\s*(?:email|message|recipient)\b", s):
        return "email"                                                           # a price per email; price_of reads it as a thousand-emails tariff at a thousand times the amount (so $0.0001 per email is never applied per thousand)
    if re.search(r"(?:/|per)\s*gb", s):
        return "gb_egress" if any(w in attr or w in s for w in EGRESS_WORDS) else "gb_month"   # traffic and outgoing are egress words ("outgoing traffic is priced at $0.05 per GB" is an egress tariff, never a price of the storage GB-months)
    if re.search(r"(?:/|per|a)\s*(?:mo|month)\b", s):
        return "flat_month"
    return None


def _mixed_units(value: str, attr: str) -> bool:
    """Whether a value joined by commas or semicolons states prices in more than one unit ("2.50 USD per million input tokens,
    10 USD per million output tokens" is two tariffs; "Hobby $0, Pro $25, Scale $499 per month" is one unit's tiers)."""
    units = {u for u in (_unit_from(seg, attr) for seg in re.split(r"[,;]", value) if _amounts(seg)) if u}
    return len(units) > 1


def _compound(value: str, attr: str) -> bool:
    """Whether a value states more than one price the rules cannot resolve to one unit price: two prices joined by and or a plus sign,
    a slash before the unit word, comma or semicolon segments priced in different
    units, or an input rate and an output rate in one value wherever the unit is written (in "$0.40 input, $2.00 output, per 1M tokens"
    the unit stands once at the end; its 0.40 is never read as an output price). A list of one unit's tiers
    ("Hobby $0, Pro $25, Scale $499 per month") is not compound."""
    amounts = _amounts(value)
    if len(amounts) < 2 or re.match(r"^\s*(?:" + _SIGN + r"\s*)?" + _NUM + r"\s*(?:/|per|a)\s*(?:mo|month)", value):
        return False
    both = bool(re.search(r"\b(?:input|prompt)\b", value) and re.search(r"\b(?:output|completion)\b", value))
    return (" and " in value) or (" + " in value) or ("/" in value.split("per")[0]) or _mixed_units(value, attr) or both


def is_compound(claim: dict) -> bool:
    """Whether a price claim's value is a compound tariff: one the rules keep as an unpriced obligation, never one unit price."""
    return _compound(str(claim.get("value") or "").lower().replace("\u00a0", " "), str(claim.get("attribute") or "").lower())


def words_of(text) -> list[str]:
    """The one tokenisation of a price field and of a product version: camelCase split, lower case, and every
    character that is not a letter or a digit a separator, underscores, hyphens and points included."""
    return re.findall(r"[^\W_]+", re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", str(text or "")).casefold())


_UNCACHED = {("non", "cached"), ("not", "cached"), ("without", "cache"), ("without", "caching"), ("cache", "miss"), ("cache", "misses")}   # read first: the ordinary rate named against the cache (the one word uncached is no cache word)


def discount_rate(claim: dict) -> bool:
    """Whether a claim states a cache-only or batch-only rate: after one tokenisation
    of its attribute, value and text, the uncached forms are read first, and any other word that starts with cach or batch is such a rate."""
    for field in ("attribute", "value", "text"):
        words, i = words_of(claim.get(field)), 0
        while i < len(words):
            if tuple(words[i:i + 2]) in _UNCACHED:
                i += 2; continue
            if words[i].startswith(("cach", "batch")):
                return True
            i += 1
    return False


QUANTITY_FIGURE = re.compile(r"\d[\d,.]*\s*(?:hours?|hrs?|minutes?|mins?|seconds?|secs?|days?|weeks?|[kmgt]i?b|emails?|messages?|requests?|documents?|images?|users?|seats?)\b")   # a figure followed by the unit it counts (time, data, a countable usage unit)


def counts_a_unit(value: str) -> bool:
    """Whether the first figure of a value counts a unit. The figure and its unit decide, wherever the figure stands: the same count
    behind any words (up to, about, at most, approximately, a maximum of) is a quantity, never a plan price."""
    first = re.search(r"\d", value)
    return bool(first and QUANTITY_FIGURE.match(value, first.start()))


def price_of(claim: dict) -> dict | None:
    """One price claim's amount and unit for the budget: {amount, unit} with unit one of hour, minute, million_tokens_in, million_tokens_out,
    thousand_emails, image, search, thousand_searches, flat_month (included: the usage credit it includes), gb_month, gb_egress, free_up_to (included: the allowance); None
    when the value states no price the rules know or a compound tariff (two prices joined by and or a plus sign, or comma or semicolon
    segments priced in different units; a list of one unit's tiers is not compound)."""
    value = str(claim.get("value") or "").lower().replace("\u00a0", " "); attr = str(claim.get("attribute") or "").lower(); text = str(claim.get("text") or "").lower()
    money = ("$" in value) or ("usd" in value) or ("cents" in value) or currency_of({"value": value}) is not None   # a price in another currency is money, read in its own currency
    if not money and (any(w in attr for w in ("quota", "allowance", "included", "limit")) or "free" in value or "free" in attr or value.strip() in ("0", "0.0")):
        inc = re.search(_NUM + r"\s*(?:gb|emails?|requests?|operations?|minutes?|hours?)", value)
        return {"amount": 0.0, "unit": "free_up_to", "included": float(inc.group(1).replace(",", "")) if inc else None, "included_text": value[:80]}
    if re.match(r"^\s*(?:\$|usd\s*)?0(?:\.0+)?\s*(?:usd)?\s*(?:/|per|a)?\s*(?:mo|month)?\s*$", value) or ("free" in value and not re.search(r"[1-9]", value)):
        return {"amount": 0.0, "unit": "free_up_to", "included": None, "included_text": value[:80]}
    if not money and re.fullmatch(r"\s*(?:up to\s+|about\s+|~\s*)?\d[\d,.]*\s*(?:months?|days?|weeks?|years?)\s*", value):
        return None                                                          # a value that states a period alone ("6 months", a credit's validity) is no price; the text's tariff is read by billing_model
    if not money and counts_a_unit(value) and docrules.generation() >= 2:
        return None                                                          # from generation 2 on, a value without a money mark whose figure counts a unit (a plan's monthly usage cap stated as a count of hours) states a quantity, never a price: otherwise the sentence's "a month" would make the count a plan price in US dollars
    amounts = _amounts(value)
    if not amounts:
        return None
    if _compound(value, attr):
        return None                                                          # a compound tariff: not one unit price ("2.50 USD per million input tokens, 10 USD per million output tokens" is never priced at 2.50 under the output unit, and neither is a value with the unit written once at the end)
    amt = amounts[0]
    m = re.search(_SIGN + r"\s*" + re.escape(f"{amt:g}" if amt != int(amt) else str(int(amt))) + r"(?:\.\d+)?", value)
    tail = value[m.end():m.end() + 40] if m else ""
    unit = (_unit_from(tail, attr) if re.match(r"\s*(?:/|per|a)\b", tail) else None) or _unit_from(value, attr) or _unit_from(attr, attr) or _unit_from(" ".join(words_of(claim.get("attribute"))), attr) or _unit_from(text, attr)
    if unit is None:
        return None
    if unit == "email":
        amt, unit = amt * 1000.0, "thousand_emails"                               # a per-email price in the budget's thousand-emails unit
    if unit == "ten_thousand_emails":
        amt, unit = amt / 10.0, "thousand_emails"                                 # a per-10,000-emails price in the same unit
    if unit in TOKEN_UNITS and discount_rate(claim):
        return None                                                          # a cache-only or batch-only token rate is no base tariff, wherever the claim states it; an uncached rate is the ordinary one
    inc = None; inc_unit = None
    if unit == "flat_month":
        im = re.search(r"includ\w*\s*(?:\$|usd\s*)?" + _NUM, value)
        if im:
            inc, inc_unit = float(im.group(1).replace(",", "")), "usd"               # a usage credit in money
        else:
            im = re.search(r"\bfor\s+" + _NUM + r"\s*(?:monthly\s+)?(emails?|messages?|minutes?|hours?|gb)\b", value)
            if im:
                inc = float(im.group(1).replace(",", "")); w = im.group(2)
                inc_unit = "thousand_emails" if w.startswith(("email", "message")) else "minute" if w.startswith("minute") else "hour" if w.startswith("hour") else "gb_month"
    elif unit in ("gb_month", "gb_egress") and m:
        im = re.search(_NUM + r"\s*(gb|tb)\b", value[:m.start()])                 # the allowance named before the price ("5 GB on Hobby, then $0.15 per GB")
        inc = (float(im.group(1).replace(",", "")) * (1000.0 if im.group(2) == "tb" else 1.0)) if im and re.search(r"then|above|beyond|exceed|includ|free|after", value) else None
    elif unit == "thousand_emails" and m:
        im = re.search(r"(?:first\s+)?" + _NUM + r"\s+(?:free\s+)?(?:emails?|messages?)(?:\s+(?:a|per)\s+month)?(?:\s+(?:are|is))?\s+free\b|" + _NUM + r"\s+free\s+(?:emails?|messages?)\b", value[:m.start()])
        inc = float((im.group(1) or im.group(2)).replace(",", "")) if im else None   # the free allowance named before the price ("First 100 emails free, then $1 per 1,000 emails") is subtracted at the usage
    return {"amount": amt, "unit": unit, "included": inc, "included_unit": inc_unit or (unit if inc is not None else None)}


CREDIT_WORD = re.compile(r"\bcredits?\b", re.I)
MONEY_CREDIT = re.compile(r"(?:\$|usd\s*)\s*\d[\d,.]*(?:\s*/\s*(?:mo|month))?\s*(?:of\s+)?(?:monthly\s+|free\s+)?(?:usage\s+)?credits?\b", re.I)   # a credit stated in money ($5 of monthly usage credits) is a plan's included allowance, which price_of reads; never a credit pack. The words usage credits without an amount of money name a credit tariff and stay
_NUM_WORDS = {"one": 1, "a": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "twenty-four": 24}
_PACK_UNITS = {"email": "emails", "emails": "emails", "message": "emails", "messages": "emails", "search": "searches", "searches": "searches", "query": "searches", "queries": "searches", "request": "searches", "requests": "searches",
               "image": "images", "images": "images", "minute": "minutes", "minutes": "minutes", "hour": "hours", "hours": "hours", "token": "tokens", "tokens": "tokens"}
_UNIT_FAMILY = {"thousand_emails": "emails", "search": "searches", "thousand_searches": "searches", "image": "images", "minute": "minutes", "hour": "hours", "million_tokens_in": "tokens", "million_tokens_out": "tokens"}
_UNIT_SCALE = {"thousand_emails": 1000.0, "thousand_searches": 1000.0, "million_tokens_in": 1e6, "million_tokens_out": 1e6}
_FAMILY_UNIT = {"emails": "thousand_emails", "searches": "thousand_searches", "images": "image", "minutes": "minute", "hours": "hour"}   # the budget unit a credit pack's units are priced in
PLAN_CREDITS = re.compile(_NUM + r"\s+(?:monthly\s+|included\s+|free\s+)?credits?\b(?!\s*(?:=|covers?|is|equals?|buys?|gives?|for|of|\(|:))", re.I)   # a count of credits beside a plan's monthly fee ("26 a month, 250 credits"): the plan's included allowance
PER_CREDIT = re.compile(r"(?:per|/|a|each|one|1)\s+credit\b|credits?\s+(?:expire|cost|covers?|buys?)", re.I)
BILLING_UNREADABLE = "a credit-based tariff whose pack size or pack price is not on record (the word credit without the units a credit covers and its price)"


def _months(amount: str, word: str) -> float:
    n = _NUM_WORDS.get(amount) if amount in _NUM_WORDS else float(amount.replace(",", ""))
    return float(n) * (12.0 if word.startswith("year") else 1.0 / 30.0 if word.startswith("day") else 1.0)


def billing_model(claim: dict) -> dict | None:
    """The billing model of a price claim read from its text, value and conditions: a credit pack ({model: credit_pack, pack_size, pack_unit,
    pack_usd, expiry_months, free, minimum_packs}) when the claim prices by credit and names what a credit covers and costs (for example: one credit
    covers 10,000 emails from about $2.50, credits expire six months after purchase, the first credit free for one month); {model: unreadable,
    reason} when the claim prices by credit but the pack's size or price is not on record (the part then stays unpriced with that reason); None
    when the claim names no credit (a per-unit rate, a flat monthly fee or a free allowance, decided by price_of's unit)."""
    text = " ".join([str(claim.get("text") or ""), str(claim.get("value") or "")] + [str(x) for x in claim.get("conditions") or []]).lower().replace("\u00a0", " ")
    stripped = MONEY_CREDIT.sub(" ", text)
    if (price_of(claim) or {}).get("unit") == "flat_month" and not PER_CREDIT.search(stripped):
        stripped = PLAN_CREDITS.sub(" ", stripped)                           # a flat monthly plan that includes a count of credits (for example "Low Usage: 26 a month, 250 credits") is a plan with an allowance, never an unreadable credit pack that drops the named plan's price
    if not CREDIT_WORD.search(stripped):
        return None                                                          # a monetary usage credit names no pack, so a flat plan that includes one is never read as an unreadable credit pack
    m = re.search(r"(?:one|1|a|each|per)\s+credit\s*(?:=|covers|is|equals|buys|gives|for|of|\(|:)\s*(?:about\s+|up to\s+)?" + _NUM + r"\s*(?:[a-z]+\s+)?(emails?|messages?|searches|queries|requests|images?|minutes?|hours?|tokens?)\b", text)
    price = None
    for pat in (r"(?:\$|usd\s*)" + _NUM + r"\s*(?:usd\s*)?(?:per|/|a|each)\s*credit", r"credits?\b[^.;]{0,60}?(?:from\s+)?(?:about\s+|around\s+)?\$\s*" + _NUM, r"(?:from\s+)?(?:about\s+)?\$\s*" + _NUM + r"\s*(?:per|/)\s*\d[\d,]*\s*(?:emails?|messages?|searches|queries|requests|images?)"):
        pm = re.search(pat, text)
        if pm:
            price = float(pm.group(1).replace(",", "")); break
    if not m or price is None:
        return {"model": "unreadable", "reason": BILLING_UNREADABLE}
    exp = re.search(r"(?:expire|expires|expiry|valid|validity)\w*\s*(?:up to\s+|for\s+|after\s+|within\s+|of\s+)?(?:up to\s+)?(\d[\d,]*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty-four)\s*(months?|days?|years?)", text)
    free = re.search(r"first (?:credit|\d[\d,]* (?:emails?|messages?|searches|queries|requests|images?)) (?:is |are )?free(?: for (?:one|a|\d+) months?)?", text)
    mn = re.search(r"minimum (?:purchase )?(?:of )?(\d+) credits?", text)
    return {"model": "credit_pack", "pack_size": float(m.group(1).replace(",", "")), "pack_unit": _PACK_UNITS.get(m.group(2), m.group(2)), "pack_usd": price,
            "expiry_months": round(_months(exp.group(1), exp.group(2)), 4) if exp else None, "free": ("the " + free.group(0)) if free else None, "minimum_packs": int(mn.group(1)) if mn else 1}


def _credit_row(bm: dict, unit: str, amt: float, usage: dict) -> tuple[float, str, dict] | None:
    """The monthly allocation of a credit pack at the usage: the packs the validity window needs (the stated monthly quantity
    times the months of validity, less the free allowance's units once (the first month's quantity up to the allowance's stated quantity,
    or up to one pack for the free first credit: a one-time allowance that never recurs), over the pack's size, at least the minimum purchase), their purchase price spread over the validity; without an
    expiry on record, the purchase consumed at the stated rate (a one-time allowance shifts no steady month). The purchase (packs times the
    pack price) stays apart from the allocation on the record and is named in the row's text with the allowance arithmetic."""
    import math
    key = USAGE_KEY.get(unit); fam = _UNIT_FAMILY.get(unit)
    if key is None or fam is None or fam != bm.get("pack_unit") or usage.get(key) is None:
        return None
    q = float(usage[key]); size = float(bm["pack_size"]); price = float(bm["pack_usd"]); months = bm.get("expiry_months")
    minimum = int(bm.get("minimum_packs") or 1)
    rec = {"model": "credit_pack", "pack_usd": price, "pack_size": size, "pack_unit": fam, "expiry_months": months, "free": bm.get("free"), "minimum_packs": minimum, "monthly_units": q}
    if months:
        fm = re.search(r"\bfirst\s+(\d[\d,]*)\s", str(bm.get("free") or ""))
        free_q = min(q, float(fm.group(1).replace(",", "")) if fm else size) if bm.get("free") else 0.0   # the allowance covers the first month's units up to its stated quantity (first 100 emails free) or, for the first credit, one pack, once
        paid = max(0.0, q * float(months) - free_q)
        packs = max(minimum, math.ceil(paid / size)) if paid > 0 else minimum
        monthly = packs * price / float(months)
        how = f"{packs} pack{'s' if packs != 1 else ''} over its validity, the purchase of {packs * price:.2f} USD spread over its validity"
        if bm.get("free"):
            rec.update({"free_units": free_q, "paid_units": paid, "window_units": q * float(months)})
    else:
        packs = max(minimum, math.ceil(q / size)) if q > 0 else minimum
        monthly = price * q / size
        how = f"no expiry on record: the purchase of {packs * price:.2f} USD consumed at the stated rate"
    rec.update({"purchase_usd": round(packs * price, 2), "packs": packs, "allocation_usd": round(monthly, 2)})
    used = (f"about {qty(q)} {fam} a month; credit pack: {price:.2f} USD for {size:,.0f} {fam}" + (f", valid {float(months):g} months" if months else "") + f" ({how})"
            + (f"; {allowance_text(rec)}" if bm.get("free") else ""))
    return monthly, used, rec


def allowance_text(rec: dict) -> str:
    """The one sentence of a credit pack's free allowance, from its pricing record, printed wherever the record is (the cost row, the
    option's price cell, the pricing sentence that replaces a stored denial or question): the allowance arithmetic of the validity window
    when the record holds it, else the statement that the one-time allowance is not in the steady month."""
    free = str(rec.get("free") or "")
    if rec.get("free_units") is not None:
        fam = rec.get("pack_unit") or "units"
        return f"{free} (a one-time allowance: {float(rec['free_units']):,.0f} of the first month's {float(rec.get('monthly_units') or 0):,.0f} {fam} free, {float(rec['paid_units']):,.0f} {fam} paid over the validity)"
    return f"{free} (a one-time allowance, not in the steady month)"


def ceiling_issue(block: dict | None) -> str | None:
    """When the plan's monthly total exceeds the founder's ceiling, the open issue that says so, naming the rows
    that exceed it: the priced rows, largest first, whose running sum passes the ceiling. None at or under the ceiling, or without a total or a ceiling."""
    total, ceiling = (block or {}).get("total_usd"), (block or {}).get("ceiling_usd")
    if total is None or ceiling is None or float(total) <= float(ceiling):
        return None
    named, running = [], 0.0
    for r in sorted((r for r in block.get("rows") or [] if r.get("monthly_usd") is not None), key=lambda r: (-float(r["monthly_usd"]), str(r.get("item")))):
        named.append(f"{r.get('item')} ({r.get('provider')}) {float(r['monthly_usd']):.2f} USD"); running += float(r["monthly_usd"])
        if running > float(ceiling):
            break
    return f"The plan's monthly total of {float(total):.2f} USD exceeds the founder's ceiling of {float(ceiling):.2f} USD by {float(total) - float(ceiling):.2f} USD; the rows that exceed it: {', '.join(named)}."


def qty(x: float) -> str:
    """The printed figure of a calculated quantity of a whole unit (hours, minutes, emails): an integral figure as an integer, otherwise with
    at most three decimals, never with an exponent (the :g format would write 10167.7 for 10167.683 hours and 1.2e+06 minutes)."""
    return f"{x:.3f}".rstrip("0").rstrip(".")


def row_for(item: str, provider: str, claim: dict, usage: dict) -> dict | None:
    """One budget row from one price claim at the usage: None when the rules cannot convert it."""
    p = price_of(claim)
    bm = billing_model(claim)
    if bm is not None and bm.get("model") == "unreadable":
        return None                                                              # a credit-based tariff whose pack cannot be read; budget_of adds the row with that reason
    if p is None and bm is not None:
        unit0 = _FAMILY_UNIT.get(bm.get("pack_unit"))
        p = {"amount": float(bm["pack_usd"]) / float(bm["pack_size"]) * _UNIT_SCALE.get(unit0 or "", 1.0), "unit": unit0, "included": None, "included_unit": None} if unit0 else None   # a pack priced per credit ($2.5 per credit, 10,000 emails) is the family's unit at the pack's rate; the credit row below prices it
    if p is None or buyer_condition(claim):
        return None                                                              # a tariff or allowance conditioned on the buyer's situation ("if hosted in Amazon EC2") is not applied to a figure
    unit = p["unit"]; amt = p["amount"]
    allowed = units_for(item)
    if allowed and unit not in allowed:
        return None                                                              # a tariff in a unit the item does not consume is not applied (a hosting row never consumes the meetings' hours)
    inc = p.get("included") or 0.0
    key = USAGE_KEY.get(unit)
    if key and usage.get(key) is None:
        return None                                                              # the founder's answer does not quantify this unit (an items record without the figure; the summary's output length when it is not recorded); budget_of adds the row that says so
    billing = None
    if bm is not None and unit not in ("flat_month", "free_up_to"):
        credit = _credit_row(bm, unit, amt, usage)
        if credit is None:
            return None                                                          # the pack's unit is not the row's (a credit of minutes under an email tariff): not read
        monthly, used, billing = credit
    elif unit == "hour":
        monthly, used = amt * usage["hours"], f"{qty(usage['hours'])} hours"
    elif unit == "minute":
        monthly, used = amt * usage["minutes"], f"{qty(usage['minutes'])} minutes"
    elif unit == "million_tokens_in":
        monthly, used = amt * usage["tokens_in"] / 1e6, f"about {usage['tokens_in'] / 1e6:.2f} M tokens in" + _formula(usage, "tokens_in")
    elif unit == "million_tokens_out":
        if usage.get("tokens_out") is None:
            return None                                                      # no recorded output quantity to apply the tariff to; budget_of adds the not-computable row that says so
        monthly, used = amt * usage["tokens_out"] / 1e6, f"about {usage['tokens_out'] / 1e6:.3f} M tokens out" + _formula(usage, "tokens_out")   # the founder's summary length at 4/3 tokens a word
    elif unit == "thousand_emails":
        monthly, used = amt * max(0.0, usage["emails"] - inc) / 1000.0, f"about {qty(usage['emails'])} emails" + (f" ({inc:g} free)" if inc else "")   # the stated free allowance subtracted
    elif unit == "image":
        monthly, used = amt * usage["illustrations"], f"{qty(usage['illustrations'])} image attempts (the founder's figure)"
    elif unit in ("search", "thousand_searches"):
        monthly, used = amt * usage["searches"] / (1000.0 if unit == "thousand_searches" else 1.0), f"about {qty(usage['searches'])} searches and page reads" + _formula(usage, "searches")
    elif unit == "gb_month":
        monthly, used = amt * max(0.0, usage["storage_gb_month"] - inc), f"about {usage['storage_gb_month']:.2f} GB-month" + (f" ({inc:g} GB included)" if inc else "")
    elif unit == "gb_egress":
        monthly, used = amt * max(0.0, usage["egress_gb"] - inc), f"about {usage['egress_gb']:.2f} GB" + (f" ({inc:g} GB included)" if inc else "")
    elif unit == "flat_month":
        monthly, used = amt, "flat monthly fee"
    elif unit == "free_up_to":
        monthly, used = 0.0, ("within the free allowance" + (f" ({p['included']:g})" if p.get("included") else ""))
    else:
        return None
    cur = currency_of(claim)
    if unit == "flat_month" and is_starting(claim):
        used = "flat monthly fee, a starting price"                             # a starting price that stands is worded as one
    return {"item": item, "provider": provider, "unit_price": str(claim.get("value") or ""), "usage": used, "monthly_usd": None if cur else round(monthly, 2), "label": LABEL_FLAT if unit in ("flat_month", "free_up_to") else LABEL, "claims": [claim["id"]], "accessed": claim.get("accessed"),   # accessed: the date the price was read
            "unit": unit, "included": p.get("included"), "included_unit": p.get("included_unit"), "exact": monthly, "_text": str(claim.get("text") or "") + " " + str(claim.get("value") or ""), "_product": str(claim.get("product_id") or ""), "_starting": unit == "flat_month" and is_starting(claim),
            **({"currency": cur[0], "currency_words": cur[1], "monthly_amount": round(monthly, 2)} if cur else {}),   # a price in another currency keeps its own amount and is no US dollar figure
            **({"billing": billing} if billing else {})}                          # the pricing record of a credit pack (the purchase apart from the allocation), kept on the row


def _formula(usage: dict, key: str) -> str:
    """The formula of a derived quantity in the row's own words, on an items record (empty on the meetings record, whose rules the assumptions state)."""
    if usage.get("mode") != "items":
        return ""
    item, word, counted = usage.get("item") or "item", usage.get("item_word") or "items", float(usage.get("counted_items") or 0)
    if key == "tokens_in":
        return f" ({float(usage.get('tokens_in_per_item') or 0):g} tokens a {item} times {counted:g} {word} and revisions)"
    if key == "tokens_out":
        tout, words = usage.get("tokens_out_per_item"), usage.get("words_per_item")
        return f" ({float(tout):g} tokens a {item} times {counted:g} {word} and revisions)" if tout else f" ({float(words or 0):g} words a {item} at 4/3 tokens a word times {counted:g} {word} and revisions)"
    if key == "searches":
        return f" ({float(usage.get('searches_per_item') or 0):g} searches and {float(usage.get('page_reads_per_item') or 0):g} page reads a {item} times {float(usage.get('items') or 0):g} {word})"
    return ""


def buyer_condition(claim: dict) -> str | None:
    """The first recorded condition of a price claim that is a clause on the buyer's situation
    (it opens with if, when, unless or only: "if hosted in Amazon EC2"), which the plan's records do not establish; None when every condition
    names the plan or the tariff itself ("Hobby plan", "as of October 2026")."""
    return next((str(x) for x in claim.get("conditions") or [] if BUYER_CONDITION.match(str(x))), None)


def is_add_on(claim: dict) -> bool:
    """Whether a price claim states an add-on, an increment on a base tariff: its value opens with a plus sign ("+$0.02/hr") or its
    attribute or text names an add-on (summarization_add_on_pricing, "speaker diarization add-on")."""
    return str(claim.get("value") or "").strip().startswith("+") or bool(ADD_ON.search(str(claim.get("attribute") or "") + " " + str(claim.get("text") or "")))


def no_overage(claim: dict) -> bool:
    """Whether a plan's record says it admits no overage (no overages, does not permit overages, sending stops at the allowance)."""
    return bool(NO_OVERAGE.search(" ".join([str(claim.get("text") or ""), str(claim.get("value") or "")] + [str(x) for x in claim.get("conditions") or []])))


def is_overage(claim: dict) -> bool:
    """Whether a price claim states an overage rate (the word overage in its attribute, value or text)."""
    return bool(OVERAGE.search(" ".join(str(claim.get(k) or "") for k in ("attribute", "value", "text"))))


def applicable_rows(rows: list[dict], usage: dict, claims: dict, notes: list | None = None, where: str = "") -> list[dict]:
    """The rows of one figure that apply at the stated usage, with every drop noted: a plan row
    (a flat fee or a free allowance) whose record admits no overage and whose allowance is below the usage in its unit cannot serve the usage
    (a free plan of 100 emails against a usage of 120); an overage rate (a usage row) applies only beside a kept plan row; an add-on applies
    only beside a base tariff of the figure (a diarization add-on alone is never the whole figure). The base is established from the rows that
    remain in the figure, so a rejected dependent tariff authorises no other dependent tariff. claims: the price claims by id, for the records the rules read."""
    kept, out = [], []
    for r in rows:
        c = claims.get((r.get("claims") or [None])[0]) or {}
        if r.get("unit") in ("flat_month", "free_up_to") and no_overage(c):
            key = USAGE_KEY.get(r.get("included_unit") or ""); inc = r.get("included")
            if key and inc is not None and float(usage.get(key) or 0) > float(inc):
                if notes is not None:
                    notes.append(f"{where}: {c.get('id')} dropped from the figure: the plan admits no overage and its allowance ({qty(float(inc))} {key.replace('_', ' ')}) is below the stated usage ({qty(float(usage.get(key) or 0))} {key.replace('_', ' ')})")
                continue
        kept.append((r, c))
    plan = any(r.get("unit") in ("flat_month", "free_up_to") for r, _ in kept)
    base = any(r.get("unit") not in ("flat_month", "free_up_to") and not is_add_on(c) and (plan or not is_overage(c)) for r, c in kept)   # the base is established from the rows that remain in the figure; an overage rate that leaves it for want of a plan row is no base
    for r, c in kept:
        usage_row = r.get("unit") not in ("flat_month", "free_up_to")
        if usage_row and is_overage(c) and not plan:
            if notes is not None:
                notes.append(f"{where}: {c.get('id')} dropped from the figure: an overage rate applies only beside a plan row of the figure, and the figure has none")
            continue
        if usage_row and is_add_on(c) and not base:
            if notes is not None:
                notes.append(f"{where}: {c.get('id')} dropped from the figure: an add-on is an increment on a base tariff, and the figure has no base tariff")
            continue
        out.append(r)
    return out


def unpriced_reason(pk: dict, usage: dict, claims: list[dict]) -> str:
    """Why a part's claims price nothing at the usage, from the records: a credit-based tariff whose pack cannot be read; a
    quantity the founder's answer does not state (named in the founder's units); a tariff in a unit this part does not consume; a tariff
    conditioned on the buyer's situation; else no tariff in a unit the rules read, with the attributes seen."""
    allowed = units_for(pk.get("item") or "") or set()
    if any((billing_model(c) or {}).get("model") == "unreadable" for c in claims):
        return BILLING_UNREADABLE
    missing = [QUANTITY_WORDS.get(USAGE_KEY[(price_of(c) or {}).get("unit")], USAGE_KEY[(price_of(c) or {}).get("unit")]) for c in claims if (price_of(c) or {}).get("unit") in USAGE_KEY and usage.get(USAGE_KEY[(price_of(c) or {}).get("unit")]) is None]
    if missing:
        return "the founder's answer does not quantify " + "; ".join(dict.fromkeys(missing))
    other = sorted({(price_of(c) or {}).get("unit") for c in claims if price_of(c) and allowed and (price_of(c) or {}).get("unit") not in allowed} - {None})
    if other:
        return "no tariff in a unit this part consumes is on record (units seen: " + ", ".join(u.replace("_", " ") for u in other) + ")"
    cond = next((buyer_condition(c) for c in claims if buyer_condition(c)), None)
    if cond:
        return f"the tariff is conditioned on the buyer's situation, which the plan's records do not establish ({cond})"
    seen = sorted({str(c.get("attribute") or "").strip() for c in claims if str(c.get("attribute") or "").strip()})
    return "no tariff in a unit the rules read is on record" + (f" (attributes seen: {'; '.join(seen)})" if seen else "")


def unpriced_row(pk: dict, why: str | None = None, claims: list | None = None) -> dict:
    """The row of a token-priced pick the rules could not price: what is missing, named from the
    records (no price claim of the pick's own model, and whether the pick names a model version; or claims whose values state no price in a
    unit the rules read, with the attributes seen); monthly_usd None, never a guessed figure, model or quantity. With why and claims:
    the row of the given claims with the given reason (an output-token tariff with no recorded quantity to apply it to)."""
    claims = pk.get("claims") or [] if claims is None else claims
    if why is not None:
        pass
    elif not claims:
        why = "no token tariff of the pick's own model is on record" + ("" if pk.get("product_version") else ": the pick names no model version")
    else:
        seen = sorted({str(c.get("attribute") or "").strip() for c in claims if str(c.get("attribute") or "").strip()})
        why = "no token tariff in a unit the rules read is on record" + (f" (attributes seen: {'; '.join(seen)})" if seen else "")
    return {"item": pk["item"], "provider": pk["provider"], "unit_price": "", "usage": "not computable: " + why, "monthly_usd": None, "label": LABEL_UNPRICED, "claims": [c["id"] for c in claims], "accessed": None}


GENERIC_PLAN_WORDS = {"api", "plan", "tier", "the", "and", "for", "with", "mode", "service", "pay", "as", "you", "go", "per", "month", "monthly", "pre", "recorded", "file", "upload", "batch", "web", "app", "cloud", "model", "models", "instance", "paid", "hosted", "open", "compute", "worker", "workers", "background"}
TIER_WORDS = {"free", "hobby", "basic", "starter", "pro", "professional", "plus", "premium", "standard", "business", "team", "teams", "scale", "growth", "enterprise", "developer", "launch", "essentials"}
ITEM_UNITS = (("email", {"thousand_emails"}), ("host", {"gb_month", "gb_egress"}), ("storage", {"gb_month", "gb_egress"}), ("transcri", {"hour", "minute"}), ("speech", {"hour", "minute"}),
              ("stt", {"hour", "minute"}), ("audio", {"hour", "minute"}), ("llm", {"million_tokens_in", "million_tokens_out"}), ("summar", {"million_tokens_in", "million_tokens_out"}), ("language model", {"million_tokens_in", "million_tokens_out"}),
              ("illustration", {"image"}), ("image", {"image"}), ("search", {"search", "thousand_searches"}))   # the last three keys: the illustration part (either spelling of its key) and the search part


def units_for(item: str) -> set | None:
    """The usage units an item of this class consumes (plus the flat and free units); None for an item of no known class."""
    low = (item or "").lower(); out = set()
    for key, units in ITEM_UNITS:
        if key in low:
            out |= units
    return (out | {"flat_month", "free_up_to"}) if out else None


USAGE_KEY = {"thousand_emails": "emails", "gb_month": "storage_gb_month", "gb_egress": "egress_gb", "hour": "hours", "minute": "minutes", "million_tokens_in": "tokens_in", "million_tokens_out": "tokens_out",
             "image": "illustrations", "search": "searches", "thousand_searches": "searches"}


_TIMES = r"(?:once|twice|(?:\d+|" + "|".join(w for w in _NUM_WORDS if w != "a") + r")[\s-]?(?:times?|off))"   # a count of the times an amount is charged (once, one-time, a one-off), from the number words the rules already read
_PERIODLY = r"(?:bi|semi)?-?(?:(?:hour|da[iy]|week|month|quarter|year)ly|annual(?:ly)?)"   # the adverb of a calendar period (monthly, yearly, annually), the adjective annual with it (the annual plan), and the period's own noun is read through it (_cadences: day as daily)
_DENOMINATOR = r"\s*(?:" + _NUM + r")?[a-z\s-]*"                                   # the words of a rate's denominator: a count (a figure read whole), then words to the next mark or figure
_CADENCE = re.compile(r"(?:/|\b(?:per|each|every)\b)(?=(?P<den>" + _DENOMINATOR + r"))|\b(?P<adv>" + _PERIODLY + r")\b|\b(?P<times>" + _TIMES + r")\b")
_CONNECTIVES = frozenset("a an the and or but nor plus with without for of from at on in by to as than then per is are was were be been has have had will would can it its this that these those which who whose where when while if so not no".split())   # the function words of the language: a word that opens another phrase, so it belongs to no amount; no one of them decides what an amount is
_stem = lambda w: w[:-1] if len(w) > 3 and w.endswith("s") else w
_AMOUNT = re.compile(_SIGN + r"\s*" + _NUM + r"|" + _NUM + r"\s*(?:us\s*)?" + _AFTER + r"\b")   # an amount with its currency mark, before it or behind it
_NEXT_PHRASE = re.compile(r",(?!(?<=\d,)\d)|\b(?:and|or|but|nor|plus|then)\b")     # where the words behind one amount end and the next amount's begin: a comma or a conjunction, and never the comma between two digits, which is a figure's own (1,000)
_SENTENCE_END = re.compile(r"[.;](?=\s|$)")                                         # what ends a sentence, by one definition for every cut of this reading: a point or a semicolon with a space or the text's end behind it; a point between two digits is a figure's own (1.0, 37.50) and ends nothing


def _denominator_words(phrase: str) -> list:
    words = []
    for w in phrase.split()[:4]:
        if w in _CONNECTIVES or w in ("each", "every"):
            break
        words.append(w.strip("-"))
    return words


def _denominator(phrase: str) -> str | None:
    """What a rate's denominator states, read whole: its words to the next function word or the next
    word that opens a rate (three words at most behind its count). The month where one of its words is the month and it counts no other
    number of them than one (per month, per calendar month, per 1 month, /mo); another cadence otherwise (per year, per 3 months, per user,
    per user-month); None where no word stands there (and/or)."""
    words = _denominator_words(phrase)
    if not any(words):
        return None
    counts = [float(n.group(0).replace(",", "")) if n else float(_NUM_WORDS[w]) for w in words for n in [re.match(_NUM, w)] if n or w in _NUM_WORDS]
    return "month" if all(c == 1 for c in counts) and any(_stem(w) in ("mo", "month") for w in words) else "other"


def _cadences(text: str, before: bool = False) -> list:
    """The cadences a text gives, each as the month or another: a rate's denominator (_denominator), the adverb of a calendar period, a count
    of times. before: the text stands before its amount, where per opens a rate whatever it counts (per user at USD 43), each and
    every open one only with a calendar period, the periods of the adverbs (every year at; with every feature at USD 43 is no rate), and a
    slash joins two names and opens none."""
    found = []
    for m in _CADENCE.finditer(text):
        if m.group("den") is None:
            found.append("month" if m.group("adv") == "monthly" else "other")
        elif not before or m.group(0) == "per" or (m.group(0) != "/" and any(re.fullmatch(_PERIODLY, _stem(w) + "ly") for w in _denominator_words(m.group("den")))):
            found.append(_denominator(m.group("den")))
    return [c for c in found if c]


def _words_before(head: str) -> str:
    """The amount's own words before it: back to its sentence's start, and where an earlier amount stands
    in the sentence, only what follows the first comma or conjunction behind that amount (the words directly behind an amount are that
    amount's: 17 USD yearly and 43 USD monthly)."""
    own = _SENTENCE_END.split(re.sub(r"\bu\.\s?s\.?", "us", head))[-1]
    earlier = None
    for earlier in _AMOUNT.finditer(own):
        pass
    if earlier is None:
        return own
    turn = _NEXT_PHRASE.search(own, earlier.end())
    return own[turn.end():] if turn else ""


def _monthly_price(low: str, start: int, end: int, known: set | None) -> bool:
    """Whether the amount that stands at low[start:end] (its currency mark included) is one the
    words give as a plan's monthly price, read from the amount's own words and never from the preposition before it. Its own words are the
    rest of its clause (to the next figure or the sentence's end) and the noun it measures. A cadence there decides: a rate's denominator
    (/, per, a, each, every, as _unit_from reads a claim's), a calendar period's adverb, a count of times; the amount is a monthly price where
    every cadence its words give is the month, and none where any is another (once, a year, per user, per thousand). Without a cadence the
    amount is a monthly price unless the words give it another role: a noun it directly modifies (43 USD deposit) or measures (a setup payment
    of 43 USD; a deposit: 43 USD) that the option's own sourced rows and the generic plan words do not use (known; None reads no role).
    The amount's own words are the words before it too (_words_before), so a cadence is read on either side of the
    amount (billed annually at USD 37 is no monthly price), and two amounts of one sentence keep their own."""
    clause = low[end:end + 40]
    stop = next((m.start() for m in re.finditer(_SENTENCE_END.pattern + r"|" + _NUM, clause) if not (m.group(0)[0].isdigit() and re.search(r"(?:/|per|each|every)\s*$", clause[:m.start()]))), len(clause))
    clause = clause[:stop]                                                     # the amount's clause ends at the sentence's end (_SENTENCE_END: the one end, as before the amount; a colon ends neither) or at the next figure, read whole, that is no denominator of its own rate (per 1,000 emails, per 1.0 month)
    following = _AMOUNT.search(low, end, end + stop + 12)
    if following and following.start() - end <= stop:                          # where that figure is another amount, the clause ends at the first comma or conjunction before it, and the rest is the other amount's
        turn = _NEXT_PHRASE.search(clause[:following.start() - end])
        clause = clause[:turn.start()] if turn else clause[:following.start() - end]
    head = low[max(0, start - 60):start]
    earlier = None
    for earlier in _AMOUNT.finditer(head):
        pass
    behind = head[earlier.end():] if earlier else head                         # the noun this amount measures stands behind an earlier amount, and is never that amount's own currency word (43 EUR, 29 USD)
    measured = re.search(r"((?:[a-z][a-z'-]*\s+){1,4})of\s+(?:about\s+|around\s+)?$", behind) or re.search(r"((?:[a-z][a-z'-]*\s+){0,3}[a-z][a-z'-]*)\s*[:,]\s*$", behind)   # a setup payment of 43 USD; a deposit: 43 USD
    of_words = []
    for w in reversed(words_of(measured.group(1)) if measured else []):
        if w in _CONNECTIVES:
            break
        of_words.append(w)
    cadences = _cadences(clause) + _cadences(_words_before(head), before=True)   # a cadence before the amount is the amount's too (billed annually at USD 37); a slash before an amount joins two names and is no rate
    direct = re.match(r"\s*an?\s(" + _DENOMINATOR + r")", clause)               # 43 USD a month, 43 USD a year: the article as a rate, directly after the amount alone
    if direct and _denominator(direct.group(1)):
        cadences.append(_denominator(direct.group(1)))
    if "monthly" in of_words:
        cadences.append("month")
    if re.search(r"\b" + _TIMES + r"[\s:,-]*(?:of\s+)?$", head):
        cadences.append("other")
    if cadences:
        return all(c == "month" for c in cadences)
    bound = list(of_words)
    for w in words_of(re.match(r"[a-z' -]*", clause).group(0))[:2]:              # the noun the amount directly modifies, with one word before it (43 USD deposit, 43 USD setup fee)
        if w in _CONNECTIVES:
            break
        bound.append(w)
    return known is None or all(_stem(w) in known for w in bound)


def plan_marks(text, known: set | None = None) -> set:
    """What a text states that tells one plan of a product from another: its monthly prices
    (a figure with a currency mark that the text's own words give as a monthly price, _monthly_price, as ("price", amount, currency): the
    currency of the amount's own mark, the code of currency_of or None for US dollars) and its
    quantities with their units (a figure directly before a word, as (figure, the word without a plural s): 2 vCPU, 4 GB, 512 MB),
    whatever preposition stands before either. A figure that is a rate's denominator (after per, /, each or every) belongs to a unit and is
    no mark."""
    low = str(text or "").lower().replace(" ", " ")
    out = set()
    for m in re.finditer(r"(?<![a-z0-9.,])" + _NUM + r"(?![0-9])", low):
        before = low[max(0, m.start() - 10):m.start()]; after = low[m.end():m.end() + 24]
        figure = float(m.group(1).replace(",", ""))
        sign = re.search(r"(?:\bu\.?s\.?\s*)?" + _SIGN + r"\s*$", before); word = re.match(r"\s*(?:u\.?s\.?\s*)?" + _AFTER + r"\b", after)
        if sign or word:
            if _monthly_price(low, m.start() - (len(sign.group(0)) if sign else 0), m.end() + (word.end() if word else 0), known):
                own = currency_of({"value": (before[max(0, len(before) - len(sign.group(0)) - 2):] if sign else "") + " " + (word.group(0) if word else "")})   # the amount's own mark, the two letters before a dollar sign with it (CA$)
                out.add(("price", round(figure, 2), own[0] if own else None))
            continue
        if re.search(r"(?:per|/|\beach|\bevery)\s*$", before):
            continue
        unit = re.match(r"[\s-]?([a-z]+)", after)
        if unit:
            out.add((figure, unit.group(1)[:-1] if len(unit.group(1)) > 2 and unit.group(1).endswith("s") else unit.group(1)))
    return out


def identified_rows(flats: list[dict], words: str) -> tuple[list[dict], set]:
    """The flat rows that the words naming a pick identify as its plan, with the marks that decide. A row carries a mark when its own
    monthly price is a price the words state in the row's currency (each amount of the words in the currency of its own mark) or its
    text states a quantity the words state. The rows that carry the
    most marks are the plan's when they state one monthly price; where no row carries a mark, or the rows that carry the most state different
    prices, the words identify no single plan and nothing is returned."""
    known = {_stem(w) for r in flats for f in ("_text", "_product") for w in words_of(r.get(f))} | {_stem(w) for w in GENERIC_PLAN_WORDS | TIER_WORDS}
    marks = plan_marks(words, known)
    if not marks or len(flats) < 2:
        return [], set()
    def carried(r):
        own = plan_marks(r.get("_text"))
        price = ("price", round(float(r.get("exact") or 0), 2), r.get("currency") or None)
        return {m for m in marks if ((m == price and r.get("unit") == "flat_month") if m[0] == "price" else m in own)}
    scored = [(r, carried(r)) for r in flats]
    top = max(len(c) for _, c in scored)
    best = [(r, c) for r, c in scored if len(c) == top]
    if not top or len(best) == len(flats) or len({round(float(r.get("exact") or 0), 2) for r, _ in best}) != 1:
        return [], set()
    return [r for r, _ in best], set().union(*(c for _, c in best))


def select_rows(rows: list[dict], provider: str, usage: dict | None = None) -> list[dict]:
    """The rows of one pick: one flat row, the claim whose words name the pick's plan (the
    label's own words beyond the vendor: "basic", "hobby", "starter"), the lowest fee among those; the usage rows that name the plan too;
    without a flat row, the lowest usage row per unit. The plan prices of a pick are never summed: the sum of every tier is not the price of one plan."""
    if not rows:
        return rows
    vendor = (provider or "").split()[0].lower() if provider else ""
    product_words = {w for r in rows for w in words_of(r.get("_product"))}        # the product's own name (its recorded id) names no plan: "pdf generator" in a starting price's sentence is not the Low Usage plan
    plan_words = {w for w in re.findall(r"[a-z0-9.]+", (provider or "").lower()) if len(w) > 2 and w not in GENERIC_PLAN_WORDS and w != vendor and w not in product_words}
    def names(r):
        text = (r.get("_text") or "").lower()
        return sum(1 for w in plan_words if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text))
    def named(r):
        text = (r.get("_text") or "").lower()
        return {w for w in plan_words if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text)}
    def other_tier(r):                                                          # a row about a competing tier of the same vendor (Pro when the pick is Hobby)
        text = (r.get("_text") or "").lower()
        return not named(r) and any(re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text) for w in TIER_WORDS - plan_words)
    credit = [r for r in rows if isinstance(r.get("billing"), dict) and r["billing"].get("model") == "credit_pack"]
    if credit:                                                                  # the pick's documented billing model is the credit pack: the one credit row of its unit family stands; the vendor's monthly tiers (a flat fee for N emails) and the bare consumption readings of the same tariff stand down, so the figure follows one billing model
        best_c = min(credit, key=lambda r: float(r.get("exact") or 0))
        fam = _UNIT_FAMILY.get(best_c.get("unit"))
        rows = [r for r in rows if r is best_c or (r.get("unit") not in ("flat_month", "free_up_to") and _UNIT_FAMILY.get(r.get("unit")) != fam)]
    flats = [r for r in rows if r.get("unit") in ("flat_month", "free_up_to")]
    others = [r for r in rows if r not in flats]
    out = []
    if flats:
        cur0 = BUDGET_CURRENCY if any(not r.get("currency") for r in flats) else flats[0].get("currency")
        pool = [r for r in flats if (r.get("currency") or BUDGET_CURRENCY) == cur0]   # one currency in a range (the budget's when the vendor is priced in it)
        standing = lambda among: max(among, key=lambda r: (names(r), 1 if r.get("included") else 0, -float(r.get("exact") or 0)))
        equal = named(standing(flats))                                           # from generation 7 of the document rules on: among the rows that name the same plan words as the row the function `standing` picks, the rows of the plan that the pick's own words identify by its monthly price or by its quantities with their units stand before the lowest fee; a version of an earlier generation keeps its selection
        plan = identified_rows([r for r in flats if named(r) == equal], provider or "")[0] if docrules.generation() >= 7 else []
        if any(names(r) for r in flats) or plan or len({round(float(r.get("exact") or 0), 2) for r in pool}) < 2:
            best = standing(plan or flats)   # the flat row whose words name the option's plan stands before a starting price and a free tier that name no plan; one sourced price stands as it is
        else:                                                                    # no row names a plan of the option and the vendor's sourced plan prices differ: never the cheapest in silence; the range is stated and the highest is the figure
            top = max(pool, key=lambda r: float(r.get("exact") or 0))
            plans = sorted(({"amount": round(float(r.get("exact") or 0), 2), "price": r.get("unit_price") or "", "claim": r["claims"][0]} for r in pool), key=lambda x: (x["amount"], x["claim"]))
            best = dict(top, claims=[top["claims"][0]] + [x["claim"] for x in plans if x["claim"] != top["claims"][0]], range={"low": plans[0]["amount"], "high": plans[-1]["amount"], "currency": cur0, "plans": plans},
                        usage=f"the highest of {len(plans)} sourced plan prices, {plans[0]['amount']:.2f} to {plans[-1]['amount']:.2f} {cur0} a month (the option names no plan)", label=LABEL_FLAT)
            flats = [best if r is top else r for r in flats]
        out.append(best)
        seen = set(named(best))
        for r in sorted(flats, key=lambda r: float(r.get("exact") or 0)):        # a second component of the pick: a flat fee naming a plan word the first does not ("512 MB" beside "Hobby")
            extra = named(r) - seen
            if r is not best and extra and r.get("unit") == "flat_month" and not best.get("range"):
                out.append(r); seen |= extra
        by_unit: dict = {}
        for r in others:                                                        # the usage rows: one per unit, the lowest; never a competing tier's; none within the plan's allowance
            if other_tier(r):
                continue
            inc = best.get("included"); key = USAGE_KEY.get(r.get("unit"))
            if inc and usage and key and best.get("included_unit") == r.get("unit") and float(usage.get(key) or 0) <= float(inc):
                continue                                                         # within the plan's allowance in this unit (a money credit is not an allowance in emails)
            if r["unit"] not in by_unit or float(r.get("exact") or 0) < float(by_unit[r["unit"]].get("exact") or 0):
                by_unit[r["unit"]] = r
        out += list(by_unit.values())
    else:
        by_unit: dict = {}
        for r in others:
            if r["unit"] not in by_unit or float(r.get("exact") or 0) < float(by_unit[r["unit"]].get("exact") or 0):
                by_unit[r["unit"]] = r
        out = list(by_unit.values())
    return out


OTHER_CURRENCY = "the price is in {words} ({amount:.2f} {code} a month), the budget in US dollars; converted at the billing date"


def option_rows(item: str, provider: str, claims: list[dict], usage: dict, names: str | None = None) -> list[dict]:
    """The one selection of an option's price rows, used by every figure a founder reads (the budget sentence of option_judgement, the plan
    budget's rows of budget_of, the option's monthly cost of app/core/editor.py price_trace, the document model's): each claim's row (row_for),
    the rows of what the option names (select_rows), the plan, overage and add-on rules (applicable_rows). A row priced
    in a currency other than the budget's keeps its own amount and carries no US dollar figure: monthly_usd None, the reason in the
    founder's words. names: the words the selection reads the option's plan from when they are more than the printed label (the recommendation's
    own words beside the candidate's label), the same string wherever the option is priced."""
    by_id = {c["id"]: c for c in claims or []}
    rows = applicable_rows(select_rows([r for r in (row_for(item, provider, c, usage) for c in claims or []) if r], names or provider, usage), usage, by_id)
    for r in rows:
        if r.get("currency"):
            r["native_usage"] = r["usage"]
            r["usage"] = "not computable: " + OTHER_CURRENCY.format(words=r["currency_words"], amount=float(r["monthly_amount"]), code=r["currency"]); r["label"] = LABEL_UNPRICED; r["exact"] = 0.0
    return rows


def output_tariffs(item: str, claims: list[dict], output_recorded: bool = False) -> list[dict]:
    """The claims of a token-priced item that state an obligation the rules cannot compute: an output-token tariff (no
    recorded quantity to apply it to), or a compound tariff the rules cannot resolve to one unit price (an output rate among its prices),
    kept as an unpriced obligation of its pick even beside a priced component; the pick is then not computable and yields no delta."""
    if not ((units_for(item) or set()) & TOKEN_UNITS):
        return []
    return [c for c in claims or [] if ((price_of(c) or {}).get("unit") == "million_tokens_out" and not output_recorded) or is_compound(c)]   # with the summary length recorded an output rate is priced; a compound tariff stays unpriced


OUTPUT_NOT_STATED_ITEMS = "the founder's answer does not quantify " + QUANTITY_WORDS["tokens_out"] + ", so an output-token tariff cannot be applied"   # the same reason in the wording for an items record


def unpriced_why(claims: list[dict], output_recorded: bool = False, usage: dict | None = None) -> str:
    """The reason of the not-computable row of output_tariffs' claims: the unrecorded output quantity when a claim states an output rate
    (alone or inside a compound tariff), and the compound tariff that is not one unit price when a claim is compound."""
    not_recorded = OUTPUT_NOT_STATED_ITEMS if (usage or {}).get("mode") == "items" else OUTPUT_NOT_RECORDED
    out = not output_recorded and any((price_of(c) or {}).get("unit") == "million_tokens_out" or re.search(r"\b(?:output|completion)\b", str(c.get("value") or "").lower()) for c in claims)
    comp = any(is_compound(c) for c in claims)
    return "; ".join(w for w, on in ((not_recorded, out), (COMPOUND_NOT_ONE_PRICE, comp)) if on) or not_recorded


def budget_of(numbers: dict, picks: list[dict], ceiling_usd: float | None) -> dict:
    """picks: [{item, provider, claims: [price claims of the pick], runner_up: {provider, claims} | None}]. Returns the budget block of
    the plan: computable, rows, total_usd, ceiling_usd, worst_case (change, delta_usd, claims), worst_case_total_usd, note, assumptions.
    An output-token tariff without a recorded quantity gets a not-computable row naming the unrecorded quantity; a pick whose own cost is not fully
    computable yields no runner-up delta (an unknown baseline is never taken as zero), and neither does a runner-up whose cost is not."""
    usage = usage_from_numbers(numbers)
    rec_out = usage.get("tokens_out") is not None                                       # whether the summary length is a recorded quantity
    rows, worst = [], []
    for pk in picks:
        if pk.get("missing"):                                                           # a price the pick's budget judgement rested on is no longer among its supported prices: no second figure is computed
            rows.append(unpriced_row(pk, "a price the budget judgement of this option rested on is no longer supported (" + ", ".join(pk["missing"]) + ")", pk.get("claims") or [])); continue
        every = option_rows(pk["item"], pk["provider"], pk.get("claims") or [], usage, pk.get("names"))   # the one selection of the option's rows, with the plan, the overage and the add-on rules
        mine = [r for r in every if not r.get("currency")]; foreign = [r for r in every if r.get("currency")]
        rows.extend(every)
        outs = output_tariffs(pk["item"], pk.get("claims") or [], rec_out)
        if outs and (mine or not all(is_compound(c) for c in outs)):
            rows.append(unpriced_row(pk, unpriced_why(outs, rec_out, usage), outs))               # the output tariff is on record, its quantity is not; a compound tariff stays an unpriced obligation beside a priced component
        elif not mine and (units_for(pk["item"]) or set()) & TOKEN_UNITS:
            rows.append(unpriced_row(pk))                                            # a token-priced pick without a tariff the rules read (a compound tariff alone among its claims included) says so and lists the claims seen, so its row is never silently missing
        used_ids = {c for r in every for c in r["claims"]} | {c["id"] for c in outs}
        allowed_pk = units_for(pk["item"]) or set()
        unquantified = [c for c in pk.get("claims") or [] if c["id"] not in used_ids and (price_of(c) or {}).get("unit") in USAGE_KEY and (price_of(c) or {}).get("unit") in allowed_pk and usage.get(USAGE_KEY[(price_of(c) or {}).get("unit")]) is None]
        if unquantified:
            rows.append(unpriced_row(pk, unpriced_reason(pk, usage, unquantified), unquantified))   # a tariff in a unit the founder's answer does not quantify (an items record without audio hours or input tokens) stays visibly unpriced with that reason
        elif not every and not outs and allowed_pk and not (allowed_pk & TOKEN_UNITS) and pk.get("claims") and all(price_of(c) is None or (billing_model(c) or {}).get("model") == "unreadable" for c in pk["claims"]):   # a claim whose credit tariff cannot be read prices nothing whatever unit its value states
            rows.append(unpriced_row(pk, unpriced_reason(pk, usage, pk["claims"]), pk["claims"]))   # a part whose claims hold no recognised unit, or a credit pack that cannot be read, is visibly unpriced with the reason (never silently absent); a part whose readable rows the plan, overage and add-on rules dropped keeps its notes and no row
        pick_priced = bool(mine) and not foreign and not outs and not unquantified and not (not mine and (units_for(pk["item"]) or set()) & TOKEN_UNITS)
        pick_total = sum(r["exact"] for r in mine)
        ru = pk.get("runner_up")
        if ru and ru.get("claims") and pick_priced and not output_tariffs(pk["item"], ru["claims"], rec_out):
            theirs = option_rows(pk["item"], ru["provider"], ru["claims"], usage)
            if theirs and not any(r.get("currency") for r in theirs):
                delta = round(sum(r["exact"] for r in theirs) - pick_total, 2)
                if delta > 0:
                    worst.append({"change": f"{ru['provider']} instead of {pk['provider']}", "delta_usd": delta, "claims": sorted({c for r in theirs for c in r["claims"]}),
                                  **({"upper_bound": True} if docrules.generation() >= 3 and any(r.get("range") for r in theirs) else {}),   # a delta that rests on the highest of an option's sourced plan prices is an upper bound, marked as one from generation 3 on; the figure is unchanged
                                  **({"from_upper_bound": True} if docrules.generation() >= 4 and any(r.get("range") for r in mine) else {})})   # a delta counted from a pick priced at the highest of its plans says so, from generation 4 on; the figure is unchanged
        for r in mine:
            if r["unit"] == "flat_month" and r.get("included"):
                worst.append({"change": f"{pk['provider']} usage at twice the included amount", "delta_usd": round(r["monthly_usd"], 2), "claims": list(r["claims"]), **({"upper_bound": True} if docrules.generation() >= 4 and r.get("range") else {})})   # the pick's own highest plan price, doubled, is an upper bound too (marked from generation 4 on)
    total = round(sum(r["exact"] for r in rows if r.get("monthly_usd") is not None), 2)   # the total from the unrounded costs of the priced rows, rounded once, so that it equals the sum of the unrounded tariffs; an unpriced row adds nothing; a renderer prints this figure, not its own sum of the printed rows (which can differ by a cent)
    unpriced = [r["item"] for r in rows if r.get("monthly_usd") is None]
    waits = [pk["item"] for pk in picks if not any(r["item"] == pk["item"] for r in rows) or pk["item"] in unpriced]   # the whole-stack worst case is decided here, once, and waits for every pick: a pick without a row, or with a row that carries no figure, leaves it unstated; the deltas of the priced picks stay, each beside its own change
    apart = [f"{r['provider']}: {float(r['monthly_amount']):.2f} {r['currency']} a month" for r in rows if r.get("currency")]   # an amount in another currency is stated apart and the total excludes it
    for r in rows:
        for k in ("unit", "included", "included_unit", "exact", "_text", "_product", "_starting", "currency_words", "native_usage"):
            r.pop(k, None)
    if usage.get("mode") == "items":                                                 # the note states the founder's figures in the founder's own units
        per_user_words = f" per user ({usage['items']:g} in all)" if usage.get("scope") == "per_user" else " in total"
        parts = [f"{usage['items_stated']:g} {usage['item_word']} a month{per_user_words}"] + [f"{usage['illustrations']:g} illustration attempts" for _ in [0] if usage.get("illustrations") is not None] \
            + [f"{usage['revisions']:g} revisions" for _ in [0] if usage.get("revisions")] + [f"{usage['users']:g} users" for _ in [0] if usage.get("users")] \
            + [f"{float(usage['words_per_item']):g} words a {usage['item']}" for _ in [0] if usage.get("words_per_item")] + [f"{float(usage['tokens_in_per_item']):g} tokens in a {usage['item']}" for _ in [0] if usage.get("tokens_in_per_item")] \
            + [f"{float(usage['tokens_out_per_item']):g} tokens out a {usage['item']}" for _ in [0] if usage.get("tokens_out_per_item")] \
            + [f"{float(usage.get('searches_per_item') or 0):g} searches and {float(usage.get('page_reads_per_item') or 0):g} page reads a {usage['item']}" for _ in [0] if usage.get("searches") is not None] \
            + [f"{usage['storage_gb_month']:g} GB of storage" for _ in [0] if usage.get("storage_gb_month") is not None]
        note = "Prices are the sourced facts named beside each row; the founder's figures are assumptions and every derived quantity a calculated estimate with its formula (" + ", ".join(parts) + ")."
    else:
        per_user_words = f" per user ({usage['meetings']:g} in all)" if usage.get("scope") == "per_user" else ""   # the note says when the figure was multiplied
        note = f"Prices are the sourced facts named beside each row; usage figures are calculated estimates from the founder's stated volume ({numbers.get('meetings_per_month')} meetings{per_user_words} of {numbers.get('minutes_per_meeting')} minutes, {numbers.get('users')} users, {usage['recipients']:g} recipients a summary)."
    if apart:
        note += " The total is in US dollars and excludes, each in its own currency (converted at the billing date): " + "; ".join(apart) + "."
    return {"computable": bool(rows) and not unpriced, "rows": rows, "total_usd": total, **({"other_currency": apart} if apart else {}), "ceiling_usd": ceiling_usd, "worst_case": worst, "worst_case_total_usd": None if waits else round(total + sum(w["delta_usd"] for w in worst), 2), "unpriced": unpriced,
            "within_ceiling": (ceiling_usd is None) or total <= float(ceiling_usd) + 1e-9, "assumptions": usage["assumptions"], "note": note}


# ---------------------------------------------------------------- a hard budget is judged by calculation
CALCULATED = "By calculation: "                                                    # the note of a hard-requirement verdict code computed (app/core/coverage_step.py record); a row that opens with it was never asked of a model
WITHIN, OVER, NOT_COMPUTABLE = "within", "over", "not_computable"


def option_judgement(item: str, label: str, product_version: str, claims: list[dict], usage, ceiling_usd: float | None, unsettled: str | None = None, names: str | None = None) -> dict:
    """One option's monthly cost at the founder's usage against the whole budget figure, by the plan budget's own rules (budget_of over
    this option alone: the sourced prices among claims, the usage record). state: within (every tariff of the option priced, the rows together
    at or under the ceiling), over (the least the priced rows can make already exceeds the ceiling: the usage tariffs together, or the largest
    flat fee alone; a figure above the budget only when a flat fee and a usage tariff are added is not a proven violation), not_computable
    (no usage record, no price claim, a tariff the rules cannot price or a quantity the founder did not state; the reason is the budget row's
    own). claim_ids: the price claims the figure rests on; parts: one line per priced row (the usage figure, the unit price, the monthly cost
    and its claim); line: the calculation in words. No model reads or writes any of it."""
    numbers = numbers_of(usage)
    base = {"state": NOT_COMPUTABLE, "monthly_usd": None, "ceiling_usd": ceiling_usd, "claim_ids": [], "parts": [], "reason": "", "line": "", "option": label}
    if ceiling_usd is None:
        return dict(base, reason="no budget figure is recorded")
    if numbers is None:
        return _judged(base, "no usage record the calculation reads is on file (the monthly volume, in meetings or in items)")
    if not claims:
        return _judged(base, unsettled or "no current, supported, in-scope price of this option is on record")   # unsettled: the option has a price claim that does not settle a figure alone (a volatile price read from a copy not confirmed live), with the rule's own reason
    block = budget_of(numbers, [{"item": item, "provider": label, "product_version": product_version or "", "claims": list(claims), "runner_up": None, "names": names}], ceiling_usd)
    priced = [r for r in block["rows"] if r.get("monthly_usd") is not None]
    unpriced = [r for r in block["rows"] if r.get("monthly_usd") is None]
    parts = [_part(r) for r in priced]
    ranged = next((r["range"] for r in priced if r.get("range")), None)
    ids = sorted({c for r in priced for c in r["claims"]})
    total = float(block["total_usd"])
    out = dict(base, monthly_usd=total if priced else None, claim_ids=ids, parts=parts, input_claim_ids=[c["id"] for c in claims], names=names, **({"range": ranged} if ranged else {}))
    flat = [float(ranged["low"]) if r.get("range") else float(r["monthly_usd"]) for r in priced if r.get("label") == LABEL_FLAT]; usage_sum = sum(float(r["monthly_usd"]) for r in priced if r.get("label") != LABEL_FLAT)
    least = max([usage_sum] + flat) if priced else 0.0                                 # the least the option can cost on these rows: its usage tariffs together (one per unit, each applies), or its largest flat fee alone; a flat fee and a usage tariff of one option are not always both owed (a plan that includes the usage, a stated monthly cost beside its own tariff)
    if priced and least > float(ceiling_usd) + 1e-9:
        return _judged(dict(out, state=OVER), "")                                      # over on every reading of the rows: a proven violation
    if priced and not unpriced and total <= float(ceiling_usd) + 1e-9:
        return _judged(dict(out, state=WITHIN), "")                                    # within on every reading (the rows together)
    if priced and not unpriced and ranged:
        return _judged(out, f"the option names no plan and the sourced plan prices run from {float(ranged['low']):.2f} to {float(ranged['high']):.2f} USD a month; at the highest the monthly cost is {total:.2f} USD, over the budget; which plan is taken is not established")
    if priced and not unpriced:
        return _judged(out, f"the flat fee and the usage tariffs on record together make {total:.2f} USD a month, over the budget, while the least they can make is {least:.2f} USD, within it; which of them are owed together is not established")
    why = "; ".join(dict.fromkeys(str(r["usage"]).replace("not computable: ", "") for r in unpriced)) or unpriced_reason({"item": item}, usage_from_numbers(numbers), list(claims))
    return _judged(out, why)


def _part(r: dict) -> str:
    """One priced row in the calculation's words; a range row names every sourced plan price with its claim."""
    if r.get("range"):
        return ("the option names no plan, so the sourced plan prices stand as a range: " + ", ".join(f"{x['amount']:.2f} USD ({x['price']}, {x['claim']})" for x in r["range"]["plans"])
                + f"; the highest, {float(r['monthly_usd']):.2f} USD, is counted")
    return f"{r['usage']} at {r['unit_price']} = {float(r['monthly_usd']):.2f} USD ({', '.join(r['claims'])})"


def _judged(j: dict, reason: str) -> dict:
    """The judgement with its sentence: the figure and the ceiling, the rows behind it, or the reason no figure stands."""
    ceiling = f"{float(j['ceiling_usd']):g} USD a month"
    shown = "; ".join(j["parts"])
    if j["state"] == WITHIN and j.get("range"):
        line = (f"{j['option']} costs between {float(j['range']['low']):.2f} and {float(j['range']['high']):.2f} USD a month for its plan" + (f", {j['monthly_usd']:.2f} USD a month in all at the highest plan and your usage" if abs(float(j['monthly_usd']) - float(j['range']['high'])) > 0.004 else "")
                + f", within the budget of {ceiling} for all services whatever plan is taken. The calculation: {shown}.")   # judged at the highest sourced plan price
    elif j["state"] == WITHIN:
        line = f"{j['option']} costs about {j['monthly_usd']:.2f} USD a month at your usage, within the budget of {ceiling} for all services. The calculation: {shown}."
    elif j["state"] == OVER:
        line = f"{j['option']} costs about {j['monthly_usd']:.2f} USD a month at your usage, above the budget of {ceiling} for all services. The calculation: {shown}."
    else:
        line = f"The monthly cost of {j['option']} at your usage is not established: {reason}" + (f" (priced so far: {shown})" if shown else "") + "."
    return dict(j, reason=reason, line=line)
