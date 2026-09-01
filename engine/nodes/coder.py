import json
from pathlib import Path

from engine.cli import run_claude
from engine.state import GraphState

# Stable content first, volatile last: the spec and these instructions are
# byte-identical across iterations, so they sit in the cacheable prefix while the
# work queue and findings — which change every turn — come after it.
PROMPT = """
You are the implementation agent.

## Architecture spec — {spec_path}

This is the invariant. It describes what the system must be. Never edit this file;
if the code and the spec disagree, the spec wins unless the work queue says
otherwise.

{spec}

## Your responsibilities

1. Work the queue below: fix the errors first, then implement the open items.
   Read the current code from the repo yourself.
2. Run formatting/linting if the project uses it.
3. Maintain the progress block in {plan_path}. It is an HTML comment the engine
   parses. Everything outside it is the user's — do not rewrite or reorder it.

   <!-- ENGINE-STATUS
   Fase 1 — Event Bus: DONE
   Fase 2 — InstrumentRegistry: IN_PROGRESS
   -->

   Update it **incrementally**: flip a unit to DONE as soon as its code is
   written, before starting the next. Do not batch the updates until the end.
   This session can be killed at any moment (timeout, usage limit); whatever is on
   disk plus this block is all the next run inherits.

   If the block does not exist yet, create it by listing every open unit, one per
   line, `<name>: TODO`.

   A unit that is TODO but whose code already exists means the human edited the
   plan: re-read that item and reconcile the code with what it now says. Any note
   on the line is theirs and tells you what changed.

   States: TODO, IN_PROGRESS, DONE, BLOCKED. Use DONE only when the code is
   written. Use BLOCKED only for work that cannot proceed without a human, and say
   why on the line — the engine stops retrying blocked work. If you find work that
   is missing, append a line for it.

Do not run the test suite, a separate deterministic step does that after you.

## Work queue — {plan_path}

{plan}

## Errors from the last test run (empty on the first iteration)

{errors}

## Root-cause analysis from the reviewer (empty on the first iteration)

{problems}
"""


def coder_node(state: GraphState) -> dict:
    plan_path, spec_path = Path(state["plan"]), Path(state["spec"])
    prompt = PROMPT.format(
        spec_path=spec_path,
        spec=spec_path.read_text(),
        plan_path=plan_path,
        plan=plan_path.read_text(),  # re-read every iteration: the coder edits it
        errors=json.dumps(state.get("errors") or [], indent=2),
        problems=json.dumps(state.get("problems") or [], indent=2),
    )
    run_claude(
        prompt,
        cwd=state["repo"],
        permission_mode="acceptEdits",
        label="coder",
        timeout=5400,
        # without these the coder silently cannot touch the plan, nor read any
        # reference codebase the spec points at
        add_dirs=[str(plan_path.parent), str(spec_path.parent), *state.get("allow_dirs", [])],
    )
    return {"iteration": state["iteration"] + 1}
