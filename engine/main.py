"""CLI entrypoint: `mae` (or `python -m engine.main`)."""
import argparse
import fcntl
import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

from engine import git, runlog
from engine.cli import DEFAULT_AGENTS, RUNNERS
from engine.graph import (
    IDLE_LIMIT,
    MAX_ITERATIONS,
    build_graph,
    plan_done,
    plan_open,
    plan_states,
    plan_unparsed,
)
from engine.nodes.tester import build_gates

# Everything project-specific lives in <repo>/.mae/, never in this package.
MAE_DIR = ".mae"
CONFIG = f"{MAE_DIR}/config.json"
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
    """<repo>/.mae/config.json, or {} if absent. Unknown keys are ignored."""
    path = repo / CONFIG
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf8"))
    except ValueError as exc:
        raise SystemExit(f"{path}: {exc}")
    if not isinstance(config, dict):
        raise SystemExit(f"{path}: must hold a JSON object")
    for key, kind in (("agents", dict), ("allow_dirs", list), ("gates", list), ("spec", str), ("plan", str)):
        if key in config and not isinstance(config[key], kind):
            raise SystemExit(f"{path}: \"{key}\" must be a {kind.__name__}")
    print(f"Config: {path}")
    return config


def _doc(parser: argparse.ArgumentParser, flag: str | None, configured: str | None,
         repo: Path, default: str, what: str) -> Path:
    """Locate a per-project document: --flag beats the config key beats <repo>/.mae/<default>."""
    if flag:
        path = Path(flag).expanduser().resolve()
    else:
        path = (repo / Path(configured or f"{MAE_DIR}/{default}").expanduser()).resolve()
    if not path.is_file():
        parser.error(f"{what} file not found: {path}\n"
                     f"Put the {what} there, point --{what} at it, or set \"{what}\" in "
                     f"{repo / CONFIG}." + (f"\nA plan starts as:\n\n{NEW_PLAN}" if what == "plan" else ""))
    return path


def _agents(parser: argparse.ArgumentParser, config: dict, flags: list[str]) -> dict[str, str]:
    """Role -> CLI, validated now so a typo fails before the run starts."""
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
    for cli in set(agents.values()):
        if not shutil.which(cli):
            parser.error(f"the {cli} CLI is not on PATH — install and log in, or bind its roles elsewhere "
                         f"with --agent ROLE=CLI")
    return agents


def _check_gates(parser: argparse.ArgumentParser, gates: list[dict], configured: bool) -> list[dict]:
    """A configured gate whose tool is missing would pass silently, so it fails now; a missing default is dropped."""
    kept = []
    for gate in gates:
        found = shutil.which(gate["cmd"][0])
        if not found and configured:
            parser.error(f"gate tool not found: {gate['cmd'][0]} (from {' '.join(gate['cmd'])!r})")
        if not found:
            print(f"Gate skipped, {gate['cmd'][0]} is not installed: {' '.join(gate['cmd'])}")
            continue
        if found.startswith("/snap/"):
            print(f"WARNING: {found} is a snap build; snap ruff can report no findings while checking nothing")
        kept.append(gate)
    return kept


def _max_iterations(parser: argparse.ArgumentParser, flag: int | None, configured) -> int:
    value = flag if flag is not None else configured if configured is not None else MAX_ITERATIONS
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        parser.error(f"max iterations must be a positive integer, got {value!r}")
    return value


@contextmanager
def _plan_lock(plan: Path):
    """One run per plan file; advisory, so a human editing it is unaffected."""
    with plan.open("r+", encoding="utf8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"another run already holds {plan} — wait for it or kill it")
        yield


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mae", description="AI software engineering loop")
    parser.add_argument("--spec", metavar="FILE",
                        help=f"Architecture doc, never edited by the engine (default: <repo>/{MAE_DIR}/{DEFAULT_SPEC})")
    parser.add_argument("--plan", metavar="FILE",
                        help=f"Work queue holding the ENGINE-STATUS block (default: <repo>/{MAE_DIR}/{DEFAULT_PLAN})")
    parser.add_argument("--repo", default=".", help="Path to the target repository")
    parser.add_argument("--allow-dir", action="append", default=[], metavar="DIR",
                        help="Extra directory the coder may read (repeatable)")
    parser.add_argument("--gate", action="append", metavar="CMD",
                        help="Gate command run in the repo after every coder pass (repeatable; replaces the "
                             "config list). --gate '' disables all gates. (default: pytest, then ruff)")
    parser.add_argument("--agent", action="append", default=[], metavar="ROLE=CLI",
                        help=f"Bind a role to a CLI (repeatable). Roles: {', '.join(DEFAULT_AGENTS)}. "
                             f"CLIs: {', '.join(sorted(RUNNERS))}.")
    parser.add_argument("--max-iterations", type=int, metavar="N",
                        help=f"Ceiling on coder passes; must exceed the number of open units (default: {MAX_ITERATIONS})")
    parser.add_argument("--log-dir", metavar="DIR", help=f"JSONL run log directory (default: <repo>/{MAE_DIR}/runs)")
    parser.add_argument("--no-git", action="store_true",
                        help="Skip the per-run branch and per-iteration commits (no rollback points)")
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

    configured = args.gate if args.gate is not None else config.get("gates")
    try:
        gates = _check_gates(parser, build_gates(configured), configured is not None)
    except SystemExit as exc:
        parser.error(str(exc))
    agents = _agents(parser, config.get("agents", {}), args.agent)
    max_iterations = _max_iterations(parser, args.max_iterations, config.get("max_iterations"))
    allow_dirs = []
    for raw in [*args.allow_dir, *config.get("allow_dirs", [])]:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"allow-dir is not a directory: {path}")
        allow_dirs.append(str(path))

    for bad in plan_unparsed(str(plan)):
        print(f"WARNING: unreadable status line, it will be ignored: {bad}")
    if plan_done(str(plan)):
        parser.error(f"{plan} has nothing left open — every item is DONE or BLOCKED. "
                     "Add a `<name>: TODO` line inside the ENGINE-STATUS block.")

    try:
        with _plan_lock(plan):
            clean = run(spec, plan, repo, allow_dirs, agents, gates, max_iterations, args.log_dir, args.no_git)
    except KeyboardInterrupt:
        sys.exit(130)
    # 0 only when the queue is closed, the gates are green and the audit is clean — usable from CI
    sys.exit(0 if clean else 1)


def run(spec: Path, plan: Path, repo: Path, allow_dirs: list[str], agents: dict[str, str],
        gates: list[dict], max_iterations: int, log_dir: str | None, no_git: bool) -> bool:
    log_path = runlog.start(log_dir or repo / MAE_DIR / "runs")
    focus = plan_open(str(plan))
    print(f"Plan: {_tally(str(plan))}")
    # main() refuses a finished plan, so no open units here means no status block yet
    print("This run: " + (", ".join(focus) if focus else "no task list yet — the coder writes it from the spec and plan"))
    print(f"Log: {log_path}")
    print("Agents: " + ", ".join(f"{role}={cli}" for role, cli in agents.items()))
    print("Gates: " + (" ; ".join(" ".join(g["cmd"]) for g in gates) or "(none — nothing gates the coder)"))

    branch = base_sha = None
    if no_git:
        print("[git] checkpoints disabled (--no-git)")
    elif git.is_repo(str(repo)):
        branch = git.start_branch(str(repo))
        if branch:
            # commit pre-existing dirty work on its own so every later checkpoint is the coder's alone
            git.checkpoint(str(repo), "engine: baseline — uncommitted work from before the run")
            base_sha = git.head(str(repo))
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
            "audit_clean": False,
            "focus": focus,
            "iteration": 0,
            "idle_passes": 0,
            "max_iterations": max_iterations,
        }, {"recursion_limit": 4 * max_iterations})  # 4 nodes per pass, worst case
    except BaseException as exc:
        runlog.log("aborted", error=f"{type(exc).__name__}: {exc}", usage=runlog.totals())
        print(f"\nAborted: {type(exc).__name__}: {exc}\nPlan: {_tally(str(plan))}\nUsage: {runlog.summary()}")
        if isinstance(exc, (RuntimeError, TimeoutError)):  # an agent failed: the message says it all
            raise SystemExit(1)
        raise

    runlog.log("finish", iterations=result["iteration"], tests_passed=result["tests_passed"],
               audit_clean=result["audit_clean"], errors=result["errors"], problems=result["problems"],
               usage=runlog.totals())
    print(f"\nFinished after {result['iteration']} iteration(s), "
          f"tests_passed={result['tests_passed']}, audit_clean={result['audit_clean']}\nPlan: {_tally(str(plan))}")
    print(f"Usage: {runlog.summary()}")
    if branch and base_sha:
        print(f"Review with: git -C {repo} log --oneline {base_sha[:7]}..{branch}")
    if result["errors"] or result["problems"]:
        print(json.dumps({"errors": result["errors"], "problems": result["problems"]}, indent=2))
    if result.get("idle_passes", 0) >= IDLE_LIMIT:
        print(f"Stopped early: {IDLE_LIMIT} coder passes in a row changed nothing. Read the last coder output above.")
    return bool(result["tests_passed"] and result["audit_clean"] and plan_done(str(plan)))


if __name__ == "__main__":
    main()
