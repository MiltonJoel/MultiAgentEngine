"""Two codex passes sharing one output shape:

- reviewer: runs on a red build, sees only the tester's errors, returns root causes.
- auditor:  runs once on a green build, checks the code against the plan's DONE claims.
"""
import json
import re
from pathlib import Path

from engine.cli import run_codex
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

PROMPT = """You are the failure analyst in an automated coding loop. You do not fix anything.

The architecture spec the code must satisfy:

{spec}

The current work queue:

{plan}

The deterministic test run failed with these errors:
{errors}

Read only the code the errors point at, then diagnose the root cause of each.
Merge errors that share one root cause into a single problem. For each problem:
- description: why it actually fails, not a restatement of the error text.
- suggested_fix: the concrete change to make, precise enough to apply without guessing."""


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


AUDIT_PROMPT = """You are the auditor in an automated coding loop. You do not fix anything.

The test suite passes and the implementation agent has marked its own work DONE.
Your job is to check whether those claims are true. The agent marked itself — the
tests are the only objective evidence so far, and passing tests do not prove the
plan was delivered.

The architecture spec the code must satisfy:

{spec}

The work queue, with the agent's status block at the top:

{plan}

{focus}

Read the actual code in the repository and, for every unit in scope, judge
whether the code genuinely delivers what that part of the plan specifies.

Report a problem for each of:
- a unit marked DONE whose code is missing, stubbed, or a placeholder;
- a unit marked DONE that is implemented but contradicts what the plan specifies;
- behaviour the plan explicitly requires that no test covers, where a bug would
  therefore pass silently.

Be specific: point at real files and lines. `suggested_fix` must say what to
change, precise enough to apply without guessing. Do not report style opinions,
refactors, or improvements the plan never asked for. If a unit genuinely delivers
what the plan asked, say nothing about it — an empty problems list is the correct
answer for an honest implementation."""


def reviewer_node(state: GraphState) -> dict:
    prompt = PROMPT.format(
        spec=Path(state["spec"]).read_text(),
        plan=Path(state["plan"]).read_text(),
        errors=json.dumps(state["errors"], indent=2),
    )
    raw = run_codex(prompt, cwd=state["repo"], label="reviewer", schema=SCHEMA)
    return {"problems": _parse_problems(raw)}


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


def auditor_node(state: GraphState) -> dict:
    prompt = AUDIT_PROMPT.format(
        spec=Path(state["spec"]).read_text(),
        plan=Path(state["plan"]).read_text(),
        focus=_focus(state),
    )
    raw = run_codex(prompt, cwd=state["repo"], label="auditor", schema=SCHEMA)
    problems = _parse_problems(raw)
    print(f"[auditor] {len(problems)} problem(s) against the plan", flush=True)
    return {"problems": problems, "audited": True}
