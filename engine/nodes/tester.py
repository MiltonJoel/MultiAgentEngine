"""Deterministic gate: runs the configured commands and emits {file, line, description} errors, never raw dumps."""
import json
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path

from engine import git, runlog
from engine.state import GraphState

# pytest exit 5 = no tests collected, which is not a failure of the code under test.
DEFAULT_GATES = [{"cmd": "pytest -q --tb=line --no-header", "ok_codes": [0, 5]},
                 {"cmd": "ruff check . --output-format json"}]

# "path:line[:col]: message" — pytest --tb=line, ruff, mypy, tsc, eslint compact, go vet, gcc.
# The file must contain a dot or slash so timestamps and prose can't match.
FAIL_LINE = re.compile(
    r"^(?P<file>[^\s:]*[./][^\s:]*):(?P<line>\d+)(?::\d+)?:\s+(?P<desc>\S.+)$", re.MULTILINE
)
MAX_ERRORS = 20
FALLBACK_CHARS = 800
GATE_TIMEOUT = 900


def _rel(path: str, repo: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(repo).resolve()))
    except ValueError:
        return path


def _run(cmd: list[str], cwd: str, timeout: float | None = None) -> tuple[int | None, str, str]:
    """(exit code, stdout, stderr); code None if the tool isn't installed, "timeout" text in stderr on a timeout.
    The gate gets its own process group so a timeout also kills the servers and workers it started."""
    print(f"$ {' '.join(cmd)}", flush=True)
    timeout = GATE_TIMEOUT if timeout is None else timeout
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf8", errors="replace", start_new_session=True)
    except FileNotFoundError:
        return None, "", ""
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException as exc:  # a timeout, or Ctrl-C: never leave the gate's children running
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        if isinstance(exc, subprocess.TimeoutExpired):
            return None, "", f"{cmd[0]} timed out after {timeout / 60:.0f} min and was killed"
        raise
    return proc.returncode, out, err


def _unparsed(output: str) -> list[dict]:
    return [{"file": "", "line": 0, "description": output.strip()[-FALLBACK_CHARS:]}]


def _json_errors(out: str, repo: str) -> list[dict] | None:
    """ruff / eslint JSON diagnostics; None if it isn't JSON we recognise."""
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
        if "filePath" in item:  # eslint
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
    """Run one gate; JSON first, then text, then the raw tail."""
    if not cmd:
        return []
    code, out, err = _run(cmd, repo)
    if code is None:  # checked at startup, so missing now (or hung) is a failure, never a silent pass
        why = err or f"{cmd[0]} is no longer installed — the gate could not run"
        return [{"file": "", "line": 0, "description": why}]
    if code in ok_codes:
        return []
    errors = _json_errors(out, repo)
    if errors is None:
        errors = _text_errors(out + err, repo)
    return errors or _unparsed(out + err)


def tester_node(state: GraphState) -> dict:
    from engine.graph import plan_issues  # graph imports this module

    repo = state["repo"]
    # the coder edits the plan freely; a status block it broke would otherwise silently drop or close units
    errors = [{"file": state["plan"], "line": 0, "description": f"plan: {issue}"}
              for issue in plan_issues(state["plan"], state.get("focus"))]
    for gate in state["gates"]:
        errors += run_gate(gate["cmd"], repo, tuple(gate.get("ok_codes") or (0,)))
    errors = errors[:MAX_ERRORS]
    for error in errors:
        print(f"  {error['file']}:{error['line']} {error['description']}", flush=True)
    print(f"[tester] {len(errors)} error(s)", flush=True)

    iteration = state["iteration"]
    runlog.log("gate", iteration=iteration, errors=len(errors),
               failures=[f"{e['file']}:{e['line']} {e['description'][:120]}" for e in errors])
    if state.get("git_branch"):  # every pass is a rollback point, red ones included
        status = "green" if not errors else f"{len(errors)} error(s)"
        sha = git.checkpoint(repo, f"engine: iteration {iteration} — {status}", strict=True)
        if sha:
            runlog.log("checkpoint", iteration=iteration, sha=sha, status=status)
    # a green build means the reviewer's diagnosis was acted on; don't keep feeding it to the coder
    return {"errors": errors, "tests_passed": not errors, "problems": state.get("problems", []) if errors else []}


def build_gates(gates: list | None) -> list[dict]:
    """Normalise a gates list: each entry is a command string, or {cmd, ok_codes} when a
    non-zero exit is fine. None = defaults; [] or [''] = nothing gates the coder."""
    out = []
    for gate in DEFAULT_GATES if gates is None else gates:
        if isinstance(gate, str):
            gate = {"cmd": gate}
        if not isinstance(gate, dict) or not isinstance(gate.get("cmd"), str):
            raise SystemExit(f"gates: each entry is a command string or {{\"cmd\": ...}}, got {gate!r}")
        ok_codes = gate.get("ok_codes") or [0]
        if not isinstance(ok_codes, list) or not all(isinstance(c, int) and not isinstance(c, bool) for c in ok_codes):
            raise SystemExit(f"gates: ok_codes must be a list of integers, got {gate.get('ok_codes')!r}")
        try:
            cmd = shlex.split(gate["cmd"])
        except ValueError as exc:
            raise SystemExit(f"gates: cannot parse {gate['cmd']!r}: {exc}")
        if cmd:
            out.append({"cmd": cmd, "ok_codes": tuple(ok_codes)})
    return out
