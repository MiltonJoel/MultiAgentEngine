"""Two read-only passes with one output shape: reviewer (red build) and auditor (green + closed queue, until clean)."""
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
                    "unit": {"type": "string"},  # status-block name the finding belongs to; "" if none
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "description": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                },
                "required": ["unit", "file", "line", "description", "suggested_fix"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["problems"],
    "additionalProperties": False,
}
NO_DIFF = "(not available — the repository is not under git, or nothing was committed)"


def _problem(item: dict) -> dict:
    line = item.get("line")
    return {"unit": str(item.get("unit") or ""), "file": str(item.get("file") or ""),
            "line": line if isinstance(line, int) and not isinstance(line, bool) else 0,
            "description": str(item.get("description") or ""), "suggested_fix": str(item.get("suggested_fix") or "")}


def _parse_problems(raw: str) -> list[dict]:
    """The first `{"problems": [...]}` object in a possibly fenced or chatty reply. Anything else — prose,
    `{"problems": null}`, a list of strings — becomes one problem: a reply we can't read is never a clean audit."""
    decoder = json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r"\{", raw)):
        try:
            payload, _ = decoder.raw_decode(raw, start)
        except ValueError:
            continue
        if isinstance(payload, dict) and "problems" in payload:
            problems = payload["problems"]
            if isinstance(problems, list) and all(isinstance(item, dict) for item in problems):
                return [_problem(item) for item in problems]
            break
    text = raw.strip()[:800] or "the agent returned an empty reply"
    return [{"unit": "", "file": "", "line": 0, "description": f"unreadable reply: {text}", "suggested_fix": ""}]


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
        spec=Path(state["spec"]).read_text(encoding="utf8", errors="replace"),
        plan=Path(state["plan"]).read_text(encoding="utf8", errors="replace"),
        errors=json.dumps(state["errors"], indent=2),
        diff=_diff(state, "HEAD~1"),  # the pass that just failed
    )
    raw = run_agent(state["agents"]["reviewer"], prompt, cwd=state["repo"], label="reviewer", schema=SCHEMA)
    return {"problems": _parse_problems(raw)}


def auditor_node(state: GraphState) -> dict:
    prompt = prompts.load(
        "auditor", state["repo"],
        spec=Path(state["spec"]).read_text(encoding="utf8", errors="replace"),
        plan=Path(state["plan"]).read_text(encoding="utf8", errors="replace"),
        focus=_focus(state),
        diff=_diff(state, state.get("base_sha")),  # everything this run changed
    )
    raw = run_agent(state["agents"]["auditor"], prompt, cwd=state["repo"], label="auditor", schema=SCHEMA)
    problems = _parse_problems(raw)
    print(f"[auditor] {len(problems)} problem(s) against the plan", flush=True)
    return {"problems": problems, "audit_clean": not problems}
