"""Regression tests for the ways a run could report "clean" when it wasn't, hang, or commit what it shouldn't."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from engine import git, prompts, runlog
from engine.cli import _limit_text, _stream
from engine.graph import plan_blocked, plan_done, plan_issues, reopen_units
from engine.nodes import tester
from engine.nodes.coder import fingerprint
from engine.nodes.reviewer import _parse_problems
from engine.nodes.tester import build_gates, run_gate


def _plan(d: str, text: str) -> str:
    path = Path(d) / "plan.md"
    path.write_bytes(text.encode())
    return str(path)


def _git_repo(d: str) -> Path:
    repo = Path(d)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    return repo


def _committed(repo: Path) -> list[str]:
    return subprocess.run(["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.split()


# --- the status block -------------------------------------------------------------------------------------

def test_a_clean_status_block_has_no_issues():
    with tempfile.TemporaryDirectory() as d:
        assert plan_issues(_plan(d, "<!-- ENGINE-STATUS\nA: DONE\nB: TODO note\n-->\n"), ["A", "B"]) == []
        assert plan_issues(_plan(d, "# no block yet\n")) == []  # the coder writes it on the first pass


def test_an_arrow_in_a_note_is_caught_instead_of_hiding_the_units_after_it():
    with tempfile.TemporaryDirectory() as d:
        plan = _plan(d, "<!-- ENGINE-STATUS\nParser: DONE lexer --> parser\nCodegen: TODO\n-->\n")
        assert plan_done(plan)  # what the engine used to conclude
        issues = plan_issues(plan, ["Parser", "Codegen"])
        assert any("closed the block early" in i for i in issues)
        assert any("'Codegen'" in i for i in issues)


def test_a_second_block_a_typo_or_a_dropped_unit_is_an_issue():
    with tempfile.TemporaryDirectory() as d:
        two = "<!-- ENGINE-STATUS\nExample: DONE\n-->\n```\n<!-- ENGINE-STATUS\nReal: TODO\n-->\n```\n"
        assert any("2 ENGINE-STATUS blocks" in i for i in plan_issues(_plan(d, two)))
        typo = _plan(d, "<!-- ENGINE-STATUS\nA: DONE\nB: Todo\n-->\n")
        assert any("unreadable status line" in i for i in plan_issues(typo, ["A", "B"]))
        gone = _plan(d, "<!-- ENGINE-STATUS\nA: DONE\n-->\n")
        assert any("'B' was open at run start" in i for i in plan_issues(gone, ["A", "B"]))
        assert plan_issues(_plan(d, "no block\n"), ["A"]) == ["the ENGINE-STATUS block is gone"]


def test_the_tester_turns_a_broken_status_block_red():
    with tempfile.TemporaryDirectory() as d:
        plan = _plan(d, "<!-- ENGINE-STATUS\nA: DONE see a --> b\nB: TODO\n-->\n")
        out = tester.tester_node({"repo": d, "plan": plan, "focus": ["A", "B"], "gates": [], "iteration": 1,
                           "git_branch": None, "problems": []})
        assert not out["tests_passed"] and all(e["file"] == plan for e in out["errors"])


def test_reopen_keeps_crlf_line_endings():
    with tempfile.TemporaryDirectory() as d:
        plan = _plan(d, "<!-- ENGINE-STATUS\r\nA: DONE\r\nB: DONE\r\n-->\r\n\r\n# Notes\r\n")
        assert reopen_units(plan, [{"unit": "A", "description": "no test"}]) == ["A"]
        data = Path(plan).read_bytes()
        assert b"A: TODO \xe2\x80\x94 audit: no test\r\n" in data
        assert data.count(b"\r\n") == data.count(b"\n")  # no bare LF crept in


def test_blocked_units_are_reported():
    with tempfile.TemporaryDirectory() as d:
        plan = _plan(d, "<!-- ENGINE-STATUS\nA: DONE\nB: BLOCKED needs keys\n-->\n")
        assert plan_done(plan) and plan_blocked(plan) == ["B"]


# --- reviewer / auditor replies ---------------------------------------------------------------------------

@pytest.mark.parametrize("reply", ['{"problems": null}', '{"problems": "none"}', '{"problems": ["Lexer is wrong"]}',
                                   "", "looks fine to me"])
def test_an_unreadable_reply_is_never_a_clean_audit(reply):
    problems = _parse_problems(reply)
    assert len(problems) == 1 and problems[0]["description"].startswith("unreadable reply")
    reopen_units(os.devnull, problems)  # the normalized shape never crashes the reopen step


def test_prose_braces_before_the_json_are_skipped():
    assert _parse_problems('Checked {spec} and {plan}. {"problems": []}') == []
    found = _parse_problems('Notes {x}\n```json\n{"problems": [{"unit": "A", "line": "7", "description": "d"}]}\n```')
    assert found == [{"unit": "A", "file": "", "line": 0, "description": "d", "suggested_fix": ""}]


# --- gates ------------------------------------------------------------------------------------------------

def test_gate_config_types_fail_with_a_message():
    for bad in ([{"cmd": "pytest", "ok_codes": 5}], [{"cmd": "pytest", "ok_codes": ["5"]}], ["pytest 'unclosed"]):
        with pytest.raises(SystemExit):
            build_gates(bad)
    assert build_gates([{"cmd": "pytest", "ok_codes": [0, 5]}]) == [{"cmd": ["pytest"], "ok_codes": (0, 5)}]


def test_a_gate_that_disappears_mid_run_is_a_failure_not_a_pass():
    with tempfile.TemporaryDirectory() as d:
        errors = run_gate(["definitely-not-a-tool-xyz"], d)
        assert errors and "could not run" in errors[0]["description"]


def test_a_timed_out_gate_fails_even_when_its_exit_code_is_allowed_and_its_children_die(monkeypatch):
    monkeypatch.setattr("engine.nodes.tester.GATE_TIMEOUT", 1)
    with tempfile.TemporaryDirectory() as d:
        marker = Path(d) / "alive"
        script = f"(sleep 3; touch {marker}) & sleep 30"
        start = time.monotonic()
        errors = run_gate(["sh", "-c", script], d, ok_codes=(0, 1, -9))
        assert time.monotonic() - start < 10
        assert errors and "timed out" in errors[0]["description"]
        time.sleep(3.5)
        assert not marker.exists()  # the backgrounded grandchild was killed with its group


def test_green_tests_clear_the_reviewers_old_diagnosis():
    with tempfile.TemporaryDirectory() as d:
        plan = _plan(d, "<!-- ENGINE-STATUS\nA: DONE\n-->\n")
        state = {"repo": d, "plan": plan, "focus": ["A"], "iteration": 2, "git_branch": None,
                 "problems": [{"description": "old"}]}
        assert tester.tester_node({**state, "gates": [{"cmd": ["true"], "ok_codes": (0,)}]})["problems"] == []
        red = tester.tester_node({**state, "gates": [{"cmd": ["false"], "ok_codes": (0,)}]})
        assert red["problems"] == [{"description": "old"}]


# --- prompts ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("body", ["Positional {0}", "Stray { brace", "Unknown {nope}"])
def test_a_broken_prompt_override_fails_at_startup(body):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / ".mae" / "prompts").mkdir(parents=True)
        (Path(d) / ".mae" / "prompts" / "auditor.md").write_text(body)
        with pytest.raises(SystemExit):
            prompts.check(d)
    with tempfile.TemporaryDirectory() as d:
        prompts.check(d)  # the built-in prompts are fine


# --- git --------------------------------------------------------------------------------------------------

def test_secrets_are_never_committed_even_when_already_staged():
    with tempfile.TemporaryDirectory() as d:
        repo = _git_repo(d)
        for name in ("app.py", ".env", ".npmrc", "credentials.json", "cert.p12", ".envrc"):
            (repo / name).write_text("x\n")
        subprocess.run(["git", "-C", str(repo), "add", ".env", "credentials.json"], check=True)
        assert git.checkpoint(str(repo), "base")
        assert _committed(repo) == ["app.py"]
        (repo / "b.py").write_text("y\n")
        subprocess.run(["git", "-C", str(repo), "add", "-f", ".npmrc"], check=True)
        assert git.checkpoint(str(repo), "two")
        assert _committed(repo) == ["b.py"]


def test_a_failed_commit_stops_a_strict_checkpoint():
    with tempfile.TemporaryDirectory() as d:
        repo = _git_repo(d)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        (repo / "a.py").write_text("x\n")
        assert git.checkpoint(str(repo), "lenient") is None
        with pytest.raises(git.CheckpointError):
            git.checkpoint(str(repo), "strict", strict=True)
        hook.unlink()
        assert git.checkpoint(str(repo), "after the fix", strict=True)
        assert git.checkpoint(str(repo), "nothing new", strict=True) is None


def test_two_runs_in_the_same_second_get_different_branches(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        repo = _git_repo(d)
        (repo / "a.py").write_text("x\n")
        git.checkpoint(str(repo), "base")
        first = git.start_branch(str(repo))
        second = git.start_branch(str(repo))
        assert first and second and first != second


def test_a_repo_mid_merge_or_with_a_stale_lock_is_busy():
    with tempfile.TemporaryDirectory() as d:
        repo = _git_repo(d)
        assert git.busy(str(repo)) is None
        (repo / ".git" / "index.lock").write_text("")
        assert "index.lock" in git.busy(str(repo))


# --- agent CLIs -------------------------------------------------------------------------------------------

def test_limit_words_in_the_agents_own_messages_do_not_trigger_a_retry():
    chatter = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "add a rate limit"}]}})
    assert "rate limit" not in _limit_text([chatter + "\n", "Traceback: boom\n"])
    result = json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1800000600"})
    assert "limit reached" in _limit_text([result + "\n"])


def test_an_exit_zero_error_result_fails_and_every_attempt_is_logged(tmp_path):
    runlog.start(tmp_path)
    attempts = []
    event = json.dumps({"type": "result", "is_error": True, "subtype": "error_max_turns"})
    cmd = [sys.executable, "-c", f"import sys; sys.stdin.read(); print({event!r})"]
    with pytest.raises(RuntimeError, match="error_max_turns"):
        _stream(cmd, "prompt", str(tmp_path), timeout=30, label="t",
                failed=lambda lines: "reported error_max_turns" if "is_error" in "".join(lines) else "",
                on_attempt=lambda lines, seconds: attempts.append(lines))
    assert len(attempts) == 1 and attempts[0]


def test_fingerprint_sees_a_change_without_git():
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "a.py").write_text("x\n")
        before = fingerprint(d)
        assert fingerprint(d) == before
        (Path(d) / "b.py").write_text("y\n")
        assert fingerprint(d) != before


# --- the whole run ----------------------------------------------------------------------------------------

def _stub_run(tmp_path: Path, sed: str, gates: list[str]) -> subprocess.CompletedProcess:
    repo, bin_dir = tmp_path / "repo", tmp_path / "bin"
    (repo / ".mae").mkdir(parents=True)
    bin_dir.mkdir()
    (repo / ".mae" / "spec.md").write_text("spec")
    (repo / ".mae" / "plan.md").write_text("<!-- ENGINE-STATUS\nLogin: TODO\nPayments: TODO\n-->\n")
    stub = bin_dir / "claude"
    stub.write_text(f"#!/bin/sh\ncat >/dev/null\nsed -i '{sed}' .mae/plan.md\n"
                    "echo '{\"type\":\"result\",\"result\":\"{\\\"problems\\\": []}\",\"total_cost_usd\":0.01}'\n")
    stub.chmod(0o755)
    _git_repo(str(repo))
    args = [sys.executable, "-m", "engine.main", "--repo", str(repo), "--agent", "reviewer=claude",
            "--agent", "auditor=claude"]
    for gate in gates:
        args += ["--gate", gate]
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
    return subprocess.run(args, capture_output=True, text=True, env=env, timeout=120, check=False)


def test_a_run_that_blocks_its_units_needs_review_instead_of_exiting_clean(tmp_path):
    proc = _stub_run(tmp_path, "s/Login: TODO/Login: DONE/; s/Payments: TODO/Payments: BLOCKED no keys/", ["true"])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "BLOCKED: Payments" in proc.stdout


def test_a_run_with_no_gates_needs_review(tmp_path):
    proc = _stub_run(tmp_path, "s/: TODO/: DONE/", [""])
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "no gate ran" in proc.stdout


def test_a_fully_done_gated_run_exits_clean(tmp_path):
    proc = _stub_run(tmp_path, "s/: TODO/: DONE/", ["true"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
