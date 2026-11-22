---
title: Getting started
---

# Getting started

Install MultiAgentEngine, put a spec and a work queue in the repository you want to build, and run
`mae`. The engine drives the agents until the queue is closed and an independent audit finds nothing.

## Install

MultiAgentEngine needs Python 3.10 or later on Linux or macOS. On Windows, run it inside
[WSL](https://learn.microsoft.com/windows/wsl/). The model providers are separate tools: the engine
does not bundle a model.

### With pipx

For normal use, install the package from Git with [pipx](https://pipx.pypa.io/). It creates an
isolated environment and puts the `mae` and `mae-ui` commands on your `PATH`:

```bash
pipx install git+https://github.com/MiltonJoel/MultiAgentEngine.git
mae --version
```

### From a clone

To work on the engine itself, clone the repository and use a virtual environment:

```bash
git clone https://github.com/MiltonJoel/MultiAgentEngine.git
cd MultiAgentEngine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
```

The `dev` extra installs the test, lint and build tools, including pytest, ruff and mypy. If
`pip install` cannot download `setuptools` because you are offline or behind a proxy, install
`setuptools` into the environment first and retry with `pip install -e . --no-build-isolation`.

## Check the tools

Ask the shell what it can find:

```bash
which mae pytest ruff git
which ollama              # the free local option
which claude codex        # the paid CLI option
```

`mae` must be on the `PATH`. You also need **at least one model provider**. Any provider can play
any role, and you can mix them per role:

| Tool | Role | Notes |
|---|---|---|
| [Ollama](https://ollama.com) with a downloaded model | any (`--free` uses it for all) | Free and local, no account or key: `ollama pull qwen2.5-coder:7b` |
| An OpenAI-compatible endpoint with a free tier | any, through the `api` runner | For example OpenRouter (`:free` models), Groq or Google AI Studio; the key goes in `MAE_API_KEY` |
| The `claude` CLI, logged in | the coder by default | Paid; must be on the `PATH` and already authenticated |
| The `codex` CLI, logged in | reviewer and auditor by default | Paid; must be on the `PATH` and already authenticated, or bind those roles to another runner |
| `pytest` | default test gate | Runs inside the **target** repo; replace it with `--gate` |
| `ruff` | default lint gate | Skipped if it is not installed; do not use the snap build — see [Troubleshooting](troubleshooting.md) |
| `git` | checkpoints in the target repo | Optional; without it there is no restore point |

With Ollama or the two CLIs you do not configure an API key. Only the `api` runner uses one, and it
reads the key from an environment variable, never from a file in the repository. See
[Free models](guide/free-models.md) for endpoint and model settings.

## Create the two files

The documents live in the repository you are building, not in the engine's installation. The engine
ships no project documents, so one install can serve any number of projects.

```bash
cd ~/code/myproject
mkdir -p .mae
echo ".mae/runs/" >> .gitignore
```

### `.mae/spec.md` — what the system must be

Write the requirements, interfaces and invariants. Prose is fine. The engine never edits this file,
and it sends the spec to every agent:

```markdown
# Spec
A FastAPI service with email/password login, password reset with a token sent by email, and a
limit of 100 requests/minute per user. Postgres through SQLAlchemy. Every endpoint has a pytest test.
```

### `.mae/plan.md` — the work queue

The HTML comment at the top is the status block. The engine reads it to decide what to do. Put one
line per unit of work inside it, and keep everything else below it:

```markdown
<!-- ENGINE-STATUS
Login flow: TODO
Password reset: TODO
Rate limiting: TODO
-->

# Notes
Anything that needs more than one line goes here. The engine never parses this part,
but the coder reads it.
```

Each line is `<name>: <STATE>`, where the state is one of `TODO`, `IN_PROGRESS`, `DONE` or
`BLOCKED`. Text after the state is a note for the coder and is kept. The block is an index, not a
sequence: order does not matter. [Plan and spec](guide/plan-and-spec.md) describes the states and the
exact parsing rules.

### If you only have the task, no TODO list

That is normal on the first run. Write the task and leave the status block out:

```markdown
# Task
Implement the service described in spec.md: login, password reset and per-user rate limiting.
```

On the first pass the coder reads the spec and the task, writes an `ENGINE-STATUS` block with one
`TODO` line per unit, and moves each unit to `DONE` as it goes. At startup you see:

```text
This run: no task list yet — the coder writes it from the spec and plan
```

From the second run the block exists. Review it, rename or split units if you do not like how it came
out, and continue with the normal flow. If you want to control the split from the start, write the
`TODO` lines yourself.

!!! note "An empty block means “never started”"
    A block with no state lines is not a finished plan. The coder treats it as an invitation to write
    the queue from scratch, so keep at least one line when you tidy up an old plan.

## Run it

From the repository:

```bash
mae
```

Or from anywhere, by naming the repository:

```bash
mae --repo ~/code/myproject
```

Before starting any agent, the engine prints what it is about to do. Check these lines and press
<kbd>Ctrl</kbd>+<kbd>C</kbd> if something is wrong:

```text
Plan: TODO=3
This run: Login flow, Password reset, Rate limiting
Log: ~/code/myproject/.mae/runs/20260913-101500.jsonl
Agents: coder=claude, reviewer=codex, auditor=codex
Gates: pytest -q --tb=line --no-header ; ruff check . --output-format json
[git] working on branch engine/20260913-101500
```

| Line | What it tells you |
|---|---|
| `Plan:` | How many units are in each state present in the block |
| `This run:` | The open units the coder will work on; or that there is no task list yet |
| `Log:` | The JSONL run log for this run |
| `Agents:` | Which runner plays each role |
| `Gates:` | The commands that decide whether a pass is green; `(none — nothing gates the coder)` means nothing does |
| `[git] working on branch` | The per-run branch where every pass is committed |

If the target is not a git repository, the last line becomes
`[git] <repo> is not a git repository — running without checkpoints or rollback`. With `--no-git`
it is `[git] checkpoints disabled (--no-git)`.

The engine then iterates: coder, tests, then the reviewer on a red pass or the auditor on a green
one, and back to the coder. A run can take hours; let it run. [The loop](concepts/loop.md) explains
the routing, and [Command line](reference/cli.md) lists every flag and exit code.

## Review the result

When the run ends it prints the counters and, with git enabled, the exact command to review the
branch:

```text
Finished after 6 iteration(s), tests_passed=True, audit_clean=True
Plan: DONE=3
Usage: ...
Review with: git -C /home/you/code/myproject log --oneline 1a2b3c4..engine/20260913-101500
```

The branch has one commit per pass, including red passes, so every pass is a rollback point:

```bash
git -C ~/code/myproject log --oneline --all --graph | head -20
git -C ~/code/myproject diff <base>                 # everything the run changed
```

Merge the branch if you like the result, or `git reset --hard <sha>` to any pass if you do not.
The exit code tells you how the run ended: `0` clean, `1` did not finish, `2` finished but needs a
human look (there are `BLOCKED` units, or no gate ran). See [Command line](reference/cli.md).

## Ask for more

Add a line to the status block, or reopen a finished one, and run `mae` again:

```markdown
Rate limiting: TODO — the limit is per user, not per IP
Audit log for logins: TODO
```

You can do the same edit with a command:

```bash
sed -i.bak 's/^Rate limiting: DONE/Rate limiting: TODO — per user, not per IP/' .mae/plan.md
rm .mae/plan.md.bak
mae
```

A `TODO` over code that already exists means “reconcile the code with the new note”. The next run
continues from the block, so nothing that is already `DONE` is repeated. The whole daily workflow is:
edit the block, run `mae`, review the branch.

## Next steps

- [Plan and spec](guide/plan-and-spec.md) — the directory layout, states, parsing and how to change work.
- [Free models](guide/free-models.md) — run every role without a subscription.
- [Dashboard](guide/dashboard.md) — edit the queue and watch a run from the browser.
- [Configuration](reference/config.md) — per-repository settings in `.mae/config.json`.
- [Troubleshooting](troubleshooting.md) — what each startup error means.
