from typing import TypedDict


class GraphState(TypedDict):
    spec: str  # path to the frozen architecture doc
    plan: str  # path to the work queue holding the status block
    repo: str
    allow_dirs: list[str]  # extra dirs the coder may read
    agents: dict[str, str]  # role -> CLI name
    gates: list[dict]  # [{cmd: [...], ok_codes: (...)}]
    git_branch: str | None  # None = no checkpoints
    base_sha: str | None  # commit the run started from; the audit diffs against it
    errors: list[dict]  # tester: [{file, line, description}]
    problems: list[dict]  # reviewer/auditor: [{file, line, description, suggested_fix}]
    tests_passed: bool
    audited: bool  # the audit runs once per run
    focus: list[str]  # units open at run start; scopes the audit
    iteration: int
    max_iterations: int
