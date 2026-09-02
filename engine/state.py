from typing import TypedDict


class GraphState(TypedDict):
    spec: str  # path to the frozen architecture doc; read-only, cacheable prefix
    plan: str  # path to the work queue; the coder reads and updates it in place
    repo: str
    allow_dirs: list[str]  # extra dirs the coder may read, e.g. a reference codebase
    agents: dict[str, str]  # role -> CLI name, e.g. {"coder": "claude"}
    gates: list[dict]  # deterministic gate commands: [{cmd: [...], ok_codes: (...)}]
    git_branch: str | None  # branch holding this run's checkpoints; None = no checkpoints
    base_sha: str | None  # commit the run started from; the audit diffs against it
    errors: list[dict]  # tester: [{file, line, description}]
    problems: list[dict]  # reviewer: [{file, line, description, suggested_fix}]
    tests_passed: bool
    audited: bool  # the audit pass runs once, on the first green + finished plan
    focus: list[str]  # units open at run start; scopes the audit to this run's work
    iteration: int
    max_iterations: int  # ceiling on coder passes; the loop normally ends before it
