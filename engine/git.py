"""Checkpoints in the target repo: one branch per run, one commit per iteration.

The coder writes with acceptEdits for hours. Without a commit after every pass
there is no rollback point and no diff to read afterwards — iteration 7 breaking
what iteration 3 got right is unrecoverable. Everything here degrades to a no-op
when the target isn't a git repo, so a non-git repo still runs, just unprotected.
"""
import subprocess
from datetime import datetime, timezone


def _git(repo: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def is_repo(repo: str) -> bool:
    return _git(repo, "rev-parse", "--git-dir")[0] == 0


def start_branch(repo: str, prefix: str = "engine") -> str | None:
    """Branch off the current HEAD so the run is isolated and reviewable as a whole.

    `checkout -b` keeps the working tree, so uncommitted work is carried over and
    captured by the first checkpoint instead of being discarded.
    """
    branch = f"{prefix}/{datetime.now(timezone.utc).astimezone().strftime('%Y%m%d-%H%M%S')}"
    code, out = _git(repo, "checkout", "-b", branch)
    if code:
        print(f"[git] could not create {branch}: {out}", flush=True)
        return None
    print(f"[git] working on branch {branch}", flush=True)
    return branch


def checkpoint(repo: str, message: str) -> str | None:
    """Commit whatever the coder just did. None if there was nothing to commit."""
    _git(repo, "add", "-A")
    code, out = _git(repo, "commit", "-m", message)
    if code:
        if "nothing to commit" not in out:
            print(f"[git] commit failed: {out.splitlines()[0] if out else code}", flush=True)
        return None
    sha = _git(repo, "rev-parse", "--short", "HEAD")[1]
    print(f"[git] {sha} {message}", flush=True)
    return sha
