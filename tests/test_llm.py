"""The free-model runners, against a scripted OpenAI-compatible server standing in for Ollama."""
import argparse
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from engine import llm
from engine.cli import FREE_AGENTS, run_agent
from engine.main import _agents
from engine.ui import run_args


class FakeModel:
    """Serves /v1/models and answers /v1/chat/completions from a script of assistant messages."""

    def __init__(self, script: list[dict], models=("qwen2.5-coder:7b",), fail_first: int = 0):
        self.script, self.requests, self.fail_first = list(script), [], fail_first
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if code == 429:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json(200, {"data": [{"id": m} for m in models]})

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"payload": payload, "auth": self.headers.get("Authorization")})
                if fake.fail_first:
                    fake.fail_first -= 1
                    return self._json(429, {"error": "rate limited"})
                message = fake.script.pop(0)
                self._json(200, {"choices": [{"message": {"role": "assistant", **message}}],
                                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def call(name: str, **args) -> dict:
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture
def ollama(monkeypatch):
    servers = []

    def start(script, **kw):
        fake = FakeModel(script, **kw)
        servers.append(fake)
        monkeypatch.setenv("MAE_OLLAMA_URL", fake.url)
        return fake

    llm.configure(None)
    yield start
    for fake in servers:
        fake.close()
    llm.configure(None)


def test_coder_edits_files_and_runs_tests(ollama):
    with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as plans:
        plan = Path(plans) / "plan.md"
        plan.write_text("Login: TODO\n")
        fake = ollama([
            {"content": "", "tool_calls": [call("write_file", path="app.py", content="def add(a, b):\n    return a + b\n")]},
            {"content": "", "tool_calls": [call("replace_in_file", path=str(plan), old="TODO", new="DONE"),
                                           call("run_command", command="python -c 'import app; print(app.add(2, 3))'")]},
            {"content": "Implemented add() and verified it."},
        ])
        out = run_agent("ollama", "implement add", cwd=repo, label="coder", write=True, add_dirs=[plans])
        assert out == "Implemented add() and verified it."
        assert (Path(repo) / "app.py").read_text().startswith("def add")
        assert plan.read_text() == "Login: DONE\n"
        tool_results = [m for m in fake.requests[-1]["payload"]["messages"] if m["role"] == "tool"]
        assert tool_results[-1]["content"].startswith("exit code 0\n5")
        assert fake.requests[0]["payload"]["model"] == "qwen2.5-coder:7b"
        assert fake.requests[0]["auth"] is None  # Ollama needs no key


def test_read_only_pass_cannot_write_or_escape(ollama):
    with tempfile.TemporaryDirectory() as repo:
        (Path(repo) / "a.py").write_text("x = 1\n")
        fake = ollama([
            {"content": "", "tool_calls": [call("write_file", path="a.py", content="hacked"),
                                           call("read_file", path="../../etc/passwd"),
                                           call("search", pattern=r"x =", path=".")]},
            {"content": '```json\n{"problems": []}\n```'},
        ])
        out = run_agent("ollama:llama3.2", "review", cwd=repo, label="reviewer", schema={"type": "object"})
        assert '"problems": []' in out
        assert (Path(repo) / "a.py").read_text() == "x = 1\n"
        results = [m["content"] for m in fake.requests[-1]["payload"]["messages"] if m["role"] == "tool"]
        assert "read-only" in results[0]
        assert "outside the repository" in results[1]
        assert results[2] == "a.py:1: x = 1"
        tools = {t["function"]["name"] for t in fake.requests[0]["payload"]["tools"]}
        assert tools == {"list_files", "read_file", "search"}
        assert fake.requests[0]["payload"]["model"] == "llama3.2"  # the per-role model wins


def test_tool_call_written_as_text_still_runs(ollama):
    with tempfile.TemporaryDirectory() as repo:
        ollama([{"content": json.dumps({"name": "write_file", "arguments": {"path": "n.txt", "content": "hi"}})},
                {"content": "done"}])
        assert run_agent("ollama", "p", cwd=repo, label="coder", write=True) == "done"
        assert (Path(repo) / "n.txt").read_text() == "hi"


def test_rate_limit_is_retried(ollama, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    with tempfile.TemporaryDirectory() as repo:
        fake = ollama([{"content": "ok"}], fail_first=2)
        assert run_agent("ollama", "p", cwd=repo, label="auditor") == "ok"
        assert len(fake.requests) == 3


def test_api_runner_sends_the_key(monkeypatch):
    fake = FakeModel([{"content": "ok"}])
    try:
        llm.configure({"api": {"url": fake.url, "model": "some/free-model:free", "key_env": "FREE_KEY"}})
        monkeypatch.setenv("FREE_KEY", "sk-test")
        assert llm.check("api") == ""
        with tempfile.TemporaryDirectory() as repo:
            assert run_agent("api", "p", cwd=repo, label="auditor") == "ok"
        assert fake.requests[0]["auth"] == "Bearer sk-test"
        monkeypatch.delenv("FREE_KEY")
        assert "FREE_KEY" in llm.check("api")
    finally:
        fake.close()
        llm.configure(None)


def test_preflight_explains_what_is_missing(ollama, monkeypatch):
    fake = ollama([], models=("llama3.2:latest",))
    assert llm.check("ollama", "llama3.2") == ""
    assert "ollama pull qwen2.5-coder:7b" in llm.check("ollama")
    fake.close()
    assert "Ollama is not reachable" in llm.check("ollama", timeout=0.5)
    with pytest.raises(SystemExit):
        llm.configure({"ollama": {"modle": "x"}})


def test_free_flag_and_model_bindings(ollama):
    ollama([])
    parser = argparse.ArgumentParser()
    assert _agents(parser, {}, [], free=True) == FREE_AGENTS
    bound = _agents(parser, {}, ["coder=ollama:qwen2.5-coder:7b"], free=True)
    assert bound["coder"] == "ollama:qwen2.5-coder:7b"
    with pytest.raises(SystemExit):  # the CLIs take no model
        _agents(parser, {}, ["coder=claude:opus"], free=True)
    assert run_args({"agents": {"coder": "ollama:qwen2.5-coder:14b", "reviewer": "ollama"}}) == [
        "--agent", "coder=ollama:qwen2.5-coder:14b", "--agent", "reviewer=ollama"]
    for bad in ("codex:gpt", "ollama:a b", "nope"):
        with pytest.raises(ValueError):
            run_args({"agents": {"coder": bad}})
