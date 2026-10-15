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

# Errors that clear on their own; the reset time may be a unix epoch or "resets <ISO> UTC".
RETRYABLE = re.compile(r"limit reached|rate.?limit|overloaded", re.IGNORECASE)
RESET_EPOCH = re.compile(r"(?:reset\w*|reached)\D{0,20}(\d{10})\b")
RESET_ISO = re.compile(r"resets?\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\s*UTC", re.IGNORECASE)
# Non-fatal codex log line carrying ~325 KB of models JSON; dropped from the stream.
CODEX_NOISE = re.compile(r"failed to refresh available models")
DEFAULT_WAIT = 1800  # no reset time in the output
CLOCK_SKEW = 30
MAX_WAITS = 4
MAX_SLEEP = 6 * 3600  # a session limit is worth waiting out; a monthly cap is not
# Only the end of a failed transcript is searched for a limit message: code or prose the agent wrote
# earlier ("add a rate limit to the API") must not turn a real crash into hours of sleeping and re-runs.
LIMIT_TAIL_LINES = 30


def _limit_text(lines: list[str]) -> str:
    """The CLI's own closing words: plain-text lines and the claude `result` event, never the agent's messages."""
    kept = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            kept.append(line)
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            kept.append(f"{ev.get('subtype', '')} {ev.get('result', '')}")
        elif not isinstance(ev, dict) or ev.get("type") not in ("assistant", "user", "system"):
            kept.append(line)
    return "".join(kept[-LIMIT_TAIL_LINES:])


def _retry_delay(text: str, now: float | None = None) -> float:
    """Seconds to wait before retrying; 0 if it wasn't a limit error."""
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
    """Kill the whole process group — the CLIs spawn children that would keep editing the repo."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


def _run_once(cmd: list[str], prompt: str, cwd: str, timeout: float, label: str, render) -> tuple[list[str], int]:
    """Run cmd with the prompt on stdin, printing progress live; returns (raw lines, exit code)."""
    print(f"\n--- {label} ---", flush=True)
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf8", errors="replace", bufsize=1, start_new_session=True,
        )
    except FileNotFoundError:
        raise RuntimeError(f"{label}: `{cmd[0]}` is not installed or not on PATH")
    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        _kill_tree(proc)

    # started before the prompt is written, so a CLI that never reads stdin can't hang us
    timer = threading.Timer(timeout, on_timeout)
    timer.start()
    lines = []
    try:
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except BrokenPipeError:  # the CLI exited before reading; its output says why
            pass
        for line in proc.stdout:
            if CODEX_NOISE.search(line):
                print(f"[{label}] (codex could not refresh its model list; using defaults)", flush=True)
                continue
            lines.append(line)
            shown = render(line) if render else line.rstrip()
            if shown:
                print(f"[{label}] {shown}", flush=True)
        proc.wait()
    except BaseException:  # Ctrl-C included
        _kill_tree(proc)
        raise
    finally:
        timer.cancel()
    if timed_out.is_set():
        raise TimeoutError(f"{label} exceeded its {timeout / 60:.0f} min budget and was killed")
    return lines, proc.returncode


def _stream(cmd: list[str], prompt: str, cwd: str, timeout: float, label: str, render=None,
            failed=None, on_attempt=None) -> list[str]:
    """Run cmd, sleeping through usage/rate limits until they reset.
    failed(lines): a reason string when an exit-0 transcript still reports an error, else ''.
    on_attempt(lines, seconds): called after every attempt, failed ones included, so their spend is logged."""
    for attempt in range(MAX_WAITS + 1):
        started = time.monotonic()
        lines = []
        try:
            lines, code = _run_once(cmd, prompt, cwd, timeout, label, render)
        finally:
            if on_attempt:
                on_attempt(lines, time.monotonic() - started)
        reason = (failed(lines) if failed else "") if code == 0 else f"exit {code}"
        if not reason:
            return lines
        delay = _retry_delay(_limit_text(lines))
        if not delay or attempt == MAX_WAITS:
            raise RuntimeError(f"{label} failed ({reason})")
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
        return line.rstrip()
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


def _claude_final(lines: list[str]) -> dict:
    """The `result` event of a stream-json transcript; {} if it never arrived."""
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            return ev
    return {}


def _claude_result(lines: list[str]) -> str:
    """Final answer only — never the transcript, or the graph state would snowball."""
    return (_claude_final(lines).get("result") or "").strip()


def _claude_usage(lines: list[str]) -> dict:
    ev = _claude_final(lines)
    if not ev:
        return {}
    usage = ev.get("usage", {})
    return {"cost_usd": ev.get("total_cost_usd", 0.0),
            "input_tokens": usage.get("input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0)}


def run_claude(prompt: str, cwd: str, label: str, write: bool = False, timeout: float = 600,
               add_dirs: list[str] | None = None, schema: dict | None = None) -> str:
    cmd = ["claude", "-p", "--permission-mode", "acceptEdits" if write else "plan",
           "--output-format", "stream-json", "--verbose"]  # --verbose is required by stream-json
    if write:  # headless acceptEdits refuses Bash, so the coder could never run the tests DONE requires
        cmd += ["--allowedTools", "Bash"]
    for directory in add_dirs or []:
        cmd += ["--add-dir", directory]
    if schema:  # claude has no --output-schema; the callers tolerate a chatty reply
        prompt += "\n\nReply with JSON only — no prose, no fences — matching this schema:\n" + json.dumps(schema)

    def failed(lines: list[str]) -> str:
        final = _claude_final(lines)
        if not final:
            return "no result event — the CLI stopped before finishing"
        return f"reported {final.get('subtype') or 'an error'}" if final.get("is_error") else ""

    def on_attempt(lines: list[str], seconds: float) -> None:
        runlog.log("agent", label=label, agent="claude", seconds=round(seconds, 1),
                   prompt_chars=len(prompt), **_claude_usage(lines))

    lines = _stream(cmd, prompt, cwd, timeout=timeout, label=label, render=_claude_progress,
                    failed=failed, on_attempt=on_attempt)
    return _claude_result(lines)


def run_codex(prompt: str, cwd: str, label: str, write: bool = False, timeout: float = 1200,
              add_dirs: list[str] | None = None, schema: dict | None = None) -> str:
    if add_dirs:
        print(f"[{label}] codex has no --add-dir: {', '.join(add_dirs)} will not be readable", flush=True)
    fd, last_message = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    cmd = ["codex", "exec", "-C", cwd, "-s", "workspace-write" if write else "read-only",
           "--skip-git-repo-check", "-o", last_message]
    schema_file = None
    if schema:
        fd, schema_file = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(schema, f)
        cmd += ["--output-schema", schema_file]

    def on_attempt(lines: list[str], seconds: float) -> None:  # codex reports no cost of its own
        runlog.log("agent", label=label, agent="codex", seconds=round(seconds, 1), prompt_chars=len(prompt))

    try:
        _stream(cmd, prompt, cwd, timeout=timeout, label=label, on_attempt=on_attempt)
        return Path(last_message).read_text(encoding="utf8", errors="replace").strip()
    finally:
        os.unlink(last_message)
        if schema_file:
            os.unlink(schema_file)


# Which CLI plays which role is configuration; every runner takes the same arguments.
RUNNERS = {"claude": run_claude, "codex": run_codex}
DEFAULT_AGENTS = {"coder": "claude", "reviewer": "codex", "auditor": "codex"}


def run_agent(agent: str, prompt: str, cwd: str, label: str, **kwargs) -> str:
    try:
        runner = RUNNERS[agent]
    except KeyError:
        raise SystemExit(f"unknown agent {agent!r} for {label} — known: {', '.join(sorted(RUNNERS))}")
    return runner(prompt, cwd, label=label, **kwargs)
