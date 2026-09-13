"""The loop: coder -> tester -> (reviewer | coder | auditor | end), routed off the plan's status block."""
import re
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from engine.nodes.coder import coder_node
from engine.nodes.reviewer import auditor_node, reviewer_node
from engine.nodes.tester import tester_node
from engine.state import GraphState

# The engine owns one HTML-comment block in the plan; the rest of the document is the user's.
STATUS_BLOCK = re.compile(r"<!--\s*ENGINE-STATUS\s*(?P<body>.*?)-->", re.DOTALL)
# `<name>: <STATE> [free-form note]` — the name may itself contain colons and dashes.
STATUS_LINE = re.compile(
    r"^\s*(?P<name>\S.*?):\s*(?P<state>TODO|IN_PROGRESS|DONE|BLOCKED)\b.*$", re.MULTILINE
)
OPEN_STATES = {"TODO", "IN_PROGRESS"}
# Ceiling on coder passes, not a target; must exceed the number of units in the plan.
MAX_ITERATIONS = 15


def plan_items(plan_path: str) -> list[tuple[str, str]]:
    """(name, state) per line of the status block; [] if not written yet."""
    block = STATUS_BLOCK.search(Path(plan_path).read_text())
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
    block = STATUS_BLOCK.search(Path(plan_path).read_text())
    if not block:
        return []
    return [line.strip() for line in block.group("body").splitlines()
            if line.strip() and not STATUS_LINE.match(line)]


def plan_done(plan_path: str) -> bool:
    """Nothing open. BLOCKED counts as finished — the loop can't unblock it."""
    states = plan_states(plan_path)
    return bool(states) and not any(s in OPEN_STATES for s in states)


def _capped(state: GraphState) -> bool:
    return state["iteration"] >= state["max_iterations"]


def after_tests(state: GraphState) -> str:
    """Red -> reviewer. Green + open items -> coder. Green + finished -> one audit, then end."""
    if _capped(state):
        return END
    if not state["tests_passed"]:
        return "reviewer"
    if not plan_done(state["plan"]):
        return "coder"
    return END if state.get("audited") else "auditor"


def after_review(state: GraphState) -> str:
    return END if _capped(state) else "coder"


def after_audit(state: GraphState) -> str:
    """Findings go back to the coder; a clean audit ends the run."""
    return "coder" if state["problems"] and not _capped(state) else END


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("coder", coder_node)
    graph.add_node("tester", tester_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("auditor", auditor_node)

    # Evaluator-optimizer: the coder generates, the tester/reviewer/auditor judge and loop back.
    graph.add_edge(START, "coder")
    graph.add_edge("coder", "tester")
    graph.add_conditional_edges("tester", after_tests, ["reviewer", "coder", "auditor", END])
    graph.add_conditional_edges("reviewer", after_review, ["coder", END])
    graph.add_conditional_edges("auditor", after_audit, ["coder", END])

    return graph.compile()
