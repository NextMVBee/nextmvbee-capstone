"""Candidate discovery by three research engines, cross-checked, with the panel check at comparison and the check that every pick is current to the day.

Discovery, per decision subject: three sources of candidates. X, Exa direct (app/adapters/research.py: type deep with the planner's
English and Hebrew variants as additionalQueries, highlights on, maxAgeHours 0 for vendor pricing and documentation pages and at most
168 for the rest). P, OpenRouter's agentic web search with the Perplexity engine, and L, the same with the Parallel engine: one chat
completion each through the gateway with the server tools openrouter:web_search (max_results 10, max_total_results 40) and
openrouter:web_fetch (engine openrouter), max_tool_calls at most 12, a session_id per run and subject, the run date and the subject's
binding requirements in the system prompt, JSON out (schemas.models.DiscoveryOut): leads only, never evidence.

Union: URLs normalised (scheme, www, trailing slash, tracking parameters) and products by identity (vendor and product, the plan or the
API as the mode); found_by records every engine that surfaced a URL or a product; agreement between engines
orders what is read first and is never evidence. Reading and evidence stay ours: every cited page is fetched by our fetcher (the
read tool) and its claims extracted with exact quotes and verified; a page our fetcher cannot read is recorded as refused ("Read
refused" in the discovery summary) and goes unread (no second reader behind our fetcher is implemented).

The panel check at comparison (the panel is three models that read a draft comparison: openrouter:fusion, the documented quality
preset, max_tool_calls 4): code reads the analysis; a
product it names that is not a candidate becomes a typed candidate, researched and verified in one bounded follow-up; a contradiction
becomes a research question; nothing from the panel enters a memo without our evidence. Currency of picks: every pick must
be the current generation on the run date, checked against the OpenRouter catalogue for models it serves and against the vendor's
own documentation fetched in this run for the rest.

This module holds the pure parts (normalisation, the union, the request shapes, the estimates, the panel reading, the currency
check); app/graph/workflow.py drives the calls through the gateway and the tool runner, under the run's caps."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.coverage import normalise_identity
from app.core.sqlguard import require_guarded
from app.core import textlimit as TL

ENGINES = {"perplexity": {"label": "Perplexity", "search_usd": 0.005}, "parallel": {"label": "Parallel", "search_usd": 0.005}, "exa": {"label": "Exa", "search_usd": 0.007}}
FETCH_ENGINE = "openrouter"                       # the free fetch engine with retrieved_at on each result
PANEL_PRESET = ["~anthropic/claude-opus-latest", "~openai/gpt-sol-latest", "~google/gemini-pro-latest"]
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id", "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid",
            "mc_cid", "mc_eid", "_hsenc", "_hsmi", "ref", "ref_src", "source", "trk", "si"}
MAX_TOOL_CALLS = 12
MAX_RESULTS = 10
MAX_TOTAL_RESULTS = 40
STOP_SHAPE = None              # no stop_server_tools_when on a discovery request. The provider's documentation says a set
                               # stop_server_tools_when overrides max_tool_calls entirely, no step condition has a verified wire shape, and the probed cost shape
                               # ([{"type": "max_cost", "amount": <USD>}], accepted by the provider) did not stop the loop: max_tool_calls is the enforced step bound alone
STOP_SHAPES = {"max_cost": lambda cap: [{"type": "max_cost", "amount": round(float(cap), 4)}],     # the probe's shapes, sent only when a caller names one explicitly
               "maxCost": lambda cap: [{"maxCost": round(float(cap), 4)}]}


# ----------------------------------------------------------------------------- normalisation
def normalise_url(url: str) -> str:
    """One spelling per page: scheme lower-cased and https for http, the host lower-cased without www., the fragment dropped, tracking
    parameters dropped and the rest sorted, a trailing slash dropped except at the root."""
    p = urlsplit((url or "").strip())
    scheme = "https" if p.scheme.lower() in ("http", "https") else p.scheme.lower()
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(":443") or host.endswith(":80"):
        host = host.rsplit(":", 1)[0]
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING))
    return urlunsplit((scheme, host, path, query, ""))


GENERIC = {"api", "apis", "service", "services", "platform", "cloud", "email", "mail", "send", "stt", "speech", "to", "text", "transcription", "hosting", "host",
           "model", "models", "llm", "tier", "plan", "the", "a", "an", "and", "of", "for", "v1", "v2", "v3", "v4", "web", "app", "inc", "ltd", "llc"}


def product_key(vendor: str, product: str, mode: str = "") -> tuple[str, str]:
    """The identity two leads are compared by: the vendor's id, then the product's own words (the
    vendor's name and the generic class words removed; "SendGrid Email", "Twilio SendGrid" and "Twilio SendGrid v3 Mail Send API" give
    the same key) joined to it; the plan or the API is the mode. A product that is only its vendor's name keys on the vendor alone."""
    v_words = [w for w in re.findall(r"[a-z0-9.]+", (vendor or "").lower())]
    p_words = [w for w in re.findall(r"[a-z0-9.]+", (product or "").lower())]
    v_set = set(v_words) | {"".join(v_words)}
    core = [w for w in p_words if w not in v_set and w not in GENERIC and not ("".join(v_words) and w in "".join(v_words) and len(w) >= 4)]
    vid = normalise_identity("-".join(v_words), "")[0] if v_words else normalise_identity("-".join(w for w in p_words if w not in GENERIC) or "-".join(p_words), "")[0]
    pid = vid + ("-" + normalise_identity("-".join(core), "")[0] if core else "")
    m = " ".join((mode or "").lower().replace("_", " ").split())
    m = " ".join(w for w in m.split() if w not in {"the", "plan", "tier", "mode"})
    return pid, m


# ----------------------------------------------------------------------------- the candidate identity
QUOTING = "`'\"\u2018\u2019\u201c\u201d\u00ab\u00bb"                              # the quoting characters an identifier never carries (for example "`stt-async-v5`" stored with its backticks)
_QUOTING_TABLE = str.maketrans("", "", QUOTING)


def bare_identifier(value) -> str:
    """An identifier field as code writes it: the quoting characters (backticks, straight and typographic quotes, guillemets) dropped
    wherever they stand and the spaces collapsed; None and an empty value are ''."""
    return " ".join(str(value or "").translate(_QUOTING_TABLE).split())


def split_label(label) -> tuple[str, str]:
    """A bare label's vendor and product, as the union reads a planner label: the first word is the vendor and the rest the product;
    a one-word label is both; an empty label is ('', '')."""
    label = bare_identifier(label)
    if not label:
        return "", ""
    vendor, _, rest = label.partition(" ")
    return (vendor, rest) if rest else (label, label)


def display_label(vendor, product, version="") -> str:
    """The display spelling of a candidate: the vendor then the product, and the product alone when it already names the vendor (its
    first word begins with the vendor's first word of three letters or more, or the whole product begins with the whole vendor): "Render",
    "Render Hobby Plan" is "Render Hobby Plan"; "Dicta", "DictaLM" is "DictaLM"; "Anthropic", "Claude Haiku 5.5" keeps both. One of the two
    empty gives the other. Then the version, unless the product already names it (its letters and digits inside the
    product's): "Anthropic", "Claude Haiku", "5.5" is "Anthropic Claude Haiku 5.5"; "Claude Haiku 5.5" at "5.5" is not repeated.
    The product alone is judged, never the vendor: "AI21 Labs", "Jamba" at "1" and at "2" are "AI21 Labs Jamba 1" and "AI21 Labs Jamba 2"."""
    vendor, product, version = bare_identifier(vendor), bare_identifier(product), bare_identifier(version)
    if not vendor or not product:
        name = vendor or product
    else:
        vw, pw = re.findall(r"[a-z0-9]+", vendor.lower()), re.findall(r"[a-z0-9]+", product.lower())
        name = product if vw and pw and ((len(vw[0]) >= 3 and pw[0].startswith(vw[0])) or "".join(pw).startswith("".join(vw))) else f"{vendor} {product}"
    if version and name and re.sub(r"[^a-z0-9]", "", version.lower()) not in re.sub(r"[^a-z0-9]", "", product.lower()):   # the product's letters and digits, not the vendor's
        return f"{name} {version}"
    return name


def insert_candidate(conn, project_id: str, run_id: str, subject: str, *, label: str | None = None, vendor: str = "", product: str = "", version: str = "", access_route: str = "",
                     kind: str = "product", urls=(), product_class: str = "", subject_class: str = "", specialised_for: str | None = None, status: str = "kept", reason: str | None = None,
                     replace: bool = False) -> str:
    """The one write to the candidates table (the planner's rows, the discovery union's, the panel follow-up's). The identity fields
    are stored bare (bare_identifier); a row given no vendor and no product takes them from its label (split_label); the label is the display spelling
    (display_label), never a vendor repeated, carrying the version when the product does not name it. INSERT OR REPLACE for the
    planner's rows; otherwise INSERT OR IGNORE, and when the label is already stored (the planner's row before the union's lead of the same
    identity) the stored row is completed, never ignored: its empty version and route take the write's, a new route joins the stored one,
    the write's pages join the stored pages; the label, the status and the reason stand. Returns the label."""
    require_guarded(conn)
    vendor, product, version, access_route = (bare_identifier(x) for x in (vendor, product, version, access_route))
    if not vendor and not product:
        vendor, product = split_label(label)
    shown = (display_label(vendor, product, version) or bare_identifier(label))[:120]
    kind = kind if kind in ("product", "approach") else "product"
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    pages = [normalise_url(u) for u in list(urls)[:5] if u]                             # the lead's pages, at most the five its contract admits
    cur = conn.execute(f"{verb} INTO candidates (project_id, run_id, subject, label, product_class, subject_class, specialised_for, status, reason, kind, vendor, product, version, access_route, urls_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (project_id, run_id, subject, shown, product_class, subject_class, specialised_for, status, reason, kind, vendor[:80], product[:120], version[:80], access_route[:120], json.dumps(pages)))
    if not replace and cur.rowcount == 0:                                              # the label is stored already: the row is completed with what this write adds
        key = (project_id, run_id, subject, shown)
        old = conn.execute("SELECT version, access_route, urls_json FROM candidates WHERE project_id=? AND run_id=? AND subject=? AND label=?", key).fetchone()
        routes = [r for r in (old[1] or "").split("; ") if r]
        routes += [r for r in access_route.split("; ") if r and r not in routes]
        stored_pages = list(json.loads(old[2] or "[]"))
        stored_pages += [u for u in pages if u not in stored_pages]
        conn.execute("UPDATE candidates SET version=?, access_route=?, urls_json=? WHERE project_id=? AND run_id=? AND subject=? AND label=?",
                     ((old[0] or version)[:80], "; ".join(routes)[:120], json.dumps(stored_pages), *key))
    return shown


# ----------------------------------------------------------------------------- the request shapes
def search_tool(engine: str, max_results: int = MAX_RESULTS, max_total: int = MAX_TOTAL_RESULTS, max_characters: int | None = None) -> dict:
    """The search tool bounded on the wire: the engine, the results per search and in total, and max_characters per result (RESULT_CHARS,
    the bound RESULT_TOKENS prices); without the character bound a result can carry a whole page."""
    if engine not in ("perplexity", "parallel", "exa"):
        raise ValueError(f"engine {engine!r} is not one of perplexity, parallel, exa")
    return {"type": "openrouter:web_search", "parameters": {"engine": engine, "max_results": int(max_results), "max_total_results": int(max_total),
                                                            "max_characters": int(max_characters if max_characters is not None else RESULT_CHARS)}}


def fetch_tool(max_uses: int = 6, max_content_tokens: int | None = None) -> dict:
    """The fetch tool bounded on the wire: max_uses and max_content_tokens (FETCH_TOKENS, the bound the reservation prices). Without
    the bound, fetches of whole pages grow the server-side loop without limit (measured: four fetches made 676,733 and
    1,301,802 prompt tokens in one send, 1.79 and 3.22 USD) and overflow the context of the smaller models (HTTP 400 after the loop)."""
    return {"type": "openrouter:web_fetch", "parameters": {"engine": FETCH_ENGINE, "max_uses": int(max_uses), "max_content_tokens": int(max_content_tokens if max_content_tokens is not None else FETCH_TOKENS)}}


def fusion_tool(models=None, max_tool_calls: int = 4) -> dict:
    return {"type": "openrouter:fusion", "parameters": {"analysis_models": list(models or PANEL_PRESET), "max_tool_calls": int(max_tool_calls)}}


def extra_body(session_id: str, max_tool_calls: int = MAX_TOOL_CALLS, cap_usd: float | None = None, stop_shape: str | None = STOP_SHAPE) -> dict:
    """The top-level fields of an agentic request: the sticky session and max_tool_calls, the step bound the provider enforces.
    No stop_server_tools_when rides by default: when set it overrides max_tool_calls entirely (the provider's documentation), no step
    condition has a verified wire shape, and the probed cost condition did not stop the loop, so a cost-only condition would
    remove the step bound. A probe may still name a shape explicitly; the reservation (request_estimate) prices the steps sent here."""
    body = {"session_id": session_id[:256], "max_tool_calls": max(1, min(int(max_tool_calls), 30))}
    if cap_usd is not None and stop_shape is not None and stop_shape in STOP_SHAPES:
        body["stop_server_tools_when"] = STOP_SHAPES[stop_shape](cap_usd)
    return body


RESULT_TOKENS = 450            # one search result (title, URL, snippet) in tokens: RESULT_CHARS on the wire (about 375 tokens) plus the title and the URL
RESULT_CHARS = 1500            # web_search max_characters per result, sent on every search tool
FETCH_TOKENS = 4000            # one fetched page in tokens: web_fetch max_content_tokens on the wire, sent on every fetch tool


def loop_tokens(max_tool_calls: int, max_results: int = MAX_RESULTS, fetch_uses: int = 6, prompt_tokens: int = 4000, result_tokens: int | None = None, fetch_tokens: int | None = None) -> int:
    """The prompt tokens an agentic send can bill: the server-side loop re-sends the growing context on every step, so k steps that
    each add R tokens of tool results bill about k times the prompt plus R times k(k+1)/2. A measured example:
    a 12-step pass with 10 results a search cost 3.75 USD on claude-opus-5 and 6.16 USD on gpt-5.6-sol, which this formula
    predicts within a third; a reservation of the prompt and the fees only would have admitted them at 0.26 USD."""
    k = max(1, int(max_tool_calls))
    per_step = max(max_results * (RESULT_TOKENS if result_tokens is None else result_tokens), (FETCH_TOKENS if fetch_tokens is None else fetch_tokens) if fetch_uses else 0)    # one step is one tool call: a search's results or one fetched page, whichever is larger; both bounded on the wire
    return int(k * prompt_tokens + per_step * k * (k + 1) / 2)


def estimate(engine: str, max_tool_calls: int, prompt_tokens: int, max_tokens: int, price: tuple[float, float], safety: float = 1.15, max_results: int = MAX_RESULTS, fetch_uses: int = 6, result_tokens: int | None = None, fetch_tokens: int | None = None) -> float:
    """The worst case reserved before an agentic send: the loop's prompt tokens at the input price, the answer and the per-step outputs
    at the output price, every tool call at the engine's fee (the fetch engine is free), plus 15 per cent."""
    fee = ENGINES.get(engine, {}).get("search_usd", 0.007)
    k = max(1, int(max_tool_calls))
    tokens_in = loop_tokens(k, max_results, fetch_uses, prompt_tokens, result_tokens, fetch_tokens)
    tokens_out = max_tokens + 300 * k
    usd = (tokens_in * price[0] + tokens_out * price[1]) / 1e6 + k * fee
    return round(usd * safety, 6)


def request_bound(extra_body: dict | None, server_tools: list | None) -> dict | None:
    """The loop bound of an agentic request as sent on the wire: the steps (max_tool_calls), the search engine and its results
    per search with their character bound, and the fetch uses with their token bound, read from the request's own fields so the reservation
    prices exactly what the provider enforces. None for a request without a search or fetch tool (the panel reserves its whole cap)."""
    search = next((t for t in server_tools or [] if t.get("type") == "openrouter:web_search"), None)
    fetch = next((t for t in server_tools or [] if t.get("type") == "openrouter:web_fetch"), None)
    if not (search or fetch):
        return None
    sp, fp = (search or {}).get("parameters") or {}, (fetch or {}).get("parameters") or {}
    k = int((extra_body or {}).get("max_tool_calls") or 30)                                 # the API's own default and maximum when the field is absent (the provider's documentation)
    chars = int(sp.get("max_characters") or RESULT_CHARS) if search else 0
    return {"steps": max(1, min(k, 30)), "engine": sp.get("engine") or "exa", "max_results": int(sp.get("max_results") or MAX_RESULTS) if search else 0,
            "result_tokens": int(chars / 4) + 75 if search else 0,                           # the snippet at four characters a token plus the title and the URL (RESULT_TOKENS for RESULT_CHARS)
            "fetch_uses": int(fp.get("max_uses") or 0) if fetch else 0, "fetch_tokens": int(fp.get("max_content_tokens") or FETCH_TOKENS) if fetch else 0}


def request_estimate(extra_body: dict | None, server_tools: list | None, prompt_tokens: int, max_tokens: int, price: tuple[float, float]) -> float | None:
    """The worst case of an agentic request from its own wire bounds (request_bound) at the sending model's prices: the gateway reserves
    at least this before the send, for the primary and for any fallback route alike. None when the request carries no loop bound."""
    b = request_bound(extra_body, server_tools)
    if b is None:
        return None
    return estimate(b["engine"], b["steps"], prompt_tokens, max_tokens, price, max_results=b["max_results"], fetch_uses=b["fetch_uses"], result_tokens=b["result_tokens"], fetch_tokens=b["fetch_tokens"])


def panel_prompt_tokens(cap_usd: float, price: tuple[float, float]) -> int:
    """The fusion panel's reservation expressed as prompt tokens of the analyst's route: the whole per-decision cap, because the panel's
    models bill their own tokens inside the one request (the provider's documentation); the gateway adds the answer at the output price and 15 per cent."""
    return int(float(cap_usd) / max(float(price[0]), 1e-9) * 1e6)


def estimate_as_prompt_tokens(engine: str, max_tool_calls: int, price: tuple[float, float], prompt_tokens: int = 4000, max_results: int = MAX_RESULTS, fetch_uses: int = 6) -> int:
    """The loop's extra prompt tokens and the engine's fees expressed as prompt tokens at the model's input price, so the gateway's own
    reservation (prompt tokens at the input price, max_tokens at the output price, plus 15 per cent) carries the whole worst case."""
    k = max(1, int(max_tool_calls))
    fee = ENGINES.get(engine, {}).get("search_usd", 0.007) * k
    extra_out = 300 * k * price[1] / 1e6
    return int(loop_tokens(k, max_results, fetch_uses, prompt_tokens) + (fee + extra_out) / max(price[0], 1e-9) * 1e6)


# ----------------------------------------------------------------------------- the reach
REACH_RESERVE = 2              # the tool calls a subject's stage instance keeps for the Researcher's own requests after the planned and the reach questions are searched (one search, one read)
_REACH_STOP = {"the", "and", "for", "api", "apis", "plan", "tier", "model", "models", "version", "service", "platform", "cloud"}


def _reach_words(text: str) -> tuple[set[str], set[str]]:
    """The words of a product name or a query: the letter words of three or more characters (the stop words left out) and the version tokens (a token
    with a digit: 5.4, 4o, 3b), so that 'GPT-4o mini' names GPT-4o mini and not GPT-5.4 Mini, while 'DictaLM' names DictaLM 2.0 (a query without a
    version token names every version)."""
    tokens = re.findall(r"[a-z0-9.]+", (text or "").lower())
    return {w for w in tokens if w.isalpha() and len(w) >= 3} - _REACH_STOP, {w.strip(".") for w in tokens if any(ch.isdigit() for ch in w)}


def _planned_names(words: set[str], digits: set[str], planned_words: list[tuple[set[str], set[str]]]) -> bool:
    """Whether a planned query already names the product: every letter word of the product's name in the query, and the product's version tokens in it too
    unless the query carries no version token at all."""
    return bool(words) and any(words <= pw and (digits <= pd or not pd) for pw, pd in planned_words)


def _reach_url(urls: list[str]) -> str:
    """The vendor pricing or documentation URL the pass read for a lead: a pricing page first, then a documentation page, else the first URL named."""
    for pattern in (r"pric|billing|plans?\b|cost", r"docs?\b|documentation|models?\b|developers?"):
        for u in urls or []:
            if re.search(pattern, (u or "").lower()):
                return u
    return (urls or [""])[0] or ""


def reach_questions(subject: str, sclass: str, products: list[dict], planned: list[dict], allowance: int, year: str) -> list[dict]:
    """One research question per distinct discovered candidate of the subject's class, written by code after the
    discovery passes, as the panel follow-up writes its candidates' questions: distinct by the canonical identity of the lead's product key (one
    question per identity whatever the spelling; its access routes are facets of the one record), in the union's own order (agreement
    first), skipping a product a planned query of the subject already names (every letter word of the product's name in the query's text or
    target fact), bounded by `allowance` (the subject's stage limit less its planned questions and REACH_RESERVE, and the run's searches left,
    computed by the caller; never a new limit), each naming the vendor pricing or documentation URL the pass read in its target fact. Returns the
    query dicts the research plan persists (text, decision_subject, target_fact, freshness volatile, language en) with the identity and the url
    beside them for the trace; [] at an allowance of 0."""
    from app.core.coverage import canonical_identity, _norm as _cnorm
    out: list[dict] = []; seen: set[str] = set()
    planned_words = [_reach_words(f"{q.get('text') or ''} {q.get('target_fact') or ''}") for q in planned or [] if _cnorm(q.get("decision_subject") or "") == _cnorm(subject)]
    cls = _cnorm(sclass or "")
    for pr in products or []:
        pclass = _cnorm(pr.get("product_class") or "")
        if cls and pclass and pclass != cls:
            continue
        pid, _ = product_key(pr.get("vendor") or "", pr.get("product") or "")
        if not pid:
            continue
        key = canonical_identity(pid, "")
        if key in seen:
            continue
        seen.add(key)
        words, digits = _reach_words(pr.get("product") or "")
        if _planned_names(words, digits, planned_words):
            continue
        if len(out) >= max(int(allowance), 0):
            break
        label = display_label(pr.get("vendor") or "", pr.get("product") or "")
        url = _reach_url(pr.get("urls") or [])
        fact = f"candidates: {label} ({sclass})" + (f" from {url}" if url else "")
        out.append({"text": f"{label} {(sclass or '').replace('_', ' ')} pricing documentation {year}".strip()[:200], "decision_subject": subject, "target_fact": fact[:200], "freshness": "volatile", "language": "en",
                    "identity": key, "url": url})
    return out


# ----------------------------------------------------------------------------- the gap query
def gap_reason(kept: list, langs: list | None) -> str | None:
    """Why the research plan re-searches a typed subject once more, or None: fewer than three kept typed candidates, else a binding
    language requirement (langs) with no language-specialised kept candidate (candidate.specialised_for). The workflow's one trigger."""
    if len(kept) < 3:
        return "fewer than three candidates"
    if langs and not any((c0.get("specialised_for") if isinstance(c0, dict) else getattr(c0, "specialised_for", None)) for c0 in kept):   # the eligible set's rows are dicts, a planner candidate an object
        return "no language-specialised candidate"
    return None


# ----------------------------------------------------------------------------- the union
def union(exa_pages: list[dict], discovered: list[tuple[str, dict]], planner: list[dict] | None = None) -> dict:
    """The candidates and URLs of every engine for one subject, merged by identity, each with found_by (every engine that surfaced it)
    and the count of engines that agree, which orders what is read first (never evidence).

    exa_pages: the stored pages of the subject's Exa search ({url, title, source_id}); discovered: [(engine, DiscoveryOut as dict)];
    planner: the planner's typed candidates ({label, product_class})."""
    urls: dict[str, dict] = {}
    products: dict[tuple[str, str], dict] = {}

    def add_url(u, engine, title=""):
        n = normalise_url(u)
        if not n or len(n) < 12:
            return
        rec = urls.setdefault(n, {"url": n, "found_by": [], "title": title or "", "source_id": None})
        if engine not in rec["found_by"]:
            rec["found_by"].append(engine)
        if title and not rec["title"]:
            rec["title"] = title

    def add_product(vendor, product, mode, product_class, engine, url_list=(), why=""):
        pid, m = product_key(vendor, product, mode)
        if not pid:
            return
        key = (pid, "")                                                                  # the identity alone keys a product; the plan or the API is one of its modes
        route = bare_identifier(mode)                                                    # the access route as the lead spelled it, a facet of the one record (the normalised m compares, never records)
        rec = products.setdefault(key, {"vendor": vendor, "product": product, "mode": m or "", "modes": [], "access_route": "", "product_class": product_class, "found_by": [], "urls": [], "why": why})
        if m and m not in rec["modes"]:
            rec["modes"].append(m)
            rec["access_route"] = route if not rec["access_route"] else f"{rec['access_route']}; {route}"
        if m and not rec["mode"]:
            rec["mode"] = m
        if engine not in rec["found_by"]:
            rec["found_by"].append(engine)
        for u in url_list:
            n = normalise_url(u)
            if n and n not in rec["urls"]:
                rec["urls"].append(n)
        if not rec["product_class"] and product_class:
            rec["product_class"] = product_class
        if not rec["why"] and why:
            rec["why"] = why

    for pg in exa_pages or []:
        add_url(pg.get("url", ""), "exa", pg.get("title") or "")
        if pg.get("source_id"):
            urls[normalise_url(pg["url"])]["source_id"] = pg["source_id"]
    for c in planner or []:
        label = c.get("label") or ""
        vendor, _, rest = label.partition(" ")
        add_product(vendor if rest else label, rest or label, "", c.get("product_class") or "", "planner")
    for engine, out in discovered or []:
        for c in (out or {}).get("candidates") or []:
            add_product(c.get("vendor", ""), c.get("product", ""), c.get("plan_or_mode", ""), c.get("product_class", ""), engine, c.get("urls") or [], c.get("why", ""))
            for u in c.get("urls") or []:
                add_url(u, engine)
        for u in (out or {}).get("urls_read") or []:
            add_url(u, engine)
    # a lead that is only its vendor's name joins the vendor's one product when the vendor has exactly one (a planner's "Soniox" and the engines' "Soniox Speech-to-Text")
    for key in [k for k in list(products) if "-" not in k[0] or k[0] == normalise_identity(products[k]["vendor"].replace(" ", "-"), "")[0]]:
        vendor_id = key[0]
        others = [k for k in products if k != key and k[0].startswith(vendor_id + "-")]
        if len(others) == 1 and key in products:
            tgt, src = products[others[0]], products.pop(key)
            for e in src["found_by"]:
                if e not in tgt["found_by"]:
                    tgt["found_by"].append(e)
            for u in src["urls"]:
                if u not in tgt["urls"]:
                    tgt["urls"].append(u)
    for rec in urls.values():
        rec["agreement"] = len(rec["found_by"])
    for rec in products.values():
        rec["found_by"] = sorted(rec["found_by"], key=lambda e: 0 if e == "planner" else 1)
        rec["agreement"] = len([e for e in rec["found_by"] if e != "planner"])
    ordered_urls = sorted(urls.values(), key=lambda r: (-r["agreement"], r["source_id"] is not None, r["url"]))
    ordered_products = sorted(products.values(), key=lambda r: (-r["agreement"], r["vendor"].lower(), r["product"].lower()))
    return {"urls": ordered_urls, "products": ordered_products, "engines": sorted({e for r in urls.values() for e in r["found_by"]} | {e for r in products.values() for e in r["found_by"] if e != "planner"})}


# ----------------------------------------------------------------------------- the panel's reading
META_TOPIC = re.compile(r"~|\bpanel\b|\banalys[ie]s\b|run date|as of (the|today|20\d\d)|future date|knowledge cutoff|training (data|cutoff)|verified as of|availability of (a|the) (panel|model)", re.I)


OUTCOME_WORDS = {"tool_unavailable": "reported the fusion tool unavailable", "error": "failed", "empty": "gave an empty answer"}   # what happened to the models that gave no substantive answer


def panel_models(analysis: dict, requested: list[str] | None) -> dict:
    """How many analysis models were requested, how many answered
    with a substantive response, and what happened to the others, from the panel's recorded responses (PanelOut.responses); when the panel
    recorded no response per model, the answered count is unknown and the line says so, never the requested count as an agreement."""
    responses = [r for r in (analysis or {}).get("responses") or [] if isinstance(r, dict)]
    n_req = len(requested) if requested else (len((analysis or {}).get("models") or []) or len(responses) or 3)
    if not responses:
        return {"requested": n_req, "answered": None, "others": {}}
    answered = sum(1 for r in responses if r.get("outcome") == "substantive")
    others: dict[str, int] = {}
    for r in responses:
        if r.get("outcome") != "substantive":
            w = OUTCOME_WORDS.get(str(r.get("outcome") or ""), str(r.get("outcome") or "gave no substantive answer"))
            others[w] = others.get(w, 0) + 1
    return {"requested": max(n_req, len(responses)), "answered": answered, "others": others}


def models_line(models) -> str:
    """The models part of the panel line: the recorded counts; a plain integer in place of the counts reads as a count of models."""
    if isinstance(models, dict):
        n = int(models.get("requested") or 0); a = models.get("answered"); others = models.get("others") or {}
        head = f"{n} model{'s' if n != 1 else ''} requested, "
        if a is None:
            return head + "which answered was not recorded"
        return head + f"{a} answered with a substantive response" + (" (" + "; ".join(f"{k} {w}" for w, k in others.items()) + ")" if others else "")
    n = int(models or 0)
    return f"{n} model{'s' if n != 1 else ''}"


def panel_reading(analysis: dict, candidates: list[dict], subject_class: str, requested: list[str] | None = None) -> dict:
    """What code takes from the fusion panel's analysis: every named product that is not already a candidate becomes a typed candidate
    (kept only when its class is the subject's), every contradiction a research question. Nothing here is evidence; the memo's line
    is written here too. requested: the analysis models the request named; the reading's models field counts what answered."""
    have = {product_key(c.get("vendor", ""), c.get("label") or c.get("product", ""), "")[0] for c in candidates}
    have |= {product_key("", c.get("label") or c.get("product", ""), "")[0] for c in candidates}
    new, dropped = [], []
    for p in (analysis or {}).get("products_named") or []:
        key = product_key(p.get("vendor", ""), p.get("product", ""), "")[0]
        if not key or key in have or any(key in h or h in key for h in have if len(h) > 3):
            continue
        rec = {"vendor": p.get("vendor", ""), "product": p.get("product", ""), "product_class": (p.get("product_class") or "").strip().lower().replace(" ", "_"), "why": p.get("why", ""), "label": f"{p.get('vendor', '')} {p.get('product', '')}".strip()}
        if subject_class and rec["product_class"] and rec["product_class"] != subject_class.lower():
            dropped.append(rec)
        else:
            new.append(rec)
        have.add(key)
    questions = [f"{c.get('topic', '')}: " + "; ".join(f"{s.get('model', '')} says {s.get('stance', '')}" for s in (c.get("stances") or [])[:3])
                 for c in (analysis or {}).get("contradictions") or [] if not META_TOPIC.search(c.get("topic") or "")]   # the panel's talk about itself, the run date or its own knowledge is not a question for the research
    kept, q_notes = [], []
    for q in (q0 for q0 in questions if len(q0) > 8):
        q1, note = TL.to_sentence_end(q, 300, "a panel note")                                 # a panel note at its cap is kept to its last sentence end with the note, never cut mid-word
        kept.append(q1)
        if note:
            q_notes.append(note)
    questions = kept
    blind = list((analysis or {}).get("blind_spots") or [])
    return {"candidates": new, "dropped": dropped, "questions": questions, "blind_spots": blind, "models": panel_models(analysis, requested), **({"notes": q_notes} if q_notes else {})}


def panel_line(reading: dict, held: bool, changed_to: str | None = None, why: str = "") -> str:
    """The one line the decision record keeps and the document prints: the models requested and the substantive answers, the
    blind spots researched, the pick."""
    researched = len(reading.get("candidates") or []) + len(reading.get("questions") or [])
    head = f"Panel check: {models_line(reading.get('models', 3))}; {researched} blind spot{'s' if researched != 1 else ''} researched; "
    if changed_to:
        return head + f"changed to {changed_to}" + (f" because {why}" if why else "")
    return head + ("the pick held" if held else "the draft had no pick")   # a comparison that gave up comes to the panel with no pick


# ----------------------------------------------------------------------------- currency of picks
def family_of(slug_or_name: str) -> tuple[str, tuple]:
    """A model's family and its version numbers: "anthropic/claude-opus-5.5" is ("anthropic/claude-opus", (5, 5)); "gpt-6.1-sol" is
    ("gpt-sol", (6, 1)). Used to find a newer model of the same family in the catalogue."""
    s = (slug_or_name or "").lower().strip()
    s = re.sub(r":[a-z0-9-]+$", "", s)                      # a variant suffix (:free, :beta)
    nums = [tuple(int(x) for x in re.findall(r"\d+", m)) for m in re.findall(r"(?<![a-z])\d+(?:[.-]\d+)*(?![a-z])", s)]
    fam = re.sub(r"(?<![a-z])\d+(?:[.-]\d+)*(?![a-z])", "", s)
    fam = re.sub(r"[-.]{2,}", "-", fam).strip("-. ")
    fam = re.sub(r"-(preview|latest|exp|experimental|\d{8})$", "", fam)
    version = nums[0] if nums else ()
    return fam, version


CURRENT_WORDS = re.compile(r"\b(current|latest|newest|new|generation|version|released?|release date|launch(?:ed)?|generally available|\bga\b|available since|introduc(?:ed|ing)|20\d\d)\b", re.I)
SUPERSEDED_WORDS = re.compile(r"\b(superseded|deprecated|replaced by|successor|succeeded by|end of life|end-of-life|retired|sunset|legacy|discontinued|no longer (?:available|supported|offered))\b", re.I)


def vendor_domain(pick: dict, url: str) -> tuple[str, bool]:
    """The source's domain (the host without www.) and whether it is the pick's vendor's own: the vendor's identity word (the first word of
    the product id, else of the option) is in the host's registrable name. A tier-1 source counts as official whatever its host."""
    host = (urlsplit(url or "").netloc or "").lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    vendor = re.split(r"[-\s/_.]+", str(pick.get("product_id") or pick.get("option") or "").lower().strip())[0] if (pick.get("product_id") or pick.get("option")) else ""
    name = host.split(".")[0] if host.count(".") <= 1 else ".".join(host.split(".")[:-1])
    return host, bool(vendor) and len(vendor) >= 3 and vendor in name.replace("-", "").replace(".", "")


def _parse_ts(ts) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) if t else None


def currency_reread_targets(pick: dict, sources: list[dict], run_start: str | datetime | None, limit: int = 2) -> list[dict]:
    """The pages of the pick's priced claims the currency check reads once more in this run: those not fetched in
    this run (retrieved before the run started, or with no readable date) that the check would count as the vendor's (the vendor's own domain,
    or tier 1), the vendor's own pages first and tier-1 pages after them, in id order, at most `limit`. A page fetched in this run is never
    read again; a secondary page is never read; without a run start nothing is. sources: {id, url, retrieved_at, tier}, as currency_check takes them."""
    today = run_start if isinstance(run_start, datetime) else _parse_ts(run_start)
    if today is None:
        return []
    today = today if today.tzinfo else today.replace(tzinfo=timezone.utc)
    own_pages, official = [], []
    for s in sources or []:
        t = _parse_ts(s.get("retrieved_at"))
        if t is not None and t >= today:
            continue
        own = vendor_domain(pick, s.get("url") or "")[1]
        if own:
            own_pages.append(s)
        elif int(s.get("tier") or 3) == 1:
            official.append(s)
    key = lambda s: str(s.get("id") or "")
    return (sorted(own_pages, key=key) + sorted(official, key=key))[:max(int(limit), 0)]


def currency_check(pick: dict, catalogue: list[dict], run_start: str | datetime | None, sources: list[dict], claims: list[dict] | None = None) -> dict:
    """Is the pick the current generation on the run date? A model OpenRouter serves is checked against the catalogue: a newer
    model of the same family (a higher version in the same family, or the family's tilde-latest alias resolving elsewhere) makes it
    superseded, with the current slug named. Anything else: current only when a supported, in-scope claim
    about the pick from the vendor's own page (the vendor's domain, or a tier-1 source) fetched in this run entails its generation (its
    currency, its release date) and superseded when such a claim names its successor or deprecation; otherwise unknown with the reason
    (no page fetched this run, only secondary pages, or no such claim). sources: {id, retrieved_at, url, tier}; claims: {id, source_id,
    verdict, scope_status, text} of the cited claims about the pick. Returns {status, current, line} with domain, tier and claim_id when
    a page decided it."""
    name = f"{pick.get('product_id') or ''} {pick.get('product_version') or ''}".strip() or (pick.get("option") or "")
    fam, ver = family_of(name)
    by_family: dict[str, list] = {}
    aliases = {}
    for m in catalogue or []:
        mid = str(m.get("id") or "")
        if mid.startswith("~"):
            tgt = (m.get("alias_target") or {}).get("slug")
            if tgt:
                aliases[family_of(mid.lstrip("~"))[0]] = tgt
            continue
        f, v = family_of(mid)
        by_family.setdefault(f, []).append((v, mid, m.get("created") or 0))
    # the pick's family in the catalogue: match on the family's tail (the slug carries the vendor, the pick may not)
    match = None
    for f, rows in by_family.items():
        if fam and (f == fam or f.endswith("/" + fam) or fam.endswith("/" + f) or f.split("/")[-1] == fam.split("/")[-1]):
            match = (f, rows); break
    today = (run_start if isinstance(run_start, datetime) else (datetime.fromisoformat(str(run_start).replace("Z", "+00:00")) if run_start else None))
    if match:
        f, rows = match
        newer = [r for r in rows if r[0] and ver and r[0] > ver]
        latest = aliases.get(f) or aliases.get(f.split("/")[-1])
        if newer:
            cur = sorted(newer, key=lambda r: r[0])[-1][1]
            return {"status": "superseded", "current": cur, "line": f"Currency: {name} is superseded in the OpenRouter catalogue by {cur}"}
        if latest and family_of(latest)[1] and ver and family_of(latest)[1] > ver:
            return {"status": "superseded", "current": latest, "line": f"Currency: {name} is superseded by the family's current model {latest}"}
        return {"status": "current", "current": None, "line": f"Currency: {name} is the current generation in the OpenRouter catalogue on the run date"}
    fetched, secondary = {}, []
    for s in sources or []:
        ts = s.get("retrieved_at")
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else None
        except ValueError:
            t = None
        if not (t and today and (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) >= (today if today.tzinfo else today.replace(tzinfo=timezone.utc))):
            continue
        domain, own = vendor_domain(pick, s.get("url") or "")
        tier = int(s.get("tier") or 3)
        if own or tier == 1:
            fetched[s.get("id")] = (domain, tier)
        else:
            secondary.append(f"{domain or s.get('id')}, tier {tier}")
    if not fetched:
        why = f"the pages of its evidence fetched in this run are not the vendor's own ({'; '.join(secondary[:3])})" if secondary else "no vendor page of its evidence was fetched in this run"
        return {"status": "unknown", "current": None, "line": f"Currency: {name} could not be checked ({why})"}
    for c in claims or []:
        if c.get("source_id") not in fetched or (c.get("verdict") or c.get("status")) != "supported" or c.get("scope_status") not in (None, "in_scope", "conditional"):
            continue
        text = str(c.get("text") or "")
        domain, tier = fetched[c["source_id"]]
        if SUPERSEDED_WORDS.search(text):
            return {"status": "superseded", "current": None, "domain": domain, "tier": tier, "claim_id": c.get("id"),
                    "line": f"Currency: {name} is superseded according to the vendor's own page fetched in this run ({domain}, tier {tier}; claim {c.get('id')})"}
        if CURRENT_WORDS.search(text):
            return {"status": "current", "current": None, "domain": domain, "tier": tier, "claim_id": c.get("id"),
                    "line": f"Currency: {name} checked against the vendor's own page fetched in this run ({domain}, tier {tier}; claim {c.get('id')} entails the current generation)"}
    pages = "; ".join(f"{d}, tier {t}" for d, t in list(dict.fromkeys(fetched.values()))[:3])
    return {"status": "unknown", "current": None, "line": f"Currency: {name} could not be checked (the vendor's own page fetched in this run carries no supported claim about its generation: {pages})"}


# ----------------------------------------------------------------------------- the leaderboards
def resolve_display_names(catalogue: list[dict], names: list[str]) -> dict:
    """The live catalogue's slug for each leaderboard display name (the catalogue's "name" field), or None when it names no model."""
    out = {}
    rows = [(str(m.get("name") or ""), str(m.get("id") or "")) for m in catalogue or [] if not str(m.get("id") or "").startswith("~")]
    for want in names:
        w = " ".join(want.lower().replace("-", " ").split())
        hit = next((mid for nm, mid in rows if " ".join(nm.lower().replace("-", " ").split()) == w), None)
        if hit is None:
            toks = set(w.split())
            scored = sorted(((len(toks & set(" ".join(nm.lower().replace("-", " ").split()).split())), -len(nm), mid) for nm, mid in rows), reverse=True)
            hit = scored[0][2] if scored and scored[0][0] >= max(2, len(toks) - 1) else None
        out[want] = hit
    return out


def run_date() -> str:
    """The run date from the system clock, for every research prompt."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
