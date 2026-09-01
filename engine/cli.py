"""Thin wrappers around the `claude` and `codex` CLIs (already authenticated locally)."""
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import runlog

# Errors that clear on their own — the CLI classifies these internally as
# rate_limit / billing_error / overloaded and ends the turn.
RETRYABLE = re.compile(r"limit reached|rate.?limit|overloaded", re.IGNORECASE)
# A reset time may come through as a unix epoch or as "resets <ISO> UTC".
RESET_EPOCH = re.compile(r"(?:reset\w*|reached)\D{0,20}(\d{10})\b")
RESET_ISO = re.compile(r"resets?\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\s*UTC", re.IGNORECASE)
# When codex's client is older than the server's model list it logs one ERROR line
# carrying the whole models JSON (~325 KB). Non-fatal — it falls back to defaults
# and exits 0 — but it drowns the terminal and would otherwise be scanned by the
# rate-limit detector below.
CODEX_NOISE = re.compile(r"failed to refresh available models")
DEFAULT_WAIT = 1800  # no reset time in the output: check back in 30 min
CLOCK_SKEW = 30
MAX_WAITS = 4
# A session limit resets in hours and is worth sleeping through; a monthly spend
# cap is not. Anything past this is reported instead of waited on.
MAX_SLEEP = 6 * 3600


def _retry_delay(text: str, now: float | None = None) -> float:
    """Seconds to wait before retrying a failed run; 0 if it wasn't a limit error."""
    if not RETRYABLE.search(text):
        return 0.0
    now = time.time() if now is None else now
    match = RESET_EPOCH.search(text)
    if match:
        return max(60.0, int(match.group(1)) - now + CLOCK_SKEW)
    match = RESET_ISO.search(text)
    if match:
        reset = datetime.strptime(match.group(1).replace("T", " ") + " +0000", "%Y-%m-%d %H:%M %z")
        return max(60.0, reset.timestamp() - now + CLOCK_SKEW)
    return DEFAULT_WAIT


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the whole process group, not just the CLI.

    `claude` and `codex` spawn their own children; killing only the parent leaves
    them running against the repo after a timeout or a Ctrl-C.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _run_once(cmd: list[str], prompt: str, cwd: str, timeout: float, label: str, render) -> tuple[list[str], int]:
    """Run cmd with prompt piped on stdin (avoids OS argv-size limits on long prompts),
    printing progress live while capturing every raw line."""
    print(f"\n--- {label} ---", flush=True)
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, start_new_session=True,  # own process group, so the kill reaches the children
    )
    proc.stdin.write(prompt)
    proc.stdin.close()
    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        _kill_tree(proc)

    timer = threading.Timer(timeout, on_timeout)
    timer.start()
    lines = []
    try:
        for line in proc.stdout:
            if CODEX_NOISE.search(line):
                print(f"[{label}] (codex could not refresh its model list; using defaults)", flush=True)
                continue
            lines.append(line)
            shown = render(line) if render else line.rstrip()
            if shown:
                print(f"[{label}] {shown}", flush=True)
        proc.wait()
    except BaseException:  # Ctrl-C included: never leave the agent writing to the repo
        _kill_tree(proc)
        raise
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise TimeoutError(f"{label} exceeded its {timeout / 60:.0f} min budget and was killed")
    return lines, proc.returncode


def _stream(cmd: list[str], prompt: str, cwd: str, timeout: float, label: str, render=None) -> list[str]:
    """Run cmd, sleeping through usage/rate limits until they reset.

    Only a non-zero exit is inspected, so a run that merely *mentions* rate limits
    in its output can't trigger a spurious sleep.
    """
    for attempt in range(MAX_WAITS + 1):
        lines, code = _run_once(cmd, prompt, cwd, timeout, label, render)
        if code == 0:
            return lines
        delay = _retry_delay("".join(lines))
        if not delay or attempt == MAX_WAITS:
            raise RuntimeError(f"{label} failed (exit {code})")
        # local time, so the printed resume time matches the user's clock
        resume = datetime.now(timezone.utc).astimezone().replace(microsecond=0) + timedelta(seconds=delay)
        if delay > MAX_SLEEP:
            raise RuntimeError(
                f"{label} hit a limit that resets ~{resume:%Y-%m-%d %H:%M} "
                f"({delay / 3600:.1f}h away) — too long to wait, re-run after it clears"
            )
        print(f"[{label}] limit reached — sleeping {delay / 60:.0f} min, resuming ~{resume:%H:%M}",
              flush=True)
        runlog.log("limit", label=label, sleep_min=round(delay / 60), resume=f"{resume:%H:%M}")
        time.sleep(delay)
    raise RuntimeError(f"{label} still limited after {MAX_WAITS} waits")


def _claude_progress(line: str) -> str:
    """One-line human summary of a claude stream-json event ('' = show nothing)."""
    try:
        ev = json.loads(line)
    except ValueError:
        return line.rstrip()  # stderr noise, not JSON: show as-is
    kind = ev.get("type")
    if kind == "assistant":
        parts = []
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                parts.append(f"({block.get('name')})")
        return " ".join(p for p in parts if p)
    if kind == "result":
        usage = ev.get("usage", {})
        return (f"done — in={usage.get('input_tokens', 0)} "
                f"cache_read={usage.get('cache_read_input_tokens', 0)} "
                f"out={usage.get('output_tokens', 0)} "
                f"cost=${ev.get('total_cost_usd', 0):.4f}")
    return ""


def _claude_result(lines: list[str]) -> str:
    """Final answer text from a `claude --output-format stream-json` transcript.

    Critical: returning only this (instead of the whole transcript) is what keeps the
    graph state from snowballing, since every node's output is re-injected downstream.
    """
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            return (ev.get("result") or "").strip()
    return ""


def _claude_usage(lines: list[str]) -> dict:
    """Token counts and cost from the final result event; {} if it never arrived."""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            usage = ev.get("usage", {})
            return {"cost_usd": ev.get("total_cost_usd", 0.0),
                    "input_tokens": usage.get("input_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0)}
    return {}


def run_claude(prompt: str, cwd: str, permission_mode: str = "plan", label: str = "claude",
               timeout: float = 600, add_dirs: list[str] | None = None) -> str:
    """permission_mode='plan' = read-only. 'acceptEdits' = can write files (coder).
    add_dirs = extra directories the tools may touch, for anything outside cwd —
    the plan file, plus any codebase the plan tells the agent to read."""
    cmd = ["claude", "-p", "--permission-mode", permission_mode,
           "--output-format", "stream-json", "--verbose"]  # --verbose is required by stream-json
    for directory in add_dirs or []:
        cmd += ["--add-dir", directory]
    with runlog.Timer() as elapsed:
        lines = _stream(cmd, prompt, cwd, timeout=timeout, label=label, render=_claude_progress)
    runlog.log("agent", label=label, agent="claude", seconds=elapsed.seconds,
               prompt_chars=len(prompt), **_claude_usage(lines))
    return _claude_result(lines)


def run_codex(prompt: str, cwd: str, sandbox: str = "read-only", label: str = "codex",
              timeout: float = 1200, schema: dict | None = None) -> str:
    """sandbox='read-only' = can't edit files (reviewer). 'workspace-write' = can.
    schema = JSON Schema the final response must conform to."""
    fd, last_message = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    cmd = ["codex", "exec", "-C", cwd, "-s", sandbox, "--skip-git-repo-check", "-o", last_message]
    schema_file = None
    if schema:
        fd, schema_file = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(schema, f)
        cmd += ["--output-schema", schema_file]
    try:
        with runlog.Timer() as elapsed:
            _stream(cmd, prompt, cwd, timeout=timeout, label=label)
        # codex reports no cost of its own, so seconds is all there is to log
        runlog.log("agent", label=label, agent="codex", seconds=elapsed.seconds, prompt_chars=len(prompt))
        return Path(last_message).read_text().strip()
    finally:
        os.unlink(last_message)
        if schema_file:
            os.unlink(schema_file)
