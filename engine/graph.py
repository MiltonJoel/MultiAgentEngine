"""The loop: coder -> tester -> (reviewer | coder | auditor | end), routed off the plan's status block."""
import re
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from engine import runlog
from engine.nodes.coder import coder_node
from engine.nodes.reviewer import auditor_node, reviewer_node
from engine.nodes.tester import tester_node
from engine.state import GraphState

# The engine owns one HTML-comment block in the plan; the rest of the document is the user's.
STATUS_BLOCK = re.compile(r"<!--\s*ENGINE-STATUS\s*(?P<body>.*?)-->", re.DOTALL)
# `<name>: <STATE> [free-form note]` — the name may itself contain colons and dashes.
STATUS_LINE = re.compile(
    r"^\s*(?P<name>\S.*?):\s*(?P<state>TODO|IN_PROGRESS|DONE|BLOCKED)\b(?P<note>.*)$", re.MULTILINE
)
OPEN_STATES = {"TODO", "IN_PROGRESS"}
# Ceiling on coder passes, not a target; must exceed the number of units in the plan.
MAX_ITERATIONS = 15
MAX_NOTE_CHARS = 160
# Consecutive coder passes that changed nothing before the run gives up instead of paying for more.
IDLE_LIMIT = 2


def plan_items(plan_path: str) -> list[tuple[str, str]]:
    """(name, state) per line of the status block; [] if not written yet."""
    block = STATUS_BLOCK.search(Path(plan_path).read_text(encoding="utf8", errors="replace"))
    if not block:
        return []
    return [(m.group("name").strip(), m.group("state")) for m in STATUS_LINE.finditer(block.group("body"))]


def plan_states(plan_path: str) -> list[str]:
    return [state for _, state in plan_items(plan_path)]


def plan_open(plan_path: str) -> list[str]:
    """Names of the units still open."""
    return [name for name, state in plan_items(plan_path) if state in OPEN_STATES]


def plan_unparsed(plan_path: str) -> list[str]:
    """Lines inside the block the engine cannot read (a typo'd state would silently drop a unit)."""
    block = STATUS_BLOCK.search(Path(plan_path).read_text(encoding="utf8", errors="replace"))
    if not block:
        return []
    return [line.strip() for line in block.group("body").splitlines()
            if line.strip() and not STATUS_LINE.match(line)]


def plan_done(plan_path: str) -> bool:
    """Nothing open. BLOCKED counts as finished — the loop can't unblock it."""
    states = plan_states(plan_path)
    return bool(states) and not any(s in OPEN_STATES for s in states)


def reopen_units(plan_path: str, problems: list[dict]) -> list[str]:
    """Flip each DONE unit named by a finding back to `TODO — audit: <finding>`; returns the names reopened.
    BLOCKED units stay blocked, and a unit name that matches no line is ignored."""
    notes = {}
    for problem in problems:
        unit = str(problem.get("unit") or "").strip().casefold()
        if unit and unit not in notes:  # first finding becomes the note; the coder sees all of them
            text = " ".join(str(problem.get("description", "")).split())
            notes[unit] = text.replace("-->", "→")[:MAX_NOTE_CHARS]  # "-->" would close the block
    text = Path(plan_path).read_text(encoding="utf8", errors="replace")
    block = STATUS_BLOCK.search(text)
    if not block or not notes:
        return []
    reopened, lines = [], []
    for line in block.group("body").split("\n"):
        match = STATUS_LINE.match(line)
        if match and match.group("state") == "DONE" and match.group("name").strip().casefold() in notes:
            name = match.group("name").strip()
            line = f"{name}: TODO — audit: {notes[name.casefold()]}"
            reopened.append(name)
        lines.append(line)
    if reopened:
        start, end = block.span("body")
        Path(plan_path).write_text(text[:start] + "\n".join(lines) + text[end:], encoding="utf8")
    return reopened


def audit_node(state: GraphState) -> dict:
    """Run the auditor, then reopen every DONE unit it found a problem in."""
    update = auditor_node(state)
    reopened = reopen_units(state["plan"], update["problems"])
    if reopened:
        print(f"[auditor] reopened: {', '.join(reopened)}", flush=True)
        runlog.log("reopen", units=reopened)
    return update


def _capped(state: GraphState) -> bool:
    return state["iteration"] >= state["max_iterations"]


def after_tests(state: GraphState) -> str:
    """Red -> reviewer. Green + open items -> coder. Green + finished -> audit until it comes back clean."""
    if _capped(state):
        return END
    if state.get("idle_passes", 0) >= IDLE_LIMIT:
        print(f"[engine] {IDLE_LIMIT} coder passes in a row changed nothing — stopping", flush=True)
        runlog.log("stalled", idle_passes=state["idle_passes"])
        return END
    if not state["tests_passed"]:
        return "reviewer"
    if not plan_done(state["plan"]):
        return "coder"
    return END if state.get("audit_clean") else "auditor"


def after_review(state: GraphState) -> str:
    return END if _capped(state) else "coder"


def after_audit(state: GraphState) -> str:
    """Findings go back to the coder and the audit reruns after the fix; a clean audit ends the run."""
    return "coder" if state["problems"] and not _capped(state) else END


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("coder", coder_node)
    graph.add_node("tester", tester_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("auditor", audit_node)

    # Evaluator-optimizer: the coder generates, the tester/reviewer/auditor judge and loop back.
    graph.add_edge(START, "coder")
    graph.add_edge("coder", "tester")
    graph.add_conditional_edges("tester", after_tests, ["reviewer", "coder", "auditor", END])
    graph.add_conditional_edges("reviewer", after_review, ["coder", END])
    graph.add_conditional_edges("auditor", after_audit, ["coder", END])

    return graph.compile()
