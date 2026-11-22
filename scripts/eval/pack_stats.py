"""The evidence packs (app/core/evidence_pack.py) measured on a copy of a backup of the application database. No model, no network.

For every decision subject of the project the script builds the packs exactly as app/graph/workflow.py compare_options builds them:
  the subject's own verified claims;
  the citable set of workflow.citable_of;
  the eligible identities of workflow.eligible_candidates;
  the binding hard requirements of applicability.binding;
  the module's default caps or the ones given on the command line.

It prints one row per subject: candidates, claims considered, included, omitted (by reason), material contradictions, insufficient_capacity packs,
and the estimated prompt tokens of the comparison's evidence input before (the `verified_claims` lines of version 6 of the comparison prompt)
and after (the `evidence_packs` view of version 7), with the totals. A development measurement: it is labelled with the code tree and the
caps, never release performance.

    .venv/bin/python scripts/eval/pack_stats.py --db <copy of a backup> --out <json> [--project p6] [--max-claims N] [--max-tokens T] [--root <code tree>]

The copy is opened read-only (file:<path>?mode=ro) and never written; the derivation, which writes derived state, runs on a scratch copy taken
through SQLite's online backup into a temporary folder beside the copy, deleted when the script ends. A live store (one of the author's working stores,
which scripts/ops/replay.py's is_live_path recognises by its path) is refused with exit 3
before anything opens. Exit 0 when the packs were built, 2 on an error. The last line printed is always
`pack_stats: subjects <n>, candidates <n>, included <n>, omitted <n>, contradictions <n>, insufficient <n>, tokens_before <n>, tokens_after <n>, exit <code>`."""
import argparse, collections, json, os, pathlib, shutil, sys, tempfile

HERE = pathlib.Path(__file__).resolve()
DEFAULT_ROOT = HERE.parents[2]
sys.path.insert(0, str(DEFAULT_ROOT / "scripts" / "ops"))
from replay import is_live_path, open_ro                                       # the fixed-data gate's refusal of a live store and its read-only open

SUMMARY = "pack_stats: subjects {}, candidates {}, included {}, omitted {}, contradictions {}, insufficient {}, tokens_before {}, tokens_after {}, exit {}"
COLS = ("id", "text", "kind", "source_id", "quote", "status", "scope_status", "conditions_json", "subject", "attribute", "value", "range_start", "range_end", "decision_subject", "product_id", "product_version", "source_version")


def v6_lines(conn, pid: str, rel: list, citable: set, rv: int) -> list:
    """The v6 prompt's verified_claims lines, as workflow.py built them before the evidence packs."""
    out = []
    for c in rel:
        if c["id"] not in citable:
            continue
        supports = [r[0] for r in conn.execute("SELECT requirement_id FROM claim_requirement_support WHERE project_id=? AND claim_id=? AND requirement_version=? AND verdict='supported' ORDER BY requirement_id", (pid, c["id"], rv))]
        out.append({"claim_id": c["id"], "text": c["text"], "status": c["status"], "scope_status": c["scope_status"], "conditions": json.loads(c["conditions_json"] or "[]"), "source_id": c["source_id"], "product_id": c.get("product_id") or "", "product_version": c.get("product_version") or "",
                    "supports_requirements": supports, "counts_for_hard_constraints": True})
    return out


def measure(db, project: str, root: pathlib.Path, caps_override: dict) -> dict:
    sys.path.insert(0, str(root))
    from app.core import applicability as AP, coverage, evidence_pack as EP, reconcile
    from app.core.sqlguard import open_connection
    from app.graph import workflow as WF
    db = pathlib.Path(db).resolve()
    scratch_dir = tempfile.mkdtemp(prefix="nmb-pack-stats-scratch-", dir=str(db.parent))
    try:
        src = open_ro(db)
        try:
            conn = open_connection(str(pathlib.Path(scratch_dir) / "scratch.db"))
            src.backup(conn)
        finally:
            src.close()
        reconcile.reconcile_project(conn, project)
        caps = dict(EP.EVIDENCE_PACK_DEFAULTS, **{k: v for k, v in caps_override.items() if v is not None})
        norm = lambda s: coverage._norm(s or "")
        rows = []
        with coverage.derived(conn, project):
            q = lambda s, *a: conn.execute(s, a).fetchall()
            rv = q("SELECT requirement_version FROM projects WHERE id=?", project)[0][0]
            reqs = [{"id": r[0], "key": r[1], "value": json.loads(r[2]), "hard": bool(r[3]), "applies_to": AP.applies_to_of({"applies_to": r[4]})} for r in q("SELECT id, key, value_json, hard, applies_to FROM requirements WHERE project_id=? AND status='confirmed' ORDER BY id", project)]
            hard_all = [r for r in reqs if r["hard"]]
            claims = [dict(zip(COLS, r)) for r in q("SELECT " + ",".join("c." + c for c in COLS) + " FROM claims c WHERE c.project_id=? ORDER BY c.id", project)]
            rq_claims: dict = collections.defaultdict(set)
            for rq, cj in q("SELECT subject_id, claim_ids_json FROM coverage WHERE project_id=? AND subject_type='research_question'", project):
                for cid in json.loads(cj):
                    rq_claims[cid].add(rq)
            rq_subject = {r[0]: r[1] for r in q("SELECT id, decision_subject FROM research_questions WHERE project_id=?", project)}
            subjects = sorted({r[0] for r in q("SELECT DISTINCT subject FROM decisions WHERE project_id=?", project)} | {norm(s) for s in rq_subject.values() if s})
            top = q("SELECT COALESCE(MAX(plan_version), 0) FROM decisions WHERE project_id=?", project)[0][0]
            current = [r[0] for r in q("SELECT DISTINCT subject FROM decisions WHERE project_id=? AND plan_version=?", project, top)]   # the subjects of the newest plan version, as the comparison knows its run's subjects
            for sub in subjects:
                rel = WF.subject_claims(conn, project, sub, claims, rq_claims, rq_subject, current)   # the comparison's own selection (the subject's claims and its predecessor's)
                if not rel:
                    rows.append({"subject": sub, "candidates": 0, "considered": 0, "included": 0, "omitted": 0, "omitted_cap": 0, "omitted_duplicate": 0, "omitted_superseded": 0, "contradictions": 0, "insufficient_capacity": 0, "not_evidence": 0, "unassigned_citable": 0, "tokens_before": 0, "tokens_after": 0, "largest_pack": None})
                    continue
                citable = WF.citable_of(conn, project, rel, rv)
                hard = AP.binding(hard_all, sub)
                ident = WF.eligible_candidates(conn, project, sub, rel)["identities"]
                inputs = EP.load_inputs(conn, project, sub, hard, rv, rel, citable, rq_claims, rq_subject, predecessor=WF.predecessor_of(conn, project, sub, current))
                packs = EP.build_packs(inputs, ident, caps)
                s0 = EP.summary(packs)
                in_packs = {c["claim_id"] for p in packs for c in p["claims"]} | {o["claim_id"] for p in packs for o in p["omitted"]}
                before = EP.tokens_of(v6_lines(conn, project, rel, citable, rv))
                after = EP.tokens_of([EP.prompt_view(p) for p in packs])
                largest = max(packs, key=lambda p: p["counts"]["included"], default=None)
                rows.append(dict({"subject": sub}, **s0, unassigned_citable=sum(1 for cid in citable if cid not in in_packs), tokens_before=before, tokens_after=after,
                                 largest_pack={"label": largest["label"], "included": largest["counts"]["included"], "omitted": largest["counts"]["omitted"], "tokens": largest["tokens"]} if largest else None,
                                 insufficient=[{"label": p["label"], "did_not_fit": p["did_not_fit"]} for p in packs if p["insufficient_capacity"]]))
        conn.close()
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    totals = {k: sum(r.get(k, 0) for r in rows) for k in ("candidates", "considered", "included", "omitted", "omitted_cap", "omitted_duplicate", "omitted_superseded", "contradictions", "insufficient_capacity", "not_evidence", "unassigned_citable", "tokens_before", "tokens_after")}
    return {"db": str(db), "project": project, "code_tree": str(root), "caps": caps, "subjects": rows, "totals": totals, "label": "development measurement on a copy of a backup; not release performance"}


def table(res: dict) -> str:
    head = "| subject | candidates | considered | included | omitted (cap / duplicate / superseded) | material contradictions | insufficient_capacity | tokens before (v6) | tokens after (v7) |"
    lines = [head, "|---|---|---|---|---|---|---|---|---|"]
    for r in res["subjects"] + [dict(res["totals"], subject="total")]:
        lines.append(f"| {r['subject']} | {r['candidates']} | {r['considered']} | {r['included']} | {r['omitted']} ({r['omitted_cap']} / {r['omitted_duplicate']} / {r['omitted_superseded']}) | {r['contradictions']} | {r['insufficient_capacity']} | {r['tokens_before']:,} | {r['tokens_after']:,} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The evidence packs measured on a copy of a backup: per decision subject, what the v7 comparison would see. Never on a live store.")
    ap.add_argument("--db", required=True, help="a copy of a backup (never the live store)")
    ap.add_argument("--out", required=True, help="the JSON written")
    ap.add_argument("--project", default="p6")
    ap.add_argument("--max-claims", type=int, default=None, help="max_claims_per_pack (default: the module's)")
    ap.add_argument("--max-tokens", type=int, default=None, help="max_tokens_per_pack (default: the module's)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="the code tree (default: this repository)")
    a = ap.parse_args(argv)
    if is_live_path(a.db):
        print(f"pack_stats: refused: {a.db} is a live store; the measurement reads only a verified copy of a backup")
        print(SUMMARY.format(*(["none"] * 8), 3))
        return 3
    try:
        res = measure(a.db, a.project, pathlib.Path(a.root).resolve(), {"max_claims_per_pack": a.max_claims, "max_tokens_per_pack": a.max_tokens})
        pathlib.Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"pack_stats: error {type(e).__name__}: {e}", file=sys.stderr)
        print(SUMMARY.format(*(["unknown"] * 8), 2))
        return 2
    print(f"pack_stats on {res['db']} (project {res['project']}, caps {res['caps']}, code tree {res['code_tree']}): {res['label']}")
    print(table(res))
    for r in res["subjects"]:
        if r.get("largest_pack"):
            print(f"  {r['subject']}: largest pack {r['largest_pack']['label']!r} with {r['largest_pack']['included']} included, {r['largest_pack']['omitted']} omitted, {r['largest_pack']['tokens']} tokens; {r['unassigned_citable']} citable claim(s) about no eligible candidate; {r['not_evidence']} claim(s) no evidence")
        for bad in r.get("insufficient") or []:
            print(f"  insufficient_capacity: {r['subject']} {bad['label']!r}: {bad['did_not_fit']}")
    t = res["totals"]
    print(SUMMARY.format(len(res["subjects"]), t["candidates"], t["included"], t["omitted"], t["contradictions"], t["insufficient_capacity"], t["tokens_before"], t["tokens_after"], 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
