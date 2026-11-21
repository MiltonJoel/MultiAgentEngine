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
from typing import Any
from urllib.parse import urlparse

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
# Tool results go stale fast: keep the newest whole and decay the rest, so every model turn
# re-prefills less. The MAX_CONTEXT_CHARS wall below stays as the final backstop.
FRESH_RESULTS = 2    # newest tool results keep their full (clipped) text
RECENT_RESULTS = 4  # after those: head+tail shrunk to RECENT_CHARS
RECENT_CHARS = 1000
STALE_CHARS = 200  # older than that: a stub that keeps the tail
# Older tool results are shortened once the conversation passes this, so a small local context never overflows.
MAX_CONTEXT_CHARS = 60000
COMMAND_TIMEOUT = 900
HTTP_RETRIES = 4
# A repo's .mae/config.json picks `key_env`, and the same file can pick the endpoint the key is sent to. So the
# variable must be a model key, never an ambient credential (GITHUB_TOKEN, AWS_*, HF_TOKEN, …): default-deny,
# allowing MAE_* names, the usual model-provider key names, and whatever the user lists in MAE_ALLOWED_KEY_ENVS
# (comma-separated, read from the user's environment — a repo cannot set it).
MODEL_KEY_ENVS = {"OPENROUTER_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_AI_API_KEY",
                  "MISTRAL_API_KEY", "DEEPSEEK_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY", "CEREBRAS_API_KEY",
                  "XAI_API_KEY", "PERPLEXITY_API_KEY", "DEEPINFRA_API_KEY", "NVIDIA_API_KEY", "SAMBANOVA_API_KEY",
                  "MOONSHOT_API_KEY", "DASHSCOPE_API_KEY", "ZHIPUAI_API_KEY", "OLLAMA_API_KEY", "LITELLM_API_KEY"}
ALLOWED_KEY_ENVS_ENV = "MAE_ALLOWED_KEY_ENVS"
KEY_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
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
        key_env = values.get("key_env")
        if key_env and not KEY_ENV_NAME.match(key_env):
            raise SystemExit(f"llm.{runner}.key_env: {key_env!r} is not an environment variable name")
        if key_env and not model_key_env(key_env):
            raise SystemExit(f"llm.{runner}.key_env: {key_env} is not a known model API key variable, so a repo's "
                             f"config may not send it to an endpoint. Use MAE_API_KEY (or any MAE_* name), or allow "
                             f"it yourself: export {ALLOWED_KEY_ENVS_ENV}={key_env}")
        url = values.get("url")
        if url and urlparse(url).scheme not in ("http", "https"):
            raise SystemExit(f"llm.{runner}.url: {url!r} must be an http:// or https:// URL")
        _config[runner] = dict(values)


def model_key_env(name: str) -> bool:
    """A variable a repo's config may name as the model key (see MODEL_KEY_ENVS)."""
    allowed = {n.strip() for n in os.environ.get(ALLOWED_KEY_ENVS_ENV, "").split(",") if n.strip()}
    return name.startswith("MAE_") or name in MODEL_KEY_ENVS or name in allowed


def settings(runner: str, model: str = "") -> dict[str, Any]:
    """url, model and key for a runner; `model` (from "ollama:<model>") wins over everything."""
    out: dict[str, Any] = dict(SETTINGS[runner])
    for key, env in ENV[runner].items():
        if os.environ.get(env):
            out[key] = os.environ[env]
    user_url = out["url"].rstrip("/")  # the built-in default or the user's own environment
    out.update(_config.get(runner, {}))
    if model:
        out["model"] = model
    out["url"] = out["url"].rstrip("/")
    out["key"] = os.environ.get(out["key_env"], "") if out["key_env"] else ""
    runlog.secret(out["key"])  # never written to a run log, whatever echoes it
    # the user chose this endpoint (built-in default or their own environment), not the repo's config file
    out["user_url"] = bool(out["url"]) and out["url"] == user_url
    return out


def insecure_key_url(url: str) -> bool:
    """A key would travel in clear text: plain http to anything but this machine."""
    parsed = urlparse(url)
    return parsed.scheme != "https" and (parsed.hostname or "") not in LOOPBACK_HOSTS


REDIRECTS = (301, 302, 303, 307, 308)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Model endpoints are never followed through redirects: urllib would forward the Authorization header to
    wherever the redirect points (another host, or plain http), past the checks made on the configured URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirects ({code}) to {newurl} — model endpoints are "
                                     "not followed through redirects; configure that URL instead", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def redact(text: str, key: str) -> str:
    """The key, scrubbed from text that is about to be printed, raised or logged (an endpoint may echo it)."""
    return text.replace(key, "[redacted]") if key else text


def _request(url: str, key: str, payload: dict | None, timeout: float) -> dict:
    if urlparse(url).scheme not in ("http", "https"):  # MAE_*_URL from the environment included: never file://
        raise ValueError(f"{url!r} is not an http:// or https:// URL")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        if insecure_key_url(url):
            raise ValueError(f"refusing to send an API key over plain http to {urlparse(url).hostname} — use https")
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf8", errors="replace"))


def check(runner: str, model: str = "", timeout: float = 3, send_key: bool = True) -> str:
    """Why this runner can't work right now, or '' if it can. Run before the run spends anything.
    send_key=False (the dashboard, which checks every runner as soon as a repo is opened) withholds the key
    unless the endpoint came from the user's own environment: a repo's config must not be able to make a
    mere click ship the key to an endpoint that same config chose."""
    s = settings(runner, model)
    send_key = send_key or bool(s["user_url"])
    if not s["url"]:
        return (f"the {runner} runner has no endpoint — set \"llm\": {{\"{runner}\": {{\"url\": ...}}}} "
                f"in .mae/config.json or {ENV[runner]['url']}")
    if not s["model"]:
        return f"the {runner} runner has no model — use {runner}:<model> or set llm.{runner}.model"
    if s["key_env"] and not s["key"]:
        return f"the {runner} runner needs its key in ${s['key_env']}"
    if s["key"] and insecure_key_url(s["url"]):
        return f"{s['url']} is plain http to another machine — the key in ${s['key_env']} needs an https:// URL"
    try:
        listed = _request(f"{s['url']}/models", s["key"] if send_key else "", None, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in REDIRECTS:
            return f"{s['url']} {exc.reason}"
        if exc.code in (401, 403):
            if not send_key:
                return ""  # the key was withheld on purpose; the launch-time check sends it
            return f"{s['url']} refused the key in ${s['key_env']} ({exc.code})"
        return ""  # some endpoints don't list models; the first call will say what's wrong
    except (urllib.error.URLError, OSError, ValueError) as exc:
        exc = getattr(exc, "reason", exc)
        if runner == "ollama":
            return (f"Ollama is not reachable at {s['url']} ({exc}) — install it from https://ollama.com, "
                    f"start it, and run `ollama pull {s['model']}`")
        return f"{s['url']} is not reachable ({exc})"
    names = _model_ids(listed)
    if runner == "ollama" and s["model"] not in names and f"{s['model']}:latest" not in names:
        return f"Ollama has no model {s['model']!r} — run `ollama pull {s['model']}`"
    return ""


def _model_ids(listed) -> set[str]:
    data = listed.get("data") if isinstance(listed, dict) else None
    return {m["id"] for m in data or [] if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"]}


def models(runner: str, timeout: float = 2) -> list[str]:
    """Model names the endpoint offers, [] when the list is unavailable — the UI picks from these.
    Sends the key only to an endpoint from the user's environment (see check); elsewhere an endpoint that
    lists models only for a key just offers its default."""
    s = settings(runner)
    if not s["url"]:
        return []
    try:
        listed = _request(f"{s['url']}/models", s["key"] if s["user_url"] else "", None, timeout)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return []  # some endpoints don't list; check() says the rest at launch time
    return sorted(_model_ids(listed))


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

    def inside(self, path: Path) -> bool:
        """The real location (symlinks followed) is the repository or an allowed directory."""
        try:
            real = path.resolve()
        except (OSError, RuntimeError):  # a symlink loop
            return False
        return any(real == root or root in real.parents for root in self.roots)

    def path(self, raw: str) -> Path:
        try:
            path = (self.cwd / Path(raw or ".").expanduser()).resolve()
        except RuntimeError as exc:  # Python 3.10 raises RuntimeError on a symlink loop; 3.11+ an OSError
            raise ValueError(f"{raw}: {exc}") from None
        if not self.inside(path):
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
        except (OSError, ValueError, RuntimeError, re.error) as exc:
            return f"error: {exc}"

    def _files(self, base: Path):
        """Every file under base whose real location is still inside the workspace: a symlink in the repo
        pointing at ~/.ssh must not let list_files or search read past the boundary read_file enforces."""
        for root, dirs, files in os.walk(base):  # os.walk does not descend into symlinked directories
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                path = Path(root) / name
                if self.inside(path):
                    yield path

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
        timeout = min(COMMAND_TIMEOUT, self.deadline - time.monotonic())
        if timeout < 1:
            return "error: the pass is out of time — no command was run; finish and reply"
        proc = subprocess.Popen(command, shell=True, cwd=self.cwd,  # noqa: S602 — the tool *is* a shell
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf8", errors="replace", start_new_session=True)
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


def _shrink(text: str, cap: int) -> str:
    """Head+tail slice with a stable marker; re-running it with the same cap is a no-op,
    so per-turn trimming never eats into content it already decided to keep."""
    if len(text) <= cap:
        return text
    return f"{text[:cap * 3 // 4]}\n[…older tool output, shrunk to {cap} chars…]\n{text[-cap // 4:]}"


def _trim(messages: list[dict]) -> None:
    """Age the tool results: the newest stay whole, older ones decay tier by tier, so each model
    turn re-prefills less. The prompt and recent results stay intact; MAX_CONTEXT_CHARS stays
    as the final backstop."""
    tools = [m for m in messages if m.get("role") == "tool"]
    for age, message in enumerate(reversed(tools)):  # age 0 = newest
        if age >= FRESH_RESULTS:
            message["content"] = _shrink(message["content"] or "",
                                         RECENT_CHARS if age < FRESH_RESULTS + RECENT_RESULTS else STALE_CHARS)
    total = sum(len(m.get("content") or "") for m in messages)
    for message in messages:
        if total <= MAX_CONTEXT_CHARS:
            return
        content = message.get("content") or ""
        if message.get("role") == "tool" and len(content) > STALE_CHARS:
            message["content"] = _shrink(content, STALE_CHARS)
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
            body = redact(exc.read().decode("utf8", errors="replace")[:500], s["key"])
            if exc.code in REDIRECTS:
                raise RuntimeError(f"{label}: {s['url']} {exc.reason}") from exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt == HTTP_RETRIES:
                raise RuntimeError(f"{label}: {s['url']} answered {exc.code}: {body}") from exc
            try:
                delay = float(exc.headers.get("Retry-After") or 0)
            except ValueError:
                delay = 0
            delay = min(max(delay, 5 * 2 ** attempt), 600)
            if delay >= deadline - time.monotonic():
                raise TimeoutError(f"{label} ran out of its time budget: {s['url']} answered {exc.code} and "
                                   f"asked to wait {delay:.0f}s") from exc
            print(f"[{label}] {exc.code} from the model endpoint — retrying in {delay:.0f}s", flush=True)
            runlog.log("limit", label=label, sleep_min=round(delay / 60, 1))
            time.sleep(delay)
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc):
                raise TimeoutError(f"{label} ran out of its time budget waiting for the model") from exc
            raise RuntimeError(f"{label}: cannot reach {s['url']} ({exc})") from exc
        except ValueError as exc:  # a reply that is not JSON (a proxy's HTML error page), or a refused plain-http key
            raise RuntimeError(f"{label}: unusable answer from {s['url']} ({redact(str(exc), s['key'])})") from exc
    raise RuntimeError(f"{label}: no answer from {s['url']}")


def run_model(runner: str, prompt: str, cwd: str, label: str, *, write: bool = False, timeout: float = 1800,
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
                raise RuntimeError(f"{label}: unexpected reply from {s['url']}: "
                                   f"{redact(json.dumps(reply)[:300], s['key'])}")
            content = message.get("content") or ""
            calls = message.get("tool_calls") or _text_calls(content)
            if content and not calls:
                print(f"[{label}] {content.strip()[:2000]}", flush=True)
            if not calls:
                return content.strip()
            messages.append({"role": "assistant", "content": content if message.get("tool_calls") else "",
                             "tool_calls": calls})  # type: ignore[dict-item]
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
