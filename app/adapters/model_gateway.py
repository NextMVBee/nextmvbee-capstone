"""Cloud model gateway: OpenRouter only, call states, budget reservation, one repair, one cross-vendor fallback.
A step whose validated result is already stored is replayed without a new request; every send gets a fresh attempt
number; the reservation is computed from the messages actually sent (repairs included); a timeout after sending makes
the call unknown and stops. Tested offline against stub OpenAI-compatible servers on 127.0.0.1 (tests/test_audit.py, tests/test_concepts.py)."""
import json as _json
import re
import time
import openai
from openai import OpenAI
from pydantic import ValidationError
from app.core.ledger import BudgetLedger, estimate_model_usd, estimate_tokens, estimate_request_tokens, AllowanceUsed, DispatchRefused, Replay, InFlight, SEND_TIMEOUT_S, SendDeadline, send_within
from app.core.privacy import provider_prefs
from app.core import react
from app.runner.recovery import assert_run_active, cancel_run, RunStopped

OPENROUTER = "https://openrouter.ai/api/v1"


def load_settings(path: str | None = None) -> dict:
    """config/settings.toml (or NMB_SETTINGS): the authoritative model ids, prices and limits."""
    import os, tomllib
    with open(path or os.environ.get("NMB_SETTINGS", "config/settings.toml"), "rb") as f:
        return tomllib.load(f)


DEFAULT_TOOLS_CFG = {"observation_chars": 6000, "observation_total_chars": 18000, "context_chars": 60000}   # mirrored by config/settings.example.toml [tools]


def stage_of(step_key: str) -> str:
    """Step keys are <run>:<stage>:<worker>:<branch>[:more]; the stage names the [output] ceiling."""
    parts = step_key.split(":")
    return parts[1] if len(parts) > 1 else parts[0]


def output_ceiling(stage: str, settings: dict | None = None) -> int:
    """The output allowance of a stage, from settings [output]. Every send and every reservation use
    this figure; no send passes a literal max_tokens. An unknown stage is an error, never a guess."""
    table = (settings if settings is not None else load_settings()).get("output") or {}
    if stage not in table or stage == "max_bump":
        raise ValueError(f"no output ceiling for stage {stage!r} in settings [output]")
    return int(table[stage])


def reasoning_allowance(settings: dict | None = None) -> int:
    """Tokens added on top of every stage ceiling for the model's reasoning, which the provider counts against max_tokens
    ([output].reasoning_allowance). It is part of max_tokens and of the reservation, never bumped."""
    return int(((settings if settings is not None else load_settings()).get("output") or {}).get("reasoning_allowance", 0))


def max_bump(settings: dict | None = None) -> float:
    """The one allowed raise of a stage ceiling when a single passage still truncates after the split ([output].max_bump)."""
    return float(((settings if settings is not None else load_settings()).get("output") or {}).get("max_bump", 1.0))


def send_bound(stage: str, settings: dict | None = None, fallback: float | None = None) -> float:
    """The bound of one model send of a stage, in seconds of elapsed time: settings [send_timeout_s].<stage>, the
    stage as the step key and [output] name it; a stage without a key keeps the fallback (the gateway's own timeout, else the code's model
    bound SEND_TIMEOUT_S["model"]). A stage may need a longer bound than an ordinary send: one request of the comparison's panel check
    carries the provider's whole tool loop (several models that search and fetch inside the provider).
    A value that is not a positive number is an error, never a guess. The bound changes how long one send may take, nothing else: a send
    past it is an unknown call with its possible charge and is never resent on its own."""
    value = ((settings or {}).get("send_timeout_s") or {}).get(stage)
    if value is None:
        return float(fallback if fallback is not None else SEND_TIMEOUT_S["model"])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"settings [send_timeout_s].{stage} must be a positive number of seconds, not {value!r}")
    return float(value)


def provider_for(prefs: dict | None, server_tools=None) -> dict | None:
    """The provider preference of one send. A request that carries OpenRouter's server tools goes without require_parameters: the
    fusion tool was measured to answer HTTP 404 ("upstream returned 404") with it and 200 without it; data_collection and zdr stay as the privacy mode sets them. The schema is advisory under server
    tools in any case (the provider does not enforce response_format inside the loop), so require_parameters buys nothing there."""
    if not prefs:
        return prefs
    if not server_tools:
        return prefs
    return {k: v for k, v in prefs.items() if k != "require_parameters"}


RAW_KEEP = 200_000   # the characters of an invalid or truncated answer kept on its ledger row (raw_json); a plan draft is well inside it


def json_tail(content: str) -> str | None:
    """The JSON object a server-tool answer ends with. OpenRouter renders the fusion tool's panel text (headed "## Panel responses") into the
    assistant content before the model's own answer, and a model under the tool loop often fences its JSON; the last fenced block, else
    the last balanced top-level object, is the answer. None when there is none."""
    if not content or not content.strip():
        return None
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.S)
    if fences:
        return fences[-1]
    depth = 0; end = None
    for i in range(len(content) - 1, -1, -1):
        ch = content[i]
        if ch == "}":
            if end is None:
                end = i
            depth += 1
        elif ch == "{" and end is not None:
            depth -= 1
            if depth == 0:
                return content[i:end + 1]
    return None


def is_truncated(finish_reason, native_finish_reason=None) -> bool:
    """Provider truncation: finish_reason length, or the native equivalents OpenRouter passes through."""
    return any(str(x or "").lower() in ("length", "max_tokens", "max_output_tokens", "max_completion_tokens") for x in (finish_reason, native_finish_reason))


def compact_tool_results(msgs: list[dict], context_chars: int) -> int:
    """Above context_chars of tool-result characters in a stage transcript, the oldest tool results are replaced by their
    metadata and a pointer (source_id, source_version, range); the full content stays in tool_calls.result_json and is
    reachable by read with offset or query. Returns the number of results compacted."""
    import json
    total = sum(len(m.get("content") or "") for m in msgs if m.get("role") == "tool")
    done = 0
    for m in msgs:
        if total <= context_chars:
            break
        if m.get("role") != "tool":
            continue
        try:
            obj = json.loads(m.get("content") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(obj, dict) or obj.get("compacted") or "pages" not in obj:
            continue
        pointer = {"ok": obj.get("ok"), "tool_call_id": obj.get("tool_call_id"), "tool": obj.get("tool"), "compacted": True,
                   "pages": [{k: p.get(k) for k in ("source_id", "source_version", "title", "url", "range_start", "range_end", "chars_total")} for p in obj["pages"]],
                   "note": "content removed from context to stay within the limit; still stored: read with source_id, source_version and offset or query"}
        new = json.dumps(pointer)
        total -= len(m["content"]) - len(new); m["content"] = new; done += 1
    return done


def gateway_from_settings(settings: dict, ledger, api_key: str, privacy_mode: str | None = None, **kwargs) -> "CloudGateway":
    """Build the gateway from the pinned ids and prices in settings; the key comes from the environment, never from settings."""
    m = settings["models"]
    prices = {k: (float(v[0]), float(v[1])) for k, v in m["prices_per_million"].items()}
    for mid in (m["default"], m["fallback"]):
        if mid not in prices:
            raise ValueError(f"no price pinned for model {mid!r} in settings [models.prices_per_million]")
    if m["default"] == m["fallback"] or m["default"].split("/")[0] == m["fallback"].split("/")[0]:
        raise ValueError(f"fallback {m['fallback']!r} must be a different vendor from default {m['default']!r} (D-03, D-09)")
    kwargs.setdefault("timeout_s", SEND_TIMEOUT_S["model"])          # the model send's timeout, in code; a send past it is an unknown call
    return CloudGateway(api_key=api_key, default_model=m["default"], fallback_model=m["fallback"], prices=prices,
                        privacy_mode=privacy_mode or settings.get("privacy", {}).get("default_mode", "cloud"), ledger=ledger, settings=settings, **kwargs)


def _inline(schema: dict) -> dict:
    defs = schema.get("$defs", {})
    def r(n):
        if isinstance(n, dict):
            if "$ref" in n:
                return r(dict(defs[n["$ref"].split("/")[-1]]))
            return {k: r(v) for k, v in n.items() if k != "$defs"}
        return [r(x) for x in n] if isinstance(n, list) else n
    return r(schema)


def _shape_hint(content: str, err) -> str:
    """Structure of the first invalid object (its key names only, never values): tells a schema defect from a model omission."""
    import json
    try:
        data = json.loads(content)
    except Exception:
        return ""
    for x in err.errors():
        loc = list(x["loc"])
        if not loc:
            continue
        node, parent = data, None
        for part in loc[:-1]:
            parent = node
            node = node[part] if isinstance(node, (dict, list)) and (isinstance(part, int) or part in node) else None
            if node is None:
                break
        if isinstance(node, dict):
            return f"; keys present at {'/'.join(map(str, loc[:-1])) or 'root'}: {sorted(node.keys())}"
    return ""


STRICT_DROP = ("default", "title", "maxLength", "minLength", "maxItems", "examples", "$schema")   # not accepted by OpenAI strict mode, maxItems refused by the pinned Google model; pydantic still enforces them on the reply


def strict_schema(schema: dict) -> dict:
    """The JSON schema sent to the provider under json_schema strict: refs inlined, every object closed
    (additionalProperties false) with every property required, and keywords strict mode rejects removed. Optional fields
    keep their null alternative, so a model answers them with null instead of omitting them. The contract itself is
    unchanged: the reply is still validated by the pydantic model, with every length and default rule. The reason:
    OpenAI endpoints return HTTP 400 'Invalid schema for response_format' for the raw pydantic schema;
    Anthropic and Google accept both forms. Keywords are dropped only at schema-node level: the names inside
    "properties" are field names and are kept whatever they are (removing a field called "title" as a keyword
    would make every model fail plan_generate)."""
    def r(n):
        if isinstance(n, dict):
            out = {}
            for k, v in n.items():
                if k == "properties":
                    out[k] = {name: r(sub) for name, sub in v.items()}   # property NAMES are data, never keywords
                elif k not in STRICT_DROP:
                    out[k] = r(v)
            if out.get("type") == "object" or "properties" in out:
                props = out.get("properties", {})
                out["additionalProperties"] = False
                out["required"] = list(props.keys())
            return out
        return [r(x) for x in n] if isinstance(n, list) else n
    return r(_inline(schema))


def truncate_lists(contract, content: str) -> tuple[str, list[str]]:
    """Before validation, a list field longer than the schema's maxItems is cut to the limit (the first items kept) and the
    cut recorded; the JSON is returned as text again. Anything else is left for the validator."""
    import json as _json
    notes = []
    try:
        data = _json.loads(content)
    except ValueError:
        tail = json_tail(content)                                               # a plain-schema answer may come fenced; the fenced object is the answer
        try:
            data = _json.loads(tail) if tail else None
        except ValueError:
            data = None
        if data is None:
            return content, []
        notes.append("the answer's JSON was fenced; the fenced object was read")
    schema = contract.model_json_schema(); defs = schema.get("$defs", {})

    def walk(obj, sch, path):
        if not isinstance(sch, dict):
            return obj
        if "$ref" in sch:
            sch = defs.get(sch["$ref"].rsplit("/", 1)[-1], {})
        if "anyOf" in sch:
            for alt in sch["anyOf"]:
                if alt.get("type") != "null":
                    return walk(obj, alt, path)
        if sch.get("type") == "string" and isinstance(obj, str):
            limit = sch.get("maxLength")
            if isinstance(limit, int) and len(obj) > limit:                     # a free-text field over its limit is cut, not refused (for example, a critic's note of 700 characters would otherwise refuse the whole verdict)
                notes.append(f"{path}: {len(obj)} characters cut to the schema's limit of {limit}")
                return obj[:limit].rstrip()
            return obj
        if sch.get("type") == "object" and isinstance(obj, dict):
            for k, v in list(obj.items()):
                if k in sch.get("properties", {}):
                    obj[k] = walk(v, sch["properties"][k], f"{path}/{k}" if path else k)
        elif sch.get("type") == "array" and isinstance(obj, list):
            limit = sch.get("maxItems")
            if isinstance(limit, int) and len(obj) > limit:
                notes.append(f"{path}: {len(obj)} items truncated to the schema's limit of {limit}")
                del obj[limit:]
            for i, item in enumerate(obj):
                obj[i] = walk(item, sch.get("items", {}), f"{path}[{i}]")
        return obj
    data = walk(data, schema, "")
    return (_json.dumps(data, ensure_ascii=False) if notes else content), notes


_ILLEGAL_HEADER = re.compile(r"(?is)(illegal header (?:value|name)\s*).*")   # everything after the phrase is the header's representation, whatever its quoting or escaping
_HEADER_VALUE = re.compile(r"(?i)(\bbearer\s+)[^\s'\"]+|\bsk-[A-Za-z0-9_-]{6,}")


def _forms(secret: str) -> list[str]:
    """The forms a secret takes in an error message: itself, stripped, and the escaped text of its str repr, its bytes repr and
    its JSON dump (an apostrophe or a newline inside it is escaped by the transport's quoting), longest first."""
    base = {secret, secret.strip()}
    forms = set(base) | {repr(v)[1:-1] for v in base} | {repr(v.encode()).split("'", 1)[1][:-1] for v in base} | {_json.dumps(v)[1:-1] for v in base}
    return sorted((f for f in forms if f), key=len, reverse=True)


def redact(text: str, secrets: tuple = ()) -> str:
    """Error text without credentials: every
    known secret (the client's key, in every escaped form a repr or a JSON dump gives it), the whole representation httpx quotes after
    "Illegal header value" (the Authorization value, whatever its quote style or escaping: a single- or double-quoted bytes repr with an
    escaped apostrophe or newline) and every credential shape the transport's own messages carry (a Bearer token; a key shape) replaced by
    [redacted]. A bounded length is not a redaction."""
    out = text
    for sec in secrets:
        for form in _forms(sec or ""):
            out = out.replace(form, "[redacted]")
    out = _ILLEGAL_HEADER.sub(lambda m: m.group(1) + "[redacted]", out)
    return _HEADER_VALUE.sub(lambda m: (m.group(1) or "") + "[redacted]", out)


def unknown_cause(e: BaseException, secrets: tuple = ()) -> str:
    """What ended a send with an unknown outcome, as error text only (never the request, never a credential): the exception's class and
    bounded message, with the class and bounded message of its cause when it has one (the SDK wraps every transport error in
    APIConnectionError, so a refused connection, a failed name lookup and a closed socket all read 'Connection error.' without it), each
    message redacted of the client's key and of any credential shape (a key with a trailing newline reaches httpx's LocalProtocolError,
    whose text quotes the Authorization header). The decision record of the attempt carries this text, so a send that ends unknown
    has its cause on record."""
    c = e.__cause__ or e.__context__
    s = f"{type(e).__name__}: {redact(str(e), secrets)[:160]}"
    return s + (f" from {type(c).__name__}: {redact(str(c), secrets)[:160]}" if c is not None else "")


class CloudGateway:
    _after_claim = None   # test hook (class attribute): called after the dispatch claim and before the send

    def __init__(self, *, api_key, default_model, fallback_model, prices: dict, privacy_mode: str, ledger: BudgetLedger,
                 base_url=OPENROUTER, fallback_base_url=None, timeout_s=SEND_TIMEOUT_S["model"], temperature=None, settings=None):
        self.default_model, self.fallback_model, self.prices, self.ledger = default_model, fallback_model, prices, ledger
        self.timeout_s = timeout_s
        for stage in ((settings or {}).get("send_timeout_s") or {}):
            send_bound(stage, settings)          # a send bound of the settings that is not a positive number is refused when the gateway is built, before any send
        self.settings = settings                 # [output] ceilings and [tools] observation and context limits; None: ceilings must be resolved from config/settings.toml
        self.tools_cfg = dict(DEFAULT_TOOLS_CFG, **((settings or {}).get("tools") or {}))
        self.reasoning_effort = ((settings or {}).get("models") or {}).get("reasoning_effort") or None   # "none" keeps reasoning tokens out of the output allowance (OpenRouter reasoning.effort); None sends nothing
        self.provider = provider_prefs(privacy_mode)
        self.clients = {default_model: OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0),
                        fallback_model: OpenAI(base_url=fallback_base_url or base_url, api_key=api_key, timeout=timeout_s, max_retries=0)}
        self._client_args = dict(base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=0)   # a client for another routed model (the strongest in the price table) is built once when first asked
        self.temperature = temperature   # None by default: not sent. Under require_parameters every parameter sent must be supported by the
                                         # endpoint, and several current models (OpenAI's gpt-6 tiers, Anthropic's own endpoint) reject temperature;
                                         # NextMVBee's determinism comes from contracts, validators and routers, not from sampling settings.

    def client_for(self, model: str):
        """The SDK client of a routed model: the default's, the fallback's, or one built for a model of the price table; a model
        without a pinned price is refused before anything is sent."""
        if model not in self.prices:
            raise ValueError(f"no price pinned for model {model!r} in settings [models.prices_per_million]; nothing sent")
        if model not in self.clients:
            self.clients[model] = OpenAI(**self._client_args)
        return self.clients[model]

    def fallback_for(self, primary: str) -> str | None:
        """The fallback route of a send: a priced route of a different vendor from the model that actually sent the failed
        attempt: the configured fallback when its vendor differs, else the configured default, else the first other-vendor route of the
        price table; None when no route of another vendor is priced (then there is no fallback). The vendor compared is
        the actual sender's, so an OpenAI primary never falls back to a configured OpenAI fallback."""
        vendor = primary.split("/")[0]
        for m in (self.fallback_model, self.default_model, *self.prices):
            if m in self.prices and m.split("/")[0] != vendor:
                return m
        return None

    def send_bound(self, step_key: str) -> float:
        """The elapsed bound of one send of this step: the settings' value for the step's stage, else this gateway's timeout."""
        return send_bound(stage_of(step_key), self.settings, self.timeout_s)

    def ceiling(self, step_key: str, max_tokens: int | None = None, bump: float | None = None) -> int:
        """The output allowance of this send: output_ceiling(stage) from settings, raised at most by [output].max_bump when a
        caller bumps it (one single-passage re-extraction after a truncation). An explicit max_tokens is honoured only for
        callers outside the stage table (the offline test suites); application code passes none."""
        if max_tokens is None:
            max_tokens = output_ceiling(stage_of(step_key), self.settings)
            if bump:
                max_tokens = int(max_tokens * min(float(bump), max_bump(self.settings)))
            return int(max_tokens) + reasoning_allowance(self.settings)                     # the stage's answer allowance plus the reasoning allowance
        if bump:
            max_tokens = int(max_tokens * min(float(bump), max_bump(self.settings)))
        return int(max_tokens)

    def _transcript_sql(self, step_key, rounds, msgs) -> tuple:
        import json
        return ("INSERT OR REPLACE INTO stage_transcripts (step_key, run_id, round, messages_json) VALUES (?,?,?,?)", (step_key, self.ledger.run_id, rounds, json.dumps(msgs)))

    def _attempt(self, model, step_key, contract, messages, prompt_tokens_est, max_tokens, finishing, repair=None, tools=None, attempt_kind="first", transcript_round=None, allow_replay=True, extra_body=None, server_tools=None):
        msgs = list(messages)
        if repair:
            msgs += [{"role": "assistant", "content": repair["raw"]},            # the invalid output is already bounded by the stage ceiling; never cut silently
                     {"role": "user", "content": "That output failed validation. Fix only these problems and return the complete JSON:\n- " + "\n- ".join(repair["errors"])}]
        schema = strict_schema(contract.model_json_schema())
        sent_tokens = estimate_request_tokens(msgs, schema, (tools or []) + (server_tools or []))
        est = estimate_model_usd(max(prompt_tokens_est, sent_tokens), max_tokens, *self.prices[model])   # messages, schema and tool schemas; a caller's server-tool fees are in prompt_tokens_est (app/core/discovery.estimate)
        if server_tools:
            from app.core.discovery import request_estimate
            wire = request_estimate(extra_body, server_tools, sent_tokens, max_tokens, self.prices[model])   # the loop bound the request itself carries (max_tool_calls, the tool sizes), priced at the sending model's prices, primary or fallback alike
            if wire is not None:
                est = max(est, wire)
        bound = self.send_bound(step_key)                    # the stage's send bound from the settings, the gateway's timeout otherwise
        deadline = time.monotonic() + bound
        while True:
            try:
                entry = self.ledger.claim_dispatch(step_key, "model", est, finishing=finishing, attempt_kind=attempt_kind,
                                                   replay_completed=(attempt_kind == "first" and repair is None and allow_replay), exclusive=True)   # run, replay, in-flight, allowance, attempt, reservation: one transaction; a loop entered at its repair point never replays the answer it repairs
                break
            except DispatchRefused as e:
                raise RunStopped(str(e)) from e                # the run was cancelled before this dispatch was claimed: nothing is sent
            except AllowanceUsed as e:
                return {"kind": "allowance_used", "entry": None, "detail": str(e)}
            except Replay as r:                                # another caller completed this step meanwhile: replay, no reservation, no send
                return {"kind": "ok", "entry": r.entry_id, "output": contract.model_validate_json(r.result_json), "replayed": True}
            except InFlight:                                   # another caller's request is in flight: wait for its result, never send twice
                if time.monotonic() > deadline:
                    return {"kind": "unknown", "entry": None, "detail": "a request of this step is still in flight after the timeout"}
                time.sleep(0.05)
        if self._after_claim is not None:
            self._after_claim()                                # test hook: a pause between the claim and the send
        try:
            self.ledger.mark_sent(entry)                       # the stop check at the wire: a Stop that landed after the claim released the row; nothing is sent
        except DispatchRefused as e:
            raise RunStopped(str(e)) from e
        t0 = time.monotonic()
        label = contract.__name__

        def finish(transition, kind, observation, change, cost=None, result_json=None, extra=(), **meta):
            """The ledger transition and the decision summary of this attempt in one transaction; extra statements join it (the transcript that carries a tool request)."""
            sql = self.ledger.settle_sql(entry, cost, result_json=result_json, **meta) if transition == "settle" else self.ledger.fail_sql(entry, **meta)
            self.ledger.atomically(sql, react.record_sql(self.ledger.run_id, step_key, entry, f"stage call {label}", "model send", observation, change), *extra)
        try:
            resp = send_within(lambda: self.client_for(model).chat.completions.create(
                model=model, messages=msgs, max_tokens=max_tokens, timeout=bound, **({"temperature": self.temperature} if self.temperature is not None else {}),
                response_format={"type": "json_schema", "json_schema": {"name": contract.__name__, "strict": True, "schema": schema}},
                extra_body={"provider": provider_for(self.provider, server_tools), "usage": {"include": True}, **({"reasoning": {"effort": self.reasoning_effort}} if self.reasoning_effort else {}), **(extra_body or {})},   # usage accounting: OpenRouter returns usage.cost; reasoning effort from settings; a caller's session_id, max_tool_calls and stop conditions
                **({"tools": (tools or []) + (server_tools or []), **({"tool_choice": "auto"} if tools else {})} if (tools or server_tools) else {})), bound)   # the bound is the elapsed time of the send; the SDK's numeric timeout stays beside it (both take the stage's bound, so the client's inactivity timeout never cuts a send its stage's bound still allows); OpenRouter's server tools ride in the same array
        except (openai.APITimeoutError, openai.APIConnectionError, SendDeadline) as e:
            why = unknown_cause(e, (self._client_args.get("api_key"),))              # the client's key and any credential shape redacted before the record and the return
            finish("fail", "unknown", f"no result stored; call unknown ({why})", "founder decides", charged=None)
            return {"kind": "unknown", "entry": entry, "detail": why}
        except openai.APIStatusError as e:
            if e.status_code == 402:
                finish("fail", "credit", "provider refused: credits exhausted", "run stops", charged=False, provider_status=e.status_code)
                cancel_run(self.ledger.conn, self.ledger.run_id, "credit_exhausted")          # credits exhausted: the shared run stops, durably, for every worker
            elif server_tools:                                                               # the error came after the server-side tool loop ran (for example HTTP 400 after 10 to 27 seconds of loop, the steps billed outside the answer); the reserved worst case stays a possible charge until the account's usage or a provider record settles it; no automatic fallback
                finish("fail", "unknown", f"HTTP {e.status_code} after the server-tool loop; the loop's steps may be charged; call unknown", "founder decides (retry, continue or reconcile against the account's usage)", charged=None, provider_status=e.status_code)
                return {"kind": "unknown", "entry": entry, "status_code": e.status_code, "detail": f"HTTP {e.status_code} after the server-tool loop (a possible charge): {str(getattr(e, 'message', e))[:1500]}"}
            else:
                finish("fail", "provider_error", "provider error, no output", "fallback once", charged=False, provider_status=e.status_code)
            return {"kind": "credit" if e.status_code == 402 else "provider_error", "entry": entry, "status_code": e.status_code,
                    "detail": f"HTTP {e.status_code}: {str(getattr(e, 'message', e))[:1500]}"}   # provider message, never prompt text
        message = resp.choices[0].message
        content = message.content or ""
        usage = resp.usage
        cost = getattr(usage, "cost", None)
        if cost is None and getattr(usage, "model_extra", None):
            cost = usage.model_extra.get("cost")
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None) if details is not None and not isinstance(details, dict) else (details or {}).get("reasoning_tokens") if details else None
        meta = dict(model_id=model, provider="openrouter", generation_id=getattr(resp, "id", None), tokens_in=usage.prompt_tokens,
                    tokens_out=usage.completion_tokens, latency_ms=int((time.monotonic() - t0) * 1000), reasoning_tokens=reasoning_tokens)   # reasoning tokens are part of tokens_out and of the allowance
        choice = resp.choices[0]
        native = getattr(choice, "native_finish_reason", None) or (getattr(choice, "model_extra", None) or {}).get("native_finish_reason")
        paid = float(cost) if isinstance(cost, (int, float)) else None
        if is_truncated(choice.finish_reason, native):               # the provider stopped the output at the ceiling: its own outcome, never a validation failure
            finish("settle", "truncated", f"output stopped at the ceiling ({max_tokens} tokens)", "split the unit, bump the ceiling once or needs_review; no identical retry", cost=paid, outcome="truncated", raw_json=content[:RAW_KEEP], **meta)   # typed outcome and summary, durable with the settlement; the text kept
            return {"kind": "truncated", "entry": entry, "raw": content, "tokens_out": usage.completion_tokens, "max_tokens": max_tokens,
                    "detail": f"output stopped at the ceiling of {max_tokens} tokens (finish_reason={choice.finish_reason}, tokens_out={usage.completion_tokens})"}
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments} for tc in tool_calls]
            assistant = {"role": "assistant", "content": content or None, "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in calls]}
            extra = [self._transcript_sql(step_key, transcript_round, msgs + [assistant])] if transcript_round is not None else []   # the assistant's tool request is durable with the row that settles it, so a crash before the tool runs resumes with the request and replays it through the transcript, never resending the settled call
            finish("settle", "tool_calls", f"{len(tool_calls)} tool request(s)", "tools executed next", cost=paid, outcome="tool_calls", extra=extra, **meta)
            return {"kind": "tool_calls", "entry": entry, "calls": calls, "assistant": assistant}
        tail_note = ""
        if server_tools and content.strip() and not content.lstrip().startswith("{"):           # under the server tools the schema is advisory and the content can carry the tool's rendered text before the answer
            tail = json_tail(content)
            if tail:
                content, tail_note = tail, "; the JSON taken from the tail of the server-tool answer"
        try:
            content, cuts = truncate_lists(contract, content)                                   # an over-long list is cut to the schema's limit and recorded, never failed
            parsed = contract.model_validate_json(content)
            gone = [("DELETE FROM stage_transcripts WHERE step_key=?", (step_key,))] if transcript_round is not None else []   # the tool loop's transcript is deleted in the transaction that settles the schema-valid answer, so no durable state holds a settled answer beside the transcript it closed and a resume after this commit builds the application's repair from the stored source as the uninterrupted repair does
            finish("settle", "ok", "contract satisfied" + (f"; {len(cuts)} list(s) truncated to the schema's limit" if cuts else "") + tail_note, "output stored", cost=paid, result_json=content, outcome="ok", extra=gone, **meta)   # durable before anything else
            if cuts:
                react.record(self.ledger.conn, self.ledger.run_id, step_key, entry, f"stage call {label}", "list truncated (D-A43)", "; ".join(cuts)[:300], "the first items kept; the answer accepted")
            raw_ann = getattr(message, "annotations", None) or ((getattr(message, "model_extra", None) or {}).get("annotations"))   # the url_citation annotations of a server-tool search (the URLs the engine read)
            annotations = []
            for a in raw_ann or []:
                d = a if isinstance(a, dict) else (a.model_dump() if hasattr(a, "model_dump") else {})
                uc = d.get("url_citation") or {}
                if uc.get("url"):
                    annotations.append({"url": uc.get("url"), "title": (uc.get("title") or "")[:200]})
            served_tool_use = getattr(usage, "server_tool_use", None) or ((getattr(usage, "model_extra", None) or {}).get("server_tool_use"))
            return {"kind": "ok", "entry": entry, "output": parsed, "annotations": annotations, "cost_usd": paid, "server_tool_use": served_tool_use if isinstance(served_tool_use, dict) else None}
        except ValidationError as e:
            finish("settle", "invalid", "output failed validation", "repair or flag", cost=paid, outcome="invalid", raw_json=content[:RAW_KEEP], **meta)   # the answer text is kept with the settlement, so a stage that stops before the repair (the one permitted draft after a time stop) never loses the paid answer
            errors = [f"{'/'.join(map(str, x['loc'])) or 'root'}: {x['msg']}" for x in e.errors()]
            return {"kind": "invalid", "entry": entry, "raw": content, "errors": errors,
                    "detail": ("; ".join(errors)[:300]) + f" (finish_reason={resp.choices[0].finish_reason}, tokens_out={usage.completion_tokens}){_shape_hint(content, e)}"}

    def _policy(self, step_key, contract, messages, prompt_tokens_est, max_tokens, finishing, tools=None, sends_left=None, before_dispatch=None, transcript_round=None, first_kind="first", first_model=None, allow_replay=True, model=None, extra_body=None, server_tools=None):
        """One bounded attempt policy for plain and tool-enabled calls: first send, then one repair on invalid output,
        or one cross-vendor fallback on a provider error; 402 and unknown stop. Each send consumes one allowance; the
        caller's before_dispatch (the shared deadline) runs before every actual attempt, schema repairs and fallbacks included.
        The policy can be entered at its repair point: first_kind "repair" (with first_model, the model that answered)
        dispatches the owed repair as the first send under the stage's one repair allowance, with the tools when given,
        and ends there as the uninterrupted repair does (no second repair, no fallback after a repair)."""
        def send(model, repair=None, attempt_kind="first"):
            if sends_left is not None and sends_left[0] <= 0:
                return {"kind": "round_limit"}
            if before_dispatch is not None:
                before_dispatch(attempt_kind)
            if sends_left is not None:
                sends_left[0] -= 1
            return self._attempt(model, step_key, contract, messages, prompt_tokens_est, max_tokens, finishing, repair=repair, tools=tools, attempt_kind=attempt_kind, transcript_round=transcript_round, allow_replay=allow_replay, extra_body=extra_body,
                                 server_tools=None if repair else server_tools)   # its summary is written with its ledger transition; a repair reformats the answer already given and never re-runs the server-side tool loop (its cost and its context)
        if first_kind == "repair":
            a = send(first_model or self.default_model, attempt_kind="repair")
            if a["kind"] == "allowance_used":
                return {"kind": "failed", "entry": None, "detail": a["detail"]}
            return a if a["kind"] in ("ok", "tool_calls", "unknown", "credit", "round_limit", "stopped", "truncated") else {"kind": "failed", "entry": a.get("entry"), "detail": a.get("detail")}
        primary = model or self.default_model                                   # the stage's route (the strongest routed model for the coverage, comparison and enrichment calls); the default otherwise
        other = self.fallback_for(primary)                                     # a priced route of another vendor than the actual primary, or none
        a = send(primary)
        if a["kind"] in ("ok", "tool_calls", "unknown", "credit", "round_limit", "stopped", "truncated"):
            return a                                                               # truncated: the caller splits, bumps or marks needs_review; never an identical retry here
        if a["kind"] == "invalid":
            b = send(primary, repair=a, attempt_kind="repair")                   # the claim itself enforces one repair per stage instance
            if b["kind"] == "allowance_used":
                return {"kind": "failed", "entry": a.get("entry"), "detail": f"{a.get('detail')} | {b['detail']}"}
            return b if b["kind"] in ("ok", "tool_calls", "unknown", "credit", "round_limit", "stopped", "truncated") else {"kind": "failed", "entry": b.get("entry"), "detail": b.get("detail")}
        if other is None:                                                        # no priced route of another vendor: no fallback, the provider error stands
            return dict(a, detail=f"{a.get('detail')} | no fallback: no priced route of another vendor than {primary.split('/')[0]} (D-A106)")
        b = send(other, attempt_kind="fallback")                                # provider error: one fallback, the other vendor, enforced by the claim
        if b["kind"] == "allowance_used":
            return {"kind": "failed", "entry": a.get("entry"), "detail": f"{a.get('detail')} | {b['detail']}"}
        if b["kind"] == "ok":
            return dict(b, fallback=True, detail=a.get("detail"))
        if b["kind"] == "tool_calls":
            return dict(b, fallback=True, detail=a.get("detail"))
        return b if b["kind"] in ("unknown", "credit", "round_limit", "stopped", "truncated") else {"kind": "failed", "entry": b.get("entry"), "detail": f"{a.get('detail')} | fallback: {b.get('detail')}"}

    def call(self, step_key, contract, messages, prompt_tokens_est, max_tokens=None, finishing=False, bump=None, max_rounds=None, before_dispatch=None, model=None, extra_body=None, server_tools=None) -> dict:
        """One stage call. max_tokens is resolved from output_ceiling(stage_of(step_key)) (settings [output]); bump raises
        it once within [output].max_bump. max_rounds is the stage instance's send allowance: the sends already made count,
        and none is left means round_limit without a send. before_dispatch runs before every actual attempt. Status
        truncated is the provider's output limit: the caller never retries it identically."""
        prior = self.ledger.completed_result(step_key, "model")
        if prior:                                                     # replay: never pay twice for a stored result
            return {"status": "replayed", "output": contract.model_validate_json(prior[1]), "attempts": 0, "entry": prior[0]}
        skipped = self.ledger.skipped(step_key)
        if skipped:                                                   # the founder continued without this interrupted call: nothing is sent, the stage proceeds without its output
            return {"status": "skipped", "entry": skipped, "attempts": 0, "detail": f"{skipped} was interrupted and the founder continued without it; no send"}
        max_tokens = self.ceiling(step_key, max_tokens, bump)
        before = self.ledger.next_attempt(step_key, "model") - 1
        sends_left = [max_rounds - self.ledger.sends_made(step_key)] if max_rounds is not None else None   # the allowance counts sends with persisted evidence
        if sends_left is not None and sends_left[0] <= 0:
            return {"status": "round_limit", "entry": None, "attempts": 0, "detail": f"send allowance of {max_rounds} used"}
        a = self._policy(step_key, contract, messages, prompt_tokens_est, max_tokens, finishing, sends_left=sends_left, before_dispatch=before_dispatch, model=model, extra_body=extra_body, server_tools=server_tools)
        attempts = self.ledger.next_attempt(step_key, "model") - 1 - before
        if a["kind"] == "ok":
            if a.get("replayed"):
                return {"status": "replayed", "output": a["output"], "attempts": 0, "entry": a.get("entry")}
            return {"status": "fallback" if a.get("fallback") else ("repaired" if attempts > 1 else "ok"), "output": a["output"], "attempts": attempts, "detail": a.get("detail"), "entry": a.get("entry"),
                    "annotations": a.get("annotations") or [], "cost_usd": a.get("cost_usd"), "server_tool_use": a.get("server_tool_use")}
        out = {"status": a["kind"], "entry": a.get("entry"), "attempts": attempts, "detail": a.get("detail")}
        if a["kind"] == "truncated":
            out.update(tokens_out=a.get("tokens_out"), max_tokens=a.get("max_tokens"))
        return out

    def repair(self, step_key, contract, messages, prompt_tokens_est, max_tokens=None, finishing=False, bump=None, max_rounds=None, model=None, before_dispatch=None, tools=None, runner=None) -> dict:
        """The one repair of a stage instance, used by a caller for a failure shape (a valid answer that refuses the task):
        the same durable allowance as the schema repair (attempt_kind repair under the same step key), so schema failures
        and failure shapes share it and a second repair of either kind is refused (allowance_used). The repair goes to
        the model that produced the failed answer (the step's latest completed attempt), never the default vendor after a
        fallback, and it respects the stage's send allowance: with max_rounds the sends already made count (round_limit
        when none is left). The repaired answer becomes the step's latest stored result and replays from then on."""
        if max_rounds is not None:
            used = self.ledger.sends_made(step_key)                                      # sends with persisted evidence, never attempts released unsent
            if used >= max_rounds:
                return {"status": "round_limit", "entry": None, "attempts": 0, "detail": f"send allowance of {max_rounds} used; no repair send"}
        if model is None:
            row = self.ledger.conn.execute("SELECT model_id FROM usage_ledger WHERE run_id=? AND step_key=? AND kind='model' AND state='completed' ORDER BY attempt DESC LIMIT 1", (self.ledger.run_id, step_key)).fetchone()
            model = row[0] if row and row[0] in self.clients else self.default_model
        if tools is not None:                                                            # a repair of a tool-enabled stage runs inside the one tool-loop policy, entered at its repair point, with the tools offered and the transcript kept
            return self.call_with_tools(step_key, contract, messages, tools, runner, prompt_tokens_est, max_tokens=max_tokens, finishing=finishing, max_rounds=max_rounds, bump=bump, before_dispatch=before_dispatch, first_kind="repair", first_model=model)
        max_tokens = self.ceiling(step_key, max_tokens, bump)
        if before_dispatch is not None:
            before_dispatch("repair")
        a = self._attempt(model, step_key, contract, messages, prompt_tokens_est, max_tokens, finishing, attempt_kind="repair")
        if a["kind"] == "ok":
            return {"status": "repaired", "output": a["output"], "attempts": 1, "entry": a.get("entry")}
        return {"status": a["kind"], "entry": a.get("entry"), "attempts": 1, "detail": a.get("detail")}

    def _save_transcript(self, step_key, rounds, msgs):
        import json
        with self.ledger.conn:
            self.ledger.conn.execute("INSERT OR REPLACE INTO stage_transcripts (step_key, run_id, round, messages_json) VALUES (?,?,?,?)", (step_key, self.ledger.run_id, rounds, json.dumps(msgs)))

    def call_with_tools(self, step_key, contract, messages, tools, runner, prompt_tokens_est, max_tokens=None, finishing=False, max_rounds=None, bump=None, before_dispatch=None, first_kind="first", first_model=None) -> dict:
        """Bounded decision-action-observation loop. The model may request tools from the allowlist; the ToolRunner
        validates, accounts, executes and stores each request; rejected requests return to the model as errors; ask_user
        pauses the run. The transcript is persisted after every round and replayed after an interruption. Every send,
        repair or fallback consumes one of max_rounds sends."""
        import json
        from app.core.tools import ToolRejected, UnknownToolPending, Paused, observation_for_model
        prior = self.ledger.completed_result(step_key, "model")
        if prior and first_kind == "first":                                   # a repair is asked because the stored answer is unusable to its caller; it is not replayed
            return {"status": "replayed", "output": contract.model_validate_json(prior[1]), "rounds": 0, "tool_calls": 0}
        skipped = self.ledger.skipped(step_key)
        if skipped and first_kind == "first":                                 # the founder continued without the interrupted send of this stage: nothing is sent
            return {"status": "skipped", "rounds": 0, "tool_calls": 0, "entry": skipped, "detail": f"{skipped} was interrupted and the founder continued without it; no send"}
        max_tokens = self.ceiling(step_key, max_tokens, bump)                 # bump: the one raise within [output].max_bump after a truncated final answer (the transcript resumes)
        cfg = self.tools_cfg
        saved = self.ledger.conn.execute("SELECT round, messages_json FROM stage_transcripts WHERE step_key=?", (step_key,)).fetchone()
        msgs = json.loads(saved[1]) if saved else list(messages); rounds = saved[0] if saved else 0
        repair_sent_turn = saved is not None and self.ledger.conn.execute("SELECT 1 FROM usage_ledger WHERE run_id=? AND step_key=? AND kind='model' AND attempt_kind='repair' AND state='completed' AND outcome='tool_calls' LIMIT 1", (self.ledger.run_id, step_key)).fetchone() is not None
        owed_repair = first_kind == "repair" and not repair_sent_turn             # entered at the repair point, the first send is the owed repair whatever the saved transcript's round, unless the transcript continues a repair tool turn already sent (then the loop continues it as first sends); the claim's allowance decides a repair already used
        if saved and owed_repair:
            msgs += [m for m in messages if m not in msgs]                        # the caller's repair input (the repair turn) joins the saved transcript instead of being lost to it
        used = self.ledger.sends_made(step_key)                                 # every send, repair and fallback already sent (persisted evidence), across interruptions
        sends_left = [(max_rounds or (runner.stage_limit + 1)) - used]
        tool_count = self.ledger.conn.execute("SELECT COUNT(*) FROM tool_calls WHERE step_key=? AND state='completed'", (step_key,)).fetchone()[0]

        def process(calls):
            """Run the model's tool requests through the runner; returns a pause result or None."""
            nonlocal tool_count
            last_assistant = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("tool_calls")), default=-1)
            answered = {m["tool_call_id"] for m in msgs[last_assistant + 1:] if m.get("role") == "tool"}   # results already attached to this assistant turn
            for c in calls:
                if c["id"] in answered:
                    continue                                                   # already has a result in the transcript
                try:
                    args = json.loads(c["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": c["arguments"]}
                if before_dispatch is not None and not runner.is_replay({"tool": c["name"], "args": args}):
                    before_dispatch("tool")                                              # the caller's shared deadline, before every new external dispatch; a replay sends nothing and is not checked
                try:
                    obs = runner.handle({"tool": c["name"], "args": args}); tool_count += 1
                    react.record(self.ledger.conn, self.ledger.run_id, step_key, obs["tool_call_id"], f"use {c['name']}", f"tool:{c['name']}", obs["summary"], "observation added to context")
                    rendered = observation_for_model(obs, cfg["observation_chars"], cfg["observation_total_chars"])   # structured, always valid JSON, bounded per page and per observation; the full result stays in tool_calls.result_json
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(rendered)})
                    n_compacted = compact_tool_results(msgs, cfg["context_chars"])
                    if n_compacted:
                        react.record(self.ledger.conn, self.ledger.run_id, step_key, obs["tool_call_id"], "keep the stage context within tools.context_chars", "compact oldest tool results",
                                     f"{n_compacted} older result(s) replaced by metadata and pointers", "full content stays stored and reachable by read")
                except Paused as p:
                    react.record(self.ledger.conn, self.ledger.run_id, step_key, p.tool_call_id, f"use {c['name']}", "tool:ask_user", "question stored; run paused", "waits for the founder")
                    self._save_transcript(step_key, rounds, msgs)
                    return {"status": "paused", "question_id": p.question_id, "tool_call_id": p.tool_call_id, "rounds": rounds, "tool_calls": tool_count}
                except UnknownToolPending as e:                                  # an interrupted request of this stage awaits the founder: the loop ends as unknown, the model is not asked to retry
                    self._save_transcript(step_key, rounds, msgs)
                    return {"status": "unknown", "rounds": rounds, "tool_calls": tool_count, "detail": str(e)[:300], "tool_call_id": e.tool_call_id}
                except ToolRejected as e:
                    react.record(self.ledger.conn, self.ledger.run_id, step_key, e.tool_call_id or f"{step_key}:rejected", f"use {c['name']}", f"tool:{c['name']}", f"rejected: {e}"[:300], "model told to continue without it")
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps({"ok": False, "error": str(e)})})
            self._save_transcript(step_key, rounds, msgs)
            return None

        last_assistant = max((i for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("tool_calls")), default=-1)
        if last_assistant >= 0:                                                  # resumed: finish the requests of the last turn that have no result yet
            done = {m["tool_call_id"] for m in msgs[last_assistant + 1:] if m.get("role") == "tool"}
            pending = [{"id": tc["id"], "name": tc["function"]["name"], "arguments": tc["function"]["arguments"]} for tc in msgs[last_assistant]["tool_calls"] if tc["id"] not in done]
            if pending:
                paused = process(pending)
                if paused:
                    return paused                                                     # paused, or unknown (an interrupted request awaits the founder)
        if sends_left[0] <= 0:                                                   # a stage at its limit sends nothing further (no transcript is written for a loop that ends here; nothing reads it)
            return {"status": "round_limit", "rounds": rounds, "tool_calls": tool_count}
        used_fallback, fallback_detail = False, None
        while sends_left[0] > 0:
            rounds += 1
            a = self._policy(step_key, contract, msgs, prompt_tokens_est, max_tokens, finishing, tools=tools, sends_left=sends_left, before_dispatch=before_dispatch, transcript_round=rounds,
                             first_kind="repair" if owed_repair else "first", first_model=first_model, allow_replay=(first_kind == "first"))   # the round the transcript of a tool request is saved under; the re-entered repair is the newly owed repair send whatever the saved round, the continuation of a sent repair tool turn is a first send, and none of its rounds replays the answer under repair
            owed_repair = False                                                   # the owed repair was dispatched (or refused by its allowance): every later round continues as a first send
            if a["kind"] == "tool_calls":
                if a.get("fallback"):
                    used_fallback, fallback_detail = True, a.get("detail")
                msgs.append(a["assistant"])
                paused = process(a["calls"])
                if paused:
                    return paused
                continue
            if a["kind"] == "ok":                                                     # the transcript was deleted with the settlement of the answer (the attempt's own transaction); a replayed answer of another caller settled the same key the same way
                return {"status": "fallback" if (a.get("fallback") or used_fallback) else "ok", "output": a["output"], "rounds": rounds, "tool_calls": tool_count, "detail": a.get("detail") or fallback_detail}
            if a["kind"] not in ("failed", "credit", "round_limit"):                   # the transcript is kept only for an ending a resume continues from (truncated: the bump; unknown: the founder's retry); a settled failure, a credit stop or the round limit writes none
                self._save_transcript(step_key, rounds, msgs)
            elif a["kind"] == "failed":
                with self.ledger.conn:
                    self.ledger.conn.execute("DELETE FROM stage_transcripts WHERE step_key=?", (step_key,))   # a loop settled failed leaves no earlier tool-turn transcript either (nothing resumes from it)
            return {"status": a["kind"], "rounds": rounds, "tool_calls": tool_count, "detail": a.get("detail"), "entry": a.get("entry"),   # the ledger entry of the attempt that ended the loop (truncated, invalid, unknown ...)
                    **({"tokens_out": a.get("tokens_out"), "max_tokens": a.get("max_tokens")} if a["kind"] == "truncated" else {})}
        return {"status": "round_limit", "rounds": rounds, "tool_calls": tool_count}
