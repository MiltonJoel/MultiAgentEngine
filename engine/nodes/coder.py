"""The only node that writes. Prompt: engine/prompts/coder.md, stable content first (cacheable prefix)."""
import json
import os
from pathlib import Path

from engine import git, prompts
from engine.cli import run_agent
from engine.state import GraphState

TIMEOUT = 5400
# Skipped when fingerprinting a repo without git checkpoints: big, generated, or written by the engine itself.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "runs"}


def fingerprint(repo: str) -> frozenset:
    """(path, size, mtime) of every file — how a run without git tells whether a pass changed anything."""
    seen = set()
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = os.path.join(root, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            seen.add((path, info.st_size, info.st_mtime_ns))
    return frozenset(seen)


def coder_node(state: GraphState) -> dict:
    plan_path, spec_path = Path(state["plan"]), Path(state["spec"])
    plan_before = plan_path.read_text(encoding="utf8", errors="replace")
    prompt = prompts.load(
        "coder", state["repo"],
        spec_path=spec_path,
        spec=spec_path.read_text(encoding="utf8", errors="replace"),
        plan_path=plan_path,
        plan=plan_before,  # re-read every iteration: the coder edits it
        errors=json.dumps(state.get("errors") or [], indent=2),
        problems=json.dumps(state.get("problems") or [], indent=2),
    )
    before = None if state.get("git_branch") else fingerprint(state["repo"])
    run_agent(
        state["agents"]["coder"], prompt, cwd=state["repo"], label="coder",
        write=True, timeout=TIMEOUT,
        add_dirs=[str(plan_path.parent), str(spec_path.parent), *state.get("allow_dirs", [])],
    )
    # with checkpoints the tree is clean before each pass, so a clean tree after it means nothing changed
    unchanged = fingerprint(state["repo"]) == before if before is not None else not git.dirty(state["repo"])
    idle = unchanged and plan_path.read_text(encoding="utf8", errors="replace") == plan_before
    return {"iteration": state["iteration"] + 1, "idle_passes": state.get("idle_passes", 0) + 1 if idle else 0}
