"""Deterministic gate: runs the project's test and lint commands and emits structured
errors, never raw dumps.

No LLM here, so it costs zero tokens. What it *saves* downstream is the shape of its
output: one {file, line, description} per failure instead of a full traceback that
would be pasted into every prompt after it.

The commands are configuration, not code — `--test-cmd` / `--lint-cmd`, or a
`.mae/config.json` in the target repo — so the same engine drives a Python, a JS or a Go
repo. Only the parsing is built in, and it falls back to the raw tail whenever it
does not recognise the output, so an unknown tool degrades instead of failing.
"""
import json
import re
import shlex
import subprocess
from pathlib import Path

from engine import git, runlog
from engine.state import GraphState

DEFAULT_TEST_CMD = "pytest -q --tb=line --no-header"
DEFAULT_LINT_CMD = "ruff check . --output-format json"
# pytest 5 = no tests collected, which is not a failure of the code under test.
TEST_OK_CODES = (0, 5)

# "path:line: message" and "path:line:col: message" — pytest --tb=line, ruff's text
# output, mypy, tsc, eslint compact, go vet and gcc all print one of these. The
# file group must contain a dot or a slash so timestamps and prose can't match.
FAIL_LINE = re.compile(
    r"^(?P<file>[^\s:]*[./][^\s:]*):(?P<line>\d+)(?::\d+)?:\s+(?P<desc>\S.+)$", re.MULTILINE
)
MAX_ERRORS = 20
FALLBACK_CHARS = 800


def _rel(path: str, repo: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(repo).resolve()))
    except ValueError:
        return path


def _run(cmd: list[str], cwd: str) -> tuple[int | None, str, str]:
    """Returns (exit code, stdout, stderr). Code is None if the tool isn't installed.
    Kept separate because ruff's JSON goes to stdout while its warnings go to stderr,
    and concatenating them makes the JSON unparseable."""
    print(f"$ {' '.join(cmd)}", flush=True)
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900, check=False)
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        print(f"({cmd[0]} not installed, skipped)", flush=True)
        return None, "", ""
    except subprocess.TimeoutExpired:
        return 1, "", f"{cmd[0]} timed out after 15 min"


def _unparsed(output: str) -> list[dict]:
    """A failure we couldn't structure still has to reach the reviewer."""
    return [{"file": "", "line": 0, "description": output.strip()[-FALLBACK_CHARS:]}]


def _json_errors(out: str, repo: str) -> list[dict] | None:
    """Diagnostics from a linter that speaks JSON. None = not JSON we recognise."""
    try:
        payload = json.loads(out)
    except ValueError:
        return None
    if not isinstance(payload, list):
        return None
    errors = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        if "filePath" in item:  # eslint: one entry per file, findings nested
            for msg in item.get("messages", []):
                errors.append({"file": _rel(item["filePath"], repo), "line": msg.get("line", 0),
                               "description": f"{msg.get('ruleId')}: {msg.get('message')}"})
        elif "filename" in item:  # ruff
            errors.append({"file": _rel(item["filename"], repo),
                           "line": (item.get("location") or {}).get("row", 0),
                           "description": f"{item.get('code')}: {item.get('message')}"})
        else:
            return None
    return errors


def _text_errors(output: str, repo: str) -> list[dict]:
    return [{"file": _rel(m["file"], repo), "line": int(m["line"]), "description": m["desc"].strip()}
            for m in FAIL_LINE.finditer(output)]


def run_gate(cmd: list[str], repo: str, ok_codes: tuple[int, ...] = (0,)) -> list[dict]:
    """Run one gate command and return its failures as structured errors."""
    if not cmd:
        return []
    code, out, err = _run(cmd, repo)
    if code is None or code in ok_codes:
        return []
    errors = _json_errors(out, repo)
    if errors is None:
        errors = _text_errors(out + err, repo)
    return errors or _unparsed(out + err)


def tester_node(state: GraphState) -> dict:
    repo = state["repo"]
    errors = []
    for gate in state["gates"]:
        errors += run_gate(gate["cmd"], repo, tuple(gate.get("ok_codes") or (0,)))
    errors = errors[:MAX_ERRORS]
    for error in errors:
        print(f"  {error['file']}:{error['line']} {error['description']}", flush=True)
    print(f"[tester] {len(errors)} error(s)", flush=True)

    iteration = state["iteration"]
    runlog.log("gate", iteration=iteration, errors=len(errors),
               failures=[f"{e['file']}:{e['line']} {e['description'][:120]}" for e in errors])
    if state.get("git_branch"):
        # every pass is a rollback point, red ones included: that is where the diff
        # showing what broke actually lives
        status = "green" if not errors else f"{len(errors)} error(s)"
        sha = git.checkpoint(repo, f"engine: iteration {iteration} — {status}")
        if sha:
            runlog.log("checkpoint", iteration=iteration, sha=sha, status=status)
    return {"errors": errors, "tests_passed": not errors}


def parse_cmd(value: str | None, default: str) -> list[str]:
    """'' disables a gate entirely; None falls back to the default."""
    return shlex.split(default if value is None else value)


def build_gates(gates: list[dict] | None, test_cmd: str | None, lint_cmd: str | None) -> list[dict]:
    """A gate is {cmd, ok_codes}. The `gates` list in the repo config is the general form —
    any number of tools, in order; --test-cmd/--lint-cmd are the two-gate shortcut."""
    if gates is not None:
        out = []
        for gate in gates:
            if not isinstance(gate, dict) or "cmd" not in gate:
                raise SystemExit(f"gates: each entry needs a 'cmd' key, got {gate!r}")
            if gate["cmd"]:
                out.append({"cmd": shlex.split(gate["cmd"]), "ok_codes": tuple(gate.get("ok_codes") or (0,))})
        return out
    pair = [{"cmd": parse_cmd(test_cmd, DEFAULT_TEST_CMD), "ok_codes": TEST_OK_CODES},
            {"cmd": parse_cmd(lint_cmd, DEFAULT_LINT_CMD), "ok_codes": (0,)}]
    return [gate for gate in pair if gate["cmd"]]
