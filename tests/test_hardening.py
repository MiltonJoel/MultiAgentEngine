"""Regression tests for the production-hardening pass: confinement, credentials, fail-closed parsing,
the dashboard's request guards, checkpoint integrity, signals, and the in-process CLI paths."""
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from engine import __version__, cli, git, llm, main, prompts, runlog, ui
from engine.nodes.reviewer import _parse_problems
from tests.test_llm import FakeModel

ROOT = Path(__file__).resolve().parent.parent


# --- the built-in agent's workspace -----------------------------------------------------------------------

def test_search_and_list_do_not_follow_symlinks_out_of_the_workspace(tmp_path):
    repo, outside = tmp_path / "repo", tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP-SECRET-VALUE\n")
    (repo / "ok.py").write_text("TOP-SECRET-VALUE is not here, this is fine\n")
    (repo / "leak.txt").symlink_to(outside / "secret.txt")
    (repo / "linked_dir").symlink_to(outside, target_is_directory=True)
    ws = llm.Workspace(str(repo), [], write=False, deadline=time.monotonic() + 60)
    hits = ws.run("search", {"pattern": "TOP-SECRET", "path": "."})
    assert "ok.py" in hits and "leak.txt" not in hits
    assert "leak.txt" not in ws.run("list_files", {"path": "."})
    assert ws.run("read_file", {"path": "leak.txt"}).startswith("error:")
    assert ws.run("read_file", {"path": "linked_dir/secret.txt"}).startswith("error:")


def test_a_command_is_refused_once_the_pass_is_out_of_time(tmp_path):
    ws = llm.Workspace(str(tmp_path), [], write=True, deadline=time.monotonic() - 1)
    assert "out of time" in ws.run("run_command", {"command": "touch ran"})
    assert not (tmp_path / "ran").exists()


# --- credentials ------------------------------------------------------------------------------------------

@pytest.fixture
def reset_llm():
    yield
    llm.configure(None)


def test_a_repo_config_cannot_aim_an_ambient_credential_at_an_endpoint(reset_llm, monkeypatch):
    monkeypatch.delenv(llm.ALLOWED_KEY_ENVS_ENV, raising=False)
    for name in ("GITHUB_TOKEN", "aws_secret_access_key", "PATH", "HF_TOKEN", "STRIPE_SECRET_KEY", "HEROKU_API_KEY",
                 "SLACK_BOT_TOKEN", "VAULT_TOKEN"):  # default-deny: no list of every secret is ever complete
        with pytest.raises(SystemExit, match="not a known model API key"):
            llm.configure({"api": {"url": "https://evil.example/v1", "key_env": name}})
    llm.configure({"api": {"key_env": "MAE_WORK_KEY"}})  # any MAE_* name
    monkeypatch.setenv(llm.ALLOWED_KEY_ENVS_ENV, "HF_TOKEN, OTHER")  # the user, not the repo, can widen it
    llm.configure({"api": {"key_env": "HF_TOKEN"}})
    with pytest.raises(SystemExit, match="not an environment variable name"):
        llm.configure({"api": {"key_env": "A B"}})
    with pytest.raises(SystemExit, match="http"):
        llm.configure({"api": {"url": "file:///etc/passwd"}})
    llm.configure({"api": {"url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY"}})  # documented


def test_a_key_never_travels_over_plain_http_to_another_host(reset_llm, monkeypatch):
    monkeypatch.setenv("MAE_API_KEY", "sk-test")
    llm.configure({"api": {"url": "http://example.com/v1", "model": "m"}})
    assert "https" in llm.check("api")
    with pytest.raises(ValueError, match="plain http"):
        llm._request("http://example.com/v1/models", "sk-test", None, 1)
    assert not llm.insecure_key_url("http://127.0.0.1:11434/v1")
    assert not llm.insecure_key_url("https://example.com/v1")


def test_the_dashboard_probe_withholds_the_key_from_a_repo_chosen_endpoint(reset_llm, monkeypatch):
    fake = FakeModel([], models=("m",))
    try:
        monkeypatch.setenv("MAE_SOME_KEY", "sk-should-not-leak")
        llm.configure({"api": {"url": fake.url, "model": "m", "key_env": "MAE_SOME_KEY"}})
        seen = []
        real = llm._request
        monkeypatch.setattr(llm, "_request", lambda url, key, *a: seen.append(key) or real(url, key, *a))
        assert llm.check("api", send_key=False) == ""
        assert llm.models("api") == ["m"]
        assert seen == ["", ""]  # neither the probe nor the model list sent the key
        assert llm.check("api") == ""  # an explicit launch does
        assert seen[-1] == "sk-should-not-leak"
    finally:
        fake.close()


def test_the_key_goes_to_an_endpoint_the_user_set_in_their_own_environment(reset_llm, monkeypatch):
    fake = FakeModel([], models=("m",))
    try:
        monkeypatch.setenv("MAE_API_URL", fake.url)
        monkeypatch.setenv("MAE_API_KEY", "sk-mine")
        seen = []
        real = llm._request
        monkeypatch.setattr(llm, "_request", lambda url, key, *a: seen.append(key) or real(url, key, *a))
        assert llm.models("api") == ["m"]
        assert seen == ["sk-mine"]
    finally:
        fake.close()


def test_a_config_restating_the_users_own_url_keeps_it_trusted(reset_llm, monkeypatch):
    monkeypatch.setenv("MAE_API_URL", "https://api.example/v1")
    llm.configure({"api": {"url": "https://api.example/v1/"}})
    assert llm.settings("api")["user_url"]
    llm.configure({"api": {"url": "https://other.example/v1"}})
    assert not llm.settings("api")["user_url"]


def test_a_redirect_never_carries_the_key_anywhere(reset_llm):
    """urllib forwards Authorization through redirects, past every check made on the configured URL."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    target = f"http://127.0.0.1:{sink.server_port}/stolen"

    class Redirect(Sink):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

    source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    for srv in (sink, source):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(llm.urllib.error.HTTPError, match="redirected to"):
            llm._request(f"http://127.0.0.1:{source.server_port}/v1/models", "sk-secret", None, 5)
        assert seen == []
    finally:
        for srv in (sink, source):
            srv.shutdown()
            srv.server_close()


def test_an_endpoint_that_echoes_the_key_never_gets_it_into_the_run_log(tmp_path, reset_llm):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Echo(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.dumps({"error": f"bad {self.headers.get('Authorization')}"}).encode()
            self.send_response(400)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    path = runlog.start(tmp_path)
    try:
        key = "sk-echoed-secret-123"
        runlog.secret(key)
        s = {"url": f"http://127.0.0.1:{srv.server_port}/v1", "key": key, "model": "m"}
        with pytest.raises(RuntimeError) as exc:
            llm._chat(s, [], [], time.monotonic() + 30, "coder")
        assert key not in str(exc.value) and "[redacted]" in str(exc.value)
        runlog.log("aborted", error=f"raw {key}")  # and the log boundary scrubs whatever slips through
    finally:
        runlog.close()
        srv.shutdown()
        srv.server_close()
    assert key not in path.read_text()


def test_a_symlink_loop_is_a_tool_error_not_a_dead_pass(tmp_path):
    (tmp_path / "loop").symlink_to(tmp_path / "loop")
    ws = llm.Workspace(str(tmp_path), [], write=True, deadline=time.monotonic() + 60)
    assert ws.run("read_file", {"path": "loop"}).startswith("error:")
    assert ws.run("write_file", {"path": "loop", "content": "x"}).startswith("error:")


def test_a_429_that_asks_to_wait_past_the_budget_fails_fast(monkeypatch):
    fake = FakeModel([{"content": "never"}], fail_first=10)
    try:
        monkeypatch.setattr(llm.time, "sleep", lambda s: pytest.fail("slept past the deadline"))
        s = {"url": fake.url, "key": "", "model": "m"}
        with pytest.raises(TimeoutError, match="time budget"):
            llm._chat(s, [], [], time.monotonic() + 2, "coder")
    finally:
        fake.close()


def test_a_non_json_answer_is_a_clear_runtime_error(monkeypatch):
    monkeypatch.setattr(llm, "_request", lambda *a: (_ for _ in ()).throw(ValueError("Expecting value")))
    with pytest.raises(RuntimeError, match="unusable answer"):
        llm._chat({"url": "http://127.0.0.1:1/v1", "key": "", "model": "m"}, [], [], time.monotonic() + 30, "x")


# --- fail-closed audit parsing ----------------------------------------------------------------------------

def test_an_empty_example_before_real_findings_is_not_a_clean_audit():
    reply = ('The format is {"problems": []}. Here are mine:\n'
             '{"problems": [{"unit": "Login", "file": "a.py", "line": 3, "description": "no hashing", '
             '"suggested_fix": "bcrypt"}]}')
    problems = _parse_problems(reply)
    assert [p["description"] for p in problems] == ["no hashing"]


def test_a_truncated_findings_object_is_never_a_clean_audit():
    for reply in ('{"problems": []}\n{"problems": [{"description": "real finding"}',
                  '{"problems": [{"description": "real finding"}]\n{"problems": []}',  # cut off: last ] missing
                  '{"problems": [{"description": "real"'):
        problems = _parse_problems(reply)
        assert problems and problems[0]["description"].startswith("unreadable reply"), reply


def test_findings_nested_under_another_key_still_count():
    reply = json.dumps({"problems": [], "notes": {"problems": [{"description": "hidden"}]}})
    assert [p["description"] for p in _parse_problems(reply)] == ["hidden"]
    bad = json.dumps({"problems": [], "notes": {"problems": "none"}})
    assert _parse_problems(bad)[0]["description"].startswith("unreadable reply")


def test_duplicate_findings_merge_and_a_malformed_object_fails_closed():
    finding = {"unit": "", "file": "a.py", "line": 1, "description": "d", "suggested_fix": ""}
    twice = json.dumps({"problems": [finding]}) + "\n" + json.dumps({"problems": [finding]})
    assert _parse_problems(twice) == [finding]
    mixed = json.dumps({"problems": []}) + json.dumps({"problems": "none"})
    assert _parse_problems(mixed)[0]["description"].startswith("unreadable reply")
    assert _parse_problems('{"problems": []}') == []


# --- the dashboard's request guards -----------------------------------------------------------------------

@pytest.fixture
def server():
    srv = ui.serve(0, "secret-token")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_port
    srv.shutdown()
    srv.server_close()


def _raw(port, method, path, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers={"X-MAE-Token": "secret-token", **(headers or {})})
    res = conn.getresponse()
    out = res.status, dict(res.getheaders()), res.read()
    conn.close()
    return out


def test_a_rebound_host_or_foreign_origin_is_refused(server):
    assert _raw(server, "GET", "/api/output", {"Host": f"evil.example:{server}"})[0] == 403
    assert _raw(server, "GET", "/", {"Host": "evil.example"})[0] == 403
    assert _raw(server, "POST", "/api/stop", {"Origin": "http://evil.example"}, b"{}")[0] == 403
    assert _raw(server, "GET", "/api/output", {"Host": f"localhost:{server}"})[0] == 200
    assert _raw(server, "POST", "/api/stop", {"Origin": f"http://127.0.0.1:{server}"}, b"{}")[0] == 200


def test_port_80_accepts_the_portless_host_browsers_send():
    class Fake:
        server_port = 80

    handler = ui.Handler.__new__(ui.Handler)
    handler.server = Fake()  # type: ignore[assignment]
    for host, ok in (("127.0.0.1", True), ("localhost", True), ("127.0.0.1:80", True), ("evil.example", False)):
        handler.headers = {"Host": host}  # type: ignore[assignment]
        assert handler._same_host() is ok, host


def test_the_page_ships_security_headers(server):
    code, headers, body = _raw(server, "GET", "/")
    assert code == 200 and b"MultiAgentEngine" in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer" and headers["X-Frame-Options"] == "DENY"


def test_an_oversized_or_bogus_body_is_refused_before_it_is_read(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    conn.putrequest("POST", "/api/doc")
    conn.putheader("X-MAE-Token", "secret-token")
    conn.putheader("Content-Length", str(ui.MAX_BODY + 1))
    conn.endheaders()  # no body follows: a server that tried to read it would hang until the timeout
    res = conn.getresponse()
    assert res.status == 400 and b"between 0 and" in res.read()
    conn.close()
    assert _raw(server, "POST", "/api/doc", {"Content-Length": "-5"})[0] == 400


def test_stop_never_signals_a_run_started_while_it_was_checking():
    """Old race: stop() checked self.proc, a new run replaced it, then stop() signalled the replacement."""
    run = ui.Run()
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    second = subprocess.Popen(sleeper, start_new_session=True)

    class First(subprocess.Popen):
        def poll(self):  # a concurrent /api/run lands right while stop() is checking liveness
            alive = super().poll()
            run.proc = second
            return alive

    first = First(sleeper, start_new_session=True)
    try:
        run.proc = first
        assert run.stop() is True
        first.wait(timeout=10)
        assert first.returncode == -signal.SIGINT
        time.sleep(0.3)
        assert second.poll() is None, "stop() signalled the run that replaced the one it checked"
    finally:
        for proc in (first, second):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert ui.Run().stop() is False


# --- checkpoints ------------------------------------------------------------------------------------------

def _git_repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    return path


def test_a_failed_git_add_never_yields_a_checkpoint(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "a.py").write_text("x\n")
    real = git._git

    def broken(repo_, *args, **kw):
        return (128, "fatal: unable to index file") if "add" in args[:3] else real(repo_, *args, **kw)

    monkeypatch.setattr(git, "_git", broken)
    assert git.checkpoint(str(repo), "m") is None
    with pytest.raises(git.CheckpointError, match="git add"):
        git.checkpoint(str(repo), "m", strict=True)
    assert real(str(repo), "rev-parse", "HEAD")[0] != 0  # nothing was committed


def test_a_signal_during_a_checkpoint_waits_for_the_commit_instead_of_leaving_index_lock(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "a.py").write_text("x\n")

    class Stop(Exception):
        pass

    def handler(signum, frame):
        raise Stop()

    previous = signal.signal(signal.SIGTERM, handler)
    real = git._git
    delivered_early = []

    def commit_then_signal(repo_, *args, **kw):
        if "commit" in args:
            os.kill(os.getpid(), signal.SIGTERM)  # docker stop, right as the commit runs
            delivered_early.append(False)
        return real(repo_, *args, **kw)

    monkeypatch.setattr(git, "_git", commit_then_signal)
    try:
        with pytest.raises(Stop):  # still delivered — just after the commit, not in the middle of it
            git.checkpoint(str(repo), "m", strict=True)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert delivered_early == [False]
    assert real(str(repo), "log", "--oneline")[1].endswith("m")
    assert not (repo / ".git" / "index.lock").exists()
    assert git.busy(str(repo)) is None


def test_gitignored_run_logs_do_not_fail_the_checkpoint(tmp_path):
    """README tells users to gitignore .mae/runs/; git then exits 1 on the exclude pathspec — not a failure."""
    repo = _git_repo(tmp_path)
    (repo / ".gitignore").write_text(".mae/runs/\n")
    (repo / ".mae" / "runs").mkdir(parents=True)
    (repo / ".mae" / "runs" / "x.jsonl").write_text("{}\n")
    (repo / "a.py").write_text("x\n")
    assert git.checkpoint(str(repo), "m", strict=True)
    assert git._only_ignored_warning(f"{git.IGNORED_WARNING}\n.mae/runs\nhint: use -f")
    assert not git._only_ignored_warning(f"{git.IGNORED_WARNING}\n.mae/runs\nerror: open(x): Permission denied")


# --- run logs, prompts, the plan lock ---------------------------------------------------------------------

def test_two_runs_in_the_same_second_get_separate_logs(tmp_path, monkeypatch):
    class Frozen:
        @staticmethod
        def now(tz=None):
            from datetime import datetime
            return datetime(2026, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(runlog, "datetime", Frozen)
    paths = []
    for n in range(3):
        paths.append(runlog.start(tmp_path))
        runlog.log("start", run=n)
    runlog.close()
    assert len(set(paths)) == 3
    for n, path in enumerate(paths):  # each log holds its own run only
        assert [json.loads(line)["run"] for line in path.read_text().splitlines()] == [n]


def test_a_non_utf8_prompt_override_fails_with_a_message(tmp_path):
    (tmp_path / ".mae" / "prompts").mkdir(parents=True)
    (tmp_path / ".mae" / "prompts" / "coder.md").write_bytes(b"caf\xe9 {spec}")
    with pytest.raises(SystemExit, match="not valid UTF-8"):
        prompts.check(tmp_path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write a read-only file")
def test_a_read_only_plan_fails_with_a_message(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("x")
    plan.chmod(0o444)
    try:
        with pytest.raises(SystemExit, match="not writable"), main._plan_lock(plan):
            pass
    finally:
        plan.chmod(0o644)


# --- the CLI runners, against stub executables ------------------------------------------------------------

def _stub(bin_dir: Path, name: str, script: str) -> None:
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n{script}")
    path.chmod(0o755)


CLAUDE = r'''
import json, re, sys
prompt = sys.stdin.read()
plan = ".mae/plan.md"
text = open(plan).read()
open(plan, "w").write(re.sub(r": TODO", ": DONE", text))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"},
                                                              {"type": "tool_use", "name": "Edit"}]}}))
print(json.dumps({"type": "result", "result": '{"problems": []}', "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 10, "output_tokens": 5}}))
'''

CODEX = r'''
import sys
sys.stdin.read()
out = sys.argv[sys.argv.index("-o") + 1]
open(out, "w").write('{"problems": []}')
print("codex: done")
'''


def test_run_claude_streams_progress_and_returns_the_final_answer(tmp_path, monkeypatch, capsys):
    _stub(tmp_path / "bin", "claude", "import sys, json\nsys.stdin.read()\n"
          "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'hi'}]}}))\n"
          "print(json.dumps({'type': 'result', 'result': 'final answer', 'total_cost_usd': 0.5,"
          " 'usage': {'input_tokens': 1}}))\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    runlog.start(tmp_path / "runs")
    try:
        out = cli.run_claude("p", str(tmp_path), label="coder", write=True, add_dirs=[str(tmp_path)],
                             schema={"type": "object"}, model="haiku")
    finally:
        runlog.close()
    assert out == "final answer"
    assert "cost=$0.5000" in capsys.readouterr().out
    assert runlog.totals()["cost_usd"] == 0.5


def test_run_claude_without_a_result_event_fails(tmp_path, monkeypatch):
    _stub(tmp_path / "bin", "claude", "import sys\nsys.stdin.read()\nprint('crashed')\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    with pytest.raises(RuntimeError, match="no result event"):
        cli.run_claude("p", str(tmp_path), label="coder")


def test_run_codex_reads_the_last_message_file_and_cleans_up(tmp_path, monkeypatch):
    _stub(tmp_path / "bin", "codex", CODEX)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    made = []
    real = cli.tempfile.mkstemp
    monkeypatch.setattr(cli.tempfile, "mkstemp", lambda **kw: made.append(real(**kw)) or made[-1])
    out = cli.run_codex("p", str(tmp_path), label="auditor", schema={"type": "object"}, model="high",
                        add_dirs=["/x"])
    assert out == '{"problems": []}'
    assert len(made) == 2 and not any(Path(path).exists() for _, path in made)  # last-message + schema files


def test_a_cli_timeout_kills_the_children_it_left_behind(tmp_path):
    marker = tmp_path / "child-alive"
    child = f"import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('x')"
    script = (f"import subprocess, sys, time\nsys.stdin.read()\n"
              f"subprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)\n")
    with pytest.raises(TimeoutError):
        cli._run_once([sys.executable, "-c", script], "p", str(tmp_path), 1, "coder", None)
    time.sleep(4)
    assert not marker.exists()


def test_a_timeout_reaches_children_after_the_cli_itself_exited(tmp_path):
    """The CLI exits at once while its child keeps stdout open: the timeout must still kill the child."""
    pid_file = tmp_path / "child.pid"
    child = f"import os, time, pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    script = f"import subprocess, sys\nsys.stdin.read()\nsubprocess.Popen([sys.executable, '-c', {child!r}])\n"
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        cli._run_once([sys.executable, "-c", script], "p", str(tmp_path), 2, "coder", None)
    assert time.monotonic() - started < 20
    assert _gone(int(pid_file.read_text())), "the child outlived the timeout"


def _gone(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:  # our own zombie child: reap it, or kill(pid, 0) keeps succeeding
            if os.waitpid(pid, os.WNOHANG)[0]:
                return True
        except ChildProcessError:
            pass
        time.sleep(0.1)
    return False


def test_children_a_cli_leaves_behind_are_killed_when_the_pass_ends(tmp_path):
    """A dev server or watcher the agent started (pipes detached) must not keep running — and editing — after."""
    pid_file = tmp_path / "child.pid"
    child = f"import os, time, pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    script = (f"import subprocess, sys, time, pathlib\nsys.stdin.read()\n"
              f"subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, "
              f"stderr=subprocess.DEVNULL)\n"
              f"while not pathlib.Path({str(pid_file)!r}).exists(): time.sleep(0.05)\nprint('done')\n")
    lines, code = cli._run_once([sys.executable, "-c", script], "p", str(tmp_path), 30, "coder", None)
    assert code == 0 and "done" in "".join(lines)
    assert _gone(int(pid_file.read_text())), "the child outlived the pass"


def test_a_missing_cli_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="not installed"):
        cli._run_once(["definitely-not-a-cli-xyz"], "p", str(tmp_path), 5, "coder", None)


def test_retry_delay_reads_reset_times():
    assert cli._retry_delay("boom") == 0
    assert cli._retry_delay("usage limit reached|2000000000", now=1999999000) == pytest.approx(1030)
    assert cli._retry_delay("rate limit, resets 2030-01-01 00:00 UTC", now=0) > 0
    assert cli._retry_delay("overloaded") == cli.DEFAULT_WAIT


# --- the whole engine, in process -------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".mae").mkdir()
    (repo / ".mae" / "spec.md").write_text("spec")
    (repo / ".mae" / "plan.md").write_text("<!-- ENGINE-STATUS\nLogin: TODO\n-->\n")
    (repo / ".gitignore").write_text(".mae/runs/\n")
    _stub(tmp_path / "bin", "claude", CLAUDE)
    _stub(tmp_path / "bin", "codex", CODEX)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    monkeypatch.chdir(repo)
    previous = signal.getsignal(signal.SIGTERM)
    yield repo
    signal.signal(signal.SIGTERM, previous)
    runlog.close()


def _main(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["mae", *argv])
    with pytest.raises(SystemExit) as exc:
        main.main()
    return exc.value.code


def test_a_full_run_in_process_with_claude_coding_and_codex_auditing(project, monkeypatch, capsys):
    assert _main(monkeypatch, "--repo", str(project), "--gate", "true") == main.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "audit_clean=True" in out and "Review with: git" in out
    assert "Login: DONE" in (project / ".mae" / "plan.md").read_text()
    log = next((project / ".mae" / "runs").glob("*.jsonl")).read_text()
    assert '"event": "finish"' in log and '"exit_code": 0' in log


def test_a_full_run_without_git(project, monkeypatch, capsys):
    assert _main(monkeypatch, "--repo", str(project), "--gate", "true", "--no-git") == main.EXIT_CLEAN
    assert "checkpoints disabled" in capsys.readouterr().out


def test_startup_refusals_name_the_problem(project, monkeypatch, capsys):
    assert _main(monkeypatch, "--repo", str(project / "nope")) == 2
    assert _main(monkeypatch, "--repo", str(project), "--spec", ".mae/plan.md") == 2
    assert _main(monkeypatch, "--repo", str(project), "--agent", "coder") == 2
    assert _main(monkeypatch, "--repo", str(project), "--allow-dir", str(project / "missing")) == 2
    assert _main(monkeypatch, "--repo", str(project), "--gate", "'unterminated") == 2
    (project / ".mae" / "plan.md").write_text("<!-- ENGINE-STATUS\nLogin: DONE\n-->\n")
    assert _main(monkeypatch, "--repo", str(project), "--gate", "true") == 2
    err = capsys.readouterr().err
    assert "not a directory" in err and "different files" in err and "nothing left open" in err


def test_an_agent_failure_aborts_with_exit_1_and_logs_it(project, monkeypatch, tmp_path):
    _stub(tmp_path / "bin", "claude", "import sys\nsys.stdin.read()\nsys.exit(3)\n")
    assert _main(monkeypatch, "--repo", str(project), "--gate", "true") == main.EXIT_FAILED
    log = next((project / ".mae" / "runs").glob("*.jsonl")).read_text()
    assert '"event": "aborted"' in log and "exit 3" in log


def test_sigterm_aborts_cleanly_and_kills_the_agent(tmp_path):
    """`docker stop` / systemd / a cancelled CI job: exit 143, an `aborted` event, and no orphaned agent."""
    import queue
    repo = _git_repo(tmp_path / "repo")
    (repo / ".mae").mkdir()
    (repo / ".mae" / "spec.md").write_text("spec")
    (repo / ".mae" / "plan.md").write_text("<!-- ENGINE-STATUS\nLogin: TODO\n-->\n")
    pid_file = tmp_path / "agent.pid"
    _stub(tmp_path / "bin", "claude", f"import os, sys, time, pathlib\nsys.stdin.read()\n"
                                      f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                                      f"print('started', flush=True)\ntime.sleep(60)\n")
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-u", "-m", "engine", "--repo", str(repo), "--gate", "true",
                             "--agent", "reviewer=claude", "--agent", "auditor=claude"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout or ()], daemon=True).start()
    seen = []
    try:
        deadline = time.monotonic() + 60
        while not any("started" in line for line in seen):
            assert time.monotonic() < deadline, "the agent never started:\n" + "".join(seen)
            try:
                seen.append(lines.get(timeout=1))
            except queue.Empty:
                assert proc.poll() is None, "the engine exited early:\n" + "".join(seen)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == main.EXIT_TERMINATED, "".join(seen)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert _gone(int(pid_file.read_text())), "the agent outlived the engine"
    assert '"event": "aborted"' in next((repo / ".mae" / "runs").glob("*.jsonl")).read_text()


def test_version_flags():
    for module in ("engine", "engine.ui"):
        out = subprocess.run([sys.executable, "-m", module, "--version"], capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": str(ROOT)}, check=True, timeout=30).stdout
        assert out.strip().endswith(__version__)
