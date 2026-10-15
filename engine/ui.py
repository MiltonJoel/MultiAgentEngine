"""`mae-ui`: a local web dashboard over the engine — edit spec/plan, launch and stop runs, watch them live,
browse past runs and their checkpoint commits. Stdlib only; binds 127.0.0.1 and requires a per-launch token."""
import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from engine import git
from engine.cli import DEFAULT_AGENTS, RUNNERS
from engine.graph import MAX_ITERATIONS, STATUS_BLOCK, STATUS_LINE, plan_unparsed
from engine.main import CONFIG, DEFAULT_PLAN, DEFAULT_SPEC, MAE_DIR, NEW_PLAN
from engine.nodes.tester import DEFAULT_GATES

PAGE = Path(__file__).resolve().parent / "ui.html"
STATES = ("TODO", "IN_PROGRESS", "DONE", "BLOCKED")
MAX_LINES = 20000  # ponytail: in-memory console buffer; older lines drop, the JSONL log keeps the record
NEW_SPEC = "# Spec\nDescribe what the system must be: requirements, interfaces, invariants.\n"
SHA = re.compile(r"^[0-9a-f]{4,40}$")


class Run:
    """The one engine subprocess this server drives, plus its console output."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.repo = None
        self.lines: list[str] = []
        self.dropped = 0

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, repo: Path, args: list[str]) -> None:
        with self.lock:
            if self.running():
                raise ValueError("a run is already in progress — stop it first")
            self.lines, self.dropped, self.repo = [], 0, str(repo)
            cmd = [sys.executable, "-u", "-m", "engine.main", "--repo", str(repo), *args]
            self.lines.append("[ui] $ mae " + " ".join(cmd[4:]))
            self.proc = subprocess.Popen(
                cmd, cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf8", errors="replace", bufsize=1, start_new_session=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
            )
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            with self.lock:
                self.lines.append(line.rstrip("\n"))
                if len(self.lines) > MAX_LINES:
                    del self.lines[:1000]
                    self.dropped += 1000
        proc.wait()
        with self.lock:
            if proc is self.proc:
                self.lines.append(f"[ui] engine exited with code {proc.returncode}")

    def stop(self) -> bool:
        """SIGINT, like Ctrl-C: the engine kills its agent, logs `aborted` and keeps its checkpoints."""
        if not self.running():
            return False
        # the engine alone, not its process group: a git commit or gate caught mid-write by the signal
        # would leave index.lock behind; the engine stops its own children on KeyboardInterrupt
        os.kill(self.proc.pid, signal.SIGINT)
        return True

    def kill(self) -> None:
        if self.running():
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except OSError:
                self.proc.kill()

    def output(self, since: int) -> dict:
        with self.lock:
            start = max(since - self.dropped, 0)
            return {"lines": self.lines[start:], "next": self.dropped + len(self.lines),
                    "running": self.running(), "repo": self.repo,
                    "exit_code": None if self.proc is None or self.running() else self.proc.returncode}


RUN = Run()


def _repo(raw: str | None) -> Path:
    if not raw:
        raise ValueError("repo is required")
    repo = Path(raw).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"not a directory: {repo}")
    return repo


def _config(repo: Path) -> dict:
    path = repo / CONFIG
    if not path.is_file():
        return {}
    config = json.loads(path.read_text(encoding="utf8"))
    if not isinstance(config, dict):
        raise ValueError(f"{path} must hold a JSON object")  # noqa: TRY004 — the API maps ValueError to a 400
    return config


def _docs(repo: Path) -> dict[str, Path]:
    config = _config(repo)
    return {"spec": (repo / (config.get("spec") or f"{MAE_DIR}/{DEFAULT_SPEC}")).resolve(),
            "plan": (repo / (config.get("plan") or f"{MAE_DIR}/{DEFAULT_PLAN}")).resolve(),
            "config": repo / CONFIG}


def units(text: str) -> list[dict]:
    """Status-block lines as {name, state, note}."""
    block = STATUS_BLOCK.search(text)
    if not block:
        return []
    return [{"name": m.group("name").strip(), "state": m.group("state"), "note": m.group("note").strip(" —-:")}
            for m in STATUS_LINE.finditer(block.group("body"))]


def edit_unit(text: str, name: str, state: str | None, note: str = "") -> str:
    """Set a unit's state (keeping its note), add it if missing, or delete it when state is None."""
    if state is not None and state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    name = " ".join(name.split()).replace("-->", "→")
    parsed = STATUS_LINE.match(f"{name}: {state or 'TODO'}")
    if not name or not parsed or parsed.group("name").strip() != name:
        raise ValueError(f"unusable unit name {name!r}")
    block = STATUS_BLOCK.search(text)
    if not block:
        if state is None:
            return text
        return f"<!-- ENGINE-STATUS\n{name}: {state}\n-->\n\n" + text
    lines, found = [], False
    for line in block.group("body").split("\n"):
        m = STATUS_LINE.match(line)
        if m and m.group("name").strip() == name:
            found = True
            if state is None:
                continue
            old_note = m.group("note").strip()
            line = f"{name}: {state}" + (f" {note or old_note}" if (note or old_note) else "")
        lines.append(line)
    if not found and state is not None:
        # insert before the trailing blank line that precedes "-->"
        tail = len(lines) - 1 if lines and not lines[-1].strip() else len(lines)
        lines.insert(tail, f"{name}: {state}" + (f" {note}" if note else ""))
    start, end = block.span("body")
    return text[:start] + "\n".join(lines) + text[end:]


def state(repo: Path) -> dict:
    docs = _docs(repo)
    read = {k: p.read_text(encoding="utf8") if p.is_file() else None for k, p in docs.items()}
    is_repo = git.is_repo(str(repo))
    return {
        "repo": str(repo),
        "paths": {k: str(p) for k, p in docs.items()},
        "docs": read,
        "units": units(read["plan"] or ""),
        "unparsed": plan_unparsed(str(docs["plan"])) if read["plan"] is not None else [],
        "git": {"is_repo": is_repo,
                "branch": git._git(str(repo), "rev-parse", "--abbrev-ref", "HEAD")[1] if is_repo else None},
        "clis": {name: shutil.which(name) for name in RUNNERS},
        "defaults": {"agents": DEFAULT_AGENTS, "gates": [g["cmd"] for g in DEFAULT_GATES],
                     "max_iterations": MAX_ITERATIONS},
    }


def runs(repo: Path) -> list[dict]:
    """One summary per JSONL run log, newest first."""
    out = []
    for path in sorted((repo / MAE_DIR / "runs").glob("*.jsonl"), reverse=True):
        summary = {"name": path.stem, "status": "running or killed", "iterations": 0, "cost_usd": 0.0}
        for event in _events(path):
            kind = event.get("event")
            if kind == "start":
                summary.update(started=event.get("ts"), branch=event.get("branch"), base_sha=event.get("base_sha"),
                               focus=event.get("focus"), agents=event.get("agents"))
            elif kind == "agent":
                summary["cost_usd"] += event.get("cost_usd") or 0
            elif kind == "gate":
                summary["iterations"] = max(summary["iterations"], event.get("iteration", 0))
            elif kind == "finish":
                # older logs have no exit_code; there clean meant green + audited (the plan check came later)
                code = event.get("exit_code", 0 if event.get("tests_passed") and event.get("audit_clean") else 1)
                status = {0: "clean", 2: "needs review"}.get(code, "finished with issues")
                summary.update(status=status, caveats=event.get("caveats") or [], ended=event.get("ts"))
            elif kind == "aborted":
                summary.update(status="aborted", error=event.get("error"), ended=event.get("ts"))
        out.append(summary)
    return out


def _events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a half-written last line while the run is live
    return events


def run_args(body: dict) -> list[str]:
    """UI launch options -> `mae` flags; validated here so a bad value fails before anything spawns."""
    args = []
    for role, cli in (body.get("agents") or {}).items():
        if role not in DEFAULT_AGENTS or cli not in RUNNERS:
            raise ValueError(f"bad agent binding {role}={cli}")
        args += ["--agent", f"{role}={cli}"]
    gates = body.get("gates")
    if gates is not None:
        if not isinstance(gates, list) or not all(isinstance(g, str) for g in gates):
            raise ValueError("gates must be a list of command strings")
        gates = [g for g in gates if g.strip()]
        for gate in gates or [""]:
            args += ["--gate", gate]
    if body.get("max_iterations") not in (None, ""):
        n = int(body["max_iterations"])
        if n < 1:
            raise ValueError("max_iterations must be at least 1")
        args += ["--max-iterations", str(n)]
    if body.get("no_git"):
        args.append("--no-git")
    return args


class Handler(BaseHTTPRequestHandler):
    token = ""
    server_version = "mae-ui"

    def log_message(self, fmt, *args):  # keep the token-bearing URLs out of the terminal
        pass

    def _send(self, code: int, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-MAE-Token", ""), self.token)

    def _dispatch(self, method: str):
        url = urlparse(self.path)
        if method == "GET" and url.path in ("/", "/index.html"):
            return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if not url.path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(403, {"error": "missing or wrong token — open the URL `mae-ui` printed"})
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length > 0 else {}
                if not isinstance(body, dict):
                    raise ValueError("the request body must be a JSON object")
            return self._send(200, self._api(method, url.path[5:], query, body))
        except (ValueError, OSError) as exc:
            return self._send(400, {"error": str(exc)})

    def _api(self, method: str, route: str, query: dict, body: dict):
        if method == "GET" and route == "state":
            return state(_repo(query.get("repo")))
        if method == "GET" and route == "output":
            return RUN.output(int(query.get("since") or 0))
        if method == "GET" and route == "runs":
            return runs(_repo(query.get("repo")))
        if method == "GET" and route == "runlog":
            repo, name = _repo(query.get("repo")), query.get("name", "")
            path = repo / MAE_DIR / "runs" / f"{Path(name).name}.jsonl"
            if not path.is_file():
                raise ValueError(f"no run log {name}")
            return _events(path)
        if method == "GET" and route == "commits":
            repo, base, branch = _repo(query.get("repo")), query.get("base", ""), query.get("branch", "")
            if not SHA.match(base) or not branch.startswith("engine/"):
                raise ValueError("base must be a sha and branch an engine/ branch")
            code, out = git._git(str(repo), "log", "--format=%h%x09%ad%x09%s", "--date=format:%H:%M",
                                 "--shortstat", f"{base}..{branch}", "--")
            if code:
                raise ValueError(out or "git log failed")
            return {"log": out}
        if method == "GET" and route == "show":
            repo, sha = _repo(query.get("repo")), query.get("sha", "")
            if not SHA.match(sha):
                raise ValueError("bad sha")
            return {"diff": git._git(str(repo), "show", "--stat", "--patch", "--format=%H%n%an %ad%n%n%s", sha)[1]}
        if method == "POST" and route == "init":
            repo = _repo(body.get("repo"))
            docs = _docs(repo)
            for key, template in (("spec", NEW_SPEC), ("plan", NEW_PLAN)):
                if not docs[key].exists():
                    docs[key].parent.mkdir(parents=True, exist_ok=True)
                    docs[key].write_text(template, encoding="utf8")
            return state(repo)
        if method == "POST" and route == "doc":
            repo = _repo(body.get("repo"))
            which, text = body.get("which"), body.get("text")
            if which not in ("spec", "plan", "config") or not isinstance(text, str):
                raise ValueError("which must be spec, plan or config, with text")
            if which == "config" and text.strip() and not isinstance(json.loads(text), dict):
                raise ValueError("config must be a JSON object")
            path = _docs(repo)[which]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf8")
            return state(repo)
        if method == "POST" and route == "unit":
            repo = _repo(body.get("repo"))
            plan = _docs(repo)["plan"]
            if not plan.is_file():
                raise ValueError("create the plan first")
            plan.write_text(edit_unit(plan.read_text(encoding="utf8"), str(body.get("name", "")),
                                      body.get("state"), str(body.get("note") or "")), encoding="utf8")
            return state(repo)
        if method == "POST" and route == "run":
            repo = _repo(body.get("repo"))
            RUN.start(repo, run_args(body))
            return RUN.output(0)
        if method == "POST" and route == "stop":
            return {"stopped": RUN.stop()}
        raise ValueError(f"unknown endpoint {method} /api/{route}")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def serve(port: int, token: str) -> ThreadingHTTPServer:
    Handler.token = token
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(prog="mae-ui", description="Local dashboard for the MultiAgentEngine")
    parser.add_argument("--repo", default=".", help="Repository to open first (default: .)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Print the URL instead of opening it")
    args = parser.parse_args()
    token = secrets.token_urlsafe(24)
    try:
        server = serve(args.port, token)
    except OSError as exc:
        raise SystemExit(f"cannot listen on 127.0.0.1:{args.port}: {exc} — try --port")
    repo = Path(args.repo).expanduser().resolve()
    url = f"http://127.0.0.1:{server.server_port}/?token={token}#{repo}"
    print(f"MultiAgentEngine UI: {url}\n(Ctrl-C to quit; a running engine is stopped too)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if RUN.stop():
            try:
                RUN.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print("engine did not stop within 30s — killing it", flush=True)
                RUN.kill()
        server.server_close()


if __name__ == "__main__":
    main()
