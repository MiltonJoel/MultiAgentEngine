"""Two read-only passes with one output shape: reviewer (red build) and auditor (once, on green)."""
import json
import re
from pathlib import Path

from engine import git, prompts
from engine.cli import run_agent
from engine.state import GraphState

SCHEMA = {
    "type": "object",
    "properties": {
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "description": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
                "required": ["file", "line", "description", "suggested_fix"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["problems"],
    "additionalProperties": False,
}
NO_DIFF = "(not available — the repository is not under git, or nothing was committed)"


def _parse_problems(raw: str) -> list[dict]:
    """JSON out of a possibly fenced or chatty reply; a garbage reply becomes one problem, not a crash."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())["problems"]
        except (ValueError, KeyError, TypeError):
            pass
    return [{"file": "", "line": 0, "description": raw.strip()[:800], "suggested_fix": ""}]


def _diff(state: GraphState, ref: str | None) -> str:
    if not state.get("git_branch") or not ref:
        return NO_DIFF
    return git.diff(state["repo"], ref) or NO_DIFF


def _focus(state: GraphState) -> str:
    """Scope the audit to the units open when the run started."""
    names = state.get("focus") or []
    if not names:
        return "SCOPE: every unit in the status block — this run started without a task list and wrote it."
    listed = "\n".join(f"- {n}" for n in names)
    return (
        "SCOPE: only the units below. They are the ones this run implemented or changed;\n"
        "everything else was already audited in an earlier run — do not re-review it.\n"
        f"{listed}"
    )


def reviewer_node(state: GraphState) -> dict:
    prompt = prompts.load(
        "reviewer", state["repo"],
        spec=Path(state["spec"]).read_text(),
        plan=Path(state["plan"]).read_text(),
        errors=json.dumps(state["errors"], indent=2),
        diff=_diff(state, "HEAD~1"),  # the pass that just failed
    )
    raw = run_agent(state["agents"]["reviewer"], prompt, cwd=state["repo"], label="reviewer", schema=SCHEMA)
    return {"problems": _parse_problems(raw)}


def auditor_node(state: GraphState) -> dict:
    prompt = prompts.load(
        "auditor", state["repo"],
        spec=Path(state["spec"]).read_text(),
        plan=Path(state["plan"]).read_text(),
        focus=_focus(state),
        diff=_diff(state, state.get("base_sha")),  # everything this run changed
    )
    raw = run_agent(state["agents"]["auditor"], prompt, cwd=state["repo"], label="auditor", schema=SCHEMA)
    problems = _parse_problems(raw)
    print(f"[auditor] {len(problems)} problem(s) against the plan", flush=True)
    return {"problems": problems, "audited": True}
