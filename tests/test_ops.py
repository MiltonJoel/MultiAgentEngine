"""Checkpoints and the run lock: the two pieces that touch real state on disk."""
import subprocess
import tempfile
from pathlib import Path

import pytest

from engine import git
from engine.main import _plan_lock


def test_checkpoints_are_a_rollback_point():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)

        assert git.is_repo(str(repo))
        branch = git.start_branch(str(repo))
        assert branch and branch.startswith("engine/")
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
        assert head == branch

        (repo / "a.py").write_text("x = 2\n")
        assert git.checkpoint(str(repo), "engine: iteration 1 — green")
        assert git.checkpoint(str(repo), "engine: iteration 2 — green") is None  # nothing to commit
        old = subprocess.run(["git", "-C", str(repo), "show", "HEAD~1:a.py"],
                             capture_output=True, text=True, check=True).stdout
        assert old == "x = 1\n"


def test_a_non_git_repo_degrades_instead_of_crashing():
    with tempfile.TemporaryDirectory() as d:
        assert git.is_repo(d) is False
        assert git.checkpoint(d, "engine: iteration 1") is None


def test_plan_lock_keeps_a_second_run_out():
    with tempfile.TemporaryDirectory() as d:
        plan = Path(d) / "plan.md"
        plan.write_text("<!-- ENGINE-STATUS\nLogin: TODO\n-->\n")
        with _plan_lock(plan), pytest.raises(SystemExit), _plan_lock(plan):
            pass
        with _plan_lock(plan):
            pass


def test_checkpoints_never_commit_secrets_or_run_logs():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        (repo / "app.py").write_text("x = 1\n")
        (repo / ".env").write_text("TOKEN=secret\n")
        (repo / "pkg").mkdir()
        (repo / "pkg" / "server.pem").write_text("key\n")
        (repo / ".mae" / "runs").mkdir(parents=True)
        (repo / ".mae" / "runs" / "1.jsonl").write_text("{}\n")
        assert git.checkpoint(str(repo), "engine: iteration 1")
        tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True,
                                 check=True).stdout.split()
        assert tracked == ["app.py"]
        assert not git.dirty(str(repo))  # excluded files don't count as the coder having changed something
        (repo / "app.py").write_text("x = 2\n")
        assert git.dirty(str(repo))


def test_coder_pass_that_changes_nothing_counts_as_idle(monkeypatch):
    from engine.graph import END, IDLE_LIMIT, after_tests
    from engine.nodes import coder

    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        (repo / "spec.md").write_text("spec")
        plan = repo / "plan.md"
        plan.write_text("<!-- ENGINE-STATUS\nLogin: TODO\n-->\n")
        git.checkpoint(str(repo), "base")
        state = {"plan": str(plan), "spec": str(repo / "spec.md"), "repo": str(repo), "agents": {"coder": "fake"},
                 "git_branch": "engine/x", "iteration": 0, "idle_passes": 0, "errors": [], "problems": []}

        monkeypatch.setattr(coder, "run_agent", lambda *a, **k: "")  # an agent that does nothing
        state.update(coder.coder_node(state))
        assert state["idle_passes"] == 1
        monkeypatch.setattr(coder, "run_agent", lambda *a, **k: (repo / "app.py").write_text("x = 1\n"))
        state.update(coder.coder_node(state))
        assert state["idle_passes"] == 0  # progress resets the count

        state.update(tests_passed=True, max_iterations=10, idle_passes=IDLE_LIMIT)
        assert after_tests(state) == END
