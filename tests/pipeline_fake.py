"""The pipeline fake: the stub model (an OpenAI-compatible HTTP server on 127.0.0.1 scripted per
contract), the simulated research adapter, the synthetic fixture text and the helpers that build a project and a Runner on them.
Shared by the offline suites. No network, no keys, synthetic text only; nothing here is a product path."""
import http.server, json, pathlib, re, sqlite3, sys, tempfile, threading, copy
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from app.adapters.model_gateway import CloudGateway, load_settings
from app.core.ledger import BudgetLedger
from app.repo import Repo, connect
from app.runner.runner import Runner
from schemas import models as M

tmp = pathlib.Path(tempfile.mkdtemp())
S = copy.deepcopy(load_settings(str(ROOT / "config/settings.example.toml"))); S["runner"]["heartbeat_seconds"] = 0.05
S["extraction"]["coverage_step"] = False; S["extraction"]["enrichment"] = False   # the coverage call and the enrichment calls are off by default (scenarios assert their send counts); a suite that tests them turns them on
S["extraction"]["editor"] = False                                                 # the Editor's call is off by default too; a suite that tests it turns it on
S["discovery"]["engines"] = []; S["discovery"]["panel"] = False; S["discovery"]["currency"] = False   # the engines, the panel and the currency check are off against the stub (the live adapter is simulated); a suite that tests them turns them on
S["models"]["strong_stages"] = []                                                  # the stub answers the default route
S["tree"]["max_tree_decisions_per_run"] = 2                                        # the shipped default is 0 (every decision takes the simple comparison); the tree scenarios (tests/test_concepts.py, tests/test_tree_prune.py, tests/test_reuse_keys.py) run with two tree decisions per run

# ---------- fixture (synthetic; public data only) ----------
IDEA = "Multilingual meeting assistant for small teams: transcribes Hebrew and English calls and emails a summary."
FACT_PRICE = "Price per hour: $0.60 per audio hour."
FACT_LABELS = "Speaker labels: English and Spanish only."
FACT_LANGS = "Languages: 42 languages including Hebrew and English."
FACT_LOCAL = "Processing: runs fully on the device; no network access is required."
def filler(n, seed=0):
    out, i = [], seed
    while sum(len(s) for s in out) < n:
        out.append(f"Section {i} of the documentation describes a general capability without any figure. "); i += 1
    return "".join(out)[:n]
PAGE_TEXT = "Speech API A v3 documentation (September 2026). " + filler(6500, 1) + " " + FACT_PRICE + " " + FACT_LABELS + " " + FACT_LANGS + " " + filler(300, 900)
PAGE_URL = "https://docs.speech-a.example/pricing"
class Sim:
    """search answers every query with the fixture page, reported as a live fetch as the live adapter does for a confirmed crawl (volatile facts of unknown fetch status are withheld); queries naming a vendor in `empty` return nothing; `per_query` gives each query its own page."""
    simulated = True
    def __init__(self, empty=(), per_query=False, local=False, distinct=False): self.calls, self.empty, self.per_query, self.local, self.distinct = [], tuple(empty), per_query, local, distinct   # distinct: each query's page has its own body, the speech facts only on a speech page
    def page_text(self): return PAGE_TEXT + (" " + FACT_LOCAL if self.local else "")       # the local-only product's page states on-device processing
    def search(self, q, f, n):
        self.calls.append(("search", q))
        if any(w in q.lower() for w in self.empty):
            return []
        if self.per_query:
            slug = "".join(ch if ch.isalnum() else "-" for ch in q.lower())[:40]
            body = (filler(2500, seed=sum(ord(ch) for ch in q) % 997) + (" " + FACT_PRICE + " " + FACT_LABELS + " " + FACT_LANGS if "speech" in q.lower() else "")) if self.distinct else PAGE_TEXT
            return [{"url": f"https://docs.example/{slug}", "title": q[:40], "text": f"{q} documentation. " + body, "cache_status": "livecrawl"}]
        return [{"url": PAGE_URL, "title": "Speech API A pricing", "text": self.page_text(), "cache_status": "livecrawl"}]
    def contents(self, urls, f): self.calls.append(("contents", urls[0])); return [{"url": urls[0], "title": "Speech API A pricing", "text": self.page_text(), "cache_status": "livecrawl"}]

class ModelStub:
    def __init__(self, handler):
        self.bodies, self.handler = [], handler; outer = self
        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); outer.bodies.append(body)
                item = outer.handler(body)
                if schema_of(body) == "ComparisonOutV2" and item.get("content"):                     # a stub's version-1 comparison reply is answered in the memo shape the current contract takes
                    try:
                        v1 = json.loads(item["content"])
                    except ValueError:
                        v1 = None
                    if isinstance(v1, dict) and "recommended" in v1:
                        item = dict(item, content=json.dumps(memo_from_v1(v1)))
                if "status" in item:
                    raw = json.dumps({"error": {"message": "stub refusal", "code": item["status"]}}).encode()
                    self.reply(item["status"], raw); return
                msg = {"role": "assistant", "content": item.get("content")}
                if item.get("tool_calls"):
                    msg["tool_calls"] = [{"id": f"call{len(outer.bodies)}_{k}", "type": "function", "function": {"name": t, "arguments": json.dumps(a)}} for k, (t, a) in enumerate(item["tool_calls"])]
                payload = {"id": f"gen-{len(outer.bodies)}", "object": "chat.completion", "created": 0, "model": body["model"], "choices": [{"index": 0, "finish_reason": item.get("finish_reason", "stop"), "message": msg}],
                           "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60, "cost": 0.001, "completion_tokens_details": {"reasoning_tokens": 7}}}
                self.reply(200, json.dumps(payload).encode())
            def reply(self, status, raw):
                try:
                    self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass                                                                   # the client gave up on this reply (its timeout: the unknown-call scenarios); the fake has nothing to report
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"

def schema_of(body): return body["response_format"]["json_schema"]["name"]
def memo_from_v1(v1: dict) -> dict:
    """The memo shape (ComparisonOutV2) of a version-1 ComparisonOut reply, so a stub that answers in the version-1 shape keeps its scenario."""
    if v1.get("failure_reason"):
        return {"decision_subject": v1.get("decision_subject", ""), "recommendation": None, "runner_up": None, "why": [], "options": [], "change_mind": [], "open": [], "failure_reason": v1["failure_reason"]}
    rec = next((o for o in v1.get("options", []) if o.get("option") == v1.get("recommended")), None)
    options = [{"option": o.get("option", "")[:120], "product_id": o.get("product_id", "") or "", "product_version": o.get("product_version", "") or "", "fit": "; ".join(o.get("strengths", []))[:300], "price": "", "monthly_cost": "",
                "risk": "; ".join(o.get("risks", []))[:300], "verdict": "rejected" if not o.get("meets_hard_constraints", True) else ("recommended" if (rec and o.get("option") == rec.get("option")) else "watch"), "claim_ids": list(o.get("claim_ids", []))[:12]} for o in v1.get("options", [])[:6]]
    return {"decision_subject": v1.get("decision_subject", ""), "recommendation": ({"option": rec["option"][:120], "product_id": rec.get("product_id", "") or "", "product_version": rec.get("product_version", "") or ""} if rec else None), "runner_up": None,
            "why": ([{"text": (v1.get("rationale") or "")[:400], "claim_ids": list(rec.get("claim_ids", []))[:10]}] if rec else []), "options": options, "change_mind": [], "open": [str(x)[:300] for x in v1.get("unresolved", [])][:6]}
def user_text(body): return next((m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), "")
def subject_ids_of(u): return re.findall(r"^- (\S+) \| ", u, flags=re.M)                  # the subject list of the send, as the v4 prompt lists it
def claims_listed(u): return re.findall(r"^Claim (\S+) \((\w+)\): (.*)$", u, flags=re.M)   # the claims of a verification group, as the v3 prompt lists them
def all_user_text(body): return "\n".join(m["content"] for m in body["messages"] if m["role"] == "user")   # a repair turn is a later user message; the claims and the subject list stay in the first
def subjects_for_claim(u, attribute, claim_text):
    """The subject ids a claim names, as a careful model would: a requirement by the attribute's meaning (a
    price bears on the budget; languages and speaker labels on the languages requirement) and a research question whose key
    facts share a word with the attribute, never a question that names a vendor absent from the claim."""
    words = set(attribute.lower().replace("_", " ").split()) - {"per"}
    req_map = {"price": "budget_monthly_usd", "monthly": "budget_monthly_usd", "cost": "budget_monthly_usd", "languages": "languages", "speaker": "languages", "labels": "languages", "processing": "processing"}
    out = []
    for sid, kind, label, facts in re.findall(r"^- (\S+) \| (\w+) \| class \d+ \| evidence \w+ \| processing \w+ \| ([^:]+): (.*)$", u, flags=re.M):
        if kind == "requirement":
            if any(req_map.get(w) == label for w in words):
                out.append(sid)
            continue
        if set(re.findall(r"\b[A-Z][a-z]+", facts)) - set(re.findall(r"\b[A-Z][a-z]+", claim_text)):
            continue
        if label != "transcription":                                                        # the claims of these stubs are about the transcription decision; a question of another decision subject is not theirs
            continue
        if words & set(facts.lower().replace("_", " ").split()):
            out.append(sid)
    return out
def SUBJ(decision_subject, facts): return [{"id": "P1.RQ-test", "type": "research_question", "label": decision_subject, "key_facts": "; ".join(facts), "hard": False, "priority": 2, "evidence_status": "pending", "processing_status": "not_started"}]
def tool_msgs(body): return [json.loads(m["content"]) for m in body["messages"] if m["role"] == "tool"]
def claims_shown(u):
    """The claim lines one comparison prompt carries: the v7 evidence packs' lines (every line with its product id and
    version, a contradicting one with its contradicts field), or the v6 verified claims of an older prompt text."""
    line = next((l for l in u.splitlines() if l.startswith("Evidence packs: ")), None)
    if line is not None:
        return [c for p in json.loads(line[len("Evidence packs: "):]) for c in p["claims"]]
    return json.loads(u[u.index("Verified claims: ") + len("Verified claims: "):])
def J(x): return {"content": json.dumps(x)}

class Script:
    """The stub model's behaviour per contract; options switch the scenarios."""
    def __init__(self, ask_user=False, clarify_fail=0, plan_truncate=False, questions=True, clarify_truncate=0, research_truncate=0, extra_queries=(), subjects=("transcription",)):
        self.ask_user, self.clarify_fail, self.plan_truncate, self.questions = ask_user, clarify_fail, plan_truncate, questions
        self.fail_count, self.clarify_truncate, self.trunc_count = 0, clarify_truncate, 0
        self.research_truncate, self.research_trunc_count = research_truncate, 0
        self.extra_queries, self.subjects = list(extra_queries), list(subjects)
        self.extra_cites, self.meets_hard, self.entail_none_for, self.cite_none, self.first_503, self.inject, self.arithmetic, self.region_hard = [], True, (), False, False, False, False, False
        self.repair_truncate = False
        self.flow_labels = None                                                       # the labels the plan gives its user flow's steps; None: the plan names none
        self.revise_scope, self.revise_fail, self.revise_queries, self.revise_extra_redo = False, False, False, ()   # the revision stub's scope question, failure shape, new queries and the ids outside the stale set it names
        self.revise_scope_key, self.revise_scope_rounds, self.local = "budget_scope", 1, False   # the scope question's key (an existing key), the rounds that ask one, the local-only product
        self.vendor, self.product, self.version = "Orion", "Compute", "v1"          # the explicit product identity the extractor names
        self.select = None                                                            # (product_id, product_version, label) the comparison selects; None: the first listed candidate
        self.second_product = False                                                   # a second candidate identity (version v2, dearer) so the comparison explores the tree
        self.tree_fail = False                                                        # the tree's expansion answers the failure shape
        self.worker_proposals = True                                                  # a Researcher worker's proposals (False: the failure shape)
        self.worker_tool_loop = False                                                 # the worker keeps asking for a search on every turn until its allowances run out
        self.last_alternatives = []
        self.hebrew_query = False                                                     # the planner writes the Hebrew query for a Hebrew requirement (a scenario switch: other scenarios count their two research questions)
        self.wrong_class = False                                                      # the planner lists a hosting candidate under the transcription subject
        self.few_candidates = False                                                   # fewer than three candidates for the subject (the gap search)
        self.coverage_verdict = None                                                  # a fixed coverage verdict; None: by the claims' words
        self.pick_label = None                                                        # the recommendation's option text as the model writes it (the mode in its parenthetical), over the candidate's label
        self.mode_checks = False                                                      # the verification plan's risk checks name the real-time path and the async path, one each
        self.how_text = None                                                          # the how text of every requirement check (a long whole sentence, or a text at the cap)
        self.cross_cite = False                                                       # the recommended option cites every supporting line of every pack, the other identity's included
    def class_of(self, subject):
        return {"transcription": "speech_to_text", "summary": "llm_model", "email": "email_delivery", "hosting": "hosting"}.get(subject, "service")
    def candidates_for(self, subject):
        cls = self.class_of(subject)
        out = [("Speech API A", cls, "he"), ("Speech API B", cls, None), ("Speech API C", cls, None)] if subject == "transcription" else [(f"{subject} vendor {k}", cls, None) for k in (1, 2, 3)]
        if self.few_candidates and subject == "transcription":
            out = out[:2]
        if self.wrong_class and subject == "transcription":
            out.append(("Railway", "hosting", None))
        return out
    def applicability_for(self, reqs_u):
        ids = re.findall(r"'id': '([^']+)'|\"id\": \"([^\"]+)\"", reqs_u)
        out = []
        for a, b_ in ids:
            rid_ = (a or b_).upper().replace("p1.req", "P1.REQ")
            out.append((rid_, ["transcription"] if "lang" in reqs_u[reqs_u.lower().find(rid_.lower()):][:200] else list(self.subjects)))
        return out
    def __call__(self, body):
        s = schema_of(body); u = user_text(body)
        if s == "ClarifyOut":
            if self.first_503 and body["model"] == "a/x":
                return {"status": 503}
            if self.repair_truncate and "failure shape" in u:
                return {"content": "{\"requirements\": [{\"key\": \"budget", "finish_reason": "length"}
            if self.trunc_count < self.clarify_truncate:
                self.trunc_count += 1
                return {"content": "{\"requirements\": [{\"key\": \"budget", "finish_reason": "length"}
            rnd = int(re.search(r"Clarification round: (\d) of 2", "\n".join(m["content"] for m in body["messages"] if m["role"] == "user")).group(1))
            if self.fail_count < self.clarify_fail:
                self.fail_count += 1
                return J({"requirements": [], "assumptions": [], "contradictions": [], "questions": [], "ready_for_research": False, "failure_reason": "the idea text is missing a budget"})
            if rnd == 1 and self.questions:
                return J({"requirements": [{"key": "budget_monthly_usd", "value": "50", "source": "user", "hard": True}, {"key": "languages", "value": "he, en", "source": "user", "hard": True}],
                          "assumptions": ["summaries are sent by email"], "contradictions": [],
                          "questions": [{"text": "Which deployment region do the users need?", "why_blocking": "data residency decides the hosting", "requirement_key": "deployment_region"}], "ready_for_research": False})
            return J({"requirements": [{"key": "budget_monthly_usd", "value": "50", "source": "user", "hard": True}, {"key": "languages", "value": "he, en", "source": "user", "hard": True}] + ([{"key": "region", "value": "eu", "source": "user", "hard": True}] if self.region_hard else [])
                                      + ([{"key": "processing", "value": "on_device", "source": "user", "hard": True}] if self.local else []),
                      "assumptions": [], "contradictions": [], "questions": [], "ready_for_research": True})
        if s in ("ResearchPlanOut", "ResearchPlanOutV2"):
            tm = tool_msgs(body)
            if self.ask_user and not any(t.get("tool") == "ask_user" or "answer" in str(t.get("data", "")) for t in tm):
                return {"tool_calls": [("ask_user", {"text": "Is an on-device transcription acceptable?", "why_blocking": "decides the candidate list", "requirement_key": "on_device"})]}
            pages = [p for t in tm for p in (t.get("pages") or [])]
            if not pages:
                return {"tool_calls": [("search", {"query": "speech api hebrew pricing per audio hour", "freshness": "volatile", "num_results": 3})]}
            last = pages[-1]
            if last.get("more_available") and last.get("next_offset") and not any(p.get("range_start", 0) > 0 for p in pages):
                return {"tool_calls": [("read", {"source_id": last["source_id"], "source_version": last["source_version"], "offset": last["next_offset"]})]}
            if self.research_trunc_count < self.research_truncate:
                self.research_trunc_count += 1
                return {"content": "{\"decision_subjects\": [\"transcription\"], \"queries\": [{\"text", "finish_reason": "length"}
            base_q = [{"text": "speech api hebrew pricing", "decision_subject": "transcription", "target_fact": "price per audio hour", "freshness": "volatile"},
                      {"text": "speech api speaker labels languages", "decision_subject": "transcription", "target_fact": "speaker labels and languages", "freshness": "technical"}] + self.extra_queries
            if s == "ResearchPlanOut":
                return J({"decision_subjects": self.subjects, "queries": base_q})
            reqs_u = u[u.index("Requirements: "):u.index("\n", u.index("Requirements: "))].lower()
            hebrew = "hebrew" in reqs_u or re.search(r"\bhe\b", reqs_u) is not None             # a language requirement naming Hebrew: one query in that language for the bound subject
            cands = [{"subject": sub, "label": f"{lbl}", "product_class": cls, "specialised_for": sp} for sub in self.subjects for lbl, cls, sp in self.candidates_for(sub)]
            return J({"decision_subjects": self.subjects, "subjects": [{"name": sub, "product_class": self.class_of(sub)} for sub in self.subjects], "candidates": cands,
                      "applicability": [{"requirement_id": rid_, "subjects": subs, "reason": "the class handles it"} for rid_, subs in self.applicability_for(reqs_u)],
                      "queries": [dict(q, language="en") for q in base_q] + ([{"text": "\u05ea\u05de\u05dc\u05d5\u05dc \u05d6\u05d9\u05d4\u05d5\u05d9 \u05d3\u05d9\u05d1\u05d5\u05e8 \u05e2\u05d1\u05e8\u05d9\u05ea", "decision_subject": "transcription", "target_fact": "hebrew transcription pricing", "freshness": "volatile", "language": "he"}] if hebrew and self.hebrew_query else [])})
        if s in ("EvidenceExtractionOut", "EvidenceExtractionOutV2"):                     # the version 2 contract: claims name the subject ids of the subject list in force
            m = re.search(r"characters (\d+) to (\d+)", u); text = u[u.index(">", u.index("<untrusted_source")) + 1: u.rindex("</untrusted_source>")]
            rec = re.search(r"do not repeat: (.*)\n", u).group(1); recorded = "" if rec.strip() == "none" else rec.lower()
            if self.inject:
                return J({"claims": [], "injection_suspected": True, "more_claims": False})
            claims = []
            if self.arithmetic and FACT_PRICE in text and "monthly_cost" not in recorded:
                claims.append({"text": "Monthly cost: 80 hours x $0.60 = $48, under the $50 budget", "kind": "price", "source_id": "x", "quote": FACT_PRICE, "decision_subject": "transcription", "subject": "Speech API A v3", "attribute": "monthly_cost", "value": "$48 per month", "conditions": [], "more_conditions": False,
                               "vendor": self.vendor, "product": self.product, "product_version": self.version, "subject_ids": subjects_for_claim(u, "monthly_cost", "Speech API A v3 monthly cost")})
            for fact, subj, attr, val, conds in ((FACT_PRICE, "Speech API A v3", "price_per_hour", "$0.60 per audio hour", []), (FACT_LABELS, "Speech API A v3", "speaker_labels", "English and Spanish", ["English and Spanish only"]), (FACT_LANGS, "Speech API A v3", "languages", "42 languages including Hebrew", []),
                                                 (FACT_LOCAL, "Speech API A v3", "processing", "on the device, no network access", [])):
                if fact in text and attr.lower() not in recorded:
                    claims.append({"text": f"{subj}: {attr} is {val}", "kind": "price" if attr == "price_per_hour" else "capability", "source_id": "x", "quote": fact, "decision_subject": "transcription", "subject": subj, "attribute": attr, "value": val, "conditions": conds, "more_conditions": False,
                                   "vendor": self.vendor, "product": self.product, "product_version": self.version, "subject_ids": subjects_for_claim(u, attr, f"{subj}: {attr} is {val}")})
            if self.second_product and FACT_PRICE in text and "v2" not in recorded:       # the same page also names a dearer second version (its price above the monthly budget under the usage answer)
                claims.append({"text": "Speech API A v2: price_per_hour is $2.00 per audio hour", "kind": "price", "source_id": "x", "quote": FACT_PRICE, "decision_subject": "transcription", "subject": "Speech API A v2", "attribute": "price_per_hour", "value": "$2.00 per audio hour", "conditions": [], "more_conditions": False,
                               "vendor": self.vendor, "product": self.product, "product_version": "v2", "subject_ids": subjects_for_claim(u, "price_per_hour", "Speech API A v2: price_per_hour is $2.00 per audio hour")})
                claims.append({"text": "Speech API A v2: languages is 42 languages including Hebrew", "kind": "capability", "source_id": "x", "quote": FACT_LANGS, "decision_subject": "transcription", "subject": "Speech API A v2", "attribute": "languages", "value": "42 languages including Hebrew", "conditions": [], "more_conditions": False,
                               "vendor": self.vendor, "product": self.product, "product_version": "v2", "subject_ids": subjects_for_claim(u, "languages", "Speech API A v2: languages is 42 languages including Hebrew")})
            claims = [c for c in claims if c["subject_ids"]]                                  # a fact that bears on no listed subject is not a claim
            return J({"claims": claims, "injection_suspected": False, "more_claims": False})
        if s in ("VerificationOut", "VerificationOutV2"):                                   # one verdict per claim id of the group
            u = all_user_text(body); listed = claims_listed(u)
            if any(cid.endswith("-001") for cid, _, _ in listed) and not tool_msgs(body):
                return {"tool_calls": [("re_fetch", {"url": PAGE_URL})]}
            hard = json.loads(u[u.index("Hard requirements: ") + len("Hard requirements: "): u.index("\n", u.index("Hard requirements: "))])
            by_key = {h["key"]: h["id"] for h in hard}
            verdicts = [self.verify_one(cid, kind, text, by_key) for cid, kind, text in listed]
            return J({"verdicts": verdicts})
        if s == "ComparisonOutV2":                                                            # the memo shape from the same inputs as the v1 answer below
            u = next((m["content"] for m in body["messages"] if m["role"] == "user" and "Candidates: " in str(m.get("content"))), u)   # a repair turn is a later user message; the comparison's inputs stay in the earlier one, answered as before
            vc = claims_shown(u)                                                                 # a careful model cites the packs' supporting lines, never a contradicting one
            ids = [c["claim_id"] for c in vc if not c.get("contradicts")]
            if self.second_product and not self.cross_cite:
                ids = [c["claim_id"] for c in vc if "v2" not in c["text"] and not c.get("contradicts")]
            cites = [] if self.cite_none else list(ids) + list(self.extra_cites)
            cands = json.loads(u[u.index("Candidates: ") + len("Candidates: "): u.index("\nFounder's usage:")])
            sel = self.select or (cands[0]["product_id"], cands[0]["product_version"], cands[0]["label"])
            if self.pick_label:
                sel = (sel[0], sel[1], self.pick_label)
            others = [c for c in cands if (c["product_id"], c["product_version"]) != (sel[0], sel[1])]
            options = [{"option": sel[2], "product_id": sel[0], "product_version": sel[1], "fit": "supports Hebrew", "price": "$0.60 per audio hour", "monthly_cost": "estimate 18 USD", "risk": "speaker labels for English and Spanish only", "verdict": "recommended" if self.meets_hard else "rejected", "claim_ids": cites[:12]}]
            options += [{"option": c["label"], "product_id": c["product_id"], "product_version": c["product_version"], "fit": "", "price": "", "monthly_cost": "", "risk": "dearer", "verdict": "runner-up" if k == 0 else "not now", "claim_ids": []} for k, c in enumerate(others[:5])]
            why = [{"text": "the only candidate with supported claims for Hebrew within the budget", "claim_ids": ([] if self.cite_none else list(ids))[:10]}] + ([{"text": "a sentence citing a claim outside the set", "claim_ids": list(self.extra_cites)[:10]}] if self.extra_cites else [])
            open_items = ["Hebrew accuracy unverified in production"]
            if getattr(self, "address_contradictions", True):                                  # the contradiction rule as a careful model follows it: every must_address item carried into open in the founder's words (the values of a tie, the claim ids of a denial); a scenario that leaves one unaddressed switches this off
                line = next((l for l in u.splitlines() if l.startswith("Evidence packs: ")), None)
                for p0 in (json.loads(line[len("Evidence packs: "):]) if line else []):
                    for m0 in p0.get("must_address") or []:
                        about = str(m0.get("about") or "").replace("_", " ")
                        open_items.append((f"Sources disagree on {about} for {p0.get('label')}: {' and '.join(str(v) for v in m0.get('values') or [])}" if m0.get("values") else f"The verifier found that the source does not state {', '.join(m0.get('claim_ids') or [])} ({about})")[:300])
            return J({"decision_subject": "transcription", "recommendation": {"option": sel[2], "product_id": sel[0], "product_version": sel[1]}, "runner_up": ({"option": others[0]["label"], "product_id": others[0]["product_id"], "product_version": others[0]["product_version"]} if others else None),
                      "why": why, "options": options, "change_mind": ["a cheaper provider with Hebrew switching evidence"], "open": open_items[:6]})
        if s == "CoverageOut":                                                                 # entails when a listed claim's text names the requirement's words
            req = u[u.index("Hard requirement: ") + len("Hard requirement: "):u.index("\n", u.index("Hard requirement: "))].lower()
            listed = re.findall(r"^Claim (\S+): (.*)$", u, flags=re.M)
            hit = [cid for cid, text in listed if ("budget" in req and "price" in text.lower()) or ("languages" in req and "languages is" in text.lower()) or ("processing" in req and "processing is" in text.lower())]
            return J({"verdict": self.coverage_verdict if self.coverage_verdict else ("entails" if hit else "none"), "claim_ids_used": hit[:20], "note": "the quotes name the requirement" if hit else "no quote names it"})
        if s in ("EditorOut", "EditorOutV2"):                                                 # the Editor's pass; a scenario scripts its answer (self.editor), the default rewrites nothing; the answer has the Editor prompt's shape (dropped, assessment, summary); a scripted answer may be a raw item (self.editor_raw: a truncated answer)
            if getattr(self, "editor_raw", None) is not None:
                return self.editor_raw(u) if callable(self.editor_raw) else self.editor_raw
            ans = self.editor(u) if callable(getattr(self, "editor", None)) else (getattr(self, "editor", None) or {})
            return J({"fields": [], "merged": [], "flags": [], "dropped": [], "assessment": [], "summary": [], **ans})
        if s in ("EnrichmentOut", "EnrichmentOutV2"):                                          # three entities, three interfaces and the relations between the listed components; the version 2 contract adds the lead and the sub-steps
            keys = re.findall(r'"key": "([^"]+)"', u[u.index("Components: "):u.index("\nDecisions:")])
            ents = [{"name": "Meeting", "fields": [{"name": "id", "type": "uuid", "key": "primary"}, {"name": "uploaded_at", "type": "datetime", "key": None}], "relations": [{"to": "Transcript", "cardinality": "1..1", "kind": "has"}]},
                    {"name": "Transcript", "fields": [{"name": "meeting_id", "type": "uuid", "key": "foreign"}, {"name": "segments", "type": "json", "key": None}], "relations": []},
                    {"name": "Summary", "fields": [{"name": "meeting_id", "type": "uuid", "key": "foreign"}, {"name": "text", "type": "text", "key": None}], "relations": []}]
            a, b_ = (keys + keys)[:2]
            itfs = [{"name": "upload", "from_key": a, "to_key": b_, "direction": "request", "kind": "call", "description": "the audio file and the recipients", "steps": [{"order": 1, "from_key": a, "to_key": b_, "message": "POST the recording", "condition": None}]},
                    {"name": "transcript", "from_key": b_, "to_key": a, "direction": "response", "kind": "callback", "description": "the segments with speakers", "steps": []},
                    {"name": "summary mail", "from_key": b_, "to_key": a, "direction": "request", "kind": "message", "description": "the summary to the recipients", "steps": []}]
            rels = [{"from_key": a, "to_key": b_, "kind": "calls", "label": "sends the recording"}, {"from_key": b_, "to_key": a, "kind": "notifies", "label": "returns the summary"}]
            if s == "EnrichmentOut":
                return J({"entities": ents, "interfaces": itfs, "component_relations": rels})
            task_keys = re.findall(r'"key": "([^"]+)"', u[u.index("Tasks (key"):u.index("\nBudget")]) if "Tasks (key" in u else []
            claim_ids = re.findall(r'"id": "([^"]+\.CLM-\d+)"', u[u.index("Claims the lead may cite"):]) if "Claims the lead may cite" in u else []
            lead = {"text": "We build a meeting assistant that transcribes Hebrew and English calls with Speech API A and emails one summary; the cost at your usage is under the budget; the risk that matters is a dropped mid-sentence switch.", "claim_ids": claim_ids[:2]}
            steps = [{"task_key": k, "steps": [{"text": f"Set up the {k} skeleton and its tests", "hours": 4.0}, {"text": f"Wire {k} to the chosen product and record a first run", "hours": 4.0}]} for k in task_keys[:20]]
            return J({"entities": ents, "interfaces": itfs, "component_relations": rels, "lead": lead, "task_steps": steps})
        if s == "VerificationPlanOut":                                                         # one check per hard requirement and per high risk listed
            hard_ids = re.findall(r'"id": "([^"]+\.REQ-\d+)"', u[u.index("Hard requirements: "):u.index("\nRisks:")])
            risks = re.findall(r'"text": "([^"]+)"', u[u.index("Risks: "):u.index("\nDecisions:")])
            checks = [{"check": f"check {rid_}", "how": self.how_text or "ten real recordings scored by a speaker", "pass_rule": "within 2 points of the best", "cost": "under 1 USD, two hours", "requirement_id": rid_, "risk": None} for rid_ in hard_ids[:6]]
            if self.mode_checks:                                                                 # one check against the real-time path, one against the async path, both on the first risk
                checks += [{"check": "check the live path", "how": "Stream a live call through the real-time API and count the dropped switches.", "pass_rule": "no dropped switch", "cost": "one hour", "requirement_id": None, "risk": r_} for r_ in risks[:1]]
                checks += [{"check": "check the upload path", "how": "Upload ten recordings to the async API and score the transcripts against a reference.", "pass_rule": "within 2 points of the reference", "cost": "two hours", "requirement_id": None, "risk": r_} for r_ in risks[:1]]
            else:
                checks += [{"check": f"check the risk", "how": "a bake-off on real audio", "pass_rule": "no dropped switch", "cost": "one hour", "requirement_id": None, "risk": r_} for r_ in risks[:4]]
            return J({"checks": checks[:10]})
        if s == "ComparisonOut":
            ids = re.findall(r'"claim_id": "([^"]+)"', u)
            if self.second_product:                                                       # the simple comparison cites the recommended identity's claims only (the v2 claims name the other version)
                vc = json.loads(u[u.index("Verified claims: ") + len("Verified claims: "):])
                ids = [c["claim_id"] for c in vc if "v2" not in c["text"]]
            cites = [] if self.cite_none else list(ids) + list(self.extra_cites)
            cands = json.loads(u[u.index("Candidates: ") + len("Candidates: "): u.index("\nVerified claims:")])
            sel = self.select or (cands[0]["product_id"], cands[0]["product_version"], cands[0]["label"])
            return J({"decision_subject": "transcription", "options": [{"option": sel[2], "product_id": sel[0], "product_version": sel[1], "meets_hard_constraints": self.meets_hard, "claim_ids": cites, "strengths": ["supports Hebrew"], "risks": ["speaker labels only in English and Spanish"]}],
                      "recommended": sel[2], "rationale": "the only candidate with supported claims for Hebrew within the budget", "unresolved": ["Hebrew accuracy unverified in production"]})
        if s == "AlternativesOut":                                                            # one alternative per product identity the claims name, with its claim ids
            if self.tree_fail:
                return J({"decision_subject": "transcription", "alternatives": [], "failure_reason": "the claims name no alternative"})
            claims = json.loads(u[u.index("Supported claims: ") + len("Supported claims: "): u.index("\nReturn at most")])
            by_prod = {}
            for c in claims:
                by_prod.setdefault((c.get("product_id") or "", c.get("product_version") or ""), []).append(c["claim_id"])
            alts = [{"label": f"{pid_} {ver}".strip() or "unnamed", "summary": f"adopt {pid_} {ver}".strip(), "claim_ids": ids[:10]} for (pid_, ver), ids in sorted(by_prod.items())][:3]
            self.last_alternatives = alts
            return J({"decision_subject": "transcription", "alternatives": alts})
        if s == "VariantsOut":
            label = u[u.index("Alternative: ") + len("Alternative: "): u.index("\nRequirements:")].strip()
            ids = next((a["claim_ids"] for a in self.last_alternatives if a["label"] == label), [])
            return J({"alternative_label": label, "variants": [{"label": f"{label} / {v}", "summary": f"{label} used in {v} mode", "claim_ids": ids} for v in ("batch", "streaming", "hybrid")]})
        if s == "BranchEvaluationOut":
            branches = json.loads(u[u.index("Branches: ") + len("Branches: "): u.index("\nRequirements:")])
            evs = [{"branch_id": b["branch_id"], "fit": 9 if "streaming" in b["label"] else (8 if "v1" in b["label"] else 6), "evidence": 7, "cost": 6 if "v1" in b["label"] else 3, "risk": 6, "claim_ids": list(b.get("claim_ids") or [])[:10], "notes": "scored from the cited claims"} for b in branches]
            return J({"evaluations": evs})
        if s == "ResearcherOut":                                                              # the Researcher worker's proposals for its subject
            if self.worker_tool_loop:
                self.loop_n = getattr(self, "loop_n", 0) + 1
                return {"tool_calls": [("search", {"query": f"speech api pricing page {self.loop_n}", "freshness": "volatile", "num_results": 3})]}
            if not self.worker_proposals:
                return J({"proposals": [], "failure_reason": "no source answered the target facts"})
            subj = u[u.index("decision subject ") + len("decision subject "): u.index("; target facts")].strip()
            wid = u[u.index("worker id ") + len("worker id "): u.index(".", u.index("worker id "))].strip()
            task = u[u.index("Task ") + len("Task "): u.index(":")].strip()
            if subj != "transcription":
                return J({"proposals": []})
            base = {"worker": wid, "task_id": task, "source_url": PAGE_URL, "kind": "price", "subject": "Speech API A v3", "attribute": "price_per_hour", "value": "$0.60 per audio hour", "conditions": [], "vendor": self.vendor, "product": self.product, "product_version": self.version, "writes": []}
            return J({"proposals": [dict(base, quote=FACT_PRICE, claim_text="Speech API A v3 costs $0.60 per audio hour"), dict(base, kind="capability", attribute="languages", value="42 languages including Hebrew", quote=FACT_LANGS, claim_text="Speech API A v3 supports Hebrew")]})
        if s == "PlanOut":
            if self.plan_truncate:
                return {"content": "{\"problem\": \"Small teams", "finish_reason": "length"}
            return J(self.plan(u))
        if s.startswith("Plan") and s.endswith("Section"):
            full = self.plan(u)
            keys = {"PlanScopeSection": ("problem", "users", "scope_in", "scope_out", "user_flow", "risks", "unresolved_checks") + (("user_flow_labels",) if "user_flow_labels" in full else ()), "PlanComponentsSection": ("components",), "PlanTasksSection": ("tasks",), "PlanAcceptanceSection": ("acceptance",)}[s]
            return J({k: full[k] for k in keys})
        if s == "ConditionContinuationOut":
            return J({"claim_id": re.search(r"Claim (\S+):", u).group(1), "conditions": [], "more_conditions": False})
        if s == "PanelOut":                                                                  # the fusion panel's reading, simulated ([discovery].simulate_panel): one contradiction about the subject, one new product of the class
            return J({"status": "ok", "consensus": ["the draft pick fits the budget"], "contradictions": [{"topic": "price per audio hour of the pick", "stances": [{"model": "a", "stance": "0.60 USD"}, {"model": "b", "stance": "0.90 USD"}]}],
                      "partial_coverage": [], "unique_insights": [], "blind_spots": [], "products_named": [{"vendor": "Speech API", "product": "Speech API C", "product_class": "transcription", "why": "a cheaper plan"}],
                      "models": ["a", "b", "c"], "responses": getattr(self, "panel_responses", None) or [{"model": m, "outcome": "substantive", "note": "answered"} for m in ("a", "b", "c")], "failure_reason": None})   # the fake panel's three substantive responses; a scenario scripts others (panel_responses)
        if s == "RevisionOut":                                                              # the revision after a requirement change
            u = all_user_text(body); stale = re.findall(r'"id": "([A-Za-z0-9-]+\.DEC-\d+)"', u[u.index("Stale decisions:"): u.index("Fresh claims available:")])   # the repair turn is a later user message; the inputs stay in the first
            scope_round = 1 if "Answered scope questions: none" in u else u.count('"question": ') + 1   # the round from the answered scope questions the prompt lists
            if self.revise_scope and scope_round <= self.revise_scope_rounds:
                return J({"decision_ids_to_redo": [], "reuse_claim_ids": [], "new_queries": [], "change_summary": "the reach of the change is unclear",
                          "scope_questions": [{"text": f"Does the new budget also cover the email summaries (round {scope_round})?", "why_blocking": "decides whether the summary component is affected", "requirement_key": self.revise_scope_key if scope_round == 1 else f"{self.revise_scope_key}_{scope_round}"}], "consistency_flags": []})
            if self.revise_fail:
                return J({"decision_ids_to_redo": [], "reuse_claim_ids": [], "new_queries": [], "change_summary": "", "scope_questions": [], "consistency_flags": [], "failure_reason": "the stale set is empty"})
            queries = [{"text": "speech api hebrew pricing after the budget change", "decision_subject": "transcription", "target_fact": "price per audio hour", "freshness": "volatile"}] if self.revise_queries else []
            return J({"decision_ids_to_redo": stale + list(self.revise_extra_redo), "reuse_claim_ids": re.findall(r'"claim_id": "([^"]+)"', u), "new_queries": queries,
                      "change_summary": f"the budget changed; {len(stale)} decision(s) re-decided", "scope_questions": [], "consistency_flags": ["check the acceptance tests of the superseded budget"]})
        raise AssertionError(f"unexpected contract {s}")
    def verify_one(self, cid, kind, text, by_key):
        """The verdict of one listed claim, applied to each member of a verification group."""
        conds = ["English and Spanish only"] if "speaker_labels" in text else []
        entailed = []
        if "price_per_hour" in text and "budget_monthly_usd" in by_key and "price" not in self.entail_none_for:
            entailed = [by_key["budget_monthly_usd"]]
        if "languages is" in text and "languages" in by_key and "languages" not in self.entail_none_for:
            entailed = [by_key["languages"]]
        if "processing is" in text and "processing" in by_key:
            entailed = [by_key["processing"]]
        return {"claim_id": cid, "verdict": "supported", "quote": FACT_PRICE if "price" in text else None, "reason": "the quote states the fact for this version", "scope_matches_claim": True, "conditions_found": conds, "version_or_date": "September 2026", "requirements_entailed": entailed}
    def plan(self, u):
        decs = re.findall(r'"id": "([A-Za-z0-9-]+\.DEC-\d+)"', u); reqs = re.findall(r'"id": "([A-Za-z0-9-]+\.REQ-\d+)"', u); leaves = not self.local   # a local-only product's components keep the data on the device
        return {"problem": "Small teams lose decisions made in bilingual calls", "users": ["founders", "small teams"], "scope_in": ["Hebrew and English transcription", "email summary"], "scope_out": ["video"],
                "user_flow": ["upload call", "receive summary"], **({"user_flow_labels": list(self.flow_labels)} if self.flow_labels is not None else {}), "risks": ["speaker labels not available for Hebrew"], "unresolved_checks": [],
                "components": [{"key": "transcription", "name": "Transcription", "responsibility": "turn audio into text", "decision_ids": decs[:1], "data_leaves_device": leaves},
                               {"key": "summary", "name": "Summary", "responsibility": "summarise and email", "decision_ids": decs[:1], "data_leaves_device": leaves}],
                "tasks": [{"key": "t1", "component_key": "transcription", "title": "integrate the speech API", "depends_on": [], "effort_hours_low": 8, "effort_hours_high": 16},
                          {"key": "t2", "component_key": "summary", "title": "summarise and send", "depends_on": ["t1"], "effort_hours_low": 6, "effort_hours_high": 12}],
                "acceptance": [{"task_key": "t1", "requirement_id": reqs[1] if len(reqs) > 1 else (reqs[0] if reqs else "P1.REQ-001"), "criterion": "a Hebrew call is transcribed"},
                               {"task_key": "t2", "requirement_id": reqs[0] if reqs else "P1.REQ-001", "criterion": "a summary arrives by email under the monthly budget"}]}

def answer_for(q: dict) -> str:
    """The suites' standing answer to a question the fake's runs ask: the usage figures, the scope and the summary's
    length; yes to a hardness, revision or confirmation question; open to a contradiction question (the decision stays open)."""
    from app.core import derived as _DV
    text, kind = q.get("text") or "", q.get("kind") or ""
    if text == _DV.SUMMARY_LENGTH_QUESTION:
        return "500 words"
    if text == _DV.USAGE_SCOPE_QUESTION:
        return "total"
    if kind in ("hardness", "revision", "confirmation"):                                  # a round asks at most three questions, so a confirmation may arrive in a later round; yes confirms it
        return "yes"
    if kind == "contradiction":
        return "open"
    return USAGE_ANSWER
def answers_for(res: dict) -> dict:
    """The answers of every question of a paused drive's interrupt (answer_for each)."""
    return {q["id"]: answer_for(q) for it in res.get("interrupt", []) for q in it.get("questions", [])}
def confirm(runner, res: dict, rounds: int = 4) -> dict:
    """A revision run pauses before its first send on its revision question (and on a hardness question); a suite that
    exercises the revision path answers yes here and goes on, the change record carried onto the resumed result."""
    change = res.get("change")
    for _ in range(rounds):
        if res.get("status") != "awaiting_user" or not any(q.get("kind") in ("revision", "hardness") for it in res.get("interrupt", []) for q in it.get("questions", [])):
            break
        res = runner.resume(res["run_id"], {q["id"]: answer_for(q) for it in res.get("interrupt", []) for q in it.get("questions", []) if q.get("kind") in ("revision", "hardness")})
    if change is not None and "change" not in res:
        res["change"] = change
    return res
HARD_STATED = ("budget_monthly_usd", "languages", "region")   # the founder's hardness statements the stub scripts rely on, seeded as answered hardness questions so the stubs' hard proposals open no question and the rows carry the stated flag
USAGE_ANSWER = "40 meetings a month in all, 45 minutes each, 3 users"   # the founder's usage answer the fakes' projects carry (a project built with usage=None asks the question); "in all" names the scope (the team's total)
def project(name, confirmed=HARD_STATED, hard=HARD_STATED, usage=USAGE_ANSWER):
    """confirmed: the keys the founder has confirmed as stated, seeded as answered confirmation questions so the stubs' user-marked requirements are stored confirmed and open no question; () seeds none. hard: the keys with a seeded hardness statement. usage: the founder's usage answer seeded as the answered usage question; None seeds none."""
    path = tmp / name / "app.db"; path.parent.mkdir(parents=True)
    c = connect(path); from app.repo import migrate; migrate(c); Repo(c).create_project(M.Project(id="P1", owner_id="U1", title="t"))
    with c:
        if usage:
            c.execute("INSERT INTO questions (id, project_id, text, why_blocking, round, status, answer_json, kind) VALUES (?,?,?,?,1,'answered',?,'usage')", ("P1.Q-899", "P1", "How much will the product be used in a month?", "the monthly cost estimate needs the usage (D-A28)", json.dumps(usage)))
        for i, k in enumerate(hard, start=1):
            c.execute("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status, answer_json, kind) VALUES (?,?,?,?,?,1,'answered','\"yes\"','hardness')", (f"P1.Q-{900 + i:03d}", "P1", f"Is {k} hard?", "a hard requirement rejects every option that fails it (D-05)", k))
        for i, k in enumerate(confirmed, start=1):
            c.execute("INSERT INTO questions (id, project_id, text, why_blocking, requirement_key, round, status, answer_json, kind) VALUES (?,?,?,?,?,1,'answered','\"yes\"','confirmation')", (f"P1.Q-{950 + i:03d}", "P1", f"Confirm {k}?", "research starts only on a requirement the founder confirmed (Phase 6)", k))
    c.close()
    return path
def gateway_factory(stub, settings=S, ledger_cls=None):
    def make(ledger):
        if ledger_cls is not None:
            ledger = ledger_cls(ledger.conn, ledger.run_id, ledger.cap, ledger.completion_reserve)
        return CloudGateway(api_key="k", default_model="a/x", fallback_model="b/y", prices={"a/x": (1, 1), "b/y": (1, 1)}, privacy_mode="cloud", ledger=ledger, base_url=stub.url, fallback_base_url=stub.url, settings=settings)
    return make
def runner_for(name, script, settings=S, ledger_cls=None, research=None):
    stub = ModelStub(script); path = project(name)
    return Runner(path, settings=settings, gateway_factory=gateway_factory(stub, settings, ledger_cls), research=research or Sim(), instance_id="inst-1", data_dir=tmp / name / "data"), stub



def house_render(model: dict, out_dir, name: str, illustrations=None) -> dict:
    """The renderer the test entries hand the hosted producer (the hosted runner of the web application, not part of this repository, takes it as its render setting): the house
    layout through the Studio's own fallback (fallback_render of the document renderer, also outside this repository; the path the hosted production takes without an
    OpenRouter key), so a production driven by the web process under a test sends nothing and reads no key. The same four-argument shape
    as the render argument of the renderer's produce_export."""
    from app.export import studio as SH
    from app.export.renderer import studio as RS
    return RS.fallback_render(model, None, pathlib.Path(out_dir), name, SH.brand_logo())
