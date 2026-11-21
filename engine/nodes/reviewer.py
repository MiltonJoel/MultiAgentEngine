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
# An empty findings list may come with a short note ("Reviewed every unit; nothing found."). With more prose than
# this around it, the reply is ambiguous — findings described in prose beside `{"problems": []}` would vanish —
# so the agent is asked once to restate its own reply as JSON (see _settle). Counted without whitespace/fences.
MAX_CLEAN_PROSE = 120
RESTATE = """Below is your previous reply to a code review request. It returned an empty findings list but also
contains prose. Restate it as JSON: put every problem, defect, risk or missing piece the prose describes into
"problems" (one entry each, with unit/file/line when the prose names them); if the prose describes no problem
at all, return {{"problems": []}}. Do not review anything again — only restate the reply.

--- previous reply ---
{reply}
--- end ---"""
NO_DIFF = "(not available — the repository is not under git, or nothing was committed)"


def _problem(item: dict) -> dict:
    line = item.get("line")
    return {"unit": str(item.get("unit") or ""), "file": str(item.get("file") or ""),
            "line": line if isinstance(line, int) and not isinstance(line, bool) else 0,
            "description": str(item.get("description") or ""), "suggested_fix": str(item.get("suggested_fix") or "")}


def _findings_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _nested_problems(value) -> list[dict]:
    """Findings filed under a nested "problems" key (`{"problems": [], "notes": {"problems": [...]}}`). A nested
    "problems" that is not a list of objects — an echoed JSON schema, a `{"problems": 0}` count — is not a
    findings container and is ignored; only the top-level list has to be well-formed."""
    found: list[dict] = []
    if isinstance(value, dict):
        if _findings_list(value.get("problems")):
            found += value["problems"]
        children = [v for k, v in value.items() if k != "problems"]
    else:
        children = value if isinstance(value, list) else []
    for child in children:
        found += _nested_problems(child)
    return found


class _Unreadable(Exception):
    pass


def _decode_problems(raw: str) -> tuple[list[dict] | None, list[tuple[int, int]]]:
    """(merged findings, the spans of every JSON object decoded); findings None if no findings object was
    found. Every decoded object is consumed whole, so its interior (an echoed schema's
    `"properties": {"problems": {...}}`) is never mistaken for a findings object of its own."""
    decoder = json.JSONDecoder()
    found: list[dict] = []
    spans: list[tuple[int, int]] = []  # every decoded object: JSON is never "prose", even an echoed schema
    readable = False
    position = 0
    for start in (m.start() for m in re.finditer(r"\{", raw)):
        if start < position:  # inside an object already decoded
            continue
        try:
            payload, end = decoder.raw_decode(raw, start)
        except ValueError:
            if PROBLEMS_START.match(raw, start):  # a findings object cut off or malformed: never skip it
                raise _Unreadable from None
            continue
        position = end
        spans.append((start, end))
        if not isinstance(payload, dict):
            continue
        if "problems" in payload:
            if not _findings_list(payload["problems"]):  # the top-level list is the answer: it must be readable
                raise _Unreadable
            items = payload["problems"] + _nested_problems({k: v for k, v in payload.items() if k != "problems"})
        else:
            items = _nested_problems(payload)  # a wrapper such as {"result": {"problems": [...]}}
            if not items:
                continue
        readable = True
        found += [p for p in map(_problem, items) if p not in found]
    return (found if readable else None), spans


def _prose_outside(raw: str, spans: list[tuple[int, int]]) -> str:
    """The reply minus its JSON objects, code fences and whitespace."""
    kept, last = [], 0
    for start, end in spans:
        kept.append(raw[last:start])
        last = end
    kept.append(raw[last:])
    return re.sub(r"```(?:json)?|\s+", "", "".join(kept))


def _unreadable(raw: str) -> list[dict]:
    text = raw.strip()[:800] or "the agent returned an empty reply"
    return [{"unit": "", "file": "", "line": 0, "description": f"unreadable reply: {text}", "suggested_fix": ""}]


def _read(raw: str) -> tuple[list[dict] | None, bool]:
    """(findings or None if unreadable, ambiguous: an empty list wrapped in more than a short note of prose)."""
    try:
        found, spans = _decode_problems(raw)
    except (_Unreadable, RecursionError):  # RecursionError: a reply nested past the decoder's depth
        return None, False
    return found, found == [] and len(_prose_outside(raw, spans)) > MAX_CLEAN_PROSE


def _parse_problems(raw: str) -> list[dict]:
    """Every `{"problems": [...]}` object in a possibly fenced or chatty reply, merged. Fails closed: a reply
    that quotes an empty example before its real findings still reports the findings, and anything unreadable —
    prose only, `{"problems": null}`, a truncated object, nesting deep enough to exhaust the decoder — becomes
    one problem: a reply we can't read is never clean. An ambiguous reply (see MAX_CLEAN_PROSE) is unreadable
    here; the nodes first give the agent one chance to restate it (_settle)."""
    found, ambiguous = _read(raw)
    return _unreadable(raw) if found is None or ambiguous else found


def _settle(agent: str, raw: str, cwd: str, label: str) -> list[dict]:
    """Parse a reply; an ambiguous one is sent back once to the same agent to restate as JSON — a cheap prompt with
    no repo context — and the restatement is parsed strictly. Clean only if it is unambiguously clean."""
    found, ambiguous = _read(raw)
    if found is None:
        return _unreadable(raw)
    if not ambiguous:
        return found
    print(f"[{label}] empty findings list beside prose — asking the agent to restate its reply as JSON", flush=True)
    restated = run_agent(agent, RESTATE.format(reply=raw[-20000:]), cwd=cwd, label=f"{label}-restate", schema=SCHEMA)
    return _parse_problems(restated)


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
    agent = state["agents"]["reviewer"]
    raw = run_agent(agent, prompt, cwd=state["repo"], label="reviewer", schema=SCHEMA)
    return {"problems": _settle(agent, raw, state["repo"], "reviewer")}


def auditor_node(state: GraphState) -> dict:
    prompt = prompts.load(
        "auditor", state["repo"],
        spec=Path(state["spec"]).read_text(encoding="utf8", errors="replace"),
        plan=Path(state["plan"]).read_text(encoding="utf8", errors="replace"),
        focus=_focus(state),
        diff=_diff(state, state.get("base_sha")),  # everything this run changed
    )
    agent = state["agents"]["auditor"]
    raw = run_agent(agent, prompt, cwd=state["repo"], label="auditor", schema=SCHEMA)
    problems = _settle(agent, raw, state["repo"], "auditor")
    print(f"[auditor] {len(problems)} problem(s) against the plan", flush=True)
    return {"problems": problems, "audit_clean": not problems}
