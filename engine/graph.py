import re
from datetime import datetime, timezone
from pathlib import Path

from langgraph.graph import END, StateGraph

from engine.nodes.coder import coder_node
from engine.nodes.reviewer import auditor_node, reviewer_node
from engine.nodes.tester import tester_node
from engine.state import GraphState

# The engine owns one block at the top of the plan; the rest of the document is
# the user's, in whatever format they wrote it.
STATUS_BLOCK = re.compile(r"<!--\s*ENGINE-STATUS\s*(?P<body>.*?)-->", re.DOTALL)
# trailing `.*` so a BLOCKED line can carry its reason, as the coder is told to write it
STATUS_LINE = re.compile(
    r"^\s*(?P<name>\S.*?):\s*(?P<state>TODO|IN_PROGRESS|DONE|BLOCKED)\b.*$", re.MULTILINE
)
OPEN_STATES = {"TODO", "IN_PROGRESS"}

# Ceiling, not a target: the loop exits as soon as the plan has nothing open, so a
# high cap costs nothing. It has to exceed the number of units in the plan — at 5,
# a 10-phase plan silently stopped around phase 2. Override with --max-iterations.
MAX_ITERATIONS = 15


def plan_items(plan_path: str) -> list[tuple[str, str]]:
    """(name, state) per line of the engine's status block; [] if not written yet."""
    block = STATUS_BLOCK.search(Path(plan_path).read_text())
    if not block:
        return []
    return [(m.group("name").strip(), m.group("state")) for m in STATUS_LINE.finditer(block.group("body"))]


def plan_states(plan_path: str) -> list[str]:
    return [state for _, state in plan_items(plan_path)]


def plan_open(plan_path: str) -> list[str]:
    """Names of the units still open — what this run is actually about."""
    return [name for name, state in plan_items(plan_path) if state in OPEN_STATES]


def archive_done(plan_path: str, archive_path: str) -> tuple[int, int]:
    """Move DONE lines out of the status block into archive_path, so the block stays
    small as changes accumulate. Returns (moved, kept).

    Deterministic — no agent involved. Nothing is lost: every moved line is appended
    to the archive verbatim, and lines the parser doesn't understand are kept in
    place rather than silently swept away.
    """
    text = Path(plan_path).read_text()
    block = STATUS_BLOCK.search(text)
    if not block:
        return 0, 0
    moved, kept = [], []
    for line in block.group("body").splitlines():
        if not line.strip():
            continue
        match = STATUS_LINE.match(line)
        (moved if match and match.group("state") == "DONE" else kept).append(line.rstrip())
    if not moved:
        return 0, len(kept)

    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    with Path(archive_path).open("a", encoding="utf8") as f:
        f.write(f"\n# archived {stamp} from {Path(plan_path).name}\n" + "\n".join(moved) + "\n")

    # An empty block reads as "never started" and would make the coder rebuild the
    # whole plan from scratch. Leave one line behind so the ledger still says done.
    if not kept:
        kept = [f"Archivado {stamp}: {len(moved)} unidades completadas: DONE"]
    replacement = "<!-- ENGINE-STATUS\n" + "\n".join(kept) + "\n-->"
    Path(plan_path).write_text(text[: block.start()] + replacement + text[block.end():])
    return len(moved), len(kept)


# Heading-ish lines below the block: "A. Something", "2. Something", "### Something".
# Loose on purpose — this only powers a hint in an error message, never routing.
HEADING = re.compile(r"^[ \t]*(?:\d+\.|[A-Z]\.|#{1,6})[ \t]*(?P<text>\S.*?)[ \t]*$", re.MULTILINE)


def plan_unlisted(plan_path: str) -> list[str]:
    """Headings written below the status block that no status line mentions.

    Prose alone is invisible to routing, and that is the one failure mode with no
    visible error — so name the suspects instead of leaving the user guessing.
    """
    text = Path(plan_path).read_text()
    block = STATUS_BLOCK.search(text)
    if not block:
        return []
    below = text[block.end():]
    listed = " ".join(name.casefold() for name, _ in plan_items(plan_path))
    out = []
    for match in HEADING.finditer(below):
        head = match.group("text")
        if match.group().lstrip().startswith("#"):
            continue  # our own instructional comments
        # crude containment check: enough to spot work nobody queued
        if head.casefold()[:40] not in listed:
            out.append(head)
    if out:
        return out
    # No headings — but plain prose is just as much a request, and used to slip
    # through silently. Report the first line of it so the user sees *something*.
    prose = [ln.strip() for ln in below.splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    unlisted = [ln for ln in prose if ln.casefold()[:40] not in listed]
    return unlisted[:1] if unlisted else []


def plan_unparsed(plan_path: str) -> list[str]:
    """Lines inside the status block the engine cannot read. They are invisible to
    routing, so a typo'd state would silently drop a unit — surface them instead."""
    block = STATUS_BLOCK.search(Path(plan_path).read_text())
    if not block:
        return []
    return [line.strip() for line in block.group("body").splitlines()
            if line.strip() and not STATUS_LINE.match(line)]


def plan_done(plan_path: str) -> bool:
    """Nothing left that could still make progress. BLOCKED counts as finished —
    the loop can't unblock it, so spinning on it just burns tokens."""
    states = plan_states(plan_path)
    return bool(states) and not any(s in OPEN_STATES for s in states)


def after_tests(state: GraphState) -> str:
    """Green build with an unfinished plan means more work, not completion.
    A finished plan gets one audit pass before the run is allowed to end."""
    if state["iteration"] >= state["max_iterations"]:
        return END
    if not state["tests_passed"]:
        return "reviewer"
    if not plan_done(state["plan"]):
        return "coder"
    return END if state.get("audited") else "auditor"


def after_review(state: GraphState) -> str:
    if state["iteration"] >= state["max_iterations"]:
        return END
    return "coder"


def after_audit(state: GraphState) -> str:
    """Audit runs once. Findings go back to the coder; a clean audit ends the run."""
    if state["iteration"] >= state["max_iterations"]:
        return END
    return "coder" if state["problems"] else END


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("coder", coder_node)
    graph.add_node("tester", tester_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("auditor", auditor_node)

    graph.set_entry_point("coder")
    graph.add_edge("coder", "tester")
    graph.add_conditional_edges(
        "tester", after_tests, {"reviewer": "reviewer", "coder": "coder", "auditor": "auditor", END: END}
    )
    graph.add_conditional_edges("reviewer", after_review, {"coder": "coder", END: END})
    graph.add_conditional_edges("auditor", after_audit, {"coder": "coder", END: END})

    return graph.compile()
