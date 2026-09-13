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
