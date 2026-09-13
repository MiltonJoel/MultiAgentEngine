"""The only node that writes. Prompt: engine/prompts/coder.md, stable content first (cacheable prefix)."""
import json
from pathlib import Path

from engine import prompts
from engine.cli import run_agent
from engine.state import GraphState

TIMEOUT = 5400


def coder_node(state: GraphState) -> dict:
    plan_path, spec_path = Path(state["plan"]), Path(state["spec"])
    prompt = prompts.load(
        "coder", state["repo"],
        spec_path=spec_path,
        spec=spec_path.read_text(),
        plan_path=plan_path,
        plan=plan_path.read_text(),  # re-read every iteration: the coder edits it
        errors=json.dumps(state.get("errors") or [], indent=2),
        problems=json.dumps(state.get("problems") or [], indent=2),
    )
    run_agent(
        state["agents"]["coder"], prompt, cwd=state["repo"], label="coder",
        write=True, timeout=TIMEOUT,
        add_dirs=[str(plan_path.parent), str(spec_path.parent), *state.get("allow_dirs", [])],
    )
    return {"iteration": state["iteration"] + 1}
