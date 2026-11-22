"""Privacy for cloud-only v1: two modes, an explicit data map, retention and complete project deletion.
Zero data retention (zdr) restricts OpenRouter inference endpoints only; it does not cover Exa or other tools.
Exa offers zero data retention on Enterprise plans. Query sanitising reduces exposure; it does not guarantee
that no private detail remains."""
import os, pathlib, shutil, sqlite3
from app.core.sqlguard import require_guarded

MODES = {
    "cloud": {"label": "Cloud AI, web research on", "provider": {"require_parameters": True, "data_collection": "deny", "allow_fallbacks": True}},
    "cloud_zdr": {"label": "Cloud AI with zero-retention model endpoints; web research still goes to Exa",
                  "provider": {"require_parameters": True, "data_collection": "deny", "allow_fallbacks": True, "zdr": True}},
}
OFFLINE_MESSAGE = "Fully offline mode is planned for a later version. I can still plan your product for local processing."

DATA_MAP = [
    # item, sent to the model provider (via OpenRouter), sent to Exa, stored by NextMVBee, retention, deletion
    ("Idea text and answers", "yes, in prompts", "no", "app.db requirements and questions", "until the project is deleted", "delete project"),
    ("Search queries", "the model drafts them", "yes, after sanitising", "app.db and run log", "until the project is deleted", "delete project"),
    ("Retrieved page text and passages", "relevant excerpts only", "Exa fetches the URL", "data/sources files, app.db passages and their FTS rows", "until the project is deleted or the source is refreshed", "delete project removes files, passages and FTS rows"),
    ("Claims, decisions, plan versions", "relevant records only", "no", "app.db", "until the project is deleted", "delete project"),
    ("Workflow checkpoints", "no", "no", "checkpoints.db by thread_id", "until the project is deleted", "checkpointer.delete_thread"),
    ("Exports (Markdown, JSON; the HTML, PDF and DOCX of a plan version with the document model, the renderer's report and the acceptance record)", "no (Phase 8d, D-A33: the renderer is deterministic code; no provider receives the plan's content for layout)", "no", "data/exports/<project>/ (v<version>/<job id>/), rows in exports and export_assets", "until the project is deleted; a downloaded copy cannot be recalled", "delete project removes the rows and the whole directory; an active production is cancelled first (forced)"),
    ("Search cache", "no", "no", "app.db search_cache, owned by the project", "until the project is deleted", "cascade delete"),
    ("Backups of data/", "no", "no", "backup location chosen by the founder", "rotate within 30 days", "a deleted project disappears from backups when they rotate"),
    ("Logs", "no", "no", "IDs, costs and errors only; never prompts", "30 days", "rotation"),
    ("Provider-side copies", "per provider policy; zdr limits inference endpoints", "per Exa policy; zero retention on Enterprise", "not controlled by NextMVBee", "provider terms", "provider process"),
]


def provider_prefs(mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown privacy mode {mode!r}; the first release offers cloud and cloud_zdr")
    return dict(MODES[mode]["provider"])


class ActiveRunBlocksDeletion(Exception):
    pass


def delete_project(conn: sqlite3.Connection, checkpointer, project_id: str, data_root: str | None = None, force: bool = False) -> dict:
    """Remove a project from NextMVBee's live data: rows (cascade, including the project's search cache, its exports, export_assets
    and production_requests rows), FTS rows, checkpoints, source files and the whole export directory (every delivered document,
    report and model of the project). Refuses while a run or a document production (an
    export planned, rendering or checked) is active unless force: a forced deletion cancels the run and the active exports (reason
    project_deleted) first, so a late production finds its rows gone and writes nothing (StaleTransition). The export directory is
    marked (.deleted) before it goes, so an interrupted deletion is completed by the startup sweep, which removes marked folders only.
    Backups rotate on their own schedule (30 days); provider-side copies follow provider terms."""
    require_guarded(conn)
    active = conn.execute("SELECT id FROM runs WHERE project_id=? AND status IN ('running','awaiting_user')", (project_id,)).fetchall()
    active_exports = [r[0] for r in conn.execute("SELECT id FROM exports WHERE project_id=? AND status IN ('planned','rendering','checked') ORDER BY id", (project_id,)).fetchall()]
    if active and not force:
        raise ActiveRunBlocksDeletion(f"run {active[0][0]} is active; stop it or delete with force")
    if active_exports and not force:
        raise ActiveRunBlocksDeletion(f"export {active_exports[0]} is in production; wait for it or delete with force")
    if active:
        with conn:
            conn.execute("UPDATE runs SET status='cancelled', stop_reason='deleted' WHERE project_id=? AND status IN ('running','awaiting_user')", (project_id,))
    if active_exports:
        with conn:
            conn.execute("UPDATE exports SET status='cancelled', reason='project_deleted', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE project_id=? AND status IN ('planned','rendering','checked')", (project_id,))
    export_dir = pathlib.Path(data_root or ".") / "exports" / project_id
    if export_dir.is_dir():
        (export_dir / ".deleted").touch()                                 # the deletion's own marker: an interrupted deletion is completed by the startup sweep, which removes marked folders only
    threads = [r[0] for r in conn.execute("SELECT thread_id FROM runs WHERE project_id=?", (project_id,))]
    files = [r[0] for r in conn.execute("SELECT text_path FROM sources WHERE project_id=? AND text_path IS NOT NULL", (project_id,))]
    files += [r[0] for r in conn.execute("SELECT export_path FROM plan_versions WHERE project_id=? AND export_path IS NOT NULL", (project_id,))]
    for tid in threads:
        if hasattr(checkpointer, "delete_thread"):
            checkpointer.delete_thread(tid)
        else:  # older savers: remove rows directly
            with checkpointer.conn:
                checkpointer.conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (tid,))
                checkpointer.conn.execute("DELETE FROM writes WHERE thread_id=?", (tid,))
    removed_files = 0
    if export_dir.exists():
        removed_files += sum(1 for _ in export_dir.rglob("*") if _.is_file() and _.name != ".deleted"); shutil.rmtree(export_dir)   # the count leaves out the deletion's own marker
    for f in files:
        path = pathlib.Path(f) if data_root is None else pathlib.Path(data_root) / f
        if path.exists():
            path.unlink(); removed_files += 1
    cache = conn.execute("SELECT COUNT(*) FROM search_cache WHERE project_id=?", (project_id,)).fetchone()[0]
    export_rows = conn.execute("SELECT COUNT(*) FROM exports WHERE project_id=?", (project_id,)).fetchone()[0]
    request_rows = conn.execute("SELECT COUNT(*) FROM production_requests WHERE project_id=?", (project_id,)).fetchone()[0]
    with conn:
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("DELETE FROM claims_fts WHERE claim_id IN (SELECT id FROM claims WHERE project_id=?)", (project_id,))
        conn.execute("DELETE FROM passages_fts WHERE passage_id IN (SELECT id FROM passages WHERE project_id=?)", (project_id,))   # FTS tables have no foreign keys
        conn.execute("DELETE FROM search_cache WHERE project_id=?", (project_id,))   # explicit even though the cascade covers it
        conn.execute("DELETE FROM export_assets WHERE export_id IN (SELECT id FROM exports WHERE project_id=?)", (project_id,))   # explicit, as the cache above
        conn.execute("DELETE FROM exports WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM production_requests WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    return {"threads": len(threads), "files": removed_files, "cache_rows": cache, "cancelled_runs": len(active),
            "cancelled_exports": len(active_exports), "export_rows": export_rows, "production_requests": request_rows}
