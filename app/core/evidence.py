"""The evidence rules as code: the four badges a claim can show (Supported by source, Supported with conditions, Conflicting,
Not supported yet), the conflict rule (both sources kept, the newer official source preferred, the conflict exposed under
Unresolved checks, no silent choice) and the deterministic quote checks used for scoring. Nothing here sends anything; the
verify node and compare_options (app/graph/workflow.py) call these after the model's verdicts are stored. Claims and sources
arrive as plain dicts (rows), so the module is testable without a run."""
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from urllib.parse import urlsplit
from app.core.sqlguard import require_guarded

SUPPORTED_BY_SOURCE = "Supported by source"
SUPPORTED_WITH_CONDITIONS = "Supported with conditions"
CONFLICTING = "Conflicting"
NOT_SUPPORTED_YET = "Not supported yet"
BADGES = (SUPPORTED_BY_SOURCE, SUPPORTED_WITH_CONDITIONS, CONFLICTING, NOT_SUPPORTED_YET)
VOLATILE_KINDS = ("price", "availability")          # facts whose source must be live (freshness class volatile) to settle anything alone


def badge(status: str, scope_status: str | None = None, conditions: list | None = None, tier: int | None = None) -> str:
    """The badge a claim shows. Supported by source needs a supported verdict, a verified in-scope status,
    no stated condition and a tier 1 or 2 source; a supported claim with conditions, a conditional scope or a secondary
    (tier 3) source is Supported with conditions; contradicted or conflicting verdicts are Conflicting; everything else
    (unverified, partial, insufficient, or supported but out of the claim's scope) is Not supported yet and cannot drive a
    hard-constraint decision."""
    if status in ("contradicted", "conflicting"):
        return CONFLICTING
    if status != "supported" or scope_status in (None, "unchecked", "out_of_scope"):
        return NOT_SUPPORTED_YET
    if conditions or scope_status == "conditional" or (tier is not None and int(tier) >= 3):
        return SUPPORTED_WITH_CONDITIONS
    return SUPPORTED_BY_SOURCE


def eligibility(verdict: str | None, scope_status: str | None, source_current: bool, requirement_current: bool) -> tuple[bool, str]:
    """The eligibility rule of the reconciliation: a claim takes part in support and in conflict detection only when
    the verifier's verdict is supported (with or without conditions), its source version is the current one and its
    verdict was given against the current requirement version, and it is in scope. The reason names what fails."""
    if verdict != "supported":
        return False, f"verdict {verdict or 'unverified'}"
    if scope_status not in ("in_scope", "conditional"):
        return False, f"scope {scope_status or 'unchecked'}"
    if not source_current:
        return False, "quote taken from a superseded source version"
    if not requirement_current:
        return False, "verdict not given against the current requirement version"
    return True, ""


def volatile_claim(kind: str | None) -> bool:
    """Volatility is a property of the claim (its kind), never of the source's freshness class."""
    return kind in VOLATILE_KINDS


NOT_CONFIRMED_LIVE = "volatile figure from a copy not confirmed live; re-fetch to settle"


def settles_alone(kind: str | None, cache_status: str | None, published_at=None, tier=None) -> tuple[bool, str]:
    """The one claim-kind rule, used by the reconciliation, the freshness events and the badges alike: a price or
    availability figure settles a hard requirement alone only when its source copy is confirmed live by the contents
    response status (cache_status livecrawl) and the page is dated or official (tier 1 or 2); cached, cache-fallback,
    unknown or undated secondary copies leave the decision unresolved with the reason. The source's freshness class and
    maxAgeHours play no part here; they keep applying to non-volatile facts through fresh_until."""
    if not volatile_claim(kind):
        return True, ""
    if (cache_status or "unknown") != "livecrawl":
        return False, NOT_CONFIRMED_LIVE
    if not published_at and int(tier or 3) >= 3:
        return False, NOT_CONFIRMED_LIVE
    return True, ""


def can_drive_hard_constraint(status: str, scope_status: str | None) -> bool:
    """The hard-constraint rule: only a supported verdict with a verified scope (in scope, or conditional with the
    conditions shown) may support a hard requirement; conditions outside the requirement are the verifier's job."""
    return status == "supported" and scope_status in ("in_scope", "conditional")


# ---------------------------------------------------------------- deterministic quote checks
def _norm(s: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9$€£%.:/-]+", (s or "").lower())).strip(" .")


def quote_in_text(quote: str | None, text: str) -> bool:
    """The exact-quote rule: the quote is a verbatim substring of the stored text (no normalisation; the extractor rejects the rest)."""
    q = (quote or "").strip()
    return bool(q) and q in (text or "")


def figures_of(value: str | None) -> list[str]:
    """Every token of a value that carries a digit ($0.36, 2.4, 500, 90-day), as the quote must carry it."""
    return [t for t in _norm(value).split() if any(ch.isdigit() for ch in t)]


def quote_carries_figures(value: str | None, quote: str | None) -> bool:
    """A quote supports a value with figures only when every figure of the value appears in the quote; a value without
    figures passes (the verifier judges the words). Used for scoring and the badge explanations, never as an
    extraction rejection (extraction keeps such claims and verification withholds support)."""
    q = _norm(quote)
    return all(f in q.split() or f in q for f in figures_of(value))


# ---------------------------------------------------------------- the language rule
SCRIPT_NAMES = {"he": "\u05e2\u05d1\u05e8\u05d9\u05ea", "en": "\u05d0\u05e0\u05d2\u05dc\u05d9\u05ea", "ar": "\u05e2\u05e8\u05d1\u05d9\u05ea"}   # the Hebrew-script names of Hebrew, English and Arabic, as a Hebrew page writes them


def unnamed_languages(quote: str | None, requirement: dict) -> list[str]:
    """The languages a requirement records (applicability.languages_of) that the quote itself does not name; [] when it
    names each, or the requirement records none. A language is named by its name, through the one tokenisation (budget.words_of), by its name in
    Hebrew script after at most three prefix letters, or by a locale code (he-IL, en_US; a bare "he" is a pronoun). The claim's text and the
    verifier's record do not count."""
    from app.core import applicability as AP
    from app.core.budget import words_of
    words, names = set(words_of(quote)), {code: name for name, code in AP.LANGUAGES.items()}

    def named(code: str) -> bool:
        native = SCRIPT_NAMES.get(code)
        return (names[code] in words or (bool(native) and any(w.endswith(native) and len(w) - len(native) <= 3 for w in words))
                or bool(re.search(rf"(?<![A-Za-z]){code}[-_][A-Z]{{2}}(?![A-Za-z])", quote or "")))
    return [names[c].capitalize() for c in AP.languages_of(requirement) if not named(c)]


_TERM_STOP = {"at", "of", "the", "and", "or", "for", "in", "on", "to", "a", "an", "is", "are", "be", "must", "with", "by", "per", "its", "all"}


def requirement_terms(requirement: dict) -> list[str]:
    """The terms a requirement's key names (the words of the key, the stop words left out): encryption_at_rest names
    encryption and rest; [] for a key of stop words alone."""
    return [w for w in re.findall(r"[a-z]+", str(requirement.get("key") or "").lower().replace("_", " ")) if w not in _TERM_STOP]


def _light_stem(word: str) -> str:
    w = word.lower()
    for suffix in ("ing", "ies", "es", "ed", "er", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return w


def _term_named(term: str, words: set[str]) -> bool:
    st = _light_stem(term)
    for w in words:
        sw = _light_stem(w)
        if term == w or st == sw:
            return True
        short, long_ = sorted((st, sw), key=len)
        if len(short) >= 5 and long_.startswith(short):
            return True
    return False


_ACRONYM = re.compile(r"(?<![A-Za-z])[A-Z]{2,}(?![A-Za-z])")


def acronym_terms(requirement: dict) -> set[str]:
    """The terms the requirement itself writes in capitals (TLS of 'connections use TLS 1.2 or higher in transit'), lowercased; an acronym of
    the source names a term only when it is one of these."""
    return {w.lower() for w in _ACRONYM.findall(str(requirement.get("value") or ""))}


def states_terms(text: str, terms: list[str], acronyms: set[str] | frozenset[str] = frozenset()) -> bool:
    """Whether the terms are stated in context: every term is named (the light stem of _term_named) within one window of
    len(terms) + 2 consecutive words of the text, its stop words left out ('encrypted at rest', 'encryption of data at rest'); 'The REST API uses
    AES-256 encryption for connections in transit' names rest and encryption five words apart and states no encryption at rest.
    A word the source writes in capitals (REST, API, TLS) is an acronym and names a term only when the requirement writes the term
    in capitals itself (acronyms); a text written wholly in capitals is read as words."""
    raw = re.findall(r"[A-Za-z0-9]+", (text or "").replace("_", " "))
    shouted = bool(raw) and all(_ACRONYM.fullmatch(w) for w in raw if w.isalpha())
    words = [w.lower() for w in raw if w.lower() not in _TERM_STOP and (shouted or not _ACRONYM.fullmatch(w) or w.lower() in acronyms)]
    span = len(terms) + 2
    return any(all(_term_named(t, set(words[i:i + span])) for t in terms) for i in range(max(1, len(words) - span + 1)))


def applies_to_requirement(claim: dict, requirement: dict) -> bool:
    """Whether a claim's own evidence is about the requirement it is recorded to support (a shared term such as AES-256 alone establishes no broader
    coverage): a budget requirement takes any claim (its support is the price arithmetic),
    a language requirement keeps its own rule (the quote names the languages: unnamed_languages, read by the reconciliation beside this), and any
    other requirement is stated when every term of its key (requirement_terms) is named within one short window of the claim's attribute or of its quote
    (states_terms: the source's own words in context; the light stem joins encryption with encrypted, the exact word rules a short term such as rest;
    the two fields are read apart; the source's acronym REST is not the word rest unless the requirement writes the term in capitals, acronym_terms). A requirement whose key names no term applies."""
    from app.core import applicability as AP
    if AP.is_budget(requirement) or AP.languages_of(requirement):
        return True
    terms = requirement_terms(requirement)
    if not terms:
        return True
    acr = acronym_terms(requirement)
    return states_terms(str(claim.get("attribute") or ""), terms, acr) or states_terms(str(claim.get("quote") or ""), terms, acr)


# ---------------------------------------------------------------- conflicts (normalised values, tier-aware keys, equal values agree)
TIER_WORDS = ("free", "hobby", "starter", "standard", "pro", "professional", "team", "business", "enterprise", "scale", "basic", "premium", "flex", "priority", "batch", "infrequent access",
              "cache read", "cache write", "fast tier", "pay as you go", "streaming", "pre-recorded", "prerecorded", "legacy", "developer", "individual", "organization", "organisation", "essentials")   # essentials: for example Google's Essentials SKUs
_SIZE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(mb|gb|tb|cpu|vcpu|minute|minutes|hour|hours|k tokens|k)\b")
ZERO_WORDS = ("free", "zero", "no charge", "at no cost", "zero-cost")
GENERIC_WORDS = {"plan", "plans", "tier", "tiers", "api", "service", "services", "pricing", "price", "the", "a", "an", "and", "of", "for", "per", "month", "monthly", "compute"}


_COMMA_GROUP = re.compile(r"(?<![\w.,])[1-9]\d{0,2}(?:,\d{3})+(?!,?\d)")
_SPACE_GROUP = re.compile(r"(?<![\w.,])[1-9]\d{0,2}(?:[ \u00a0\u2009\u202f]\d{3})+(?!\d)")
_TOKENS_FIRST = re.compile(r"\b(?:(input|output)\s+)?tok(?:en)?s?\s+per\s*(1\s*m|1\s*k|1000000|1000|million|thousand)\b")
_TOKEN_RATE = re.compile(r"(?:\b(input|output)\s+)?\bper\s*(?:(1\s*m|m|million|mtok|1000000)\b|(1\s*k|1000|thousand)\b(?=\s*(?:(?:input|output)\s+)?tok))\s*(input|output)?\s*(?:tok(?:en)?s?\b)?\s*\(?(input|output)?\)?")


def ungrouped(text: str) -> str:
    """Each whole, well-formed grouped number as plain digits: one to three digits, then groups of exactly three under one separator
    (a comma, a space, a non-breaking or a thin space). Numbers that do not form such a token stay apart (10 20; 1,000,00; 1000 000; 1,000 2,000 is two)."""
    return _SPACE_GROUP.sub(lambda m: re.sub(r"\D", "", m.group(0)), _COMMA_GROUP.sub(lambda m: m.group(0).replace(",", ""), text))


def _token_rate(m) -> str:
    """A token rate in canonical text: one token unit, its direction with it once, wherever the direction was written."""
    way = m.group(1) or m.group(4) or m.group(5) or ""
    return f" per {'million' if m.group(2) else '1000'} {way} tokens "


def norm_value(value) -> str:
    """A price or figure in canonical text: amounts as plain numbers (0.800 -> 0.8; 15 cents -> 0.15; 3,000 -> 3000), the per-million forms
    alike (per 1M tokens, per million tokens, /1m token -> per million tokens), per-1000 forms alike, currency words dropped, free or zero
    without another figure -> 0, white space collapsed; a value without a figure is its words lower-cased and sorted (a list in another order
    is the same list)."""
    s = ungrouped(str(value or "").lower().replace(" ", " "))
    s = re.sub(r"\b(\d+(?:\.\d+)?)\s*cents?\b", lambda m: f"{float(m.group(1)) / 100:g}", s)
    s = s.replace("$", " ").replace("usd", " ").replace("dollars", " ").replace("dollar", " ")
    s = re.sub(r"\s*/\s*", " per ", s)
    s = _TOKENS_FIRST.sub(lambda m: f" per {m.group(2)} {m.group(1) or ''} tokens ", s)
    s = _TOKEN_RATE.sub(_token_rate, s)
    s = re.sub(r"\bper\s*1000\b", " per 1000 ", s)
    s = re.sub(r"\bper\s*(?:mo|month)\b", " per month ", s)
    s = re.sub(r"\b(?:a|each)\s+month\b", " per month ", s)
    s = re.sub(r"\bgb[- ]month\b", " gb month ", s)
    s = re.sub(r"[^a-z0-9.% ]+", " ", s)
    s = re.sub(r"\b(\d+)\.0+\b", r"\1", s)
    s = re.sub(r"\b(\d+\.\d*?[1-9])0+\b", r"\1", s)
    s = re.sub(r"(?<![\d.])0+(\d)", r"\1", s)
    words = s.split()
    figs = [w for w in words if re.match(r"^\d+(?:\.\d+)?%?$", w)]
    if any(z in s for z in ZERO_WORDS) and all(float(f.rstrip("%")) == 0 for f in figs):
        return "0"
    if not figs:
        return " ".join(sorted(set(words)))
    return " ".join(words).strip(" .")


_SKU = re.compile(r"\bskus?\b")
_SKU_NAME = re.compile(r"^[a-z0-9&+]+$")                                                                   # a word of a SKU's name (Enterprise + Atmosphere); a sentence mark or a bracket ends the name
_SKU_STOP = {"a", "an", "the", "this", "that", "these", "those", "each", "every", "any", "all", "both", "per", "of", "for", "in", "on", "at", "to", "by", "with", "and", "or", "is", "are", "was", "were", "be", "as", "from", "into", "under", "via", "its", "their", "our", "your"}


def sku_words(blob: str) -> set[str]:
    """The name of the SKU a claim is about: the words right before "SKU" (or "SKUs") in the lower-cased subject
    and text, at most four, read backwards from "SKU" and ended by a sentence mark, a bracket or a function word (each, the, of, for): the prose around the
    name is not the name, so "Each Place Details Essentials SKU" and "Place Details Essentials SKU" are one SKU; set() when the claim names no SKU."""
    out: set[str] = set()
    for m in _SKU.finditer(blob):
        for tok in reversed(blob[:m.start()].split()[-4:]):
            if not _SKU_NAME.match(tok) or tok in _SKU_STOP:
                break
            out.add(tok)
    return {w for t in out for w in re.findall(r"[a-z0-9]+", t)}


def tier_of(claim: dict, product_id: str | None = None) -> str:
    """The plan tier of a claim, the key's tier part: the tier words found in its subject and text, the SKU name the subject or text states
    (the words right before "SKU", at most four, ended by a sentence mark or a function word, minus the product's, the version's and the generic words), and the size, count and time tokens
    of its subject (the extractor names the plan or instance in the claim's subject; a size in the text is part of the statement, not of the identity),
    sorted; '' when none. The readers select the claim's text so that the text is read in production (reconcile.base_claims, claims_with_sources)."""
    subject = str(claim.get("subject") or "").lower().replace("-", " ")
    blob = " ".join(str(claim.get(k) or "") for k in ("subject", "text")).lower().replace("-", " ")
    found = {w.replace("-", " ") for w in TIER_WORDS if re.search(r"\b" + re.escape(w.replace("-", " ")) + r"\b", blob)}
    found |= {f"{a} {b}" for a, b in _SIZE.findall(subject)}
    product_words = set(re.findall(r"[a-z0-9]+", str(product_id or "").lower().replace("-", " ")))
    version_words = set(re.findall(r"[a-z0-9]+", str(claim.get("product_version") or "").lower().replace("-", " ").replace(".", " ")))
    version_words |= {("v" + w) for w in version_words} | {w[1:] for w in version_words if w.startswith("v")}    # the version token in either spelling (3, v3)
    subject_words = set(re.findall(r"[a-z0-9]+", subject)) - product_words - version_words - GENERIC_WORDS   # the claim's subject names the plan or instance beyond the product's name and version
    found |= subject_words
    found |= sku_words(blob) - product_words - version_words - GENERIC_WORDS                                     # the SKU name separates the facts of one price table
    return " ".join(sorted(found))


def figures(s: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", s)


_RATE_UNITS = ((r"(?:hour|hr)s?", "hour", 1.0), (r"min(?:ute)?s?", "hour", 60.0), (r"(?:million|1m|mtok)(?: (?:input|output))?(?: tok(?:en)?s?)?", "million tokens", 1.0),
               (r"(?:1k|1000|thousand)(?: (?:input|output))? tok(?:en)?s?", "million tokens", 1000.0), (r"month", "month", 1.0), (r"(?:year|yr)", "month", 1.0 / 12.0))


def unit_amount(norm: str) -> tuple | None:
    """A normalised rate in the budget's units, (unit, amount, direction): a minute is a sixtieth of an hour, a thousand tokens a thousandth of a
    million, a year twelve months, the direction input, output or ''; None when the value is not one figure per one such unit."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?) per (.+)", norm)
    if m:
        way = next((w for w in ("input", "output") if w in m.group(2).split()), "")
        for pattern, unit, factor in _RATE_UNITS:
            if re.fullmatch(pattern, m.group(2)):
                return unit, round(float(m.group(1)) * factor, 9), way
    return None


_NEGATION = {"no", "not", "non", "none", "never", "without", "except", "excluding", "cannot", "unsupported", "unavailable"}
_BOUNDS = {"under", "below", "over", "above", "least", "most", "less", "more", "than", "up", "minimum", "maximum", "min", "max", "within", "exceeding"}   # the words that make a figure a bound
_DIRECTIONS = {"input", "output"}


def lists_agree(a: str, b: str) -> bool:
    """Two normalised values without a figure agree when one's words are a subset of the other's: a shorter
    list omits, it does not contradict. A value that negates (not supported, all except Hebrew) is no list, and neither is one with a figure."""
    wa, wb = set(a.split()), set(b.split())
    return bool(wa and wb) and not (figures(a) or figures(b)) and not (wa | wb) & _NEGATION and (wa <= wb or wb <= wa)


def same_value(a: str, b: str) -> bool:
    """Equal after normalisation, or the same figures in the same order with the same unit words, or the same rate in the budget's units (two
    rates differ when they name different directions, and a rate that names none agrees with one that does: the attribute says which), or word
    lists of which one holds the other (lists_agree). The named directions are compared first, so a qualifier that keeps a value out
    of the rate grammar (about $1 per million output tokens) cannot reach the fallback that drops them. With equal figures in the same order
    and no negation word on either side, one non-empty word set holding the other agrees (the rule of lists_agree extended to values with a figure), provided
    both sides carry the same bound words (_BOUNDS): "5 per month" against "under 5 per month" is an exact figure against a
    bound on it, two statements, while "under 5" and "under 5 waived" are one."""
    if a == b or lists_agree(a, b):
        return True
    way_a, way_b = set(re.findall(r"[a-z]+", a)) & _DIRECTIONS, set(re.findall(r"[a-z]+", b)) & _DIRECTIONS
    if way_a and way_b and not way_a & way_b:
        return False
    ua, ub = unit_amount(a), unit_amount(b)
    if ua and ub:
        return ua[:2] == ub[:2] and (ua[2] == ub[2] or not (ua[2] and ub[2]))
    fa, fb = figures(a), figures(b)
    if fa and fa == fb:
        drop = {"per", "tokens", "token", "input", "output", "approximately", "about"}
        wa, wb = set(re.findall(r"[a-z]+", a)) - drop, set(re.findall(r"[a-z]+", b)) - drop
        if wa == wb:
            return True
        return bool(wa and wb) and not (wa | wb) & _NEGATION and wa & _BOUNDS == wb & _BOUNDS and (wa <= wb or wb <= wa)   # equal figures in order, no negation on either side, the same bound words on both sides ("5 per month" against "under 5 per month" stays apart), one non-empty word set holds the other (under 5; under 5 waived)
    return False


def normalise_value(value: str | None) -> str:
    return norm_value(value)


_OPERATOR = re.compile(r"(<=|=<|\u2264|\u2a7d|\u2266|>=|=>|\u2265|\u2a7e|\u2267|!=|<>|\u2260|==|<|>|=)")
_OPERATOR_FORM = {"=<": "<=", "\u2264": "<=", "\u2a7d": "<=", "\u2266": "<=", "=>": ">=", "\u2265": ">=", "\u2a7e": ">=", "\u2267": ">=", "<>": "!=", "\u2260": "!=", "==": "="}


def _condition_text(item) -> str:
    """One recorded condition in comparable text: its words through the one tokenisation, its grouped
    numbers plain, and its comparison operators kept in one form each, which the tokenisation alone would erase (<= and > are different conditions)."""
    from app.core.budget import words_of
    parts = _OPERATOR.split(ungrouped(unicodedata.normalize("NFKC", str(item or ""))))
    return " ".join(w for i, p in enumerate(parts) for w in ([_OPERATOR_FORM.get(p, p)] if i % 2 else words_of(p)))


def conditions_of(c: dict) -> str:
    """A claim's recorded conditions as one comparable text: each through _condition_text, the set sorted; '' for none."""
    try:
        items = json.loads(c.get("conditions_json") or "[]")
    except (TypeError, ValueError):
        items = []
    return "; ".join(sorted({t for t in map(_condition_text, items) if t}))


def _conflict_key(c: dict) -> tuple:
    pid = c.get("product_id") or _norm(c.get("subject"))
    return (pid, c.get("product_version") or "", _norm(c.get("attribute")), tier_of(c, c.get("product_id")), conditions_of(c))   # claims under different conditions are different facts, never a conflict


def _read_ts(x) -> float:
    try:
        return datetime.fromisoformat((x or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _ascii_label(text: str) -> str:
    """An internationalised name in its ASCII (punycode) form, lower case; the name as it stands when the codec refuses it."""
    try:
        return text.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return text.lower()


def host_of(url: str | None) -> str:
    """The host a source was read from, normalised by the standard URL parser: lower case, no user information, no
    port, no trailing dot, a leading www. removed, an internationalised host in its ASCII form; '' when the url has no host."""
    try:
        host = (urlsplit(url or "").hostname or "").rstrip(".")
    except ValueError:
        return ""
    return _ascii_label(host[4:] if host.lower().startswith("www.") else host)


def _vendor_word(c: dict) -> str:
    """The vendor's recorded identity word: the first word of the product id, else of the subject."""
    return re.split(r"[-\s/_.]+", (c.get("product_id") or c.get("subject") or "").lower().strip())[0]


def official_hosts(records: list[dict]) -> dict[str, set[str]]:
    """The official hosts per vendor word: the hosts of the tier-1 sources about that vendor in the
    records, and the host that is exactly <the vendor's identity word>.<one label> (openai.com for openai-gpt-4o-mini; a host under it gives it).
    Nothing else: no suffix is inferred, so vendor.co.id owns neither review.co.id nor co.id, and openai.co.uk is not openai's."""
    out: dict[str, set[str]] = {}
    for c in records:
        host, word = host_of(c.get("url")), _vendor_word(c)
        labels = host.split(".")
        if host and int(c.get("tier") or 3) == 1:
            out.setdefault(word, set()).add(host)
        if len(word) >= 3 and len(labels) >= 2 and labels[-2] == _ascii_label(word):
            out.setdefault(word, set()).add(".".join(labels[-2:]))
    return out


def standing(c: dict, official: set | None = None) -> tuple[int, str]:
    """The standing of a claim's source: 0 for the vendor's own page, 1 for another page, with the host. A page is the
    vendor's own only when it is a tier-1 source (the records name it the official source), or its host equals an official host of the
    vendor (official_hosts; the claim's own when none are given) or is a subdomain of one; never because its host contains the vendor's name."""
    host = host_of(c.get("url"))
    official = official_hosts([c]).get(_vendor_word(c), set()) if official is None else official
    return (0 if int(c.get("tier") or 3) == 1 or any(host == o or host.endswith("." + o) for o in official) else 1), host


def _preference(c: dict, official: set | None = None) -> tuple:
    """The order of preference: the vendor's own page first, then, among sources of equal standing, the newest read, then the id."""
    return (standing(c, official)[0], -_read_ts(c.get("retrieved_at")), c["id"])


def detect_conflicts(claims: list[dict], records: list[dict] | None = None) -> list[dict]:
    """Claims about the same product identity and attribute with different values are a conflict. Only verified claims
    with a value take part (supported or partial, scope not out of scope). Every claim is kept. The conflict is settled
    per attribute: the vendor's own page beats a third-party page, then the newest read wins among sources of equal standing;
    the losers are superseded (recorded with the reason, never deleted); only a tie (equal standing, the same read, a different
    value) leaves it unresolved for the checks. Deterministic: sorted keys and ids. `records` are the claims the official
    hosts are read from (all the project's, the conflicting ones when none are given)."""
    groups: dict[tuple, list[dict]] = {}
    hosts_of = official_hosts(claims if records is None else records)
    for c in claims:
        if c.get("status") not in ("supported", "partial", "conflicting", "contradicted") or c.get("scope_status") == "out_of_scope" or not _norm(c.get("value")):
            continue
        if not (c.get("product_id") or c.get("subject")) or not _norm(c.get("attribute")):
            continue
        groups.setdefault(_conflict_key(c), []).append(c)
    out = []
    for key in sorted(groups):
        members = groups[key]
        values = []
        for v in sorted({normalise_value(m.get("value")) for m in members}):
            if not any(same_value(v, d) for d in values):                      # equal normalised values are agreements, never a conflict
                values.append(v)
        if len(values) < 2:
            continue
        official = hosts_of.get(_vendor_word(members[0]), set())                                 # the vendor's official hosts in the records
        ranked = sorted(members, key=lambda m: _preference(m, official))
        preferred = ranked[0]
        rank = lambda m: _preference(m, official)[:2]
        pv = normalise_value(preferred.get("value"))
        tied = [m for m in ranked[1:] if rank(m) == rank(preferred) and not same_value(normalise_value(m.get("value")), pv)]   # equal standing and the same read, a different value: an unresolved tie
        st_p, host_p = standing(preferred, official); read = str(preferred.get("retrieved_at") or "")[:19]
        skipped = "" if any(standing(m, official)[0] == 0 for m in members) else (("the records name no vendor for this fact" if len(_vendor_word(preferred)) < 3 else "no source of this fact is on an official host of the vendor")
                                                                                 + ", so the vendor's-own-page step is skipped")   # the skipped step is named in the settled and the tie reasons
        if tied:
            reason = f"tie: {len(tied) + 1} claims of equal standing read at the same time ({read}) state different values; the question stays conflicting" + (f"; {skipped}" if skipped else "")
        elif skipped:
            reason = f"{skipped}; the newest read preferred: {preferred['id']} read {read}"
        elif st_p == 0 and any(standing(m, official)[0] == 1 for m in ranked[1:]):
            reason = f"the vendor's own page preferred over third-party pages: {preferred['id']} on {host_p}, read {read}"
        else:
            reason = f"the newest read preferred among sources of equal standing: {preferred['id']} on {host_p}, read {read}"
        out.append({"key": f"{key[0]}@{key[1]}:{key[2]}" + (f" [{key[3]}]" if key[3] else "") + (f" {{{key[4]}}}" if key[4] else ""), "product_id": key[0], "product_version": key[1], "attribute": key[2], "tier": key[3], "values": values,
                    "claim_ids": sorted(m["id"] for m in members), "preferred_claim_id": preferred["id"], "settled": not tied,
                    "reason": reason, "sources": sorted({(m.get("source_id") or "", m.get("url") or "") for m in members})})
    return out


def claims_with_sources(conn: sqlite3.Connection, project_id: str, claim_ids: list[str] | None = None) -> list[dict]:
    """Claim rows joined with what the rules need from their sources (tier, dates, cache status, freshness class)."""
    require_guarded(conn)
    sql = ("SELECT c.id, c.status, c.scope_status, c.subject, c.attribute, c.value, c.kind, c.source_id, c.product_id, c.product_version, c.conditions_json, c.text, "
           "s.tier, s.published_at, s.retrieved_at, s.url, s.cache_status, s.freshness FROM claims c LEFT JOIN sources s ON s.id=c.source_id WHERE c.project_id=?")   # the claim's text is selected because tier_of reads it
    params: list = [project_id]
    if claim_ids is not None:
        if not claim_ids:
            return []
        sql += f" AND c.id IN ({','.join('?' * len(claim_ids))})"; params += list(claim_ids)
    cols = ("id", "status", "scope_status", "subject", "attribute", "value", "kind", "source_id", "product_id", "product_version", "conditions_json", "text", "tier", "published_at", "retrieved_at", "url", "cache_status", "freshness")
    return [dict(zip(cols, r)) for r in conn.execute(sql + " ORDER BY c.id", params).fetchall()]


def conflict_lines(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """What the plan's unresolved checks and the coverage report say about each stored conflict: both sources shown with
    their values and dates, the preferred one named, never a silent choice."""
    require_guarded(conn)
    out = []
    for key, cj, pref, reason in conn.execute("SELECT conflict_key, claim_ids_json, preferred_claim_id, reason FROM claim_conflicts WHERE project_id=? AND status='open' ORDER BY conflict_key", (project_id,)).fetchall():
        ids = json.loads(cj)
        members = {c["id"]: c for c in claims_with_sources(conn, project_id, ids)}
        shown = []
        for cid in ids:
            m = members.get(cid)
            if m:
                when = (m.get("published_at") or m.get("retrieved_at") or "")[:10]
                shown.append(f"{cid}: {m.get('value') or '?'} ({m.get('url') or m.get('source_id') or 'no source'}, {when or 'undated'})")
        out.append(f"Conflicting evidence on {key}: " + " vs ".join(shown) + f"; preferred {pref} ({reason}); both sources kept, no decision rests on this fact until it is settled")
    return out


# ---------------------------------------------------------------- injection
def obeyed_injection(claims: list[dict], marker: str) -> bool:
    """A planted instruction was obeyed when any extracted claim carries the marker in its text or value."""
    m = (marker or "").lower()
    return bool(m) and any(m in f"{c.get('text', '')} {c.get('value', '')}".lower() for c in claims)
