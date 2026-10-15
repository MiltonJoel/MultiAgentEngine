"""Checkpoints in the target repo: one branch per run, one commit per iteration.
Everything degrades to a no-op when the target isn't a git repo."""
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

MAX_DIFF_CHARS = 20000
# A git call that waits on a gpg pinentry, a credential prompt or a slow hook must not hang the run.
TIMEOUT = 300
# Never committed by a checkpoint, even when the target repo's .gitignore forgets them.
SECRET_GLOBS = ["**/.env", "**/.env.*", "**/.envrc", "**/*.pem", "**/*.key", "**/*.p12", "**/*.pfx",
                "**/id_rsa*", "**/id_ed25519*", "**/.npmrc", "**/.pypirc", "**/.netrc",
                "**/credentials.json", "**/*credentials*.json", "**/service-account*.json",
                "**/kubeconfig", "**/.kube/config"]
NEVER_COMMIT = [*SECRET_GLOBS, "**/__pycache__/**", ".mae/runs/**"]
EXCLUDE = [f":(exclude,glob){glob}" for glob in NEVER_COMMIT]


class CheckpointError(RuntimeError):
    """A commit failed for a reason other than "nothing to commit"; later checkpoints and diffs would lie."""


def _git(repo: str, *args: str, timeout: float = TIMEOUT) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf8",
                              errors="replace", check=False, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} timed out after {timeout:.0f}s"
    except FileNotFoundError:
        return 127, "git is not installed"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def is_repo(repo: str) -> bool:
    return _git(repo, "rev-parse", "--git-dir")[0] == 0


def start_branch(repo: str) -> str | None:
    """`checkout -b engine/<stamp>`; keeps the working tree so uncommitted work carries over."""
    branch = f"engine/{datetime.now(timezone.utc).astimezone().strftime('%Y%m%d-%H%M%S')}"
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")[0] == 0:
        branch += f"-{secrets.token_hex(2)}"  # two runs started in the same second
    code, out = _git(repo, "checkout", "-b", branch)
    if code:
        print(f"[git] could not create {branch}: {out}", flush=True)
        return None
    print(f"[git] working on branch {branch}", flush=True)
    return branch


def busy(repo: str) -> str | None:
    """Why the repo can't take checkpoints right now (mid-merge/rebase, stale lock), or None."""
    code, git_dir = _git(repo, "rev-parse", "--absolute-git-dir")
    if code:
        return None
    root = Path(git_dir)
    for marker, why in (("MERGE_HEAD", "a merge is in progress"), ("rebase-merge", "a rebase is in progress"),
                        ("rebase-apply", "a rebase is in progress"), ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
                        ("index.lock", f"{root / 'index.lock'} exists (a git command crashed or is still running)")):
        if (root / marker).exists():
            return why
    return None


def checkpoint(repo: str, message: str, strict: bool = False) -> str | None:
    """Commit everything except NEVER_COMMIT; None if there was nothing to commit.
    strict: a failed commit raises CheckpointError instead of printing a warning."""
    _git(repo, "add", "-A", "--", ".", *EXCLUDE)
    # `add` with excludes leaves alone what was already staged; unstage secrets explicitly
    secret_specs = [f":(glob){glob}" for glob in SECRET_GLOBS]
    if head(repo):  # back to the committed version: a tracked file keeps it, an untracked one leaves the index
        _git(repo, "reset", "-q", "HEAD", "--", *secret_specs)
    else:
        _git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *secret_specs)
    code, out = _git(repo, "commit", "-m", message)
    if code:
        if "nothing to commit" in out or "nothing added to commit" in out or "no changes added" in out:
            return None
        reason = out.splitlines()[0] if out else f"exit {code}"
        if strict:
            raise CheckpointError(f"[git] checkpoint commit failed: {reason} — fix it (git identity, hooks, "
                                  "signing) or re-run with --no-git")
        print(f"[git] commit failed: {reason}", flush=True)
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


def dirty(repo: str) -> bool:
    """Uncommitted changes outside the excluded paths."""
    return bool(_git(repo, "status", "--porcelain", "--", ".", *EXCLUDE)[1])


def head(repo: str) -> str | None:
    code, out = _git(repo, "rev-parse", "HEAD")
    return None if code else out
