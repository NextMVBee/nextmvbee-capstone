"""The offline retrieval diagnosis: where the evidence a plan needs is lost between the collection and the
decision, measured on a copy of a backup with this code tree. No model, no network, 0 USD. The sample is the evidence list of the gold file
(the manually marked reference answer of an evaluation project: its decisions and the claims they cite): every claim a gold decision cites, with its quote and its source, is one expected evidence item of
that decision. For each item the script reads the store's own records stage by stage and names the first stage at which the item is lost:

  collected   a sources row whose url or canonical_url equals the gold's URL after normalisation (norm_url: whitespace trimmed, lower case,
              the http or https scheme and a leading www. removed, trailing slashes removed) or, when the gold names no URL or its URL
              matches no row, the row of the gold's source id (matched_by records url or id); the row id, its fetch status and the
              characters of its current version are recorded
  chunked     a passage of the source's canonical row (identical content is indexed once, retrieval.canonical_source) whose text holds the
              quote: exact (a verbatim substring), normalised (evidence._norm of both), or phrase (a run of PHRASE_WORDS consecutive words of
              the normalised quote, which also catches a quote across a chunk boundary); the chunk id and the match kind are recorded
  queried     a research_questions row of the decision's subject whose target fact or query text names the claim's attribute (half of
              its words, inflection allowed: names_attribute); the questions that also name the product are preferred for the ranking
  ranked      the source enters the candidate list of such a question within the cut-off: under the stored ranking (the run's
              extract_evidence plan step holds every subject's candidate list with the best passage rank of each source; the lists of every
              run plan holding the question are read, newest first, and the newest list holding the source counts, its run id and passage
              rank recorded; when no list holds the source, the newest plan holding the question is recorded with in_list false) or, for a
              question no stored plan holds, under the code's own retrieval.candidate_sources at the settings' k_per_subject and
              max_passages_scanned; the chunk's own rank in the full FTS5 ranking is recorded beside it
  gates       the gates between retrieval and claims, from the stored records: the fetch (fetch_status, injection_flag, stale_at), the
              admission (the source linked to one of those questions in coverage, or admitted into a stored plan's base set) and the
              processing (the extraction batches of the current source version: none, in progress or needs_review with its reason)
  cited       a claims row for the gold claim (by id, else a claim of the same source with the same quote, else a claim of the same source
              whose quote holds the gold's quote after evidence._norm of both, the lowest id; found_by records id, quote or quote_within)
              with a verdict; the verdict, the status and the value are recorded; a claim without a verdict is lost here (awaits
              verification, the extract report's reason)
  pack        an evidence pack line of the subject holds the claim (app.core.evidence_pack, built as the comparison builds them, on the
              scratch after the reconciliation, over the subject's claims as workflow.subject_claims selects them: the claims of its own
              questions and, when a later plan renamed the subject, of its predecessor's): the candidate's label, the line's role or
              the omission's reason are recorded
  decision    a stored decision of the subject at its newest plan version names the claim: a claim_decision edge to it, or its complete id
              (never a longer id it is a prefix of) in the memo, the rationale or the rejected options; every decision citing the claim is
              listed with its plan version

Diagnosis only: nothing in the application changes, the copy is opened read-only (file:<path>?mode=ro) and never written; the derivation
runs on a scratch copy taken through SQLite's online backup into a temporary folder beside the copy, deleted when the script ends. A live
store (scripts/ops/replay.py's rule) is refused with exit 3 before anything opens. Exit 0 when the funnel was written, 2 on an error.

    .venv/bin/python scripts/eval/retrieval_diagnosis.py --db <copy of a backup> --project <project id> --gold <gold file> --out <folder>

Writes <out>/funnel.json and <out>/RETRIEVAL_DIAGNOSIS_<project>.md. The last line printed is always
`retrieval_diagnosis: expected <n>, collected <n>, chunked <n>, queried <n>, ranked <n>, gates <n>, cited <n>, pack <n>, decision <n>, exit <code>`."""
import argparse, collections, datetime, hashlib, json, os, pathlib, re, shutil, subprocess, sys, tempfile, tomllib

HERE = pathlib.Path(__file__).resolve()
DEFAULT_ROOT = HERE.parents[2]
sys.path.insert(0, str(DEFAULT_ROOT / "scripts" / "ops"))
from replay import is_live_path, open_ro                                       # the fixed-data gate's refusal of a live store and its read-only open

STAGES = ("collected", "chunked", "queried", "ranked", "gates", "cited", "pack", "decision")
LABELS = {"expected": "expected", "collected": "collected", "chunked": "chunked", "queried": "queried", "ranked": "ranked within k", "gates": "passed the gates", "cited": "cited", "pack": "in a pack", "decision": "in the decision"}
FIXES = {
    "collected": "collect the page: the search stage never fetched this URL, so a query naming the vendor page, or a read of it, is the fix.",
    "chunked": "re-index or re-fetch the source: the quote is not in any passage of the current version, so the page changed or its text was not indexed.",
    "queried": "plan a research question naming the attribute: no question of the subject asks for it, so no ranking can find it.",
    "ranked": "raise the cut-off or improve the ranking for the question: the chunk is indexed and asked for, but its source is outside the question's candidate list in every stored plan.",
    "gates": "process the admitted source: the fetch, the admission links or the extraction batches of the current version stopped it before any claim.",
    "cited": "extract or verify the claim: the source was processed, but no claim with a verdict holds the quote.",
    "pack": "make the claim citable for an eligible candidate: it has a verdict but no pack line carries it (scope, validity, conflict or identity).",
    "decision": "carry the pack line into the memo: the comparison saw the claim and the newest decision does not name it.",
}
PHRASE_WORDS = 6
SUMMARY = "retrieval_diagnosis: expected {}, collected {}, chunked {}, queried {}, ranked {}, gates {}, cited {}, pack {}, decision {}, exit {}"


# ---------------------------------------------------------------- the sample: the gold's evidence list
def norm_url(u: str | None) -> str:
    u = (u or "").strip().lower()
    u = re.sub(r"^https?://", "", u); u = re.sub(r"^www\.", "", u)
    return u.rstrip("/")


def norm_subject(s) -> str:
    return " ".join(str(s or "").lower().replace("_", " ").replace("-", " ").split())


def load_gold(path) -> dict:
    """The legacy document-model gold: decisions with why and alternatives citing claim ids, and
    an evidence section with the claims (quotes, sources) and the sources (url when known). A gold without an evidence list is refused."""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    ev = data.get("evidence") or {}
    if not isinstance(ev.get("claims"), dict) or not isinstance(data.get("decisions"), list):
        raise ValueError(f"{path}: the gold has no evidence list (evidence.claims) or no decisions list")
    return data


def expected_items(gold: dict) -> list[dict]:
    """One item per (decision, cited claim): the why items cite for the recommendation, the alternatives for their option; the first mention
    of a claim in a decision stands. Each item carries the quote(s), the gold source id and its URL when the gold names one."""
    claims, sources = gold["evidence"]["claims"], gold["evidence"].get("sources") or {}
    out, seen = [], set()
    for d in gold["decisions"]:
        mentions = [(cid, d.get("recommendation") or "", "why") for w in d.get("why") or [] for cid in w.get("claims") or []]
        mentions += [(cid, a.get("option") or "", "alternative") for a in d.get("alternatives") or [] for cid in a.get("claims") or []]
        for cid, option, via in mentions:
            if (d["id"], cid) in seen:
                continue
            seen.add((d["id"], cid))
            g = claims.get(cid) or {}
            quotes = [q.get("text") or "" for q in g.get("quotes") or [] if q.get("text")]
            src = (g.get("quotes") or [{}])[0].get("source") or (g.get("sources") or [None])[0]
            out.append({"decision_id": d["id"], "subject": d.get("subject") or "", "claim_id": cid, "claim_text": g.get("text") or "", "quotes": quotes, "quote": quotes[0] if quotes else "",
                        "gold_source_id": src, "gold_url": (sources.get(src) or {}).get("url"), "option": option, "via": via, "gold_status": g.get("status")})
    return out


# ---------------------------------------------------------------- the stages
def limits(root: pathlib.Path) -> tuple[int, int]:
    path = next((p for p in (root / "config/settings.toml", root / "config/settings.example.toml") if p.exists()), None)
    if path is None:
        return 8, 40
    x = tomllib.loads(path.read_text(encoding="utf-8")).get("extraction") or {}
    return int(x.get("k_per_subject", 8)), int(x.get("max_passages_scanned", 40))


def phrase_windows(quote: str, n: int = PHRASE_WORDS) -> list[str]:
    words = quote.split()
    if len(words) <= n:
        return [" ".join(words)] if words else []
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


def match_in(quote: str, text: str, norm) -> tuple[str | None, str | None]:
    """(match kind, the matched phrase for a phrase match): exact, normalised, phrase or None."""
    q = (quote or "").strip()
    if not q:
        return None, None
    if q in text:
        return "exact", None
    nq, nt = norm(q), norm(text)
    if nq and nq in nt:
        return "normalised", None
    for w in phrase_windows(nq):
        if w and w in nt:
            return "phrase", w
    return None, None


class Diagnosis:
    def __init__(self, conn, project: str, root: pathlib.Path):
        from app.core import applicability as AP, coverage, evidence, evidence_pack as EP, retrieval
        from app.graph import workflow as WF
        self.conn, self.pid, self.root = conn, project, root
        self.AP, self.cov, self.ev, self.EP, self.RT, self.WF = AP, coverage, evidence, EP, retrieval, WF
        self.k, self.scan = limits(root)
        q = self.q
        self.rv = q("SELECT requirement_version FROM projects WHERE id=?", project)[0][0]
        self.sources = {r[0]: dict(zip(("id", "url", "canonical_url", "fetch_status", "injection_flag", "stale_at", "content_hash", "retrieved_at"), r)) for r in
                        q("SELECT id, url, canonical_url, fetch_status, injection_flag, stale_at, content_hash, retrieved_at FROM sources WHERE project_id=?", project)}
        self.by_url: dict[str, list[str]] = {}
        for s in self.sources.values():
            for u in {norm_url(s["url"]), norm_url(s["canonical_url"])}:
                if u:
                    self.by_url.setdefault(u, []).append(s["id"])
        self.chars = {r[0]: r[1] for r in q("SELECT sv.source_id, sv.chars_total FROM source_versions sv JOIN sources s ON s.id=sv.source_id AND s.content_hash=sv.content_hash WHERE s.project_id=?", project)}
        self.questions = [dict(zip(("id", "subject", "target_fact", "query_text"), r)) for r in q("SELECT id, decision_subject, target_fact, query_text FROM research_questions WHERE project_id=? ORDER BY id", project)]
        self.linked = {r[0]: {x.get("source_id") for x in json.loads(r[1] or "[]") if isinstance(x, dict)} for r in q("SELECT subject_id, source_versions_json FROM coverage WHERE project_id=? AND subject_type='research_question'", project)}
        self.plans = []                                                                   # the stored candidate lists, newest first
        for key, rid, oj in q("SELECT rs.step_key, rs.run_id, rs.output_json FROM run_steps rs JOIN runs r ON r.id=rs.run_id WHERE r.project_id=? AND rs.node='extract_evidence' AND rs.status='completed' AND rs.step_key LIKE '%:plan' ORDER BY rs.completed_at DESC, rs.step_key DESC", project):
            d = json.loads(oj or "{}")
            lists = {c["subject_id"]: {"sources": {s[0]: int(s[1]) for s in c.get("sources") or []}, "mark": c.get("mark"), "scanned": c.get("scanned")} for c in d.get("candidates") or []}
            admitted = set(d.get("order") or []) | {s for ext in (d.get("extensions") or {}).values() for s in ext}
            self.plans.append({"run_id": rid, "step_key": key, "lists": lists, "admitted": admitted})
        report = q("SELECT rs.output_json FROM run_steps rs JOIN runs r ON r.id=rs.run_id WHERE r.project_id=? AND rs.node='extract_report' AND rs.status='completed' ORDER BY rs.completed_at DESC, rs.step_key DESC LIMIT 1", project)
        rep = json.loads(report[0][0] or "{}") if report else {}
        self.unverified_reason = {x["claim_id"]: x.get("reason") or "" for items in (rep.get("unverified") or {}).values() for x in items}
        self.unprocessed_reason = {x["source_id"]: x.get("reason") or "" for items in (rep.get("unprocessed") or {}).values() for x in items}
        memo = "memo_json" if any(r[1] == "memo_json" for r in q("PRAGMA table_info(decisions)")) else "NULL"   # a store made from schema.sql alone has no memo column yet
        self.decisions = [dict(zip(("id", "subject", "status", "plan_version", "rationale", "rejected_json", "memo_json"), r)) for r in
                          q(f"SELECT id, subject, status, plan_version, rationale, rejected_json, {memo} FROM decisions WHERE project_id=? ORDER BY plan_version, id", project)]
        self.cites: dict[str, set] = {}
        for f, t in q("SELECT from_id, to_id FROM trace_edges WHERE project_id=? AND kind='claim_decision'", project):
            self.cites.setdefault(f, set()).add(t)
        self._ranking: dict[str, list] = {}; self._cands: dict[str, dict] = {}; self._packs: dict[str, dict] = {}
        self.pool = int(q("SELECT COUNT(*) FROM passages WHERE project_id=?", project)[0][0])

    def q(self, sql, *a):
        return self.conn.execute(sql, a).fetchall()

    # ---- collected
    def collected(self, item: dict) -> dict:
        ids = self.by_url.get(norm_url(item["gold_url"]), []) if item["gold_url"] else []
        how = "url" if ids else None
        if not ids and item["gold_source_id"] in self.sources:
            ids, how = [item["gold_source_id"]], "id"
        if not ids:
            return {"ok": False, "reason": "no sources row for the URL" if item["gold_url"] else "no sources row for the gold's source id"}
        sid = sorted(ids)[0]; s = self.sources[sid]
        canon = self.RT.canonical_source(self.conn, self.pid, sid)
        return {"ok": True, "source_id": sid, "matched_by": how, "rows": sorted(ids), "canonical_id": canon, "url": s["url"], "fetch_status": s["fetch_status"], "chars": int(self.chars.get(canon) or self.chars.get(sid) or 0), "content_hash": s["content_hash"]}

    # ---- chunked
    def chunked(self, item: dict, col: dict) -> dict:
        canon = col["canonical_id"]
        rows = self.q("SELECT id, ordinal, text FROM passages WHERE project_id=? AND source_id=? ORDER BY ordinal", self.pid, canon)
        if not rows:
            return {"ok": False, "reason": f"no passages for the source (fetch_status {col['fetch_status']}, {col['chars']} characters stored)"}
        best = None
        order = {"exact": 0, "normalised": 1, "phrase": 2}
        for quote in item["quotes"] or [item["quote"]]:
            for pid, ordinal, text in rows:
                kind, phrase = match_in(quote, text, self.ev._norm)
                if kind and (best is None or order[kind] < order[best["match"]]):
                    best = {"ok": True, "chunk_id": pid, "ordinal": ordinal, "match": kind, "phrase": phrase, "passages": len(rows)}
                if best and best["match"] == "exact":
                    break
            if best and best["match"] == "exact":
                break
        if best:
            return best
        changed = any(True for _ in self.q("SELECT 1 FROM source_versions WHERE project_id=? AND source_id=? AND superseded_at IS NOT NULL LIMIT 1", self.pid, col["source_id"]))
        return {"ok": False, "reason": "the quote is in none of the source's passages" + (" (the source changed version since the gold)" if changed else ""), "passages": len(rows)}

    # ---- queried
    def queried(self, item: dict, claim: dict | None) -> dict:
        attribute = (claim or {}).get("attribute") or item.get("claim_text") or ""
        product = (claim or {}).get("subject") or (claim or {}).get("product_id") or item.get("option") or ""
        subj = norm_subject(item["subject"])
        named, both = [], []
        for qr in self.questions:
            if norm_subject(qr["subject"]) != subj:
                continue
            text = f"{qr['target_fact'] or ''} {qr['query_text'] or ''}"
            if names_attribute(attribute, text):
                named.append(qr["id"])
                if names_product(product, text):
                    both.append(qr["id"])
        return {"ok": bool(named), "attribute": attribute, "product": product, "questions": named, "questions_naming_product": both, "best": both or named,
                "reason": None if named else f"no research question of {item['subject']} names the attribute"}

    # ---- ranked
    def ranking(self, qid: str) -> list:
        if qid not in self._ranking:
            fact = next((q["target_fact"] for q in self.questions if q["id"] == qid), "") or ""
            self._ranking[qid] = self.RT.FTS5Retriever(self.conn).retrieve(self.pid, fact, k=self.pool or 1)
        return self._ranking[qid]

    def candidates(self, qid: str) -> dict:
        if qid not in self._cands:
            fact = next((q["target_fact"] for q in self.questions if q["id"] == qid), "") or ""
            self._cands[qid] = self.RT.candidate_sources(self.conn, self.pid, fact, self.k, self.scan)
        return self._cands[qid]

    def ranked(self, item: dict, col: dict, chk: dict, qd: dict) -> dict:
        ids = {col["source_id"], col["canonical_id"]} | set(col["rows"])
        per = []; within = False
        for qid in qd["best"]:
            rec = {"question_id": qid, "k": self.k, "scan": self.scan}
            holding = [p for p in self.plans if qid in p["lists"]]                           # newest first
            hits = [(p, [s for s in p["lists"][qid]["sources"] if s in ids]) for p in holding]
            hits = [(p, h) for p, h in hits if h]
            if hits:                                                                         # the stored ranking admitted the source for this question in some run
                plan, hit = hits[0]; lst = plan["lists"][qid]
                rec.update({"ranking": "stored", "run_id": plan["run_id"], "in_list": True, "passage_rank": min(lst["sources"][s] for s in hit), "mark": lst["mark"], "list_size": len(lst["sources"]), "runs_in_list": [p["run_id"] for p, _ in hits], "plans_holding_question": len(holding)})
            elif holding:
                plan = holding[0]; lst = plan["lists"][qid]
                rec.update({"ranking": "stored", "run_id": plan["run_id"], "in_list": False, "passage_rank": None, "mark": lst["mark"], "list_size": len(lst["sources"]), "runs_in_list": [], "plans_holding_question": len(holding)})
            else:
                c = self.candidates(qid); hit = [(s, r) for s, r in c["sources"] if s in ids]
                rec.update({"ranking": "code", "in_list": bool(hit), "passage_rank": min(r for _, r in hit) if hit else None, "source_rank": min(i for i, (s, _) in enumerate(c["sources"], start=1) if s in ids) if hit else None, "mark": c["mark"], "list_size": len(c["sources"])})
            full = self.ranking(qid); pos = {p: i for i, (p, _) in enumerate(full, start=1)}
            rec["chunk_rank"] = pos.get(chk.get("chunk_id")) if chk.get("ok") else None; rec["ranked_passages"] = len(full)
            per.append(rec); within = within or rec["in_list"]
        return {"ok": within, "k": self.k, "scan": self.scan, "questions": per,
                "reason": None if within else "the source is outside the candidate list of every question naming the attribute (best chunk rank " + str(min((r["chunk_rank"] for r in per if r["chunk_rank"]), default="none")) + ")"}

    # ---- gates
    def gates(self, item: dict, col: dict, chk: dict, qd: dict) -> dict:
        s = self.sources[col["source_id"]]; ids = {col["source_id"], col["canonical_id"]} | set(col["rows"])
        checks = []
        if s["fetch_status"] != "ok":
            checks.append(("fetch", f"fetch_status {s['fetch_status']}"))
        if s["injection_flag"]:
            checks.append(("injection", "injection_flag set"))
        if s["stale_at"]:
            checks.append(("stale", f"stale_at {s['stale_at']}"))
        linked = [qid for qid in qd["best"] if self.linked.get(qid, set()) & ids]
        admitted = any(p["admitted"] & ids for p in self.plans)
        if not linked and not admitted:
            checks.append(("admission", "the source was never linked to a question naming the attribute nor admitted into a stored plan's base set" + (f" ({self.unprocessed_reason[col['source_id']]})" if col["source_id"] in self.unprocessed_reason else "")))
        batches = self.q("SELECT status, review_reason FROM extraction_batches WHERE project_id=? AND source_id=? AND source_version=?", self.pid, col["source_id"], s["content_hash"])
        if not batches and col["canonical_id"] != col["source_id"]:
            batches = self.q("SELECT status, review_reason FROM extraction_batches WHERE project_id=? AND source_id=? AND source_version=?", self.pid, col["canonical_id"], s["content_hash"])
        if not batches:
            checks.append(("processing", "no extraction batch at the current source version"))
        elif any(b[0] not in ("completed", "needs_review") for b in batches):
            checks.append(("processing", "an extraction batch of the current version is " + ", ".join(sorted({b[0] for b in batches if b[0] not in ("completed", "needs_review")}))))
        elif all(b[0] == "needs_review" for b in batches):
            checks.append(("processing", "every batch of the current version needs review: " + "; ".join(sorted({b[1] or "" for b in batches}))))
        gate, reason = (checks[0] if checks else (None, None))
        return {"ok": not checks, "gate": gate, "reason": reason, "all": [{"gate": g, "reason": r} for g, r in checks], "linked_questions": linked, "admitted_in_a_plan": admitted,
                "batches": dict(collections.Counter(b[0] for b in batches))}

    # ---- cited
    def claim_of(self, item: dict, col: dict | None) -> dict | None:
        cols = ("id", "source_id", "quote", "verdict", "status", "scope_status", "subject", "attribute", "value", "product_id", "product_version", "source_version", "kind")
        r = self.q("SELECT " + ",".join(cols) + " FROM claims WHERE id=? AND project_id=?", item["claim_id"], self.pid)
        if not r and col:
            r = self.q("SELECT " + ",".join(cols) + " FROM claims WHERE project_id=? AND source_id IN (" + ",".join("?" * len(col["rows"])) + ") AND quote=? ORDER BY id LIMIT 1", self.pid, *col["rows"], item["quote"])
        if not r and col:                                                                 # the gold's sentence inside a longer claim quote of the same source, both normalised as the chunked stage normalises (evidence._norm); the lowest id wins
            nq = self.ev._norm(item["quote"])
            if nq:
                for row in self.q("SELECT " + ",".join(cols) + " FROM claims WHERE project_id=? AND source_id IN (" + ",".join("?" * len(col["rows"])) + ") AND quote IS NOT NULL ORDER BY id", self.pid, *col["rows"]):
                    if nq in self.ev._norm(row[2]):
                        r = [row + ("quote_within",)]; break
        if not r:
            return None
        found = dict(zip(cols, r[0]))
        found["found_by"] = "id" if found["id"] == item["claim_id"] else ("quote_within" if len(r[0]) > len(cols) else "quote")
        return found

    def cited(self, item: dict, claim: dict | None) -> dict:
        if claim is None:
            return {"ok": False, "reason": "no claim of the source holds the quote"}
        if claim["verdict"] is None:
            return {"ok": False, "claim_id": claim["id"], "reason": "the claim awaits verification" + (f" ({self.unverified_reason[claim['id']]})" if claim["id"] in self.unverified_reason else ""), "status": claim["status"], "scope_status": claim["scope_status"], "value": claim["value"]}
        return {"ok": True, "claim_id": claim["id"], "verdict": claim["verdict"], "status": claim["status"], "scope_status": claim["scope_status"], "value": claim["value"], "product_id": claim["product_id"], "product_version": claim["product_version"],
                "found_by": claim["found_by"]}

    # ---- pack
    def packs_of(self, subject: str) -> dict:
        """The subject's packs exactly as scripts/eval/pack_stats.py builds them (compare_options' inputs), once per subject."""
        sub = subject
        if sub in self._packs:
            return self._packs[sub]
        AP, cov, EP, WF = self.AP, self.cov, self.EP, self.WF
        norm = lambda s: cov._norm(s or "")
        q = self.q
        with cov.derived(self.conn, self.pid):
            reqs = [{"id": r[0], "key": r[1], "value": json.loads(r[2]), "hard": bool(r[3]), "applies_to": AP.applies_to_of({"applies_to": r[4]})} for r in q("SELECT id, key, value_json, hard, applies_to FROM requirements WHERE project_id=? AND status='confirmed' ORDER BY id", self.pid)]
            cols = ("id", "text", "kind", "source_id", "quote", "status", "scope_status", "conditions_json", "subject", "attribute", "value", "range_start", "range_end", "decision_subject", "product_id", "product_version", "source_version")
            claims = [dict(zip(cols, r)) for r in q("SELECT " + ",".join(cols) + " FROM claims WHERE project_id=? ORDER BY id", self.pid)]
            rq_claims: dict = collections.defaultdict(set)
            for rq, cj in q("SELECT subject_id, claim_ids_json FROM coverage WHERE project_id=? AND subject_type='research_question'", self.pid):
                for cid in json.loads(cj or "[]"):
                    rq_claims[cid].add(rq)
            rq_subject = {r[0]: r[1] for r in q("SELECT id, decision_subject FROM research_questions WHERE project_id=?", self.pid)}
            top = max([int(d["plan_version"] or 0) for d in self.decisions] or [0])
            current = sorted({d["subject"] for d in self.decisions if int(d["plan_version"] or 0) == top})   # the subjects of the newest plan version, as the comparison knows its run's subjects
            rel = WF.subject_claims(self.conn, self.pid, sub, claims, rq_claims, rq_subject, current)   # the comparison's own selection (the subject's claims and its predecessor's)
            out = {"packs": [], "citable": set(), "rel": {c["id"]: c for c in rel}, "stale": {}, "identities": {}}
            if rel:
                citable = WF.citable_of(self.conn, self.pid, rel, self.rv)
                hard = AP.binding([r for r in reqs if r["hard"]], sub)
                ident = WF.eligible_candidates(self.conn, self.pid, sub, rel)["identities"]
                inputs = EP.load_inputs(self.conn, self.pid, sub, hard, self.rv, rel, citable, rq_claims, rq_subject, predecessor=WF.predecessor_of(self.conn, self.pid, sub, current))
                out["packs"] = EP.build_packs(inputs, ident, EP.caps_of(None)); out["citable"] = citable; out["identities"] = {f"{k[0]} {k[1]}".strip(): v for k, v in ident.items()}
                out["stale"] = {r["id"]: r["stale_reason"] for r in cov.claim_rows(self.conn, self.pid, sorted(out["rel"]), self.rv) if not r["valid"]}
                out["open_conflict"] = cov.open_conflict_claims(self.conn, self.pid)
        self._packs[sub] = out
        return out

    def in_pack(self, item: dict, claim: dict) -> dict:
        P = self.packs_of(item["subject"]); cid = claim["id"]
        for p in P["packs"]:
            for line in p["claims"]:
                if line["claim_id"] == cid:
                    return {"ok": True, "candidate": p["label"], "product_id": p["product_id"], "product_version": p["product_version"], "role": "contradicting" if line.get("contradicts") else "supporting", "contradicts": line.get("contradicts"),
                            "candidate_matches_option": option_matches(item["option"], p["label"])}
            for o in p.get("omitted") or []:
                if o["claim_id"] == cid:
                    return {"ok": False, "candidate": p["label"], "reason": f"omitted from the candidate's pack: {o['reason']}" + (f" ({o['detail']})" if o.get("detail") else ""), "citable": cid in P["citable"]}   # a withheld claim's own reason (status, scope or currency) with its detail
        if cid not in P["rel"]:
            return {"ok": False, "reason": "the claim is not among the subject's verified claims (no question of the subject attaches it)"}
        why = P["stale"].get(cid)
        if cid in P.get("open_conflict", set()):
            why = "in an open conflict"
        elif not why and claim["status"] != "supported":
            why = f"status {claim['status']}"
        elif not why and claim["scope_status"] not in ("in_scope", "conditional"):
            why = f"scope {claim['scope_status']}"
        elif not why:
            why = "no eligible candidate holds the claim's product identity" if cid in P["citable"] else "not citable"
        return {"ok": False, "reason": why, "citable": cid in P["citable"], "eligible_candidates": len(P["identities"])}

    # ---- decision
    def in_decision(self, item: dict, claim: dict) -> dict:
        subj = norm_subject(item["subject"]); cid = claim["id"]
        mine = [d for d in self.decisions if norm_subject(d["subject"]) == subj]
        if not mine:
            return {"ok": False, "reason": "no stored decision for the subject"}
        top = max(int(d["plan_version"] or 0) for d in mine)
        newest = [d for d in mine if int(d["plan_version"] or 0) == top]                   # every decision of the subject at its newest plan version (a version may hold a stale row and its replacement)
        whole = re.compile(r"(?<![\w-])" + re.escape(cid) + r"(?![\w-])")                        # the complete id: devA.CLM-231 is not named by devA.CLM-2316
        def names(d) -> bool:
            if d["id"] in self.cites.get(cid, set()):
                return True
            return any(whole.search(d.get(f) or "") for f in ("memo_json", "rationale", "rejected_json"))
        citing = [{"decision_id": d["id"], "plan_version": d["plan_version"], "status": d["status"]} for d in mine if names(d)]
        hit = [d for d in newest if names(d)]
        ok = bool(hit)
        return {"ok": ok, "newest": [{"decision_id": d["id"], "plan_version": d["plan_version"], "status": d["status"], "names_claim": names(d)} for d in newest], "plan_version": top, "citing": citing,
                "reason": None if ok else (f"no decision of the subject at plan version {top} names the claim" + (f" (named by {len(citing)} earlier decision(s), last at plan version {max(c['plan_version'] for c in citing)}; " if citing else " (no stored decision ever named it; ")
                                           + "the newest " + ", ".join(sorted({d['status'] for d in newest})) + ")")}

    # ---- one item
    def diagnose(self, item: dict) -> dict:
        """Every stage judged on its own record (a later stage is not conditioned on an earlier one: evidence can reach a claim through another
        subject's list or a revision's own research and still be outside the question's ranking); lost_at is the first stage that fails."""
        row = dict(item); stages = {}
        col = self.collected(item); stages["collected"] = col
        claim = self.claim_of(item, col if col["ok"] else None)
        stages["chunked"] = self.chunked(item, col) if col["ok"] else {"ok": False, "reason": "not collected"}
        qd = self.queried(item, claim); stages["queried"] = qd
        if col["ok"] and qd["ok"]:
            stages["ranked"] = self.ranked(item, col, stages["chunked"], qd)
        else:
            stages["ranked"] = {"ok": False, "reason": "not collected" if not col["ok"] else "no question names the attribute, nothing to rank for"}
        if col["ok"]:
            gt = self.gates(item, col, stages["chunked"], qd); stages["gates"] = gt
        else:
            stages["gates"] = {"ok": False, "gate": None, "reason": "not collected", "all": []}
        stages["cited"] = self.cited(item, claim)
        if claim is not None:
            stages["pack"] = self.in_pack(item, claim); stages["decision"] = self.in_decision(item, claim)
        else:
            stages["pack"] = {"ok": False, "reason": "no claim"}; stages["decision"] = {"ok": False, "reason": "no claim"}
        row["stages"] = stages
        row["passed"] = [s for s in STAGES if stages[s]["ok"]]
        lost = next((s for s in STAGES if not stages[s]["ok"]), None)
        row["lost_at"] = lost
        row["reason"] = stages[lost].get("reason") if lost else None
        if lost == "gates" and stages["gates"].get("gate"):
            row["reason"] = f"{stages['gates']['gate']}: {stages['gates']['reason']}"
        return row


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower().replace("_", " ")) if len(w) > 1]


def _same_word(a: str, b: str) -> bool:
    """One word names the other: equal, or (four letters or more each) one a prefix of the other (pricing/price, supported/support, languages/language)."""
    return a == b or (len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)))


def names_attribute(attribute: str, text: str) -> bool:
    """A question text names a claim's attribute when at least half of the attribute's words (one for a one-word attribute) appear in it, inflection
    allowed (_same_word). The product's own matcher (coverage.subject_matches_question) is for product subjects and needs two exact words, which a
    one-word attribute such as pricing can never give."""
    cs, qs = _tokens(attribute), _tokens(text)
    if not cs:
        return False
    hit = sum(1 for a in cs if any(_same_word(a, b) for b in qs))
    return hit >= max(1, (len(cs) + 1) // 2)


def names_product(product: str, text: str) -> bool:
    """A question names the product when the product's first significant word (its vendor or family word, three letters or more) is in the text."""
    words = [w for w in _tokens(product) if len(w) >= 3 and w.isalpha()]
    return bool(words) and any(_same_word(words[0], b) for b in _tokens(text))


def option_matches(option: str, label: str) -> bool:
    """The gold option names the pack's candidate: the option's first significant word is a word of the label."""
    words = [w for w in re.findall(r"[a-z0-9]+", (option or "").lower()) if len(w) >= 3 and w not in ("the", "and", "with", "for", "free", "plan", "api", "async", "tier")]
    have = set(re.findall(r"[a-z0-9]+", (label or "").lower()))
    return bool(words) and words[0] in have


# ---------------------------------------------------------------- the funnel and the documents
def funnel_totals(rows: list[dict]) -> dict:
    """Per stage: the items that pass it (each stage on its own record) and the items that pass it and every stage before it."""
    tot = {"expected": len(rows), "passed": {}, "cumulative": {}}
    for i, s in enumerate(STAGES):
        tot["passed"][s] = sum(1 for r in rows if s in r["passed"])
        tot["cumulative"][s] = sum(1 for r in rows if all(x in r["passed"] for x in STAGES[:i + 1]))
    return tot


FIX_BY_REASON = {                                                                    # a sharper fix when one reason dominates a stage's losses
    ("pack", "cap"): "raise the pack caps or rank the claims a decision cited above recency: the claim is citable but the cap left it out of the candidate's pack.",
    ("gates", "no extraction batch"): "extract the current version of the re-fetched source: the quote is still in its passages, but no batch of the current version exists, so its claims stay at the old version.",
    ("cited", "awaits verification"): "verify the claim: it was extracted and never received a verdict, so no comparison may cite it.",
    ("chunked", "changed version"): "re-fetch or re-index the source: the page changed since the gold and the quote is no longer in its passages.",
    ("decision", "earlier decision"): "carry the earlier decisions' cited claims into the newest comparison: an earlier version named the claim and the newest decision of the subject does not.",
}


def reason_key(reason: str | None) -> str:
    """The reason's leading words for the breakdown (before any parenthesis or colon detail)."""
    r = re.split(r"[(:]", reason or "")[0].strip()
    return r or "unknown"


def losses(rows: list[dict]) -> list[dict]:
    c = collections.Counter(r["lost_at"] for r in rows if r["lost_at"])
    out = []
    for s, n in sorted(c.items(), key=lambda x: (-x[1], STAGES.index(x[0]))):
        reasons = collections.Counter(reason_key(r["reason"]) for r in rows if r["lost_at"] == s)
        top = reasons.most_common(1)[0][0] if reasons else ""
        fix = next((f for (stage, word), f in FIX_BY_REASON.items() if stage == s and word in " ".join(r["reason"] or "" for r in rows if r["lost_at"] == s and reason_key(r["reason"]) == top)), FIXES[s])
        out.append({"stage": s, "label": LABELS[s], "count": n, "reasons": dict(reasons.most_common()), "fix": fix})
    return out


def sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for part in iter(lambda: f.read(1 << 20), b""):
            h.update(part)
    return h.hexdigest()


def git_head(root: pathlib.Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def mark(r: dict, stage: str) -> str:
    if stage in r["passed"]:
        return "yes"
    return "LOST" if r["lost_at"] == stage else "no"


def document(res: dict) -> str:
    h = res["header"]; rows = res["items"]; tot = res["totals"]
    L = [f"# Retrieval diagnosis of project {h['project']}", "",
         f"Backup SHA-256: {h['db_sha256']}. Commit: {h['commit']}. Date: {h['date']}. Gold: {h['gold']} (SHA-256 {h['gold_sha256']}). Code tree: {h['code_tree']}. "
         f"Cut-off: k_per_subject {h['k']}, max_passages_scanned {h['scan']}; pool {h['pool_passages']:,} passages. A development measurement on a copy of a backup, never release performance (scripts/eval/retrieval_diagnosis.py, D-A189).", "",
         "The sample is the gold's own evidence list: every claim a gold decision cites (its why items and its alternatives), with its quote and its source, is one expected evidence item of that decision. "
         "This list is the plan's \"manually marked sample\"; nothing was marked by hand beyond the gold. The script reads the store's records stage by stage and names the first stage at which an item is lost; no behaviour of the application changes.", "",
         "## The funnel", "",
         "| decision | claim | source | " + " | ".join(LABELS[s] for s in STAGES) + " | lost at | reason |", "|---|---|---|" + "---|" * len(STAGES) + "---|---|"]
    for r in rows:
        src = (r["stages"].get("collected") or {}).get("source_id") or r.get("gold_source_id") or ""
        chunk = (r["stages"].get("chunked") or {})
        chunked = f"yes ({chunk['match']}, {chunk['chunk_id']})" if chunk.get("ok") else mark(r, "chunked")
        rk = r["stages"].get("ranked") or {}
        best = min((x["passage_rank"] for x in rk.get("questions", []) if x.get("passage_rank")), default=None)
        ranked = (f"yes (rank {best})" if best else "yes") if rk.get("ok") else mark(r, "ranked")
        ct = r["stages"].get("cited") or {}
        cited = f"yes ({ct['verdict']})" if ct.get("ok") else mark(r, "cited")
        pk = r["stages"].get("pack") or {}
        pack = f"yes ({pk['role']}, {pk['candidate']})" if pk.get("ok") else mark(r, "pack")
        dc = r["stages"].get("decision") or {}
        dec = f"yes (v{dc['plan_version']})" if dc.get("ok") else mark(r, "decision")
        cells = [mark(r, "collected"), chunked, mark(r, "queried"), ranked, mark(r, "gates"), cited, pack, dec]
        L.append(f"| {r['decision_id']} | {r['claim_id']} | {src} | " + " | ".join(cells) + f" | {r['lost_at'] or 'reached the decision'} | {(r['reason'] or '').replace('|', '/')} |")
    L += ["", "## Totals per stage", "", "| stage | items passing the stage | items passing it and every stage before |", "|---|---|---|", f"| expected | {tot['expected']} | {tot['expected']} |"]
    L += [f"| {LABELS[s]} | {tot['passed'][s]} | {tot['cumulative'][s]} |" for s in STAGES]
    n_dec, n_lost = tot["passed"]["decision"], sum(x["count"] for x in res["losses"])
    L += ["", f"Every stage is judged on its own record, so a later stage can hold an item an earlier one lost (evidence reaches a claim through another subject's list, a revision's own research or an earlier run's ranking). "
          f"{n_dec} of {tot['expected']} expected items are named by a decision of their subject at its newest plan version; {n_lost} items fail at least one stage, counted below at the first stage they fail.", "",
          "## Where evidence is lost", ""]
    if res["losses"]:
        for i, x in enumerate(res["losses"], start=1):
            why = "; ".join(f"{k} ({v})" for k, v in x["reasons"].items())
            L.append(f"{i}. {x['label']}: {x['count']} item(s): {why}. Fix: {x['fix']}")
    else:
        L.append("No expected item is lost.")
    L += ["", "## How each stage is read", "",
          "- collected: a sources row whose url or canonical_url equals the gold's URL after normalisation (whitespace trimmed, lower case, the http or https scheme and a leading www. removed, trailing slashes removed) or, when the gold names no URL or its URL matches no row, the row of the gold's source id (matched_by records url or id).",
          "- chunked: a passage of the source's canonical row holds the quote: exact (verbatim), normalised (letters, digits and a few signs, lower case), or phrase (" + str(PHRASE_WORDS) + " consecutive words of the normalised quote; a quote across a chunk boundary matches this way).",
          "- queried: a research question of the decision's subject names the claim's attribute (half of the attribute's words in its target fact or query text, inflection allowed); questions that also name the product (its first significant word) are preferred.",
          "- ranked within k: the source enters the candidate list of such a question under the stored ranking (the lists of every run plan holding the question are read, newest first, and the newest list holding the source counts, its run id and passage rank recorded; when no list holds the source, the newest plan holding the question is recorded with in_list false) or, when no plan holds the question, under retrieval.candidate_sources with the settings' limits; the chunk's rank in the full FTS5 ranking is recorded in funnel.json.",
          "- passed the gates: fetch_status, injection_flag and stale_at of the source; the admission (linked to such a question in coverage, or in a stored plan's base set); the extraction batches of the current source version.",
          "- cited: a claims row for the gold claim with a verdict (by id, else by source and quote).",
          "- in a pack: a 10.2 evidence pack line of the subject (built as the comparison builds them, on the scratch after the reconciliation) holds the claim, as support or as a contradiction.",
          "- in the decision: a stored decision of the subject at its newest plan version names the claim (a claim_decision edge, or the complete id, never a longer id it is a prefix of, in its memo, rationale or rejected options).", ""]
    return "\n".join(L)


def run(db, project: str, gold_path, out, root: pathlib.Path) -> dict:
    sys.path.insert(0, str(root))
    from app.core import reconcile
    from app.core.sqlguard import open_connection
    from app.repo import migrate
    db = pathlib.Path(db).resolve(); out = pathlib.Path(out); out.mkdir(parents=True, exist_ok=True)
    gold = load_gold(gold_path); items = expected_items(gold)
    before = sha256_of(db)
    scratch_dir = tempfile.mkdtemp(prefix="nmb-retrieval-diagnosis-scratch-", dir=str(db.parent))
    try:
        src = open_ro(db)
        try:
            conn = open_connection(str(pathlib.Path(scratch_dir) / "scratch.db"))
            src.backup(conn)
        finally:
            src.close()
        migrate(conn)                                                              # the scratch carries this code's schema; the copy never gains a column
        reconcile.reconcile_project(conn, project)
        from app.core import coverage
        with coverage.derived(conn, project):                                      # every derived read (statuses, coverage) inside the one accessor; packs_of nests its own
            dg = Diagnosis(conn, project, root)
            rows = [dg.diagnose(it) for it in items]
        pool = dg.pool
        conn.close()
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    after = sha256_of(db)
    res = {"header": {"project": project, "db": str(db), "db_sha256": before, "db_sha256_after": after, "copy_unchanged": before == after, "gold": str(gold_path), "gold_sha256": sha256_of(gold_path), "commit": git_head(root), "code_tree": str(root),
                      "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "k": dg.k, "scan": dg.scan, "pool_passages": pool, "sample": "the gold's evidence list (the plan's manually marked sample)",
                      "label": "development measurement on a copy of a backup; not release performance; diagnosis only, no behaviour changed"},
           "items": rows, "totals": funnel_totals(rows), "losses": losses(rows)}
    (out / "funnel.json").write_text(json.dumps(res, indent=1, default=lambda o: sorted(o) if isinstance(o, set) else str(o)), encoding="utf-8")
    (out / f"RETRIEVAL_DIAGNOSIS_{project}.md").write_text(document(res), encoding="utf-8")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The offline retrieval diagnosis: for every expected evidence item of the gold, the stage at which the store lost it. Never on a live store.")
    ap.add_argument("--db", required=True, help="a copy of a backup (never the live store)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--gold", required=True, help="the gold file (the legacy document-model gold with its evidence list)")
    ap.add_argument("--out", required=True, help="the folder for funnel.json and RETRIEVAL_DIAGNOSIS_<project>.md")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="the code tree (default: this repository)")
    a = ap.parse_args(argv)
    if is_live_path(a.db):
        print(f"retrieval_diagnosis: refused: {a.db} is a live store; the diagnosis reads only a verified copy of a backup")
        print(SUMMARY.format(*(["none"] * 9), 3))
        return 3
    try:
        res = run(a.db, a.project, a.gold, a.out, pathlib.Path(a.root).resolve())
    except Exception as e:
        print(f"retrieval_diagnosis: error {type(e).__name__}: {e}", file=sys.stderr)
        print(SUMMARY.format(*(["unknown"] * 9), 2))
        return 2
    t = res["totals"]
    print(f"retrieval_diagnosis on {res['header']['db']} (project {res['header']['project']}, gold {res['header']['gold']}, k {res['header']['k']}, scan {res['header']['scan']}): {res['header']['label']}")
    for x in res["losses"]:
        print(f"  lost at {x['label']}: {x['count']}")
    print(SUMMARY.format(t["expected"], *(t["passed"][s] for s in STAGES), 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
