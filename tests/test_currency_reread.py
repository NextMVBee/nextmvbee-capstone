"""The currency check under reuse: a second run on a project whose priced claims rest on pages an
earlier run stored reads the vendor's own page of those claims once more, through the read tool and the ledger, and the volatile-figure rule
reads the confirmation from that fresh copy (for example, two acceptable picks whose prices rest on stored pages are not left unresolved
for want of a fetch). Offline, through the Runner on the pipeline fake (tests/pipeline_fake.py: a stub model server and a simulated research adapter; two runs on one store, the
provider's content status under the scenario's control) and one pure rule. No network, no keys.

    cr1  the re-read restores the price: run 1 stores the vendor's pricing page with status unknown (as a page reads when the
         provider states no status) and leaves the pick unresolved with the volatile-figure reason; run 2 on the same store (the provider now confirming a live crawl)
         makes exactly one read of that page under the worker cur<n> (the fake counts it, the tool row and its ledger row exist, the page's
         copy reads livecrawl with a date inside run 2), the decision is active with no volatile-figure reason, and the memo's currency line
         no longer says that no vendor page was fetched in this run
    cr2  the read fails (the provider refuses with HTTP 500): the tool row is failed, nothing else is read, the decision keeps its
         volatile-figure reason, the memo's currency line stays "no vendor page of its evidence was fetched in this run", the run completes
    cr3  a same-run fetch makes no extra read: one run whose pages the provider confirmed live makes no re-read: the contents calls of the
         run are the base's one (the verifier's re_fetch) and no cur<n> tool row exists
    cr4  the status mapping: the provider's documented value for a live fetch, crawled, reads as livecrawl (as live does); cached and no
         status read as before
    cr5  the targets rule (discovery.currency_reread_targets): the pages of the pick's priced claims not fetched in this run that are the
         vendor's own or tier 1, the vendor's own first, at most two; a page fetched in this run is never read again; no vendor page, nothing
    cr6  the cap across the draft and the final: three vendor pages of the one pick stored by run 1; run 2 reaches the
         final comparison on the same pick (the follow-up of the panel, the three models that read a draft comparison): the draft reads two pages and the final reads none, so the run's cur<n> rows
         count two, the two pages distinct and the third page's copy untouched
    cr7  the changed-price path refreshes the cost table's inputs: the fresh copy of the vendor's page carries a new
         price, so the stored price claim loses its verdict; run 2's derived cost table computes no estimate from it (its row reads
         not computable with the claim's current status, never the old rate as a calculated estimate within budget) and the memo's options
         and why sentences cite it no more
    cr8  an alternative's price cleared while the pick's own citations survive: the fresh copy carries the real
         adapter's zero-hour metadata, so only the changed price sentence loses its verdict and the capability sentences keep theirs;
         the pick's row and the why sentence cite capabilities only and all of them survive, the dearer alternative's row cites its own price
         claim, which the fresh copy cleared: that row cites it no more (every option row is filtered, not only when one of the pick's own
         citations was cleared)

Run one scenario with its name as the argument, or all of them without one."""
import copy, json, os, pathlib, re, sys
from datetime import datetime
ROOT = pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("NMB_TEST_FIXTURES", "1")

results = []
def check(name, cond, detail=None):
    results.append((name, bool(cond))); assert cond, (name if detail is None else f"{name} [{detail}]")
import background_threads; background_threads.install()

import pipeline_fake as PF
from app.adapters.research import ExaHTTPError, cache_status_from
from app.core import coverage, discovery as DS, evidence
from app.core.sqlguard import inspect as insp

SC = copy.deepcopy(PF.S); SC["discovery"]["currency"] = True; SC["research"]["simulate_fresh_reads"] = True   # the currency check on; a stub adapter reads fresh only when the settings say so (research.simulate_fresh_reads)
NOT_FETCHED = "no vendor page of its evidence was fetched in this run"
NOT_LIVE = evidence.NOT_CONFIRMED_LIVE


class SimStatus(PF.Sim):
    """The pipeline fake's Sim with the provider's content status under the scenario's control (unknown in run 1; livecrawl in
    run 2), and a contents call that fails on demand with the provider's HTTP status."""
    def __init__(self, status="unknown", fail=None):
        super().__init__(); self.status, self.fail = status, fail
    def search(self, q, f, n):
        return [dict(p, cache_status=self.status) for p in super().search(q, f, n)]
    def contents(self, urls, f):
        if self.fail is not None:
            self.calls.append(("contents", urls[0])); raise ExaHTTPError(self.fail, "stub refusal")
        return [dict(p, cache_status=self.status) for p in super().contents(urls, f)]


def script():
    sc = PF.Script(questions=False, subjects=("transcription",))
    sc.vendor, sc.product = "Speech", "API A"            # the product id speech-api-a: the fixture page's host (docs.speech-a.example) is the vendor's own (discovery.vendor_domain)
    return sc


def drive(runner, res):
    for _ in range(4):
        if res["status"] != "awaiting_user":
            break
        res = runner.resume(res["run_id"], PF.answers_for(res))
    return res


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def decisions(c):
    return [{"id": d[0], "status": d[1], "rationale": d[2], "memo": json.loads(d[3]) if d[3] else None}
            for d in insp(c).execute("SELECT id, status, rationale, memo_json FROM decisions WHERE project_id='P1' ORDER BY id")]


def source(c):
    r = c.execute("SELECT id, cache_status, retrieved_at, tier FROM sources WHERE project_id='P1' AND url=?", (PF.PAGE_URL,)).fetchone()
    return dict(zip(("id", "cache_status", "retrieved_at", "tier"), r)) if r else None


def reread_rows(c, rid):
    rows = [tuple(x) for x in c.execute("SELECT id, state, ledger_entry FROM tool_calls WHERE run_id=? AND step_key LIKE ? ORDER BY id", (rid, f"{rid}:research_plan:cur%"))]
    ledger = [tuple(x) for x in c.execute(f"SELECT kind, state FROM usage_ledger WHERE id IN ({','.join('?' * len(rows)) or 'NULL'}) ORDER BY id", tuple(r[2] for r in rows))]
    return rows, ledger


def close(r, stub):
    r.close(); stub.srv.shutdown(); stub.srv.server_close()


def fixture(name, sim):
    """Run 1 on a fresh project: the vendor's pricing page stored with status unknown, the pick unresolved with the volatile-figure reason (the devA shape)."""
    r, stub = PF.runner_for(name, script(), settings=SC, research=sim)
    res1 = drive(r, r.start("P1", PF.IDEA)); c = r.conn
    d1, s1 = decisions(c), source(c)
    check(f"{name} the fixture: run 1 completed, its one decision is a conditional recommendation (Phase 19 package 19.1, D-A407 and D-A408: the budget cannot be calculated from a price read from a copy not confirmed live, so the pick is recommended and not yet proven on the budget, the confirming check carrying the volatile-figure reason), and the vendor's page is stored with status unknown",
          res1["status"] == "completed" and len(d1) == 1 and d1[0]["status"] == "active" and d1[0]["rationale"].startswith("Recommended, not yet proven on Monthly budget (USD)") and NOT_LIVE in str(((d1[0]["memo"] or {}).get("conditional") or {}).get("check")) and s1 is not None and s1["cache_status"] == "unknown",
          {"res": res1["status"], "decisions": [(x["id"], x["status"], x["rationale"][:160]) for x in d1], "source": s1})
    return r, stub, c, res1


# ---------------------------------------------------------------- cr1: the re-read restores the price
def cr1():
    sim = SimStatus("unknown")
    r, stub, c, res1 = fixture("cr1", sim)
    n = len(sim.calls); sim.status = "livecrawl"
    res2 = drive(r, r.requality("P1", "cr1: the second run on the stored pages", research=False)); rid2 = res2["run_id"]
    started2 = c.execute("SELECT started_at FROM runs WHERE id=?", (rid2,)).fetchone()[0]
    reads = [x for x in sim.calls[n:] if x[0] == "contents"]
    rows, ledger = reread_rows(c, rid2)
    s2 = source(c)
    check("cr1 run 2 reads the vendor's page of the pick's priced claims exactly once (the fake counts one contents call, to that url), under the durable id <run>:research_plan:cur1:b0:tool:1 with its ledger row (kind contents, completed), and the stored copy now reads livecrawl with a date inside run 2",
          res2["status"] == "completed" and reads == [("contents", PF.PAGE_URL)] and rows == [(f"{rid2}:research_plan:cur1:b0:tool:1", "completed", rows[0][2] if rows else None)] and rows[0][2] and ledger == [("contents", "completed")]
          and s2["cache_status"] == "livecrawl" and ts(s2["retrieved_at"]) >= ts(started2),
          {"res": res2["status"], "reads": reads, "rows": rows, "ledger": ledger, "source": s2, "started": started2})
    d2 = decisions(c)
    last = d2[-1]
    check("cr1 the pick is not left unresolved for want of a fetch: run 2's decision is active and neither its rationale nor its memo carries the volatile-figure reason",
          res2["status"] == "completed" and len(d2) == 2 and last["status"] == "active" and NOT_LIVE not in last["rationale"] and NOT_LIVE not in json.dumps(last["memo"] or {}),
          [(x["id"], x["status"], x["rationale"][:160]) for x in d2])
    cur = (last["memo"] or {}).get("currency") or ""
    check("cr1 the memo's currency line is today's check applied to the fresh copy: it names the vendor's own page fetched in this run and no longer says that no vendor page was fetched",
          cur.startswith("Currency:") and NOT_FETCHED not in cur and "fetched in this run" in cur, cur)
    close(r, stub)


# ---------------------------------------------------------------- cr2: the read fails
def cr2():
    sim = SimStatus("unknown")
    r, stub, c, res1 = fixture("cr2", sim)
    n = len(sim.calls); sim.status = "livecrawl"; sim.fail = 500
    res2 = drive(r, r.requality("P1", "cr2: the second run, the provider refusing the read", research=False)); rid2 = res2["run_id"]
    reads = [x for x in sim.calls[n:] if x[0] == "contents"]
    rows, ledger = reread_rows(c, rid2)
    d2 = decisions(c); last = d2[-1]; s2 = source(c)
    check("cr2 the one read is attempted and fails (the tool row failed with its ledger row, nothing else read), the stored copy stays as run 1 left it, and the run completes",
          res2["status"] == "completed" and reads == [("contents", PF.PAGE_URL)] and [(x[0], x[1]) for x in rows] == [(f"{rid2}:research_plan:cur1:b0:tool:1", "failed")] and ledger == [("contents", "failed")] and s2["cache_status"] == "unknown",
          {"res": res2["status"], "reads": reads, "rows": rows, "ledger": ledger, "source": s2})
    cur = (last["memo"] or {}).get("currency") or ""
    check("cr2 the run's line stands as run 1 left it: the decision is a conditional recommendation, not yet proven on the budget, its confirming check carrying the volatile-figure reason, and the memo's currency line says that no vendor page of its evidence was fetched in this run",
          len(d2) == 2 and last["status"] == "active" and last["rationale"].startswith("Recommended, not yet proven on Monthly budget (USD)") and NOT_LIVE in str(((last["memo"] or {}).get("conditional") or {}).get("check")) and cur.startswith("Currency:") and NOT_FETCHED in cur, [(x["id"], x["status"], x["rationale"][:160], (x["memo"] or {}).get("currency")) for x in d2])
    close(r, stub)


# ---------------------------------------------------------------- cr3: a same-run fetch makes no extra read
def cr3():
    sim = SimStatus("livecrawl")
    r, stub = PF.runner_for("cr3", script(), settings=SC, research=sim)
    res = drive(r, r.start("P1", PF.IDEA)); c = r.conn; rid = res["run_id"]
    reads = [x for x in sim.calls if x[0] == "contents"]
    rows, _ = reread_rows(c, rid)
    d = decisions(c); s = source(c)
    check("cr3 a run whose page the provider confirmed live takes today's path: the decision is active on the price confirmed in this run, the run's contents calls are the base's one (the verifier's re_fetch of the fixture page) and no cur<n> tool row exists",
          res["status"] == "completed" and len(d) == 1 and d[0]["status"] == "active" and reads == [("contents", PF.PAGE_URL)] and rows == [] and s["cache_status"] == "livecrawl",
          {"res": res["status"], "decisions": [(x["id"], x["status"], x["rationale"][:120]) for x in d], "reads": reads, "rows": rows, "source": s})
    close(r, stub)


# ---------------------------------------------------------------- cr4: the status mapping
def cr4():
    got = {s: (cache_status_from(s, 0), cache_status_from(s, 168)) for s in ("crawled", "livecrawl", "cached", None)}
    check("cr4 the provider's documented value for a live fetch, crawled, reads as livecrawl under any age (as live does); a cached copy reads cache_fallback when a live fetch was demanded and cached otherwise; no status reads unknown",
          got == {"crawled": ("livecrawl", "livecrawl"), "livecrawl": ("livecrawl", "livecrawl"), "cached": ("cache_fallback", "cached"), None: ("unknown", "unknown")}, got)


# ---------------------------------------------------------------- cr5: the targets rule
def cr5():
    pick = {"product_id": "neon-postgres", "product_version": "", "option": "Neon on the Free plan"}
    start = "2026-11-03T07:20:36.471Z"
    old, new = "2026-11-02T10:00:00Z", "2026-11-03T12:14:03Z"
    own1 = {"id": "p.SRC-001", "url": "https://neon.com/pricing", "retrieved_at": old, "tier": 2}
    own2 = {"id": "p.SRC-002", "url": "https://neon.com/docs/introduction/plans", "retrieved_at": old, "tier": 2}
    own3 = {"id": "p.SRC-003", "url": "https://www.neon.com/blog/pricing-update", "retrieved_at": old, "tier": 2}
    sec = {"id": "p.SRC-004", "url": "https://selfhost.dev/neon-pricing", "retrieved_at": old, "tier": 3}
    tier1 = {"id": "p.SRC-005", "url": "https://docs.example.org/neon", "retrieved_at": old, "tier": 1}
    ids = lambda rows: [r["id"] for r in rows]
    check("cr5 the vendor's own pages of the pick's priced claims not fetched in this run are read, in id order, at most two; a secondary page is never read",
          ids(DS.currency_reread_targets(pick, [sec, own2, own1], start)) == ["p.SRC-001", "p.SRC-002"] and ids(DS.currency_reread_targets(pick, [own3, own2, own1], start)) == ["p.SRC-001", "p.SRC-002"] and ids(DS.currency_reread_targets(pick, [sec], start)) == [],
          [ids(DS.currency_reread_targets(pick, rows, start)) for rows in ([sec, own2, own1], [own3, own2, own1], [sec])])
    check("cr5 a page fetched in this run is never read again (the own page retrieved after the run's start is left out), a tier-1 page counts as the vendor's and comes after the vendor's own pages, and an unreadable date reads as not fetched",
          ids(DS.currency_reread_targets(pick, [dict(own1, retrieved_at=new), own2, sec], start)) == ["p.SRC-002"] and ids(DS.currency_reread_targets(pick, [tier1, own1, own2], start)) == ["p.SRC-001", "p.SRC-002"]
          and ids(DS.currency_reread_targets(pick, [tier1, own1], start)) == ["p.SRC-001", "p.SRC-005"] and ids(DS.currency_reread_targets(pick, [dict(own1, retrieved_at="never")], start)) == ["p.SRC-001"]
          and ids(DS.currency_reread_targets(pick, [own1, own2], None)) == [],
          [ids(DS.currency_reread_targets(pick, rows, start)) for rows in ([dict(own1, retrieved_at=new), own2, sec], [tier1, own1, own2], [tier1, own1], [dict(own1, retrieved_at="never")])])


# ---------------------------------------------------------------- cr6: the cap of two reads per pick across the draft and the final
URLS3 = [PF.PAGE_URL, "https://docs.speech-a.example/plans", "https://docs.speech-a.example/billing"]
FOLLOWUP_URL = "https://docs.speech-a.example/followup"
PLANS = dict(zip(URLS3 + [FOLLOWUP_URL], ("basic", "standard", "volume", "followup")))


class PlanScript(PF.Script):
    """The pipeline fake's Script with one atomic price subject per page (the plan the page names before its price), so that extraction keeps
    the three pages' price claims apart; the product identity and the tariff are the fixture's."""
    def __call__(self, body):
        response = super().__call__(body)
        if PF.schema_of(body) in ("EvidenceExtractionOut", "EvidenceExtractionOutV2") and response.get("content"):
            m = re.search(r"Speech API A (basic|standard|volume|followup) plan[.]", PF.all_user_text(body))
            if m:
                payload = json.loads(response["content"])
                for claim in payload.get("claims", []):
                    if claim["kind"] == "price":
                        claim["subject"] = "Speech API A " + m.group(1) + " plan"
                        claim["text"] = claim["subject"] + ": price_per_hour is $0.60 per audio hour"
                response = PF.J(payload)
        return response


class SimThree(SimStatus):
    """Three vendor pages of the one pick in run 1 (each naming its plan before the price), the follow-up's page in run 2."""
    def __init__(self, status="unknown"):
        super().__init__(status); self.second = False
    def text_for(self, url):
        return self.page_text().replace(PF.FACT_PRICE, "Speech API A " + PLANS[url] + " plan. " + PF.FACT_PRICE)
    def search(self, q, f, n):
        self.calls.append(("search", q))
        urls = [FOLLOWUP_URL] if self.second else URLS3
        return [dict(url=u, title="Speech API A pricing", text=self.text_for(u), cache_status=self.status) for u in urls[:n]]
    def contents(self, urls, f):
        self.calls.append(("contents", urls[0]))
        return [dict(url=u, title="Speech API A pricing", text=self.text_for(u), cache_status=self.status) for u in urls]


def drive8(runner, res):
    for _ in range(8):
        if res["status"] != "awaiting_user":
            break
        res = runner.resume(res["run_id"], PF.answers_for(res))
    return res


def cr6():
    settings = copy.deepcopy(SC)
    sc = PlanScript(questions=False, subjects=("transcription",)); sc.vendor, sc.product = "Speech", "API A"; sc.select = ("speech-api-a", "v1", "Speech API A")
    sim = SimThree("unknown")
    r, stub = PF.runner_for("cr6", sc, settings=settings, research=sim)
    res1 = drive8(r, r.start("P1", PF.IDEA)); c = r.conn
    seed = [tuple(x) for x in c.execute("SELECT DISTINCT s.id, s.url, s.cache_status FROM sources s JOIN claims c ON c.source_id=s.id WHERE c.project_id='P1' AND c.kind='price' AND c.product_id='speech-api-a' ORDER BY s.id")]
    check("cr6 the fixture: run 1 completed and stored the pick's three vendor pages, each carrying a price claim of the pick, all with status unknown",
          res1["status"] == "completed" and [x[1] for x in seed] == URLS3 and {x[2] for x in seed} == {"unknown"}, {"res": res1["status"], "seed": seed})
    settings["discovery"].update(panel=True, simulate_panel=True, panel_cap_usd=0.05)   # the panel's follow-up takes the subject to the final comparison
    n = len(sim.calls); sim.second, sim.status = True, "livecrawl"
    res2 = drive8(r, r.requality("P1", "cr6: the second run reaching the final comparison", research=False)); rid2 = res2["run_id"]
    reads = [x for x in sim.calls[n:] if x[0] == "contents"]
    rows, ledger = reread_rows(c, rid2)
    finals = [x[0] for x in c.execute("SELECT step_key FROM run_steps WHERE run_id=? AND node='compare_options' AND step_key LIKE '%:final' AND status='completed'", (rid2,))]
    picks = {coverage.canonical_identity(x[0], x[1]) for x in c.execute("SELECT product_id, product_version FROM decisions WHERE project_id='P1'")}
    third = c.execute("SELECT cache_status FROM sources WHERE project_id='P1' AND url=?", (URLS3[2],)).fetchone()[0]
    check("cr6 run 2 completes and reaches the final comparison on the same pick (one final step completed; every decision of the project names speech-api-a v1 by canonical identity)",
          res2["status"] == "completed" and len(finals) == 1 and picks == {coverage.canonical_identity("speech-api-a", "v1")}, {"res": res2["status"], "finals": finals, "picks": sorted(picks)})
    check("cr6 at most two reads per pick across the draft and the final: run 2's cur<n> tool rows count two (the draft's two vendor pages, in id order, each completed with its ledger row), the fake counts the same two contents calls, no page twice, and the third page's copy stays as run 1 left it",
          len(rows) == 2 and [x[1] for x in rows] == ["completed", "completed"] and ledger == [("contents", "completed")] * 2 and reads == [("contents", URLS3[0]), ("contents", URLS3[1])] and third == "unknown",
          {"rows": rows, "ledger": ledger, "reads": reads, "third_page": third})
    close(r, stub)


# ---------------------------------------------------------------- cr7: the changed-price path refreshes the cost table's inputs
NEW_PRICE = "Price per hour: $0.90 per audio hour."


class SimChanged(SimStatus):
    """The fixture's Sim whose contents call returns the page with a changed price once `changed` is set (the vendor raised the rate between the runs)."""
    changed = False
    def contents(self, urls, f):
        rows = super().contents(urls, f)
        return [dict(p, text=p["text"].replace(PF.FACT_PRICE, NEW_PRICE)) for p in rows] if self.changed else rows


def cr7():
    sim = SimChanged("unknown")
    r, stub, c, res1 = fixture("cr7", sim)
    n = len(sim.calls); sim.status, sim.changed = "livecrawl", True
    res2 = drive(r, r.requality("P1", "cr7: the second run, the vendor's page carrying a new price", research=False)); rid2 = res2["run_id"]
    reads = [x for x in sim.calls[n:] if x[0] == "contents"]
    claims = [dict(zip(("id", "kind", "value", "status", "scope_status", "verdict", "invalidated_at"), x)) for x in insp(c).execute("SELECT id, kind, value, status, scope_status, verdict, invalidated_at FROM claims WHERE project_id='P1' ORDER BY id")]
    invalid = sorted(x["id"] for x in claims if x["kind"] == "price" and x["verdict"] is None and x["invalidated_at"])
    tables = [dict(id=t[0], decision_id=t[1], fields=json.loads(t[2]), rows=json.loads(t[3]), claim_ids=json.loads(t[4])) for t in c.execute("SELECT id, decision_id, fields_json, rows_json, claim_ids_json FROM derived_tables WHERE project_id='P1' AND run_id=? ORDER BY id", (rid2,))]
    stale = [(t["id"], row) for t in tables for row, ids in zip(t["rows"], t["claim_ids"]) if set(ids) & set(invalid) and "calculated estimate" in row]
    affected = [(t["id"], row) for t in tables for row, ids in zip(t["rows"], t["claim_ids"]) if set(ids) & set(invalid)]
    check("cr7 the fixture's second run: one read of the vendor's page, whose fresh copy carries a new price, so the stored price claim lost its verdict (D-A114: invalidated, status unverified), and the run completes",
          res2["status"] == "completed" and reads == [("contents", PF.PAGE_URL)] and invalid == ["P1.CLM-001"] and [x["status"] for x in claims if x["id"] == "P1.CLM-001"] == ["unverified"],
          {"res": res2["status"], "reads": reads, "claims": [(x["id"], x["kind"], x["status"], x["verdict"], bool(x["invalidated_at"])) for x in claims]})
    check("cr7 run 2's derived cost table computes no estimate from the invalidated price: its row for that claim is not computable and names the claim's current status (unverified), never the old rate as a calculated estimate within budget",
          tables and affected and not stale and all("not computable: price evidence not eligible (status unverified" in row[-1] and row[7] is None for _, row in affected),
          {"tables": [(t["id"], t["decision_id"]) for t in tables], "stale": stale, "affected": affected})
    d2 = decisions(c)[-1]; memo = d2["memo"] or {}
    cited_in_memo = sorted({c0 for o in memo.get("options", []) for c0 in o.get("claim_ids", [])} | {c0 for w in memo.get("why", []) for c0 in w.get("claim_ids", [])})
    check("cr7 the memo of run 2's decision cites the invalidated claim nowhere (no option row and no why sentence names it) and the decision is not active on it",
          not (set(cited_in_memo) & set(invalid)) and d2["status"] != "active", {"decision": (d2["id"], d2["status"]), "cited_in_memo": cited_in_memo})
    close(r, stub)


# ---------------------------------------------------------------- cr8: an alternative's price cleared while the pick's own citations survive
class OptionOnlyScript(PF.Script):
    """The pipeline fake's Script whose comparison cites the capability claims alone for the pick's row and its why sentence (no price), and the
    dearer second version's own price claim on that alternative's row."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k); self.second_product = True; self.answers = []
    def __call__(self, body):
        response = super().__call__(body)
        if PF.schema_of(body) != "ComparisonOutV2" or not response.get("content"):
            return response
        u = next(m["content"] for m in body["messages"] if m["role"] == "user" and "Candidates: " in str(m.get("content")))
        shown = PF.claims_shown(u)
        prices = {c0["claim_id"] for c0 in shown if "price" in c0["text"].lower() and not c0.get("contradicts")}
        alt_prices = {c0["claim_id"] for c0 in shown if "v2" in c0["text"] and "price" in c0["text"].lower() and not c0.get("contradicts")}
        payload = json.loads(response["content"])
        for option in payload["options"]:
            option["claim_ids"] = sorted(alt_prices) if str(option["product_version"]).lower().lstrip("v") == "2" else [c0 for c0 in option["claim_ids"] if c0 not in prices]
        for why in payload["why"]:
            why["claim_ids"] = [c0 for c0 in why["claim_ids"] if c0 not in prices]; why["text"] = "The cited capability supports Hebrew."
        self.answers.append({"pick": sorted(c0 for o in payload["options"] if str(o["product_version"]).lower().lstrip("v") == "1" for c0 in o["claim_ids"]),
                             "alternative": sorted(alt_prices), "why": sorted(c0 for w in payload["why"] for c0 in w["claim_ids"])})
        return PF.J(payload)


class SimZeroHour(SimChanged):
    """SimChanged whose contents rows carry the real adapter's page metadata of a volatile read (freshness, max_age_hours 0: page_record), so the
    fresh copy takes the zero-hour path in store_pages: a verified claim whose sentence is byte for byte in the new text keeps its verdict,
    a changed sentence loses it. The claims are snapshotted immediately before the changed read (the state the comparison's inputs were loaded from)."""
    conn = None; before = None
    def contents(self, urls, f):
        if self.changed and self.conn is not None:
            self.before = claim_rows(self.conn)
        return [dict(p, freshness=f, max_age_hours=0) for p in super().contents(urls, f)]


def claim_rows(c):
    names = ("id", "kind", "status", "scope_status", "verdict", "invalidated_at", "source_version", "current_version")
    return {row[0]: dict(zip(names, row)) for row in insp(c).execute("SELECT c.id, c.kind, c.status, c.scope_status, c.verdict, c.invalidated_at, c.source_version, s.content_hash FROM claims c JOIN sources s ON s.id=c.source_id WHERE c.project_id='P1' ORDER BY c.id")}


def cr8():
    sc = OptionOnlyScript(questions=False, subjects=("transcription",)); sc.vendor, sc.product = "Speech", "API A"; sc.select = ("speech-api-a", "v1", "Speech API A")
    sim = SimZeroHour("unknown")
    r, stub = PF.runner_for("cr8", sc, settings=SC, research=sim); sim.conn = r.conn
    res1 = drive8(r, r.start("P1", PF.IDEA)); c = r.conn
    s1 = source(c)
    n = len(sim.calls); sim.status, sim.changed = "livecrawl", True
    res2 = drive8(r, r.requality("P1", "cr8: the second run, the vendor's page carrying a new price beside unchanged capabilities", research=False)); rid2 = res2["run_id"]
    reads = [x for x in sim.calls[n:] if x[0] == "contents"]
    rows, ledger = reread_rows(c, rid2)
    answer = sc.answers[-1]; before = sim.before or {}; after = claim_rows(c)
    own = sorted(set(answer["pick"]) | set(answer["why"])); alt = answer["alternative"]
    live = lambda row: row["status"] == "supported" and row["verdict"] == "supported" and not row["invalidated_at"] and row["source_version"] == row["current_version"]
    check("cr8 the fixture: both runs complete, run 1 stored the vendor's page unknown, run 2's comparison cites capability claims alone for the pick's row and its why sentence and the alternative's own price claim on its row, and every one of them was supported at the current version immediately before run 2's one read of the page",
          res1["status"] == "completed" and res2["status"] == "completed" and s1 is not None and s1["cache_status"] == "unknown" and reads == [("contents", PF.PAGE_URL)] and len(rows) == 1 and ledger == [("contents", "completed")]
          and own and alt and all(live(before[x]) for x in own + alt),
          {"res": (res1["status"], res2["status"]), "source": s1, "reads": reads, "rows": rows, "answer": answer, "before": {x: (row["status"], row["verdict"], bool(row["invalidated_at"])) for x, row in before.items()}})
    invalid = sorted(x for x, row in after.items() if row["kind"] == "price" and row["verdict"] is None and row["invalidated_at"])
    check("cr8 the zero-hour path: the fresh copy cleared the price claims (the changed sentence; invalidated, status unverified) and kept the capability claims verified at the new version, so every citation of the pick's row and its why sentence survives",
          set(alt) <= set(invalid) and all(after[x]["status"] == "unverified" for x in invalid) and all(live(after[x]) for x in own) and all(after[x]["kind"] != "price" for x in own),
          {"invalid": invalid, "after": {x: (row["kind"], row["status"], row["verdict"], bool(row["invalidated_at"]), row["source_version"] == row["current_version"]) for x, row in after.items()}})
    d2 = decisions(c)[-1]; memo = d2["memo"] or {}
    option_cites = {o.get("option"): sorted(o.get("claim_ids", [])) for o in memo.get("options", [])}
    stale = sorted({c0 for ids in option_cites.values() for c0 in ids} & set(invalid))
    edges = {x[0] for x in c.execute("SELECT from_id FROM trace_edges WHERE project_id='P1' AND to_id=? AND kind='claim_decision'", (d2["id"],))}
    check("cr8 the memo's option rows cite no claim the fresh copy cleared, the alternative's row included, while the pick's row keeps its surviving citations; the decision is not active on the cleared price: it is a conditional recommendation, not yet proven on the budget (D-A408), and no cleared claim is among its citations",
          not stale and sorted(set(answer["pick"])) == sorted(set(option_cites.get("Speech API A", [])) & set(answer["pick"]))
          and (d2["status"] != "active" or ("budget_monthly_usd" in [q.get("key") for q in (memo.get("conditional") or {}).get("requirements") or []] and not (edges & set(invalid)) and (memo.get("budget") or {}).get("state") != "within")),
          {"decision": (d2["id"], d2["status"]), "option_cites": option_cites, "stale_option_citations": stale, "invalid": invalid})
    close(r, stub)


SCENARIOS = {"cr1": cr1, "cr2": cr2, "cr3": cr3, "cr4": cr4, "cr5": cr5, "cr6": cr6, "cr7": cr7, "cr8": cr8}

if __name__ == "__main__":
    names = sys.argv[1:] or list(SCENARIOS)
    for name in names:
        SCENARIOS[name]()
    print(f"test_currency_reread: {sum(1 for _, ok in results if ok)} of {len(results)} checks passed")
