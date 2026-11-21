"""Checkpoints in the target repo: one branch per run, one commit per iteration.
Everything degrades to a no-op when the target isn't a git repo."""
import secrets
import signal
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Below this a diff rides whole in the reviewer/auditor prompt; above it they get the stat and read the
# changed files themselves — every runner can, and a long diff is ~5k tokens they would mostly skip.
FULL_DIFF_CHARS = 2000
STAT_FALLBACK = ("The full diff is long, so it is not included. Read the changed files listed above"
                 " directly — you have the repository.")
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
                        ("rebase-apply", "a rebase is in progress"),
                        ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
                        ("index.lock", f"{root / 'index.lock'} exists (a git command crashed or is still running)")):
        if (root / marker).exists():
            return why
    return None


@contextmanager
def _signals_deferred():
    """Hold SIGINT/SIGTERM until the checkpoint is done. Raised mid-`subprocess.run`, they make it SIGKILL the git
    child mid-write, leaving .git/index.lock behind — and git.busy() then refuses every later run until a human
    deletes it. A blocked signal stays pending and is delivered (and raised) as soon as the block lifts."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "pthread_sigmask"):
        yield
        return
    held = {signal.SIGINT, signal.SIGTERM}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, held)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


IGNORED_WARNING = "The following paths are ignored by one of your .gitignore files:"


def _only_ignored_warning(out: str) -> bool:
    """git's "paths are ignored" notice, and nothing else that would mean a file was left unstaged."""
    lines = [line for line in out.splitlines() if line.strip()]
    if not lines or lines[0] != IGNORED_WARNING:
        return False
    return not any(line.startswith(("fatal:", "error:", "warning:")) for line in lines)


def checkpoint(repo: str, message: str, strict: bool = False) -> str | None:
    """Commit everything except NEVER_COMMIT; None if there was nothing to commit.
    strict: a failed commit raises CheckpointError instead of printing a warning.
    Ctrl-C and SIGTERM wait until it is done (bounded by TIMEOUT per git call), so it never leaves index.lock."""
    with _signals_deferred():
        return _checkpoint(repo, message, strict)


def _checkpoint(repo: str, message: str, strict: bool) -> str | None:

    def failed(step: str, out: str, code: int) -> None:
        reason = out.splitlines()[0] if out else f"exit {code}"
        if strict:
            raise CheckpointError(f"[git] checkpoint {step} failed: {reason} — fix it (git identity, hooks, "
                                  "signing, permissions) or re-run with --no-git")
        print(f"[git] {step} failed: {reason}", flush=True)

    # a partial `add` would commit a tree the gates never ran on, and every later diff would lie
    code, out = _git(repo, "-c", "advice.addIgnoredFile=false", "add", "-A", "--", ".", *EXCLUDE)
    if code == 1 and _only_ignored_warning(out):
        code = 0  # an exclude naming a gitignored path (.mae/runs/) makes git exit 1 after staging everything
    if code:
        failed("git add", out, code)
        return None
    # `add` with excludes leaves alone what was already staged; unstage secrets explicitly
    secret_specs = [f":(glob){glob}" for glob in SECRET_GLOBS]
    if head(repo):  # back to the committed version: a tracked file keeps it, an untracked one leaves the index
        code, out = _git(repo, "reset", "-q", "HEAD", "--", *secret_specs)
    else:
        code, out = _git(repo, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *secret_specs)
    if code:  # a secret could still be staged: never commit on top of that
        failed("unstaging secrets", out, code)
        return None
    code, out = _git(repo, "commit", "-m", message)
    if code:
        if "nothing to commit" in out or "nothing added to commit" in out or "no changes added" in out:
            return None
        failed("commit", out, code)
        return None
    sha = _git(repo, "rev-parse", "--short", "HEAD")[1]
    print(f"[git] {sha} {message}", flush=True)
    return sha


def diff(repo: str, ref: str = "HEAD~1") -> str:
    """Stat plus body since `ref`. A short diff goes in whole; a long one sends only the stat and
    lets the read-only agent open the files itself — cheaper than flooding every audit round."""
    code, out = _git(repo, "diff", "--stat", ref)
    if code:
        return ""
    body = _git(repo, "diff", ref)[1]
    if len(body) > FULL_DIFF_CHARS:
        return f"{out}\n\n{STAT_FALLBACK}".strip()
    return f"{out}\n\n{body}".strip()


def dirty(repo: str) -> bool:
    """Uncommitted changes outside the excluded paths."""
    return bool(_git(repo, "status", "--porcelain", "--", ".", *EXCLUDE)[1])


def head(repo: str) -> str | None:
    code, out = _git(repo, "rev-parse", "HEAD")
    return None if code else out
