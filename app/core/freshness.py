"""Research freshness policy. Exa serves cached page content by default; maxAgeHours sets the oldest acceptable
cache age (0 = always livecrawl, -1 = cache only, omitted = livecrawl only when no cache exists). If a livecrawl fails,
Exa may fall back to cached content (exa.ai/docs/reference/livecrawling-contents, checked Oct 2026). The runtime rule is POLICY:
0 for the volatile class, 168 hours for policy, technical and stable, livecrawlTimeout on every class;
config/settings.example.toml [research.max_age_hours] mirrors it (tested)."""
from datetime import datetime, timedelta, timezone

POLICY = {
    "volatile":  {"max_age_hours": 0,    "livecrawl_timeout_ms": 10000, "why": "prices, model availability, rate limits"},
    "policy":    {"max_age_hours": 168,  "livecrawl_timeout_ms": 10000, "why": "licences, privacy and retention terms"},
    "technical": {"max_age_hours": 168,  "livecrawl_timeout_ms": 10000, "why": "framework docs, release notes (Phase 9b, D-A52: at most 168 hours for every class but the live ones)"},
    "stable":    {"max_age_hours": 168,  "livecrawl_timeout_ms": 10000, "why": "concepts and protocols (Phase 9b: at most 168 hours, no longer the Exa default)"},
}
MAX_AGE_HOURS_CAP = 168      # a cached copy older than a week is never used; vendor pricing and documentation pages are fetched live (0)


def exa_content_options(freshness: str) -> dict:
    p = POLICY[freshness]
    opts = {"text": True}
    if p["max_age_hours"] is not None:
        opts["maxAgeHours"] = p["max_age_hours"]
        opts["livecrawlTimeout"] = p["livecrawl_timeout_ms"]
    return opts


def source_label(freshness: str, cache_status: str, retrieved_at: datetime, published_at: datetime | None, tier: int) -> dict:
    """What the interface shows, and whether the source may settle a volatile fact on its own."""
    day = retrieved_at.strftime("%d %b %Y")
    if cache_status == "livecrawl":
        fetched = f"Fetched live on {day}"
    elif cache_status == "cached":
        fetched = f"Cached copy, retrieved {day}"
    elif cache_status == "cache_fallback":
        fetched = f"Cached copy: the live fetch failed on {day}"
    else:
        fetched = f"Retrieved {day}; freshness not confirmed by the provider"
    dated = (f"published {published_at.strftime('%d %b %Y')}" if published_at
             else ("official page with no date shown" if tier == 1 else "undated secondary page"))
    live_ok = cache_status == "livecrawl" or (cache_status == "cached" and POLICY[freshness]["max_age_hours"] not in (0,))
    can_settle_volatile = (freshness != "volatile") or (cache_status == "livecrawl" and tier in (1, 2))
    if published_at is None and tier == 3:
        can_settle_volatile = False
    return {"label": f"{fetched}; {dated}", "can_settle_volatile": can_settle_volatile, "usable": live_ok or tier == 1,
            "warning": None if can_settle_volatile else "Check this fact before relying on it: the copy may be old or the page is undated."}


VOLATILE_CLAIM_HOURS = 24          # a volatile claim older than this is re-fetched or marked for checking before export
VOLATILE_CLAIM_KINDS = ("price", "availability")


def fresh_until(freshness: str, retrieved_at: datetime, kind: str | None = None) -> datetime | None:
    """When a claim taken from a source of this class stops counting as fresh: volatile facts 24 hours after the fetch,
    every other class at its maxAgeHours (168 hours for policy, technical and stable; no class is
    unbounded). The None branch stays for a policy entry without a window; POLICY carries none."""
    hours = POLICY[freshness]["max_age_hours"]
    if freshness == "volatile" or kind in VOLATILE_CLAIM_KINDS:
        return retrieved_at + timedelta(hours=VOLATILE_CLAIM_HOURS)
    if hours is None:
        return None
    return retrieved_at + timedelta(hours=hours)


def parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def claim_freshness(kind: str, freshness: str, cache_status: str, retrieved_at, published_at, tier: int) -> dict:
    """The freshness label of one claim from its source: the source label, whether this claim may
    settle a fact alone (a price or availability figure needs a live copy from an official or reputable page; a cached
    fallback or an undated secondary page is labelled and kept but never used alone), and fresh_until. An undated official
    page is labelled, not discarded."""
    ret = parse_ts(retrieved_at) or datetime.now(timezone.utc)
    pub = parse_ts(published_at)
    cls = freshness if freshness in POLICY else "technical"
    label = source_label(cls, cache_status or "unknown", ret, pub, int(tier or 3))
    from app.core.evidence import volatile_claim as _volatile, settles_alone as _settles   # one claim-kind rule (app/core/evidence.py); the source's class decides fresh_until only
    volatile_claim = _volatile(kind)
    settles_alone, why = _settles(kind, cache_status, pub, tier)
    until = fresh_until(cls, ret, kind)
    warning = None if settles_alone else why
    return {"label": label["label"], "settles_alone": settles_alone, "warning": warning,
            "fresh_until": until.isoformat() if until else None, "volatile": volatile_claim}
