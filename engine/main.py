"""CLI entrypoint: `mae` (or `python -m engine.main`)."""
import argparse
import fcntl
import json
from contextlib import contextmanager
from pathlib import Path

from engine import git, runlog
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
from engine.nodes.tester import DEFAULT_LINT_CMD, DEFAULT_TEST_CMD, parse_cmd

# Anchored to the project, not the shell's cwd, so the defaults hold from anywhere.
WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
# Per-repo settings, so daily use across projects doesn't mean retyping flags.
REPO_CONFIG = ".mae.json"


def _tally(plan: str) -> str:
    states = plan_states(plan)
    return " ".join(f"{s}={states.count(s)}" for s in sorted(set(states))) or "(no status block written)"


def _repo_config(repo: Path) -> dict:
    """{test_cmd, lint_cmd, max_iterations, allow_dirs} from <repo>/.mae.json. CLI flags win."""
    path = repo / REPO_CONFIG
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text())
    except ValueError as exc:
        raise SystemExit(f"{path}: {exc}")
    print(f"Config: {path}")
    return config


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
    parser.add_argument("--spec", default=str(WORKSPACE / "arquitectura.md"), metavar="FILE",
                        help="Architecture doc: the frozen invariant. Never edited by the engine. "
                             "(default: %(default)s)")
    parser.add_argument("--plan", default=str(WORKSPACE / "trabajo.txt"), metavar="FILE",
                        help="Work queue: holds the ENGINE-STATUS block and the open items. "
                             "(default: %(default)s)")
    parser.add_argument("--repo", default=".", help="Path to the target repository")
    parser.add_argument("--archive", action="store_true",
                        help="Move DONE lines out of the status block into <plan>.archive and exit. "
                             "Deterministic, no agent runs; keeps the plan small as changes pile up.")
    parser.add_argument("--allow-dir", action="append", default=[], metavar="DIR",
                        help="Extra directory the coder may read (repeatable). Use it for any "
                             "codebase the plan references, e.g. --allow-dir ~/Desktop/FreqBot")
    parser.add_argument("--test-cmd", metavar="CMD",
                        help=f"Test gate, run in the repo. '' disables it. (default: {DEFAULT_TEST_CMD})")
    parser.add_argument("--lint-cmd", metavar="CMD",
                        help=f"Lint gate, run in the repo. '' disables it. (default: {DEFAULT_LINT_CMD})")
    parser.add_argument("--max-iterations", type=int, metavar="N",
                        help=f"Ceiling on coder passes; must exceed the number of open units. "
                             f"(default: {MAX_ITERATIONS})")
    parser.add_argument("--log-dir", metavar="DIR",
                        help="Where the JSONL run log goes (default: <plan dir>/runs)")
    parser.add_argument("--no-git", action="store_true",
                        help="Skip the per-run branch and the per-iteration commits in the target repo. "
                             "You lose every rollback point — only for a repo you are not protecting.")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    plan = Path(args.plan).expanduser().resolve()
    if not plan.is_file():
        parser.error(f"plan file not found: {plan}")
    spec = Path(args.spec).expanduser().resolve()
    if not spec.is_file():
        parser.error(f"spec file not found: {spec}")
    if spec == plan:
        parser.error("--spec and --plan must be different files")
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        parser.error(f"--repo is not a directory: {repo}")

    if args.archive:
        archive = plan.with_suffix(plan.suffix + ".archive")
        moved, kept = archive_done(str(plan), str(archive))
        print(f"Archived {moved} DONE line(s) to {archive}; {kept} left in the block.")
        return

    config = _repo_config(repo)
    test_cmd = parse_cmd(args.test_cmd if args.test_cmd is not None else config.get("test_cmd"),
                         DEFAULT_TEST_CMD)
    lint_cmd = parse_cmd(args.lint_cmd if args.lint_cmd is not None else config.get("lint_cmd"),
                         DEFAULT_LINT_CMD)
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
        run(spec, plan, repo, allow_dirs, test_cmd, lint_cmd, max_iterations, args.log_dir, args.no_git)


def run(spec: Path, plan: Path, repo: Path, allow_dirs: list[str], test_cmd: list[str],
        lint_cmd: list[str], max_iterations: int, log_dir: str | None, no_git: bool):
    log_path = runlog.start(log_dir or plan.parent / "runs")
    focus = plan_open(str(plan))
    print(f"Plan: {_tally(str(plan))}")
    print("This run: " + (", ".join(focus) if focus else "audit only"))
    print(f"Log: {log_path}")

    branch = None
    if no_git:
        print("[git] checkpoints disabled (--no-git)")
    elif git.is_repo(str(repo)):
        branch = git.start_branch(str(repo))
        if branch:
            # commit whatever was already dirty on its own, so every later checkpoint
            # is the coder's work alone and `git diff HEAD~1` means what it says
            git.checkpoint(str(repo), "engine: baseline — uncommitted work from before the run")
    else:
        print(f"[git] {repo} is not a git repository — running without checkpoints or rollback")

    runlog.log("start", repo=str(repo), plan=str(plan), spec=str(spec), branch=branch,
               focus=focus, test_cmd=test_cmd, lint_cmd=lint_cmd, max_iterations=max_iterations)

    app = build_graph()
    try:
        result = app.invoke({
            "spec": str(spec),
            "plan": str(plan),
            "repo": str(repo),
            "allow_dirs": allow_dirs,
            "test_cmd": test_cmd,
            "lint_cmd": lint_cmd,
            "git_branch": branch,
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
