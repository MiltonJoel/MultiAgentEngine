"""CLI entrypoint: `mae` (or `python -m engine.main`)."""
import argparse
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path

from engine import git, runlog
from engine.cli import DEFAULT_AGENTS, RUNNERS
from engine.graph import (
    MAX_ITERATIONS,
    archive_done,
    build_graph,
    plan_done,
    plan_open,
    plan_states,
    plan_unlisted,
    plan_unparsed,
)
from engine.nodes.tester import DEFAULT_LINT_CMD, DEFAULT_TEST_CMD, build_gates

# Everything project-specific lives in the target repo, never in this package:
# <repo>/.mae/{config.json, spec.md, plan.md, prompts/*.md, runs/}. The engine ships
# no documents of its own, so it drives any repo without being edited.
MAE_DIR = ".mae"
REPO_CONFIG = [f"{MAE_DIR}/config.json", ".mae.json"]  # second form kept: older repos use it
DEFAULT_SPEC = "spec.md"
DEFAULT_PLAN = "plan.md"

NEW_PLAN = """<!-- ENGINE-STATUS
First unit: TODO
-->

# Work queue — describe anything that needs more than one line below.
"""


def _tally(plan: str) -> str:
    states = plan_states(plan)
    return " ".join(f"{s}={states.count(s)}" for s in sorted(set(states))) or "(no status block written)"


def _repo_config(repo: Path) -> dict:
    """{spec, plan, gates|test_cmd|lint_cmd, agents, max_iterations, allow_dirs} from
    <repo>/.mae/config.json.

    CLI flags win. Unknown keys are left alone — a typo is the user's business, not
    a reason to refuse to start.
    """
    for name in REPO_CONFIG:
        path = repo / name
        if path.is_file():
            try:
                config = json.loads(path.read_text())
            except ValueError as exc:
                raise SystemExit(f"{path}: {exc}")
            print(f"Config: {path}")
            return config
    return {}


def _doc(parser: argparse.ArgumentParser, flag: str | None, configured: str | None,
         repo: Path, default: str, what: str) -> Path:
    """Locate a per-project document: --flag (relative to the shell) beats the config
    key (relative to the repo) beats <repo>/.mae/<default>. No path into this package."""
    if flag:
        path = Path(flag).expanduser().resolve()
    else:
        path = (repo / Path(configured or f"{MAE_DIR}/{default}").expanduser()).resolve()
    if not path.is_file():
        parser.error(f"{what} file not found: {path}\n"
                     f"Put the {what} there, point --{what} at it, or set \"{what}\" in "
                     f"{repo / REPO_CONFIG[0]}." + (f"\nA plan starts as:\n\n{NEW_PLAN}" if what == "plan" else ""))
    return path


def _agents(parser: argparse.ArgumentParser, config: dict, flags: list[str]) -> dict[str, str]:
    """Role -> CLI. Validated here so a typo fails now, not three hours into a run."""
    agents = {**DEFAULT_AGENTS, **config}
    for flag in flags:
        role, _, cli = flag.partition("=")
        if not cli:
            parser.error(f"--agent expects ROLE=CLI, got {flag!r}")
        agents[role.strip()] = cli.strip()
    for role, cli in agents.items():
        if role not in DEFAULT_AGENTS:
            parser.error(f"unknown role {role!r} — known: {', '.join(DEFAULT_AGENTS)}")
        if cli not in RUNNERS:
            parser.error(f"unknown CLI {cli!r} for {role} — known: {', '.join(sorted(RUNNERS))}")
    return agents


@contextmanager
def _plan_lock(plan: Path):
    """One run per plan file. Two engines sharing a status block interleave their
    writes and corrupt it; the lock is advisory, so a human editing it is unaffected."""
    with plan.open("r+", encoding="utf8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"another run already holds {plan} — wait for it or kill it")
        yield


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mae", description="AI software engineering loop")
    parser.add_argument("--spec", metavar="FILE",
                        help="Architecture doc: the frozen invariant. Never edited by the engine. "
                             f"(default: <repo>/{MAE_DIR}/{DEFAULT_SPEC})")
    parser.add_argument("--plan", metavar="FILE",
                        help="Work queue: holds the ENGINE-STATUS block and the open items. "
                             f"(default: <repo>/{MAE_DIR}/{DEFAULT_PLAN})")
    parser.add_argument("--repo", default=".", help="Path to the target repository")
    parser.add_argument("--archive", action="store_true",
                        help="Move DONE lines out of the status block into <plan>.archive and exit. "
                             "Deterministic, no agent runs; keeps the plan small as changes pile up.")
    parser.add_argument("--allow-dir", action="append", default=[], metavar="DIR",
                        help="Extra directory the coder may read (repeatable). Use it for any "
                             "codebase the plan references, e.g. --allow-dir ../reference-repo")
    parser.add_argument("--test-cmd", metavar="CMD",
                        help=f"Test gate, run in the repo. '' disables it. (default: {DEFAULT_TEST_CMD})")
    parser.add_argument("--lint-cmd", metavar="CMD",
                        help=f"Lint gate, run in the repo. '' disables it. (default: {DEFAULT_LINT_CMD})")
    parser.add_argument("--agent", action="append", default=[], metavar="ROLE=CLI",
                        help="Bind a role to a CLI, e.g. --agent reviewer=claude (repeatable). "
                             f"Roles: {', '.join(DEFAULT_AGENTS)}. CLIs: {', '.join(sorted(RUNNERS))}. "
                             f"(default: {', '.join(f'{r}={a}' for r, a in DEFAULT_AGENTS.items())})")
    parser.add_argument("--max-iterations", type=int, metavar="N",
                        help=f"Ceiling on coder passes; must exceed the number of open units. "
                             f"(default: {MAX_ITERATIONS})")
    parser.add_argument("--log-dir", metavar="DIR",
                        help=f"Where the JSONL run log goes (default: <repo>/{MAE_DIR}/runs)")
    parser.add_argument("--no-git", action="store_true",
                        help="Skip the per-run branch and the per-iteration commits in the target repo. "
                             "You lose every rollback point — only for a repo you are not protecting.")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        parser.error(f"--repo is not a directory: {repo}")
    config = _repo_config(repo)
    spec = _doc(parser, args.spec, config.get("spec"), repo, DEFAULT_SPEC, "spec")
    plan = _doc(parser, args.plan, config.get("plan"), repo, DEFAULT_PLAN, "plan")
    if spec == plan:
        parser.error("--spec and --plan must be different files")

    if args.archive:
        archive = plan.with_suffix(plan.suffix + ".archive")
        moved, kept = archive_done(str(plan), str(archive))
        print(f"Archived {moved} DONE line(s) to {archive}; {kept} left in the block.")
        return

    if config.get("gates") is not None and (args.test_cmd is not None or args.lint_cmd is not None):
        parser.error("--test-cmd/--lint-cmd conflict with the 'gates' list in the repo config; "
                     "edit the list, or drop it and use the flags")
    try:
        gates = build_gates(config.get("gates"),
                            args.test_cmd if args.test_cmd is not None else config.get("test_cmd"),
                            args.lint_cmd if args.lint_cmd is not None else config.get("lint_cmd"))
    except SystemExit as exc:
        parser.error(str(exc))
    agents = _agents(parser, config.get("agents", {}), args.agent)
    max_iterations = args.max_iterations or config.get("max_iterations") or MAX_ITERATIONS
    allow_dirs = []
    for raw in [*args.allow_dir, *config.get("allow_dirs", [])]:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"allow-dir is not a directory: {path}")
        allow_dirs.append(str(path))

    # diagnostics first: the guard below would otherwise hide the reason it fired
    for bad in plan_unparsed(str(plan)):
        print(f"WARNING: unreadable status line, it will be ignored: {bad}")
    if plan_done(str(plan)):
        unlisted = plan_unlisted(str(plan))
        hint = ""
        if unlisted:
            lines = "\n".join(f"    {h}: TODO" for h in unlisted)
            hint = ("\n\nThese sections are written below the block but no status line "
                    "mentions them,\nso the engine cannot see them. Add a line inside "
                    f"<!-- ENGINE-STATUS ... --> for each:\n\n{lines}")
        parser.error(f"{plan} has nothing left open — every item is DONE or BLOCKED{hint}")

    with _plan_lock(plan):
        run(spec, plan, repo, allow_dirs, agents, gates, max_iterations, args.log_dir, args.no_git)


def run(spec: Path, plan: Path, repo: Path, allow_dirs: list[str], agents: dict[str, str],
        gates: list[dict], max_iterations: int, log_dir: str | None, no_git: bool):
    log_path = runlog.start(log_dir or repo / MAE_DIR / "runs")
    focus = plan_open(str(plan))
    print(f"Plan: {_tally(str(plan))}")
    print("This run: " + (", ".join(focus) if focus else "audit only"))
    print(f"Log: {log_path}")

    print("Agents: " + ", ".join(f"{role}={cli}" for role, cli in agents.items()))
    print("Gates: " + (" ; ".join(" ".join(g["cmd"]) for g in gates) or "(none — nothing gates the coder)"))

    branch = base_sha = None
    if no_git:
        print("[git] checkpoints disabled (--no-git)")
    elif git.is_repo(str(repo)):
        branch = git.start_branch(str(repo))
        if branch:
            # commit whatever was already dirty on its own, so every later checkpoint
            # is the coder's work alone and `git diff HEAD~1` means what it says
            git.checkpoint(str(repo), "engine: baseline — uncommitted work from before the run")
            base_sha = git.head(str(repo))  # the audit diffs the whole run against this
    else:
        print(f"[git] {repo} is not a git repository — running without checkpoints or rollback")

    runlog.log("start", repo=str(repo), plan=str(plan), spec=str(spec), branch=branch,
               base_sha=base_sha, focus=focus, agents=agents, max_iterations=max_iterations,
               gates=[" ".join(g["cmd"]) for g in gates])

    app = build_graph()
    try:
        result = app.invoke({
            "spec": str(spec),
            "plan": str(plan),
            "repo": str(repo),
            "allow_dirs": allow_dirs,
            "agents": agents,
            "gates": gates,
            "git_branch": branch,
            "base_sha": base_sha,
            "errors": [],
            "problems": [],
            "tests_passed": False,
            "audited": False,
            "focus": focus,
            "iteration": 0,
            "max_iterations": max_iterations,
        }, {"recursion_limit": 4 * max_iterations})  # 4 nodes per pass, worst case
    except BaseException as exc:
        # a killed run still gets its line in the log, and the checkpoints survive
        runlog.log("aborted", error=f"{type(exc).__name__}: {exc}", usage=runlog.totals())
        print(f"\nAborted: {type(exc).__name__}: {exc}\nPlan: {_tally(str(plan))}\nUsage: {runlog.summary()}")
        raise

    runlog.log("finish", iterations=result["iteration"], tests_passed=result["tests_passed"],
               audited=result["audited"], errors=result["errors"], problems=result["problems"],
               usage=runlog.totals())
    print(f"\nFinished after {result['iteration']} iteration(s), "
          f"tests_passed={result['tests_passed']}, audited={result['audited']}\nPlan: {_tally(str(plan))}")
    print(f"Usage: {runlog.summary()}")
    if branch:
        print(f"Review with: git -C {repo} log --oneline main..{branch}")
    # unresolved findings matter whether they came from a red build or the audit
    if result["errors"] or result["problems"]:
        print(json.dumps({"errors": result["errors"], "problems": result["problems"]}, indent=2))


if __name__ == "__main__":
    main()
