"""The fixed-data gate: re-derive one project on a copy of a backup with this code tree and report what the comparison
would see. No model, no network.

    .venv/bin/python scripts/ops/replay.py --db <copy of a backup> --out <json> [--project p6] [--root <code tree>]

The copy is opened read-only (file:<path>?mode=ro, the path percent-escaped so that the file opened is the file named), so a replay can never write the copy it reads; the derivation, which writes
derived state, runs on a scratch copy taken through SQLite's online backup from that handle into a temporary folder beside the
copy, deleted when the replay ends. An output path that is the copy's file under any name (the same spelling, a link, a hard link) is an error before anything opens. Exit 0 when reconcile.decisions_changed is 0 and no decision changed its derived status;
1 otherwise; 2 on an error; 3 when the path names a live store (one of the author's working stores, which is_live_path
recognises by its path), refused before anything opens. The last line printed is always
`replay: decisions_changed <n>, status_changed <n>, exit <code>`."""
import argparse, collections, json, os, pathlib, shutil, sys, tempfile, urllib.parse

HERE = pathlib.Path(__file__).resolve()
DEFAULT_ROOT = HERE.parents[2]                                           # scripts/ops/replay.py: the repository root of the script
LIVE_SUFFIX = "NextMVBee-atelier/data/app.db"                             # a path that ends with this names a live store
WORKTREES = str(DEFAULT_ROOT.parent) + os.sep                            # so does a data/app.db in any working copy beside this repository (the folder that holds it)
PID = "p6"
SUMMARY = "replay: decisions_changed {}, status_changed {}, exit {}"


def is_live_path(path) -> bool:
    """A live store, judged on the path as given (made absolute) and on its resolved form, without opening anything."""
    given = os.fspath(path)
    forms = {os.path.abspath(given), os.path.realpath(given)}
    return any(p.endswith(LIVE_SUFFIX) or (p.startswith(WORKTREES) and p.endswith("/data/app.db")) for p in forms)


def open_ro(path):
    """The copy, read-only, as the application's guarded handle (never a raw connection)."""
    from app.core.sqlguard import open_connection
    return open_connection("file:" + urllib.parse.quote(str(pathlib.Path(path).resolve())) + "?mode=ro", uri=True)   # the path escaped: a '#' or '?' in it names a file, never a fragment or a parameter


def exit_code(rep: dict) -> tuple[int, int, int]:
    """(exit, decisions_changed, status_changed) from a reconcile report: 0 only when no decision changed at all."""
    changed = rep.get("decisions_changed") or []
    status = sum(1 for d in changed if d.get("from") != d.get("to"))
    return (0 if not changed and status == 0 else 1), len(changed), status


def replay(db, project: str, root: pathlib.Path) -> dict:
    sys.path.insert(0, str(root))
    from app.core import coverage, reconcile
    from app.core.sqlguard import open_connection
    from app.graph import workflow as WF
    PID = project
    db = pathlib.Path(db).resolve()
    scratch_dir = tempfile.mkdtemp(prefix="nmb-replay-scratch-", dir=str(db.parent))
    try:
        src = open_ro(db)
        try:
            conn = open_connection(str(pathlib.Path(scratch_dir) / "scratch.db"))
            src.backup(conn)
        finally:
            src.close()
        rep = reconcile.reconcile_project(conn, PID)
        norm = lambda s: " ".join(str(s or "").lower().replace("_", " ").split())
        res = {"reconcile": {k: len(rep.get(k) or []) for k in ("claims_status_changed", "support_withdrawn", "support_restored", "conflicts_opened", "conflicts_resolved", "conflicts_reopened", "decisions_changed", "coverage_changed")}}
        with coverage.derived(conn, PID):
            q = lambda s, *a: conn.execute(s, a).fetchall()
            rv = q("SELECT requirement_version FROM projects WHERE id=?", PID)[0][0]
            res["rq"] = dict(collections.Counter(r[0] for r in q("SELECT evidence_status FROM coverage WHERE project_id=? AND subject_type='research_question'", PID)))
            res["conflicts"] = dict(collections.Counter(r[0] for r in q("SELECT status FROM claim_conflicts WHERE project_id=?", PID)))
            res["claims"] = dict(collections.Counter(r[0] for r in q("SELECT status FROM claims WHERE project_id=?", PID)))
            cols = ("id", "text", "kind", "source_id", "status", "scope_status", "subject", "attribute", "value", "decision_subject", "product_id", "product_version")
            claims = [dict(zip(cols, r)) for r in q("SELECT " + ",".join(cols) + " FROM claims WHERE project_id=? ORDER BY id", PID)]
            rq_claims = collections.defaultdict(set)
            for rq, cj in q("SELECT subject_id, claim_ids_json FROM coverage WHERE project_id=? AND subject_type='research_question'", PID):
                for cid in json.loads(cj):
                    rq_claims[cid].add(rq)
            rq_subject = {r[0]: r[1] for r in q("SELECT id, decision_subject FROM research_questions WHERE project_id=?", PID)}
            rq_status = {r[0]: r[1] for r in q("SELECT subject_id, evidence_status FROM coverage WHERE project_id=? AND subject_type='research_question'", PID)}
            subj = {}
            for sub in ("email_delivery_service", "speech_to_text_service", "summary_llm_model", "web_hosting_and_storage"):
                own = {cid for cid, rs in rq_claims.items() if any(norm(rq_subject.get(rq)) == norm(sub) for rq in rs)} | {c["id"] for c in claims if norm(c["decision_subject"]) == norm(sub)}
                rel = [c for c in claims if c["status"] != "unverified" and c["id"] in own]
                if hasattr(WF, "citable_of"):
                    cit = WF.citable_of(conn, PID, rel, rv)
                else:
                    ids = {c["id"] for c in rel if c["status"] == "supported" and c["scope_status"] in ("in_scope", "conditional")}
                    ids &= {row["id"] for row in coverage.claim_rows(conn, PID, sorted(ids), rv) if row["valid"]}
                    cit = {cid for cid in ids if any(rq_status.get(rq) == "supported" and norm(rq_subject.get(rq)) == norm(sub) for rq in rq_claims.get(cid, ()))}
                el = WF.eligible_candidates(conn, PID, sub, rel)
                by_id = {c["id"]: c for c in rel}
                heb = collections.Counter((by_id[c]["product_id"], by_id[c]["product_version"]) for c in cit if "hebrew" in f"{by_id[c]['text']} {by_id[c]['value']}".lower())
                ident = el["identities"]
                subj[sub] = {"rel": len(rel), "citable": len(cit), "eligible": len(ident), "refused": len(el.get("refused") or {}), "eligible_with_citable_hebrew": sorted(f"{k[0]} {k[1]}".strip() for k in ident if heb.get(k)),
                             "citable_of_key_identities": {f"{p} {v}".strip(): sum(1 for c in cit if by_id[c]["product_id"] == p and by_id[c]["product_version"] == v) for p, v in (("openai-gpt-4o-mini", ""), ("openai-gpt-4o-mini", "gpt-4o-mini"), ("railway-railway", "hobby"), ("resend-resend", "free"), ("soniox-soniox-stt-api", ""))}, "citable_ids": sorted(cit), "identities": sorted(f"{k[0]} {k[1]}".strip() for k in ident)}
            res["subjects"] = subj
        conn.close()
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    code, n_changed, n_status = exit_code(rep)
    res["decisions"] = rep.get("decisions_changed") or []
    res["status_changed"] = n_status; res["exit"] = code
    res["db"] = str(db); res["project"] = PID; res["code_tree"] = str(root)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The fixed-data gate: re-derive one project on a copy of a backup and report what changed. Never on a live store.")
    ap.add_argument("--db", required=True, help="a copy of a backup (never the live store)")
    ap.add_argument("--out", required=True, help="the JSON written")
    ap.add_argument("--project", default=PID)
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="the code tree (default: this repository)")
    a = ap.parse_args(argv)
    if is_live_path(a.db):
        print(f"replay: refused: {a.db} is a live store; a replay reads only a verified copy of a backup")
        print(SUMMARY.format("none", "none", 3))
        return 3
    try:
        if os.path.exists(a.out) and os.path.samefile(a.out, a.db):
            raise ValueError(f"--out {a.out} names the copy itself; the JSON is written elsewhere")                     # an output never replaces its input; the same file under any name (a link, a hard link, the same spelling)
        res = replay(a.db, a.project, pathlib.Path(a.root).resolve())
        pathlib.Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"replay: error {type(e).__name__}: {e}", file=sys.stderr)
        print(SUMMARY.format("unknown", "unknown", 2))
        return 2
    print(json.dumps({"reconcile": res["reconcile"], "rq": res["rq"], "conflicts": res["conflicts"], "claims": res["claims"], "subjects": {k: {x: v[x] for x in ("rel", "citable", "eligible", "refused")} for k, v in res["subjects"].items()}}, indent=0))
    print(SUMMARY.format(res["reconcile"]["decisions_changed"], res["status_changed"], res["exit"]))
    return res["exit"]


if __name__ == "__main__":
    sys.exit(main())
