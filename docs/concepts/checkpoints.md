---
title: Checkpoints
---

# Checkpoints

Checkpoints are git commits that `mae` makes in your repository, one per pass, so that every change the agents
make can be reviewed, compared and undone. This page describes the branch a run works on, what gets committed and
what never does, how to roll back, and what happens when the repository is not under git or is in an unusual
state.

## What a run does to git

A run with git on, the default, does four things:

1. **Creates a branch.** `mae` runs `git checkout -b engine/<stamp>` in the repository, where `<stamp>` is the
   local time when the run starts, as `YYYYMMDD-HHMMSS`. The working tree is kept, so uncommitted work carries
   over. If a branch with that name already exists, a random four-character suffix is added.
2. **Commits your uncommitted work as a baseline.** If there is anything uncommitted, it is committed first with
   the message `engine: baseline — uncommitted work from before the run`. This keeps your work out of the agent's
   diffs. The commit the run starts from is recorded as `base_sha`, and the auditor diffs against it.
3. **Commits after every pass.** The tester commits after each pass, whether the build is green or red. The
   message says which:

   ```text
   engine: iteration 1 — 3 error(s)
   engine: iteration 2 — green
   ```

4. **Prints the review command at the end.** When the run finishes, it prints:

   ```text
   Review with: git -C /home/you/code/myproject log --oneline 37aeaf3..engine/20260913-101500
   ```

A checkpoint that fails stops the run. It does not carry on without rollback points.

!!! note
    The branch is created when the run starts, so `mae` leaves you on the `engine/` branch when it exits. To
    go back to the branch you were on, use `git switch -`.

## What is committed

Every checkpoint is `git add -A` over the repository, with exclusions. Your `.gitignore` applies as usual, so a
file ignored there is not committed. The spec and the plan are ordinary files and are committed too, along with
the rest of `.mae/`, except for the run logs.

The exclusions (`NEVER_COMMIT` in `engine/git.py`) apply even when the repository's `.gitignore` forgets them:

| Pattern | What it covers |
|---|---|
| `**/.env`, `**/.env.*`, `**/.envrc` | environment files |
| `**/*.pem`, `**/*.key`, `**/*.p12`, `**/*.pfx` | certificates and private keys |
| `**/id_rsa*`, `**/id_ed25519*` | SSH keys |
| `**/.npmrc`, `**/.pypirc`, `**/.netrc` | registry and login files |
| `**/credentials.json`, `**/*credentials*.json`, `**/service-account*.json` | cloud and service credentials |
| `**/kubeconfig`, `**/.kube/config` | Kubernetes configuration |
| `**/__pycache__/**` | Python bytecode |
| `.mae/runs/**` | the run logs |

A secret file that is already staged is unstaged before the commit. If a tracked secret file has changed, the
change is not committed. If the file was already committed, its committed version stays in history.

!!! warning "A safety net, not a guarantee"
    These exclusions catch common names only. Keep secrets out of the repository you point `mae` at. A key with an
    unusual name, or a secret pasted into a source file, is committed like any other text.

A checkpoint with nothing to commit makes no commit and no message, and the run continues.

## Rolling back

Each pass is a commit, so every earlier state of the repository can be reached. Before you change anything,
list the passes:

```bash
git -C ~/code/myproject log --oneline 37aeaf3..engine/20260913-101500
```

To see what one pass changed:

```bash
git -C ~/code/myproject show a91c02b          # one pass: the message and the diff
git -C ~/code/myproject diff HEAD~1           # the last pass, from the branch tip
```

To return the working tree to a pass:

```bash
git -C ~/code/myproject switch engine/20260913-101500
git -C ~/code/myproject branch keep-the-later-passes HEAD   # keep the commits you are about to drop
git -C ~/code/myproject reset --hard a91c02b
```

`reset --hard` discards every commit after `a91c02b` on the branch, and any uncommitted change in the working
tree. Create a branch first, as above, if you may want those commits back. A checkpoint is taken after every pass,
so the only uncommitted changes you could lose are the ones made after the last pass.

To go back to where you started, use `git switch -`, which returns to the branch you were on when the run began.

## Not a git repository, and `--no-git`

The engine checks whether the repository is under git at the start. If it is not, the run continues without
checkpoints and says so:

```text
[git] /home/you/code/myproject is not a git repository — running without checkpoints or rollback
```

Without git, the engine cannot diff passes, so it judges idle passes from a fingerprint of the files: each file's
path, size and modification time, before and after the pass. Directories named `.git`, `node_modules`, `.venv`,
`venv`, `__pycache__`, the usual caches, `.claude` and `runs` are skipped.

`--no-git` turns checkpoints off in a repository that is under git:

```bash
mae --no-git
```

The run prints `[git] checkpoints disabled (--no-git)`. It makes no branch and no commits. Use it when you do
not want any commits from the run, or when you have a reason to keep the repository's history untouched. It is
the same switch as **no git checkpoints** in the [dashboard](../guide/dashboard.md).

!!! warning
    Without checkpoints there is no rollback point. Keep your own backup, or run in a clean checkout.

## When the repository is busy

Before the run starts, with git on, `mae` checks that the repository is not in the middle of an operation that
would make its commits go wrong. It refuses to start if any of these is true:

| Condition | Message ends with |
|---|---|
| a merge is in progress (`MERGE_HEAD` exists) | `a merge is in progress` |
| a rebase is in progress | `a rebase is in progress` |
| a cherry-pick is in progress | `a cherry-pick is in progress` |
| a `git` command crashed or is still running (`index.lock` exists) | `... exists (a git command crashed or is still running)` |

The refusal is printed as `<repo>: <reason> — finish or abort it first, or run with --no-git`, and it exits with
code `3` (refused to start). Finish or abort the operation with `git merge --abort`, `git rebase --abort` or
`git cherry-pick --abort`, then start the run again.

For a stale `index.lock`, first check that no `git` process is running in the repository. Then delete the lock file
in the repository's git directory, which `git rev-parse --git-dir` prints. A lock left behind by a process killed
during a checkpoint is the usual cause. See [CI, Docker and signals](../guide/ci.md) for why the engine avoids
leaving one.

## Checkpoint failures

A checkpoint that cannot be written stops the run. The baseline and every pass checkpoint are strict. The
message names the step and the reason, and suggests what to fix:

```text
[git] checkpoint commit failed: Author identity unknown — fix it (git identity, hooks, signing, permissions) or re-run with --no-git
```

The reason is the first line of git's own output, so its wording comes from git. Common causes are a missing git identity (`user.name` and `user.email`), a commit hook that fails, commit signing
that needs a passphrase, and files the process cannot write. The run's exit code is `1`, and the run log has an
`aborted` event with the error.

If the engine cannot create the branch at all, it prints `[git] could not create engine/...: <git's message>` and
continues without checkpoints. It does not stop. Look for that line at the top of the run's output.

Each git call has a 300-second limit. A git command that waits for a credential prompt, a pinentry or a slow hook
is killed when it expires, and the checkpoint fails.

## Stale `engine/` branches

Every run leaves its branch behind. The engine never deletes branches, so they accumulate.

List them:

```bash
git -C ~/code/myproject branch --list 'engine/*'
```

List the ones already merged into `master`:

```bash
git -C ~/code/myproject branch --merged master --list 'engine/*'
```

Delete the ones you no longer need, one at a time:

```bash
git -C ~/code/myproject branch -d engine/20260913-101500
```

Use `-d`, which refuses to delete a branch with commits that are not merged. `-D` deletes them anyway, so use it
only when you have decided those commits are not needed.

## Related pages

- [The loop](loop.md) explains which passes are committed and why the reviewer and the auditor diff against them.
- [CI, Docker and signals](../guide/ci.md) covers the signal handling that keeps a checkpoint from being cut short.
- [Configuration](../reference/config.md) and [Command line](../reference/cli.md) list the flags for git.
