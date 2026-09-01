from typing import TypedDict


class GraphState(TypedDict):
    spec: str  # path to the frozen architecture doc; read-only, cacheable prefix
    plan: str  # path to the work queue; the coder reads and updates it in place
    repo: str
    allow_dirs: list[str]  # extra dirs the coder may read, e.g. a reference codebase
    test_cmd: list[str]  # deterministic gate; [] disables it
    lint_cmd: list[str]
    git_branch: str | None  # branch holding this run's checkpoints; None = repo not under git
    errors: list[dict]  # tester: [{file, line, description}]
    problems: list[dict]  # reviewer: [{file, line, description, suggested_fix}]
    tests_passed: bool
    audited: bool  # the audit pass runs once, on the first green + finished plan
    focus: list[str]  # units open at run start; scopes the audit to this run's work
    iteration: int
    max_iterations: int  # ceiling on coder passes; the loop normally ends before it
