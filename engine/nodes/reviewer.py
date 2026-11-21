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
# The start of a findings object; one that then fails to decode (cut off mid-reply) must not be skipped.
PROBLEMS_START = re.compile(r'\{\s*"problems"\s*:')
NO_DIFF = "(not available — the repository is not under git, or nothing was committed)"


def _problem(item: dict) -> dict:
    line = item.get("line")
    return {"unit": str(item.get("unit") or ""), "file": str(item.get("file") or ""),
            "line": line if isinstance(line, int) and not isinstance(line, bool) else 0,
            "description": str(item.get("description") or ""), "suggested_fix": str(item.get("suggested_fix") or "")}


def _nested_problems(value) -> list[dict] | None:
    """Findings filed under a nested "problems" key (`{"problems": [], "notes": {"problems": [...]}}`) count too;
    None if a nested one is malformed — fail closed, like the top level."""
    found: list[dict] = []
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    if isinstance(value, dict) and "problems" in value:
        items = value["problems"]
        if not (isinstance(items, list) and all(isinstance(item, dict) for item in items)):
            return None
        found += items
        children = [v for k, v in value.items() if k != "problems"]
    for child in children:
        inner = _nested_problems(child)
        if inner is None:
            return None
        found += inner
    return found


def _parse_problems(raw: str) -> list[dict]:
    """Every `{"problems": [...]}` object in a possibly fenced or chatty reply, merged. Fails closed: a reply
    that quotes an empty example before its real findings still reports the findings, and anything unreadable —
    prose, `{"problems": null}`, a list of strings — becomes one problem: a reply we can't read is never clean."""
    decoder = json.JSONDecoder()
    found, readable, position = [], False, 0
    for start in (m.start() for m in re.finditer(r"\{", raw)):
        if start < position:  # inside an object already decoded
            continue
        try:
            payload, end = decoder.raw_decode(raw, start)
        except ValueError:
            if PROBLEMS_START.match(raw, start):  # a findings object cut off or malformed: never skip it
                readable = False
                break
            continue
        if not (isinstance(payload, dict) and "problems" in payload):
            continue
        problems = payload["problems"]
        if not (isinstance(problems, list) and all(isinstance(item, dict) for item in problems)):
            readable = False
            break
        nested = _nested_problems({k: v for k, v in payload.items() if k != "problems"})
        if nested is None:
            readable = False
            break
        readable, position = True, end
        found += [p for p in map(_problem, problems + nested) if p not in found]
    if readable:
        return found
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
