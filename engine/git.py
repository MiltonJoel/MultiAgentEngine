"""Checkpoints in the target repo: one branch per run, one commit per iteration.
Everything degrades to a no-op when the target isn't a git repo."""
import subprocess
from datetime import datetime, timezone

MAX_DIFF_CHARS = 20000


def _git(repo: str, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def is_repo(repo: str) -> bool:
    return _git(repo, "rev-parse", "--git-dir")[0] == 0


def start_branch(repo: str) -> str | None:
    """`checkout -b engine/<stamp>`; keeps the working tree so uncommitted work carries over."""
    branch = f"engine/{datetime.now(timezone.utc).astimezone().strftime('%Y%m%d-%H%M%S')}"
    code, out = _git(repo, "checkout", "-b", branch)
    if code:
        print(f"[git] could not create {branch}: {out}", flush=True)
        return None
    print(f"[git] working on branch {branch}", flush=True)
    return branch


def checkpoint(repo: str, message: str) -> str | None:
    """Commit everything; None if there was nothing to commit."""
    _git(repo, "add", "-A")
    code, out = _git(repo, "commit", "-m", message)
    if code:
        if "nothing to commit" not in out:
            print(f"[git] commit failed: {out.splitlines()[0] if out else code}", flush=True)
        return None
    sha = _git(repo, "rev-parse", "--short", "HEAD")[1]
    print(f"[git] {sha} {message}", flush=True)
    return sha


def diff(repo: str, ref: str = "HEAD~1") -> str:
    """Stat plus body since `ref`, truncated head-first so it can't flood a prompt."""
    code, out = _git(repo, "diff", "--stat", ref)
    if code:
        return ""
    body = _git(repo, "diff", ref)[1]
    if len(body) > MAX_DIFF_CHARS:
        body = body[:MAX_DIFF_CHARS] + f"\n[... diff truncated at {MAX_DIFF_CHARS} chars ...]"
    return f"{out}\n\n{body}".strip()


def head(repo: str) -> str | None:
    code, out = _git(repo, "rev-parse", "HEAD")
    return None if code else out
