"""Two agent passes sharing one output shape:

- reviewer: runs on a red build, sees the errors and the failing pass's diff.
- auditor:  runs once on a green build, checks the code against the plan's DONE claims.

Both prompts live in engine/prompts/, and which CLI runs them is configuration.
"""
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
    """The output schema should guarantee clean JSON; a fenced or chatty reply
    must still not crash the loop."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())["problems"]
        except (ValueError, KeyError, TypeError):
            pass
    return [{"file": "", "line": 0, "description": raw.strip()[:800], "suggested_fix": ""}]


def _diff(state: GraphState, ref: str | None) -> str:
    """The change under review. Without it both passes re-read the whole repo blind."""
    if not state.get("git_branch") or not ref:
        return NO_DIFF
    return git.diff(state["repo"], ref) or NO_DIFF


def _focus(state: GraphState) -> str:
    """Scope the audit to what this run actually worked on. Re-auditing units that
    were already DONE before the run started costs a lot and finds nothing new."""
    names = state.get("focus") or []
    if not names:
        return "SCOPE: every unit in the status block — nothing was open when this run started."
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
