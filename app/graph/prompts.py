"""Runtime prompts for the workflow nodes: the System and User templates live in prompts/runtime/<stage>.v<n>.md and are
loaded here, never written in code. Placeholders {name} are filled for the given keys only, so braces
inside requirement values or page text are left alone. VERSIONS holds the version in use per stage; the files of earlier
versions are kept for the runs stored under them. Extraction loads its own prompt (extract_evidence) in
app.core.extraction."""
import json, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
VERSIONS = {"clarify": 2, "research_plan": 4, "verify": 3, "compare_options": 10, "plan_generate": 5, "plan_sections": 5, "failure_repair": 1, "revise": 4,
            "tree_alternatives": 2, "tree_variants": 2, "tree_evaluate": 2, "researcher_worker": 1, "coverage": 3, "enrich": 4, "verify_plan": 2,
            "discover": 2, "panel": 3, "panel_direct": 1, "editor": 4}   # the version in use per stage; an earlier version's file is kept for the stored runs (a completed step replays its stored output and is never resent; the tree key and the comparison key carry the prompt version, so nothing stored under one version is replayed under another; a coverage verdict stored under an earlier coverage version is not reused). What the versions in use carry: clarify.v2 and editor.v4 the product rule (the document describes the founder's product, never NextMVBee's method; the Editor flags and drops such a statement where its write scope allows); research_plan.v4 asks the planner for the candidate identity fields and names the candidate questions as code's (one per discovered candidate after the discovery passes); verify.v3 sends one group of claims per send; coverage.v3 has the contradicts verdict under the disqualification rule; tree_alternatives.v2 copies the identity from the claims and prefers the versioned product over the family; tree_variants.v2 types each variant (product, route, approach) with the route named; tree_evaluate.v2 asks the model for fit, evidence, cost and risk only (the currency term is code's); verify_plan.v2 receives each active decision's recommendation text, runner-up and code-derived access mode; discover.v2 spells its output keys (the schema is advisory under OpenRouter's server tools); panel.v3 asks for each analysis model's response outcome; panel_direct.v1 is the panel send with no tool offered, the one model's own reading; revise.v4 carries the run date
_cache: dict[str, dict] = {}


def template(stage: str) -> dict:
    """{"system": ..., "user": ...} of the stage's runtime prompt (the first fenced block of the file)."""
    if stage not in _cache:
        path = ROOT / "prompts" / "runtime" / f"{stage}.v{VERSIONS[stage]}.md"
        block = path.read_text(encoding="utf-8").split("```")[1].strip("\n")
        sys_lines, user_lines, mode = [], [], None
        for line in block.splitlines():
            if line.startswith("System: "):
                mode = "s"; sys_lines.append(line[len("System: "):]); continue
            if line.startswith("User: "):
                mode = "u"; user_lines.append(line[len("User: "):]); continue
            (sys_lines if mode == "s" else user_lines).append(line)
        _cache[stage] = {"system": "\n".join(sys_lines).strip(), "user": "\n".join(user_lines).strip()}
    return _cache[stage]


def fill(text: str, values: dict) -> str:
    for k, v in values.items():
        text = text.replace("{" + k + "}", v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str))
    return text


def messages(stage: str, values: dict) -> list[dict]:
    t = template(stage)
    if "run_date" not in values:                                                   # the run date from the system clock goes into every prompt that names it
        from app.core.discovery import run_date
        values = dict(values, run_date=run_date())
    return [{"role": "system", "content": fill(t["system"], values)}, {"role": "user", "content": fill(t["user"], values)}]


def repair_turn(failure_reason: str) -> dict:
    """The one user turn appended after a failure shape (failure_repair.v1)."""
    return {"role": "user", "content": fill(template("failure_repair")["user"], {"failure_reason": failure_reason})}


def section_messages(values: dict):
    """build_section_messages for app.core.extraction.generate_plan: the plan inputs plus the section to produce."""
    def build(name: str, sections_so_far: dict) -> list[dict]:
        keys = {}
        for sec_name, sec in sections_so_far.items():
            d = sec.model_dump() if hasattr(sec, "model_dump") else dict(sec)
            if "components" in d:
                keys["component_keys"] = [c["key"] for c in d["components"]]
            if "tasks" in d:
                keys["task_keys"] = [t["key"] for t in d["tasks"]]
        return messages("plan_sections", dict(values, section=name, sections_so_far=keys or "none yet"))
    return build
