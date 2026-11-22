"""Deterministic LangGraph skeleton with stub adapters.

Runnable offline (tests/test_workflow.py; langgraph 1.2.13, langgraph-checkpoint-sqlite 3.1.1).
It demonstrates the control pattern only: small graph state, domain records in the app database,
idempotent step keys, one interrupt per node, and resume after a failure. Real nodes replace the stubs.
"""
from __future__ import annotations
from app.core.sqlguard import require_guarded

import hashlib
import json
import sqlite3
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from app.core.ids import make_id


class GraphState(TypedDict, total=False):
    project_id: str
    run_id: str
    questions_open: int
    evidence_count: int
    plan_version: int
    stop_reason: str


def step_key(run_id: str, node: str, payload: object) -> str:
    raw = json.dumps([run_id, node, payload], sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def completed(db: sqlite3.Connection, key: str) -> dict | None:
    require_guarded(db)
    row = db.execute("SELECT output_json FROM run_steps WHERE step_key = ? AND status = 'completed'", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def mark(db: sqlite3.Connection, key: str, run_id: str, node: str, output: dict) -> None:
    require_guarded(db)
    db.execute(
        "INSERT OR REPLACE INTO run_steps (step_key, run_id, node, input_hash, status, output_json, completed_at) "
        "VALUES (?,?,?,?, 'completed', ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (key, run_id, node, key, json.dumps(output)),
    )


def build_graph(db: sqlite3.Connection, model: Callable, search: Callable, checkpointer):
    require_guarded(db)
    def draft_questions(state: GraphState) -> GraphState:
        key = step_key(state["run_id"], "draft_questions", state["project_id"])
        if (done := completed(db, key)) is not None:
            return {"questions_open": done["n"]}
        questions = model("clarify", state)          # one bounded, schema-checked call
        with db:
            for i, text in enumerate(questions, 1):
                db.execute("INSERT OR IGNORE INTO questions (id, project_id, text, why_blocking) VALUES (?,?,?,?)",
                           (make_id(state["project_id"], "Q", i), state["project_id"], text, "blocks research"))
            mark(db, key, state["run_id"], "draft_questions", {"n": len(questions)})
        return {"questions_open": len(questions)}

    def await_answers(state: GraphState) -> GraphState:
        answers = interrupt({"kind": "clarify", "open_questions": state["questions_open"]})  # exactly one interrupt
        with db:  # idempotent upserts after the interrupt
            for i, (k, v) in enumerate(sorted(answers.items()), 1):
                db.execute("INSERT OR REPLACE INTO requirements (id, project_id, key, value_json, status, source, version) "
                           "VALUES (?,?,?,?, 'confirmed', 'user', 1)", (make_id(state["project_id"], "REQ", i), state["project_id"], k, json.dumps(v)))
            db.execute("UPDATE questions SET status = 'answered' WHERE project_id = ?", (state["project_id"],))
        return {"questions_open": 0}

    def explore(state: GraphState) -> GraphState:
        key = step_key(state["run_id"], "explore", "q1")
        if (done := completed(db, key)) is not None:
            return {"evidence_count": done["n"]}
        results = search("official pricing page")      # may raise; the checkpoint lets the run resume here
        with db:
            for i, r in enumerate(results, 1):
                db.execute("INSERT OR IGNORE INTO sources (id, project_id, url, canonical_url, tier, retrieved_at, content_hash, freshness) "
                           "VALUES (?,?,?,?,1, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, 'volatile')",
                           (make_id(state["project_id"], "SRC", i), state["project_id"], r, r, "sha256:" + "0" * 64))
            mark(db, key, state["run_id"], "explore", {"n": len(results)})
        return {"evidence_count": len(results)}

    def plan(state: GraphState) -> GraphState:
        return {"plan_version": 1, "stop_reason": "complete"}

    def after_draft(state: GraphState) -> str:          # deterministic router, not a model decision
        return "await_answers" if state.get("questions_open", 0) > 0 else "explore"

    g = StateGraph(GraphState)
    g.add_node("draft_questions", draft_questions)
    g.add_node("await_answers", await_answers)
    g.add_node("explore", explore)
    g.add_node("plan", plan)
    g.add_edge(START, "draft_questions")
    g.add_conditional_edges("draft_questions", after_draft, ["await_answers", "explore"])
    g.add_edge("await_answers", "explore")
    g.add_edge("explore", "plan")
    g.add_edge("plan", END)
    return g.compile(checkpointer=checkpointer)
