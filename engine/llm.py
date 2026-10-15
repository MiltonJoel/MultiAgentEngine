"""Free-model runners: a small tool-using agent over any OpenAI-compatible chat endpoint (stdlib only).

`ollama` talks to a local Ollama server (no account, no key, no cost). `api` talks to any hosted
OpenAI-compatible endpoint, e.g. a free tier on OpenRouter, Groq or Google AI Studio. Both give the model
the same tools the CLIs have: read and search files, and — for the coder — write files and run commands,
so it can run the tests DONE requires."""
import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from engine import runlog

# Ollama's own default model for code; pick a bigger one with "ollama:<model>" if the machine allows.
DEFAULT_MODEL = "qwen2.5-coder:7b"
OLLAMA_URL = "http://localhost:11434/v1"
API_KEY_ENV = "MAE_API_KEY"
# How each runner finds its endpoint: config "llm.<runner>" beats these env vars beats the defaults.
SETTINGS = {
    "ollama": {"url": OLLAMA_URL, "model": DEFAULT_MODEL, "key_env": ""},
    "api": {"url": "", "model": "", "key_env": API_KEY_ENV},
}
ENV = {"ollama": {"url": "MAE_OLLAMA_URL", "model": "MAE_OLLAMA_MODEL"},
       "api": {"url": "MAE_API_URL", "model": "MAE_API_MODEL"}}

MAX_STEPS = 200  # model turns per call; a coder pass that needs more is stuck
MAX_OUTPUT = 10000  # chars of one tool result kept in the conversation
# Older tool results are shortened once the conversation passes this, so a small local context never overflows.
MAX_CONTEXT_CHARS = 60000
COMMAND_TIMEOUT = 900
HTTP_RETRIES = 4
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}

_config: dict[str, dict] = {}


def configure(llm: dict | None) -> None:
    """Take the `llm` section of .mae/config.json: {"ollama": {...}, "api": {"url", "model", "key_env"}}."""
    _config.clear()
    for runner, values in (llm or {}).items():
        if runner not in SETTINGS or not isinstance(values, dict):
            raise SystemExit(f"llm: unknown runner {runner!r} — known: {', '.join(SETTINGS)}")
        for key, value in values.items():
            if key not in SETTINGS[runner] or not isinstance(value, str):
                raise SystemExit(f"llm.{runner}: \"{key}\" is not a setting — use {', '.join(SETTINGS[runner])}")
        _config[runner] = dict(values)


def settings(runner: str, model: str = "") -> dict:
    """url, model and key for a runner; `model` (from "ollama:<model>") wins over everything."""
    out = dict(SETTINGS[runner])
    for key, env in ENV[runner].items():
        if os.environ.get(env):
            out[key] = os.environ[env]
    out.update(_config.get(runner, {}))
    if model:
        out["model"] = model
    out["url"] = out["url"].rstrip("/")
    out["key"] = os.environ.get(out["key_env"], "") if out["key_env"] else ""
    return out


def _request(url: str, key: str, payload: dict | None, timeout: float) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf8", errors="replace"))


def check(runner: str, model: str = "", timeout: float = 3) -> str:
    """Why this runner can't work right now, or '' if it can. Run before the run spends anything."""
    s = settings(runner, model)
    if not s["url"]:
        return (f"the {runner} runner has no endpoint — set \"llm\": {{\"{runner}\": {{\"url\": ...}}}} "
                f"in .mae/config.json or {ENV[runner]['url']}")
    if not s["model"]:
        return f"the {runner} runner has no model — use {runner}:<model> or set llm.{runner}.model"
    if s["key_env"] and not s["key"]:
        return f"the {runner} runner needs its key in ${s['key_env']}"
    try:
        listed = _request(f"{s['url']}/models", s["key"], None, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return f"{s['url']} refused the key in ${s['key_env']} ({exc.code})"
        return ""  # some endpoints don't list models; the first call will say what's wrong
    except (urllib.error.URLError, OSError, ValueError) as exc:
        exc = getattr(exc, "reason", exc)
        if runner == "ollama":
            return (f"Ollama is not reachable at {s['url']} ({exc}) — install it from https://ollama.com, "
                    f"start it, and run `ollama pull {s['model']}`")
        return f"{s['url']} is not reachable ({exc})"
    names = {m.get("id") for m in listed.get("data", []) if isinstance(m, dict)}
    if runner == "ollama" and s["model"] not in names and f"{s['model']}:latest" not in names:
        return f"Ollama has no model {s['model']!r} — run `ollama pull {s['model']}`"
    return ""


# ---- tools ----------------------------------------------------------------------------------------------

def _tool(name: str, description: str, **params) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {k: {"type": "string", "description": v}
                                                         for k, v in params.items()},
                       "required": list(params)}}}


READ_TOOLS = [
    _tool("list_files", "List files under a directory, recursively (skips .git, venvs, caches).",
          path="directory, relative to the repository root ('.' for the root)"),
    _tool("read_file", "Read a text file.", path="file path, relative to the repository root"),
    _tool("search", "Find lines matching a regular expression in the files under a directory.",
          pattern="Python regular expression", path="directory to search ('.' for the repository)"),
]
WRITE_TOOLS = [
    _tool("write_file", "Create or overwrite a file with the given content.",
          path="file path, relative to the repository root", content="the complete new file content"),
    _tool("replace_in_file", "Replace one exact occurrence of `old` with `new` in a file.",
          path="file path, relative to the repository root", old="exact text to replace, must occur once",
          new="replacement text"),
    _tool("run_command", "Run a shell command in the repository root (tests, linters, formatters) and get "
          "its exit code and output.", command="the shell command"),
]


class Workspace:
    """The files the model may touch: the repository plus the extra directories it was given."""

    def __init__(self, cwd: str, add_dirs: list[str], write: bool, deadline: float):
        self.cwd = Path(cwd).resolve()
        self.roots = [self.cwd, *(Path(d).resolve() for d in add_dirs)]
        self.write = write
        self.deadline = deadline

    def path(self, raw: str) -> Path:
        path = (self.cwd / Path(raw or ".").expanduser()).resolve()
        if not any(path == root or root in path.parents for root in self.roots):
            raise ValueError(f"{raw} is outside the repository and the allowed directories")
        return path

    def run(self, name: str, args: dict) -> str:
        if name in {t["function"]["name"] for t in WRITE_TOOLS} and not self.write:
            return f"error: {name} is not available — this pass is read-only"
        try:
            return getattr(self, f"_{name}")(**args)
        except AttributeError:
            return f"error: unknown tool {name!r}"
        except TypeError as exc:
            return f"error: bad arguments for {name}: {exc}"
        except (OSError, ValueError, re.error) as exc:
            return f"error: {exc}"

    def _files(self, base: Path):
        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                yield Path(root) / name

    def _list_files(self, path: str = ".") -> str:
        base = self.path(path)
        names = [str(p.relative_to(base)) for p in self._files(base)]
        return "\n".join(names[:2000]) + ("\n(truncated)" if len(names) > 2000 else "") or "(empty)"

    def _read_file(self, path: str) -> str:
        return self.path(path).read_text(encoding="utf8", errors="replace")

    def _search(self, pattern: str, path: str = ".") -> str:
        regex, base, hits = re.compile(pattern), self.path(path), []
        for file in self._files(base):
            try:
                text = file.read_text(encoding="utf8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{file.relative_to(base)}:{number}: {line.strip()[:200]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n(more matches not shown)"
        return "\n".join(hits) or "no matches"

    def _write_file(self, path: str, content: str) -> str:
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf8")
        return f"wrote {len(content)} chars to {path}"

    def _replace_in_file(self, path: str, old: str, new: str) -> str:
        target = self.path(path)
        text = target.read_text(encoding="utf8")
        count = text.count(old) if old else 0
        if count != 1:
            return f"error: `old` occurs {count} times in {path}; it must occur exactly once"
        target.write_text(text.replace(old, new, 1), encoding="utf8")
        return f"replaced 1 occurrence in {path}"

    def _run_command(self, command: str) -> str:
        timeout = max(1.0, min(COMMAND_TIMEOUT, self.deadline - time.monotonic()))
        proc = subprocess.Popen(command, shell=True, cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf8", errors="replace",
                                start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except BaseException as exc:  # timeout or Ctrl-C: never leave the command's children running
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                return f"error: command timed out after {timeout:.0f}s and was killed"
            raise
        return f"exit code {proc.returncode}\n{out}"


# ---- the loop ---------------------------------------------------------------------------------------------

def _system(ws: Workspace) -> str:
    rights = ("You can read, write and edit files and run shell commands (use them to run the tests)."
              if ws.write else "This pass is read-only: you can read and search files but not change anything.")
    extra = "".join(f"\nAlso readable{' and writable' if ws.write else ''}: {r}" for r in ws.roots[1:])
    return (f"You are a software engineering agent working in the repository at {ws.cwd}.{extra}\n{rights}\n"
            "Use the tools to look at the actual files instead of guessing. Paths are relative to the repository "
            "root. When the work is finished, stop calling tools and reply with your final answer.")


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n… ({len(text) - MAX_OUTPUT} chars cut) …\n{text[-half:]}"


def _trim(messages: list[dict]) -> None:
    """Shorten the oldest tool results until the conversation fits; the prompt and recent results stay intact."""
    total = sum(len(m.get("content") or "") for m in messages)
    for message in messages:
        if total <= MAX_CONTEXT_CHARS:
            return
        content = message.get("content") or ""
        if message.get("role") == "tool" and len(content) > 200:
            message["content"] = content[:200] + "\n(older output trimmed to save context)"
            total -= len(content) - len(message["content"])


def _text_calls(content: str) -> list[dict]:
    """Small models sometimes write the tool call as JSON text instead of using tool_calls."""
    try:
        payload = json.loads(content.strip().removeprefix("```json").removeprefix("```").removesuffix("```"))
    except ValueError:
        return []
    if isinstance(payload, dict) and isinstance(payload.get("name"), str) and "arguments" in payload:
        return [{"id": "call_text", "type": "function",
                 "function": {"name": payload["name"], "arguments": payload["arguments"]}}]
    return []


def _chat(s: dict, messages: list[dict], tools: list[dict], deadline: float, label: str) -> dict:
    payload = {"model": s["model"], "messages": messages, "tools": tools, "temperature": 0.2}
    for attempt in range(HTTP_RETRIES + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{label} ran out of its time budget")
        try:
            return _request(f"{s['url']}/chat/completions", s["key"], payload, remaining)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf8", errors="replace")[:500]
            if exc.code not in (429, 500, 502, 503, 504) or attempt == HTTP_RETRIES:
                raise RuntimeError(f"{label}: {s['url']} answered {exc.code}: {body}")
            try:
                delay = float(exc.headers.get("Retry-After") or 0)
            except ValueError:
                delay = 0
            delay = min(max(delay, 5 * 2 ** attempt), 600)
            print(f"[{label}] {exc.code} from the model endpoint — retrying in {delay:.0f}s", flush=True)
            runlog.log("limit", label=label, sleep_min=round(delay / 60, 1))
            time.sleep(delay)
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc):
                raise TimeoutError(f"{label} ran out of its time budget waiting for the model")
            raise RuntimeError(f"{label}: cannot reach {s['url']} ({exc})")
    raise RuntimeError(f"{label}: no answer from {s['url']}")


def run_model(runner: str, prompt: str, cwd: str, label: str, write: bool = False, timeout: float = 1800,
              add_dirs: list[str] | None = None, schema: dict | None = None, model: str = "") -> str:
    s = settings(runner, model)
    if schema:  # tools and a JSON response format don't mix on most servers; ask for it in words, like claude
        prompt += "\n\nReply with JSON only — no prose, no fences — matching this schema:\n" + json.dumps(schema)
    started = time.monotonic()
    ws = Workspace(cwd, add_dirs or [], write, started + timeout)
    tools = READ_TOOLS + (WRITE_TOOLS if write else [])
    messages = [{"role": "system", "content": _system(ws)}, {"role": "user", "content": prompt}]
    usage = {"input_tokens": 0, "output_tokens": 0}
    print(f"\n--- {label} ({runner}: {s['model']}) ---", flush=True)
    try:
        for _ in range(MAX_STEPS):
            _trim(messages)
            reply = _chat(s, messages, tools, ws.deadline, label)
            counted = reply.get("usage") or {}
            usage["input_tokens"] += counted.get("prompt_tokens") or 0
            usage["output_tokens"] += counted.get("completion_tokens") or 0
            try:
                message = reply["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise RuntimeError(f"{label}: unexpected reply from {s['url']}: {json.dumps(reply)[:300]}")
            content = message.get("content") or ""
            calls = message.get("tool_calls") or _text_calls(content)
            if content and not calls:
                print(f"[{label}] {content.strip()[:2000]}", flush=True)
            if not calls:
                return content.strip()
            messages.append({"role": "assistant", "content": content if message.get("tool_calls") else "",
                             "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                raw = fn.get("arguments")
                try:
                    args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                except ValueError:
                    args = None
                name = fn.get("name", "")
                shown = (args.get("path") or args.get("command") or "") if isinstance(args, dict) else ""
                print(f"[{label}] ({name}) {str(shown)[:200]}", flush=True)
                result = ws.run(name, args) if isinstance(args, dict) else "error: arguments were not a JSON object"
                messages.append({"role": "tool", "tool_call_id": call.get("id") or name, "content": _clip(result)})
        raise RuntimeError(f"{label} did not finish within {MAX_STEPS} model turns")
    finally:
        runlog.log("agent", label=label, agent=runner, model=s["model"], seconds=round(time.monotonic() - started, 1),
                   prompt_chars=len(prompt), cost_usd=0.0, **usage)


def run_ollama(prompt: str, cwd: str, label: str, **kwargs) -> str:
    return run_model("ollama", prompt, cwd, label, **kwargs)


def run_api(prompt: str, cwd: str, label: str, **kwargs) -> str:
    return run_model("api", prompt, cwd, label, **kwargs)
