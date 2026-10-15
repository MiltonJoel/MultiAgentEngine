"""The dashboard: unit editing, the token gate, and a real engine launch through the HTTP API."""
import json
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from engine import ui


def test_units_and_edit_unit_round_trip():
    plan = "<!-- ENGINE-STATUS\nLogin: DONE\nRate limit: TODO — per user\n-->\n\n# Notes\nkeep me\n"
    assert ui.units(plan) == [{"name": "Login", "state": "DONE", "note": ""},
                              {"name": "Rate limit", "state": "TODO", "note": "per user"}]
    plan = ui.edit_unit(plan, "Rate limit", "BLOCKED")
    assert "Rate limit: BLOCKED — per user" in plan  # the note survives a state change
    plan = ui.edit_unit(plan, "Audit log", "TODO", "— new")
    assert ui.units(plan)[-1] == {"name": "Audit log", "state": "TODO", "note": "new"}
    assert plan.endswith("-->\n\n# Notes\nkeep me\n")  # prose outside the block untouched
    plan = ui.edit_unit(plan, "Login", None)
    assert [u["name"] for u in ui.units(plan)] == ["Rate limit", "Audit log"]
    assert ui.units(ui.edit_unit("# no block\n", "First", "TODO")) == [{"name": "First", "state": "TODO", "note": ""}]
    for name, state in (("", "TODO"), ("x", "PENDING"), ("Odd: DONE", "TODO")):
        with pytest.raises(ValueError):
            ui.edit_unit(plan, name, state)


def test_run_args_validates_before_spawning():
    assert ui.run_args({"agents": {"reviewer": "claude"}, "gates": ["pytest -q", " "], "max_iterations": "3",
                        "no_git": True}) == ["--agent", "reviewer=claude", "--gate", "pytest -q",
                                             "--max-iterations", "3", "--no-git"]
    assert ui.run_args({"gates": []}) == ["--gate", ""]  # explicitly no gates
    assert ui.run_args({}) == []  # repo config / defaults decide
    for bad in ({"agents": {"tester": "claude"}}, {"agents": {"coder": "gpt"}}, {"max_iterations": 0},
                {"gates": "pytest"}):
        with pytest.raises(ValueError):
            ui.run_args(bad)


@pytest.fixture
def server():
    srv = ui.serve(0, "secret-token")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _call(base, path, body=None, token="secret-token"):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-MAE-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_api_requires_the_token(server):
    assert _call(server, "/api/output", token="nope")[0] == 403
    assert _call(server, "/api/output", token="")[0] == 403
    with urllib.request.urlopen(server + "/") as res:  # the page itself is public; it carries no data
        assert b"MultiAgentEngine" in res.read()


def test_api_edits_documents_and_reports_bad_input(server):
    with tempfile.TemporaryDirectory() as d:
        code, state = _call(server, "/api/init", {"repo": d})
        assert code == 200 and state["docs"]["plan"].startswith("<!-- ENGINE-STATUS")
        code, state = _call(server, "/api/unit", {"repo": d, "name": "Login", "state": "TODO"})
        assert [u["name"] for u in state["units"]] == ["First unit", "Login"]
        assert _call(server, "/api/doc", {"repo": d, "which": "config", "text": "[1]"})[0] == 400
        assert _call(server, "/api/doc", {"repo": d, "which": "../../etc/passwd", "text": "x"})[0] == 400
        assert _call(server, "/api/state?repo=/definitely/not/here")[0] == 400
        assert _call(server, "/api/runlog?repo=" + d + "&name=../../x")[0] == 400
        assert _call(server, "/api/show?repo=" + d + "&sha=HEAD;rm")[0] == 400


def test_api_launches_a_real_run_and_streams_it(server):
    """End to end through the real `mae` entrypoint, with a stub `claude` that marks the unit DONE."""
    with tempfile.TemporaryDirectory() as d:
        repo, bin_dir = Path(d) / "repo", Path(d) / "bin"
        (repo / ".mae").mkdir(parents=True)
        bin_dir.mkdir()
        (repo / ".mae" / "spec.md").write_text("spec")
        (repo / ".mae" / "plan.md").write_text("<!-- ENGINE-STATUS\nLogin: TODO\n-->\n")
        stub = bin_dir / "claude"
        stub.write_text("#!/bin/sh\ncat >/dev/null\nsed -i 's/Login: TODO/Login: DONE/' .mae/plan.md\n"
                        "echo '{\"type\":\"result\",\"result\":\"{\\\"problems\\\": []}\",\"total_cost_usd\":0.01}'\n")
        stub.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)

        import os
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        try:
            body = {"repo": str(repo), "agents": {"reviewer": "claude", "auditor": "claude"}, "gates": ["true"]}
            assert _call(server, "/api/run", body)[0] == 200
            assert _call(server, "/api/run", body)[0] == 400  # one run at a time
            deadline = time.time() + 60
            while (out := _call(server, "/api/output?since=0")[1])["running"] and time.time() < deadline:
                time.sleep(0.2)
        finally:
            os.environ["PATH"] = old_path
        text = "\n".join(out["lines"])
        assert out["exit_code"] == 0, text
        assert "--- coder ---" in text and "audit_clean=True" in text
        runs = _call(server, "/api/runs?repo=" + str(repo))[1]
        assert runs[0]["status"] == "clean" and runs[0]["iterations"] == 1 and runs[0]["cost_usd"] == pytest.approx(0.02)
        log = _call(server, f"/api/commits?repo={repo}&base={runs[0]['base_sha']}&branch={runs[0]['branch']}")[1]["log"]
        assert "engine: iteration 1 — green" in log


def test_a_malformed_request_body_is_a_400_not_a_dropped_connection(server):
    assert _call(server, "/api/run", [])[0] == 400
    req = urllib.request.Request(server + "/api/run", data=b"{not json", headers={"X-MAE-Token": "secret-token"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 400


def test_run_history_uses_the_engines_own_verdict(tmp_path):
    runs_dir = tmp_path / ".mae" / "runs"
    runs_dir.mkdir(parents=True)
    finish = {"event": "finish", "tests_passed": True, "audit_clean": True, "exit_code": 2, "caveats": ["BLOCKED: B"]}
    (runs_dir / "20261014-000000.jsonl").write_text(json.dumps(finish) + "\n")
    (runs_dir / "20261013-000000.jsonl").write_text(json.dumps({"event": "finish", "tests_passed": True,
                                                                "audit_clean": True}) + "\n")  # older log
    assert [r["status"] for r in ui.runs(tmp_path)] == ["needs review", "clean"]
