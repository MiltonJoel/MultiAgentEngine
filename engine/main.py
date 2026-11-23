"""CLI entrypoint: `mae` (or `python -m engine`)."""
import argparse
import json
import os
import shutil
import signal
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # Windows: the process-group and locking code below is POSIX-only
    fcntl = None  # type: ignore[assignment]

from engine import __version__, git, llm, prompts, runlog
from engine.cli import DEFAULT_AGENTS, FREE_AGENTS, MODEL_RUNNERS, RUNNERS, split_agent
from engine.graph import (
    IDLE_LIMIT,
    MAX_ITERATIONS,
    build_graph,
    plan_blocked,
    plan_done,
    plan_issues,
    plan_open,
    plan_states,
)
from engine.nodes.tester import build_gates

# Everything project-specific lives in <repo>/.mae/, never in this package.
MAE_DIR = ".mae"
CONFIG = f"{MAE_DIR}/config.json"
DEFAULT_SPEC = "spec.md"
DEFAULT_PLAN = "plan.md"
# Exit codes, usable from CI. NEEDS_REVIEW: the queue closed green and audited, but a human still has to
# look — some units are BLOCKED, or no gate ran so "green" checked nothing. REFUSED: the run never started
# (bad arguments or config, a missing tool, a busy repo or plan, nothing left open) — no agent ran, nothing
# was spent, nothing changed; its own code so CI never mistakes it for a run's verdict.
EXIT_CLEAN, EXIT_FAILED, EXIT_NEEDS_REVIEW, EXIT_REFUSED = 0, 1, 2, 3
EXIT_INTERRUPTED, EXIT_TERMINATED = 130, 143  # 128 + SIGINT / SIGTERM, as a shell reports them

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
    kinds = (("agents", dict), ("llm", dict), ("allow_dirs", list), ("gates", list), ("spec", str), ("plan", str))
    for key, kind in kinds:
        if key in config and not isinstance(config[key], kind):
            raise SystemExit(f"{path}: \"{key}\" must be a {kind.__name__}")
    if not all(isinstance(d, str) for d in config.get("allow_dirs", [])):
        raise SystemExit(f"{path}: \"allow_dirs\" must be a list of path strings")
    if not all(isinstance(r, str) and isinstance(c, str) for r, c in config.get("agents", {}).items()):
        raise SystemExit(f"{path}: \"agents\" maps role names to CLI names, both strings")
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


def _agents(parser: argparse.ArgumentParser, config: dict, flags: list[str], free: bool = False) -> dict[str, str]:
    """Role -> runner, validated now so a typo or a missing CLI/model fails before the run starts.
    --free binds every role to the local Ollama model; config and --agent still override single roles."""
    agents = {**(FREE_AGENTS if free else DEFAULT_AGENTS), **config}
    for flag in flags:
        role, _, cli = flag.partition("=")
        if not cli:
            parser.error(f"--agent expects ROLE=CLI, got {flag!r}")
        agents[role.strip()] = cli.strip()
    for role, agent in agents.items():
        cli, model = split_agent(agent)
        if role not in DEFAULT_AGENTS:
            parser.error(f"unknown role {role!r} — known: {', '.join(DEFAULT_AGENTS)}")
        if cli not in RUNNERS:
            parser.error(f"unknown CLI {cli!r} for {role} — known: {', '.join(sorted(RUNNERS))}")
        if model and (not model.strip() or any(c.isspace() for c in model)):
            parser.error(f"{agent!r} for {role}: the model or effort after ':' must be one word — "
                         "a claude model, a codex effort or model, an ollama/api model")
    for agent in sorted(set(agents.values())):
        cli, model = split_agent(agent)
        if cli in MODEL_RUNNERS:
            if why := llm.check(cli, model):
                parser.error(why)
        elif not shutil.which(cli):
            parser.error(f"the {cli} CLI is not on PATH — install and log in, bind its roles elsewhere "
                         f"with --agent ROLE=CLI, or use a free local model with --free")
    for agent in sorted(set(agents.values())):
        cli, model = split_agent(agent)
        if cli in MODEL_RUNNERS and (s := llm.settings(cli, model))["key"]:  # say where the key goes, up front
            host = urlparse(s["url"]).hostname
            if s["user_url"]:
                print(f"{cli}: sends ${s['key_env']} to {host}")
            else:  # the repo's own config chose this endpoint — the case SECURITY.md warns about
                print(f"WARNING: {cli}: sends ${s['key_env']} to {host}, an endpoint chosen by this repo's "
                      ".mae/config.json — make sure you trust it")
    return agents


def _check_gates(parser: argparse.ArgumentParser, gates: list[dict], configured: bool, repo: Path) -> list[dict]:
    """A configured gate whose tool is missing would pass silently, so it fails now; a missing default is dropped.
    Gates run inside the repo, so a relative tool like scripts/check.sh is looked up there."""
    kept = []
    for gate in gates:
        tool = gate["cmd"][0]
        local = repo / tool
        found = shutil.which(str(local) if "/" in tool and not Path(tool).is_absolute() else tool)
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
    try:
        handle = plan.open("r+", encoding="utf8")
    except PermissionError:
        raise SystemExit(f"{plan} is not writable — the engine records progress in the plan, so it needs write access")
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f"another run already holds {plan} — wait for it or kill it")
        yield


class Terminated(KeyboardInterrupt):
    """SIGTERM (systemd, docker stop, CI cancel), handled like Ctrl-C: the agent's process group is killed,
    `aborted` is logged and the checkpoints stay. A bare SIGTERM would orphan the agent, still editing the repo."""


def _on_sigterm(signum, frame):
    raise Terminated()


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which would read as EXIT_NEEDS_REVIEW; every refusal exits 3."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_REFUSED, f"{self.prog}: error: {message}\n")


@contextmanager
def _refusals():
    """A SystemExit carrying a message (bad config, locked plan, broken prompt override…) raised before the run
    starts is a refusal: print it like a usage error and exit EXIT_REFUSED. Numeric exits pass through."""
    try:
        yield
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):
            raise
        reason = str(exc.code)
    except BrokenPipeError:  # stdout went away (`mae ... | head`): not a reason to call the run refused
        raise
    except OSError as exc:  # an unreadable config, plan or prompt; a path that cannot be resolved
        reason = f"{exc.filename + ': ' if exc.filename else ''}{exc.strerror or exc}"
    except RuntimeError as exc:
        if "Symlink loop" not in str(exc):  # Python 3.10's Path.resolve(); any other RuntimeError is a bug
            raise
        reason = str(exc)
    else:
        return
    print(f"mae: error: {reason}", file=sys.stderr)
    raise SystemExit(EXIT_REFUSED) from None


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="mae", description="AI software engineering loop",
                     epilog="Exit codes: 0 clean, 1 failed, 2 needs review, 3 refused to start, "
                            "130 interrupted, 143 terminated.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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
                        help=f"Bind a role to a runner (repeatable). Roles: {', '.join(DEFAULT_AGENTS)}. "
                             f"Runners: {', '.join(sorted(RUNNERS))}; ollama and api take a model, "
                             f"e.g. coder=ollama:qwen2.5-coder:14b.")
    parser.add_argument("--free", action="store_true",
                        help=f"Run every role on a free local model through Ollama ({llm.DEFAULT_MODEL}) — "
                             "no subscription or API key")
    parser.add_argument("--max-iterations", type=int, metavar="N",
                        help="Ceiling on coder passes; must exceed the number of open units "
                             f"(default: {MAX_ITERATIONS})")
    parser.add_argument("--log-dir", metavar="DIR", help=f"JSONL run log directory (default: <repo>/{MAE_DIR}/runs)")
    parser.add_argument("--no-git", action="store_true",
                        help="Skip the per-run branch and per-iteration commits (no rollback points)")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    with _refusals():
        prepared = _prepare(parser, args)
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        with ExitStack() as stack:
            with _refusals():  # another run holding the plan is a refusal; failing inside the run is not
                stack.enter_context(_plan_lock(prepared["plan"]))
            code = run(**prepared, log_dir=args.log_dir, no_git=args.no_git)
    except Terminated:
        sys.exit(EXIT_TERMINATED)
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPTED)
    sys.exit(code)


def _prepare(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict:
    """Every check that can refuse the run, before any agent starts; returns run()'s arguments."""
    if fcntl is None or os.name != "posix":
        raise SystemExit("this tool runs on Linux and macOS only (it manages process groups and POSIX file "
                         "locks) — on Windows, run it inside WSL")

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
        built = build_gates(configured)
    except SystemExit as exc:
        parser.error(str(exc))
    gates = _check_gates(parser, built, configured is not None, repo)
    try:
        llm.configure(config.get("llm"))
    except SystemExit as exc:
        parser.error(str(exc))
    agents = _agents(parser, config.get("agents", {}), args.agent, args.free)
    max_iterations = _max_iterations(parser, args.max_iterations, config.get("max_iterations"))
    allow_dirs = []
    for raw in [*args.allow_dir, *config.get("allow_dirs", [])]:
        path = Path(raw).expanduser().resolve()
        if not path.is_dir():
            parser.error(f"allow-dir is not a directory: {path}")
        allow_dirs.append(str(path))

    for issue in plan_issues(str(plan)):
        print(f"WARNING: {issue}")
    prompts.check(repo)
    if not args.no_git and git.is_repo(str(repo)) and (why := git.busy(str(repo))):
        parser.error(f"{repo}: {why} — finish or abort it first, or run with --no-git")
    if plan_done(str(plan)):
        parser.error(f"{plan} has nothing left open — every item is DONE or BLOCKED. "
                     "Add a `<name>: TODO` line inside the ENGINE-STATUS block.")
    _check_log_dir(parser, Path(args.log_dir).expanduser() if args.log_dir else repo / MAE_DIR / "runs")
    return {"spec": spec, "plan": plan, "repo": repo, "allow_dirs": allow_dirs, "agents": agents, "gates": gates,
            "max_iterations": max_iterations}


def _check_log_dir(parser: argparse.ArgumentParser, log_dir: Path) -> None:
    """The run log is the first thing a run writes; a log directory it cannot create is a refusal, not a crash
    after the branch exists. Checked without creating anything."""
    path = log_dir.resolve()
    if path.exists():
        if not path.is_dir():
            parser.error(f"--log-dir is not a directory: {path}")
        if not os.access(path, os.W_OK | os.X_OK):
            parser.error(f"--log-dir is not writable: {path}")
        return
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        parser.error(f"--log-dir {path} cannot be created: {parent} is not a writable directory")


def run(spec: Path, plan: Path, repo: Path, allow_dirs: list[str], agents: dict[str, str],
        gates: list[dict], max_iterations: int, log_dir: str | None, no_git: bool) -> int:
    """EXIT_CLEAN only when the queue is closed, the gates are green and the audit is clean."""
    log_path = runlog.start(log_dir or repo / MAE_DIR / "runs")
    focus = plan_open(str(plan))
    print(f"Plan: {_tally(str(plan))}")
    # main() refuses a finished plan, so no open units here means no status block yet
    print("This run: " + (", ".join(focus) if focus
                          else "no task list yet — the coder writes it from the spec and plan"))
    print(f"Log: {log_path}")
    print("Agents: " + ", ".join(f"{role}={cli}" for role, cli in agents.items()))
    print("Gates: " + (" ; ".join(" ".join(g["cmd"]) for g in gates) or "(none — nothing gates the coder)"))

    branch = base_sha = None
    if no_git:
        print("[git] checkpoints disabled (--no-git)")
    elif git.is_repo(str(repo)):
        branch = git.start_branch(str(repo))
        if branch:
            # commit pre-existing dirty work on its own so every later checkpoint is the coder's alone;
            # a commit that fails here would fail every pass, so stop before paying for one
            try:
                git.checkpoint(str(repo), "engine: baseline — uncommitted work from before the run", strict=True)
            except git.CheckpointError as exc:  # before any agent: say why, keep the log honest, spend nothing
                runlog.log("aborted", error=f"CheckpointError: {exc}", branch=branch, usage=runlog.totals())
                print(f"\nAborted before any agent ran: {exc}\nThe branch {branch} was created and is empty "
                      f"— delete it with: git -C {repo} branch -D {branch}", file=sys.stderr)
                return EXIT_FAILED
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
        if isinstance(exc, RuntimeError | TimeoutError):  # an agent or a checkpoint failed: the message says it all
            raise SystemExit(EXIT_FAILED)
        raise

    finished = bool(result["tests_passed"] and result["audit_clean"] and plan_done(str(plan)))
    blocked = plan_blocked(str(plan))
    caveats = ([f"BLOCKED: {', '.join(blocked)}"] if blocked else []) + \
        (["no gate ran — green checked nothing"] if not gates else [])
    code = EXIT_FAILED if not finished else EXIT_NEEDS_REVIEW if caveats else EXIT_CLEAN
    runlog.log("finish", iterations=result["iteration"], tests_passed=result["tests_passed"],
               audit_clean=result["audit_clean"], errors=result["errors"], problems=result["problems"],
               clean=code == EXIT_CLEAN, exit_code=code, caveats=caveats, usage=runlog.totals())
    print(f"\nFinished after {result['iteration']} iteration(s), "
          f"tests_passed={result['tests_passed']}, audit_clean={result['audit_clean']}\nPlan: {_tally(str(plan))}")
    print(f"Usage: {runlog.summary()}")
    if branch and base_sha:
        print(f"Review with: git -C {repo} log --oneline {base_sha[:7]}..{branch}")
    if result["errors"] or result["problems"]:
        print(json.dumps({"errors": result["errors"], "problems": result["problems"]}, indent=2))
    if result.get("idle_passes", 0) >= IDLE_LIMIT:
        print(f"Stopped early: {IDLE_LIMIT} coder passes in a row changed nothing. Read the last coder output above.")
    if finished and caveats:
        print("Needs a human look (exit 2): " + "; ".join(caveats))
    return code


if __name__ == "__main__":
    main()
