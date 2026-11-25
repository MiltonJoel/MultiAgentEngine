---
title: CI, Docker and signals
---

# CI, Docker and signals

Run `mae` without a person at the keyboard: in a CI job, in a container, or as a systemd service. This page covers
the exit codes a script can rely on, a GitHub Actions example, how to stop a run cleanly from Docker or systemd,
and how to run a repository you did not write.

## Exit codes

| Code | Meaning | Where it comes from |
|---|---|---|
| `0` | Finished clean. The queue is closed, the gates are green, and the audit found nothing. | `EXIT_CLEAN` |
| `1` | Did not finish. The pass limit was reached, an agent or a checkpoint failed, the audit keeps finding problems, or the engine stalled. | `EXIT_FAILED` |
| `2` | Finished, but a person should look. Some units are `BLOCKED`, or no gate ran. | `EXIT_NEEDS_REVIEW` |
| `3` | Refused to start: a bad flag or value, a bad config, a missing gate tool, a busy repository or plan, or nothing left open. No agent ran and nothing was spent. | `EXIT_REFUSED` |
| `130` | Interrupted with Ctrl-C (`SIGINT`). | `EXIT_INTERRUPTED` |
| `143` | Stopped with `SIGTERM`. | `EXIT_TERMINATED` |

!!! note "Exit 3 is the only refusal code"
    Every refusal exits `3`, including the argument parser's usage errors and a `.mae/config.json` that is not valid
    JSON or has a key of the wrong type (for example `"agents"` not an object). The message is on stderr as
    `mae: error: <message>`. No log file is written and no branch is created. A run that started never exits `3`,
    so a script can read `0`, `1` and `2` as the run's own verdict.

### Telling the codes apart in a script

Give the run its own log directory, so that a log found there belongs to this run and not to an earlier one:

```bash
logdir=$(mktemp -d)
set +e
mae --repo . --log-dir "$logdir" --agent coder=api --agent reviewer=api --agent auditor=api
code=$?
set -e

case "$code" in
  0)  echo "clean" ;;
  1)  echo "did not finish, see the run log in $logdir"; exit 1 ;;
  2)  echo "finished, but a person should look, see the run log in $logdir"; exit 1 ;;  # or exit 0 to only warn
  3)  echo "mae refused to start, nothing ran (see its message above)"; exit 3 ;;
  *)  echo "mae exited with $code"; exit "$code" ;;
esac
```

## GitHub Actions

This job runs the loop on a checkout of the repository and keeps the run log as an artifact. It uses the `api`
runner with a secret, because the `claude` and `codex` CLIs are not installed by default and need a login. The endpoint
and model come from the repository's `.mae/config.json`, as in the [free models](free-models.md#the-api-runner-with-openrouter)
guide.

```yaml
name: mae

on:
  workflow_dispatch:

permissions:
  contents: write          # only if the job pushes the engine branch

jobs:
  loop:
    runs-on: ubuntu-latest  # macOS works too; Windows does not (use WSL)
    timeout-minutes: 240    # a pass can take 90 minutes; the whole run needs its own budget

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0    # the reviewer and the auditor diff against earlier commits

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Git identity for checkpoint commits
        run: |
          git config user.name "mae-ci"
          git config user.email "mae-ci@users.noreply.github.com"

      - name: Install
        run: pip install "git+https://github.com/MiltonJoel/MultiAgentEngine.git"

      - name: Run the loop
        env:
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
        run: mae --repo . --agent coder=api --agent reviewer=api --agent auditor=api

      - name: Keep the run log
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: mae-run-log
          path: .mae/runs/

      - name: Push the engine branch (optional)
        if: success()
        run: |
          branch=$(git branch --show-current)
          case "$branch" in
            engine/*) git push origin "$branch" ;;
            *) echo "not on an engine/ branch; nothing pushed" ;;
          esac
```

Notes on the example:

- **Git is required for checkpoints.** A clean checkout has nothing to commit at the baseline, but the first
  pass's checkpoint commit fails without an identity. The run then stops with exit `1`.
- **`fetch-depth: 0`** gives the run the history it diffs against. With a shallow clone, the reviewer may get no
  diff, because it asks for the diff against the commit before the failing pass.
- **The engine does not push.** The commits stay on the runner unless a step pushes them or the job uploads
  them. The push step only pushes an `engine/` branch, so it cannot change `master`. Remove that step if you only
  want the artifact.
- **Any nonzero exit fails the step.** To keep `2` from failing the job, use the script in
  [Telling the codes apart](#telling-the-codes-apart-in-a-script).
- **The key is passed only to the step that needs it.** Do not add it at the workflow level.

## Running in Docker

Docker stops a container by sending `SIGTERM` to its PID 1, then `SIGKILL` after a grace period. `mae` handles
`SIGTERM` the same way as Ctrl-C: it kills the agent's process group, writes an `aborted` event to the run log,
keeps the checkpoints, and exits with `143`.

The example below installs `mae` into a stock Python image, uses the `api` runner, and mounts the repository:

```bash
docker run --rm --init --name mae-run \
  -v "$PWD:/work" -w /work \
  -e OPENROUTER_API_KEY \
  python:3.12 \
  sh -c 'apt-get update -qq && apt-get install -y -qq git >/dev/null \
    && git config --global user.name mae && git config --global user.email mae@localhost \
    && git config --global --add safe.directory /work \
    && pip install -q "git+https://github.com/MiltonJoel/MultiAgentEngine.git" \
    && exec mae --repo /work --agent coder=api --agent reviewer=api --agent auditor=api'
```

Things to get right:

- **Use `exec`.** `exec` replaces the shell with `mae`, so `mae` is the process that gets the signal. A shell
  that does not forward signals would leave the agent running after `docker stop`.
- **Use `--init`.** An init process reaps the agent's child processes and forwards signals. It is a good default
  for any long-running container.
- **Give the stop time.** `docker stop` waits 10 seconds by default. Use `docker stop -t 60` so that a checkpoint
  that is in progress can finish:

  ```bash
  docker stop -t 60 mae-run
  ```

- **The container runs as root.** Files it writes in the repository belong to root on the host afterwards. Add
  `--user` with your own uid and gid if that matters, and keep in mind that the install step then needs a
  writable home directory.
- **Mount only what the run needs.** The repository, and nothing else. Do not mount `~/.ssh`, `~/.claude` or
  other credential directories. A coder that can read them can use them.

### Signals and checkpoints

A checkpoint is a sequence of git commands (`add`, `reset`, `commit`). If `mae` is killed in the middle of one,
git can leave `index.lock` in the repository's git directory. The next run then refuses to start (exit `3`), with a
message that the lock exists, until someone deletes it.

So `mae` does not stop in the middle of a checkpoint. When `SIGINT` or `SIGTERM` arrives during one, the engine
records the signal, lets the git steps finish, and then stops with `130` or `143`. The wait is bounded: each git
call has a 300-second limit. Gates and agents, by contrast, are stopped at once. Their process groups are killed
when the signal arrives.

This also covers the repository's own git hooks, which run inside the commit. A slow hook makes the stop slow.

!!! warning "Do not let the grace period run out"
    A `SIGKILL` cannot be handled. If the grace period is shorter than the checkpoint, the process is killed
    mid-commit and the lock can be left behind. Before you start a new run, check that no `git` process is
    running in that repository, then delete the lock:

    ```bash
    rm "$(git -C /path/to/repo rev-parse --git-dir)/index.lock"   # only when no git process is running
    ```

## systemd

A unit file for a run that starts at boot or on demand. Replace the paths with your own. Find the path to `mae`
with `which mae`.

```ini
[Unit]
Description=MultiAgentEngine run for myproject
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/srv/myproject
EnvironmentFile=/etc/mae/myproject.env
ExecStart=/usr/local/bin/mae --repo /srv/myproject
KillMode=mixed
TimeoutStopSec=60
SuccessExitStatus=143

[Install]
WantedBy=multi-user.target
```

- **`TimeoutStopSec=60`** is the grace period, as with `docker stop -t 60`. After it, systemd sends `SIGKILL`.
- **`KillMode=mixed`** sends `SIGTERM` only to the main process, so the engine decides how to stop its children.
  With the default `control-group`, systemd sends `SIGTERM` to every process in the unit at once, including a git
  process in the middle of a commit, which the engine cannot defer.
- **`SuccessExitStatus=143`** tells systemd that a stop by `SIGTERM` is a normal end and not a failure. `mae`
  exits with `143` rather than dying from the signal, so systemd would otherwise report the unit as failed.
- **Keep the key in `EnvironmentFile`.** Make the file readable only by the service's user, for example
  `chmod 600 /etc/mae/myproject.env`.

## Running a repository you did not write

`mae` runs commands on your machine. A coder can run any shell command, and a repository's
`.mae/config.json` can define the gates, which are shell commands too. See the [threat model](../security.md).
Run an unfamiliar repository inside a container or a virtual machine, with these rules:

1. **Read the repository's configuration first.** Look at `.mae/config.json` (gates, agents, `llm`) and at
   `.mae/prompts/`, if present. The prompts are what the agents are told to do.
2. **Check the endpoint.** A config's `llm.api.url` decides where your key is sent. The startup line prints the
   host, and a `WARNING` appears when the config chose it.
3. **Use a key you can revoke.** Give the container a key that is scoped to the job, and not your main account
   key.
4. **Keep the container's files apart from your own.** Mount a copy or a fresh clone of the repository, not your
   home directory.
5. **Keep the dashboard off the network.** Do not run `mae-ui` in a container with its port published to other
   machines.

## Related pages

- [Exit codes](../reference/cli.md) lists every flag and code.
- [Checkpoints](../concepts/checkpoints.md) explains the branch and commits the signals protect.
- [Cost and limits](../concepts/cost.md) explains the usage-limit waits, which a CI job will sit through.
- [Troubleshooting](../troubleshooting.md) has the messages you may see when a run stops or refuses to start.
