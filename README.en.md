<div align="center">

# MultiAgentEngine

**A loop of coding agents that implements, tests and audits your spec until it is done.**

[![ci](https://github.com/MiltonJoel/MultiAgentEngine/actions/workflows/tests.yml/badge.svg)](https://github.com/MiltonJoel/MultiAgentEngine/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)
![platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS-lightgrey)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**English** · [Español](README.md)

</div>

MultiAgentEngine (`mae`) is an orchestrator built on [LangGraph](https://github.com/langchain-ai/langgraph)
that drives coding agents — the `claude` CLI, the `codex` CLI, or a **free model** (local through
Ollama, or any OpenAI-compatible API) — against a repository until an architecture spec is
implemented, tested and audited.

You write the spec and the work queue. The engine runs the agents, validates every pass with
deterministic gates (tests, lint), keeps the queue up to date, takes a git checkpoint per pass, and
stops when nothing is left open and the audit comes back clean.

**No paid subscription needed:** with `mae --free` all three roles use a local model through
[Ollama](https://ollama.com) — no account, no key, no cost.

## Features

- **Evaluator-optimizer loop** — coder → tester → reviewer (on red) or auditor (on green) → coder,
  until the queue is closed. The auditor reopens `DONE` units that do not meet the spec.
- **Deterministic gates** — pytest and ruff by default, or any command (`npm test`, `tsc`, `eslint`,
  `go vet`…); their output becomes structured `file:line` errors.
- **Any model in any role** — `claude`, `codex`, Ollama or an OpenAI-compatible API; mix them per role.
- **Safe by design** — one branch and one commit per pass (roll back to any point), secrets kept out
  of commits, file tools confined to the repo, API keys protected, clean `SIGTERM`/Ctrl-C handling.
  See [SECURITY.md](SECURITY.md).
- **Observable** — a JSONL log per run with timings, tokens and cost; exit codes designed for CI.
- **Local web dashboard** (`mae-ui`) — edit the queue and the spec, start and stop runs, watch them
  live, and browse the history with its diffs.
- **No heavy dependencies** — the standard library plus LangGraph; no web framework, no front-end build.

## Contents

- [Example](#example)
- [Quick start](#quick-start)
- [Web dashboard — `mae-ui`](#web-dashboard--mae-ui)
- [The two files that are yours](#the-two-files-that-are-yours)
- [Flags and exit codes](#flags-and-exit-codes)
- [Per-repo settings — `.mae/config.json`](#per-repo-settings--maeconfigjson)
- [The loop](#the-loop)
- [Free models — no subscription](#free-models--no-subscription)
- [Roles, CLIs and prompts](#roles-clis-and-prompts)
- [Checkpoints](#checkpoints) · [The run log](#the-run-log) · [Cost and limits](#cost-and-limits)
- [Troubleshooting](#troubleshooting) · [Good practices](#good-practices)
- [Security](#security) · [Development](#development) · [License](#license)

---

## Example

A complete session, from zero to a branch with the work done. Copy and paste:

```bash
# 1. Install the engine (once)
pipx install git+https://github.com/MiltonJoel/MultiAgentEngine.git

# 2. Prepare the project you want to build
mkdir -p ~/code/myproject/.mae && cd ~/code/myproject
git init -q 2>/dev/null; echo ".mae/runs/" >> .gitignore

cat > .mae/spec.md <<'EOF'
# Spec
A FastAPI service with email/password login and a limit of 100 requests/minute
per user. Every endpoint has a pytest test.
EOF

cat > .mae/plan.md <<'EOF'
<!-- ENGINE-STATUS
Login flow: TODO
Rate limiting: TODO
-->
EOF

# 3. Run
mae

# 4. Review the result (the exact command is printed at the end)
git log --oneline --all --graph | head -20
```

Common variations:

```bash
mae --repo ~/code/myproject                              # from any folder
mae --free                                               # everything free, with a local model (Ollama)
mae --agent reviewer=claude --agent auditor=claude       # claude only, no codex
mae --gate "npm test" --gate "eslint . -f json"          # a JavaScript project
mae --gate ''                                            # no tests or lint
mae --allow-dir ~/code/reference-lib                     # the coder may read another repo
mae --max-iterations 30                                  # a plan with many units
mae --spec docs/architecture.md --plan docs/queue.md     # documents outside .mae/
mae --no-git                                             # no branch or commits
```

To ask for more work later, add or reopen a line in `.mae/plan.md` and run `mae` again:

```bash
sed -i.bak 's/^Rate limiting: DONE/Rate limiting: TODO — per user, not per IP/' .mae/plan.md && rm .mae/plan.md.bak
mae
```

---

## Quick start

### 1. Install once

You need Python 3.10 or later on **Linux or macOS** (on Windows, inside
[WSL](https://learn.microsoft.com/windows/wsl/)). Nothing else from the author's machine is
needed: everything the engine uses is in this repository.

To just use it (puts `mae` and `mae-ui` on your `PATH`):

```bash
pipx install git+https://github.com/MiltonJoel/MultiAgentEngine.git
```

To work on it, clone it anywhere and use a virtual environment:

```bash
git clone https://github.com/MiltonJoel/MultiAgentEngine.git
cd MultiAgentEngine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                               # the engine + pytest, ruff, mypy, build
python -m pytest -q                                   # everything should pass
```

If `pip install` fails to download `setuptools` (offline or behind a proxy), install `setuptools`
into the environment and retry with `pip install -e . --no-build-isolation`. With normal access to
PyPI this is not needed.

Check the tools it uses:

```bash
which mae pytest ruff git
which ollama              # free option
which claude codex        # paid option (subscription or key)
```

You need **at least one** model provider. Any of these works for any role, and you can mix them:

| Tool | Role | Notes |
|---|---|---|
| [Ollama](https://ollama.com) with a downloaded model | any (`--free` uses it for all) | **free**, local, no account or key: `ollama pull qwen2.5-coder:7b` |
| An OpenAI-compatible endpoint with a free tier | any (`api` runner) | e.g. OpenRouter (`:free` models), Groq, Google AI Studio; key in `MAE_API_KEY` |
| The `claude` CLI, logged in | the coder by default | paid; on the `PATH`, already authenticated |
| The `codex` CLI, logged in | reviewer and auditor by default | paid; on the `PATH`, already authenticated — or bind those roles to another runner |
| `pytest` | default test gate | runs inside the **target** repo; change it with `--gate` |
| `ruff` | default lint gate | skipped if not installed. **Do not use the snap build** — see [Troubleshooting](#troubleshooting) |
| `git` | checkpoints in the target repo | optional — without it there is no restore point |

With Ollama or the CLIs no API key is configured. Only the `api` runner uses one, and it reads it
from an environment variable, never from a file in the repo.

### 2. Put two files in the repo you want to build

```bash
cd ~/code/myproject
mkdir -p .mae
echo ".mae/runs/" >> .gitignore
```

**`.mae/spec.md`** — what the system must be. Any format, prose included. The engine never edits it.

```markdown
# Spec
A FastAPI service with email/password login, password reset with a token sent by email, and a
limit of 100 requests/minute per user. Postgres through SQLAlchemy. Every endpoint has a pytest test.
```

**`.mae/plan.md`** — the work queue. The block at the top is what the engine uses to decide what to
do. One line per unit of work:

```markdown
<!-- ENGINE-STATUS
Login flow: TODO
Password reset: TODO
Rate limiting: TODO
-->

# Notes
Anything that needs more than one line goes down here. The engine never parses it,
but the coder reads it.
```

#### Only have the task, no TODO list?

That is normal on the first run. Write the task and **leave the block out**:

```markdown
# Task
Implement the service described in spec.md: login, password reset and per-user rate limiting.
```

On the first pass the coder reads the spec and the task, writes the `ENGINE-STATUS` block with one
`TODO` line per unit, and moves them to `DONE` as it goes. At startup you will see
`This run: no task list yet — the coder writes it from the spec and plan`.

From the second run on the block exists: review it, fix names or split units if you don't like how
it came out, and carry on with the normal flow. If you prefer to control the split from the start,
write the `TODO` lines yourself.

### 3. Run it

```bash
mae --repo ~/code/myproject        # or cd into the folder and run: mae
```

Before starting any agent it prints what it is about to do — press Ctrl-C if something is wrong:

```
Plan: TODO=3
This run: Login flow, Password reset, Rate limiting
Log: ~/code/myproject/.mae/runs/20260913-101500.jsonl
Agents: coder=claude, reviewer=codex, auditor=codex
Gates: pytest -q --tb=line --no-header ; ruff check . --output-format json
[git] working on branch engine/20260913-101500
```

Then it iterates (coder → tests → reviewer or auditor → coder …) until every line is `DONE` or
`BLOCKED` and the audit comes back clean. A run can take hours; let it run.

### 4. Review what it did

At the end it prints the exact command:

```bash
git -C ~/code/myproject log --oneline <base>..engine/20260913-101500   # one commit per pass
git -C ~/code/myproject diff <base>                                     # the whole run
```

Merge the branch if you like it, or `git reset --hard <sha>` to any pass if you don't.

### 5. Ask for more

Add or reopen a line in the block and run `mae` again:

```
Rate limiting: TODO — the limit is per user, not per IP
Audit log for logins: TODO
```

That is the whole daily workflow: **edit the block, run `mae`, review the branch.**

---

## Web dashboard — `mae-ui`

Everything above can also be done from the browser:

```bash
mae-ui --repo ~/code/myproject          # opens http://127.0.0.1:8765/?token=…
mae-ui --port 9000 --no-browser         # another port; prints the URL without opening it
```

- **Run** — the coder → tester → reviewer/auditor graph with the active node live, passes, gate
  errors, audit result and cost; pick the runner and model for each role (the model list comes from
  the endpoint itself), gates, the pass ceiling and `--no-git`; start and stop (stop = Ctrl-C: the
  checkpoints are kept); a live console filterable by agent.
- **Plan & spec** — the units of the `ENGINE-STATUS` block in columns by state: add, change state or
  delete without touching the rest of the plan; editors for `spec.md`, `plan.md` and
  `.mae/config.json` (<kbd>Ctrl</kbd>+<kbd>S</kbd> saves).
- **Runs** — every log in `.mae/runs/` with its status, passes and cost; its timeline and the
  branch's checkpoint commits, each with its diff. **Clear** deletes the run logs (the log of a run
  in progress is kept); checkpoints are not touched.

The UI launches the same `mae` as a subprocess, one run at a time. It listens on `127.0.0.1` only,
and every start generates a token the API requires: open the URL it prints. It rejects requests
whose `Host` or `Origin` is not that local address (DNS-rebinding protection). With a repo open it
checks every runner, but **it does not send the API key** to an endpoint chosen by the repo's
`.mae/config.json`; the key only leaves at launch. See [SECURITY.md](SECURITY.md).

---

## The two files that are yours

They live **in the target repo**, not in this one. The engine ships no documents of its own, so one
install serves any number of projects.

```
<repo>/.mae/
├── spec.md        --spec (default)
├── plan.md        --plan (default)
├── config.json    per-repo settings (optional)
├── prompts/       per-repo prompt overrides (optional)
└── runs/          one JSONL log per run (add it to .gitignore)
```

They can live elsewhere too — with `--spec`/`--plan`, or with the `spec`/`plan` keys in `config.json`.

### `spec.md` — the architecture (frozen)

Requirements, interfaces, data models, diagrams, invariants. Only you edit it. It goes first in
every prompt and does not change between iterations, so it stays in the model's cached prefix and
costs ~10% after the first call.

### `plan.md` — the work queue (changes constantly)

It holds the **status block**, an HTML comment the engine parses:

```
<!-- ENGINE-STATUS
Login flow: DONE
Password reset: IN_PROGRESS
Rate limiting: TODO — changed the window to 60s
Payment webhook: BLOCKED missing the provider's sandbox keys
-->
```

Everything outside the block is yours; the coder is told not to touch it.

| State | Meaning | Engine behaviour |
|---|---|---|
| `TODO` | not started, or you reopened it | keeps working |
| `IN_PROGRESS` | started | keeps working |
| `DONE` | the code is written | leaves it alone |
| `BLOCKED` | needs a human (credentials, a product decision) | stops retrying — write the reason on the line |

States are always written in English and upper case; unit names can be in any language.

Important rules:

- One item per line, `<name>: <STATE>`. Text after the state is a free-form note and is kept.
- The name may contain colons and dashes (`Step 6 — Orders: state machine: DONE` parses fine).
- Order does not matter — the block is an index, not a sequence.
- A line that cannot be parsed **is reported at startup, not silently dropped**.
- Deleting `<!-- ENGINE-STATUS` or `-->` erases the engine's memory of what is done.
- A `TODO — audit: …` line was reopened by the auditor; the note describes the problem. Do not mark
  it `DONE` by hand without fixing it: the next audit would reopen it.

### Adding and changing work

**The spec is the content; the status block is the routing.** The coder reads the whole spec, but
only runs when the block has something open. Editing only the prose, with everything `DONE`,
changes nothing.

| You want | Do this |
|---|---|
| A small change | One line: `Idempotent retries in the sync job: TODO` |
| To correct finished work | Reopen it with a note: `Rate limiting: TODO — per user, not per IP`. The coder reads a `TODO` over existing code as "reconcile with the new note". |
| A new subsystem | Describe it in the spec (or below the block) and add its line. Use the same name in both places so the agent finds the detail. |
| A shorter block | Delete old `DONE` lines. Keep at least one — an empty block reads as "never started" and the coder rewrites the plan from scratch. |

---

## Flags and exit codes

| Flag | Meaning |
|---|---|
| `--repo DIR` | the repository being built (default `.`) |
| `--spec FILE` | architecture document (default `<repo>/.mae/spec.md`) |
| `--plan FILE` | work queue (default `<repo>/.mae/plan.md`) |
| `--allow-dir DIR` | extra directory the coder may read; repeatable |
| `--gate CMD` | validation command after every coder pass; repeatable, replaces the config list; `--gate ''` disables all gates |
| `--agent ROLE=RUNNER` | binds a role to `claude`, `codex`, `ollama` or `api`, e.g. `--agent reviewer=claude`; every runner takes a knob after `:` — a model for `claude`/`ollama`/`api` (`--agent coder=ollama:qwen2.5-coder:14b`, `--agent coder=claude:opus`) and an effort or model for `codex` (`--agent reviewer=codex:high`); repeatable |
| `--free` | every role on the free local Ollama model; `--agent` and `"agents"` still change single roles |
| `--max-iterations N` | ceiling on coder passes (default 15); must exceed the number of open units |
| `--log-dir DIR` | JSONL log directory (default `<repo>/.mae/runs`) |
| `--no-git` | skips the per-run branch and the per-iteration commits |
| `--version` | prints the version and exits |

| Code | Meaning |
|---|---|
| `0` | finished clean: the queue is closed, the gates are green and the audit found nothing |
| `1` | did not finish: the pass ceiling, an agent or a checkpoint failed, or the audit keeps finding problems |
| `2` | finished, but needs a human look: there are `BLOCKED` units, or no gate ran |
| `130` | interrupted with Ctrl-C |
| `143` | stopped with `SIGTERM` (`docker stop`, systemd, a cancelled CI job) |

On Ctrl-C or `SIGTERM` the engine kills the agent's process group, writes `aborted` to the log and
keeps the checkpoints; if the signal lands mid-commit it waits for the commit to finish so it never
leaves `.git/index.lock` behind — including the repo's own git hooks. Under `docker stop` or
systemd, allow a grace period longer than those hooks take (`docker stop -t 60`,
`TimeoutStopSec=60`): after it a `SIGKILL` arrives, which nothing can intercept.

Before starting it checks that each role's CLI and every configured gate tool exist (a relative gate
like `scripts/check.sh` is looked up inside the repo), that the prompts in `.mae/prompts/` can be
filled, and that the repo is not mid-merge/rebase or holding a stale `index.lock`. A configured gate
whose tool is missing is an error, not a silent pass, and one that disappears or times out mid-run
counts as red. If a checkpoint commit fails (git identity, a hook, signing) the run stops instead of
carrying on without rollback points.

After every coder pass the tester also checks the status block: a `-->` inside a note, a second
block, a misspelt state, or a unit that was open at the start and has disappeared turns the
iteration red, so the coder fixes it instead of the unit being silently lost.

One run per plan file: a second one exits at once instead of interleaving its writes in the same
status block.

`--allow-dir` matters more than it looks: the coder runs with `--repo` as its working directory and
**cannot read outside it** without this. The spec's and plan's directories are added automatically;
any reference code you add yourself:

```bash
mae --repo ~/code/myproject --allow-dir ~/code/reference-lib
```

---

## Per-repo settings — `.mae/config.json`

Optional. Saves typing flags on every run. Command-line flags take precedence.

```json
{
  "spec": "docs/architecture.md",
  "plan": "docs/queue.md",
  "gates": [
    "npm test -- --run",
    {"cmd": "tsc --noEmit", "ok_codes": [0, 1]},
    "eslint . -f json"
  ],
  "agents": {"reviewer": "claude", "auditor": "claude"},
  "llm": {"ollama": {"model": "qwen2.5-coder:14b"}},
  "max_iterations": 20,
  "allow_dirs": ["~/code/reference-lib"]
}
```

- `spec` / `plan` — relative to the repo; set them only to move the documents out of `.mae/`.
- `gates` — run in order. Each entry is a command (exit code 0 = pass), or `{"cmd", "ok_codes"}`
  when other codes also count as a pass. `[]` = no gates. Omitted = pytest, then ruff.
- `agents` — the roles are `coder`, `reviewer`, `auditor`; the runners are `claude`, `codex`,
  `ollama` and `api` (the last two with an optional model: `"ollama:llama3.1:8b"`).
- `llm` — where the free models are; see [Free models](#free-models--no-subscription).

A gate passed with `--gate` only accepts exit code 0. pytest exits 5 when it finds no tests, so in
a repo with no tests yet use the config form `{"cmd": "pytest -q --tb=line", "ok_codes": [0, 5]}`
(the default gates already do this).

---

## The loop

```
 spec + queue → CODER → TESTER → tests pass? ── no → REVIEWER → CODER
                                     │
                                    yes
                                     ↓
                        items still open? ── yes → CODER
                                     │
                                     no
                                     ↓
                         last audit clean? ── yes → END
                                     │
                                     no
                                     ↓
                                  AUDITOR ── findings? ── no → END
                                                  │
                                                 yes → reopen those units (TODO) → CODER
```

If two coder passes in a row change no file (with git on), the run stops instead of paying for more
empty passes.

Every return to the coder stops at `--max-iterations`. It is a ceiling, not a target — the loop
ends as soon as nothing is open and the audit comes back clean.

**CODER** (`claude`, may edit files and run commands, 90 min budget) — implements the open items,
writes tests for each one and runs only those tests; moves the line to `DONE` when they pass, so an
interrupted session loses nothing. Anything that needs data, network, credentials or a human
decision goes to `BLOCKED` with the reason, never to `DONE`. The full suite is the TESTER's job. It
is the only agent that writes code.

**TESTER** (no LLM, free) — runs the gates in order and turns their output into structured errors,
at most 20:

```json
[{"file": "app/auth/login.py", "line": 84, "description": "assert 0 == 2"}]
```

Parsing tries ruff/eslint JSON first, then `path:line[:col]: message` (pytest `--tb=line`, ruff,
mypy, tsc, eslint compact, `go vet`, gcc), and finally the last 800 characters of the raw output. An
unknown tool gives an imprecise error, but never breaks the loop. A tool that is not installed is
skipped. **It commits after every pass**, red ones included.

**REVIEWER** (`codex`, read-only) — runs **only when the build is red**. It sees the spec, the
queue, the errors and the diff of the pass that just failed; it returns the root causes as JSON:

```json
{"problems": [{"file": "...", "line": 84, "description": "why it fails",
               "suggested_fix": "what to change"}]}
```

**AUDITOR** (`codex`, read-only) — runs when the tests pass and the queue is closed. The coder grades
its own work, so the auditor reads the diff of the whole run against the spec and reports units
marked `DONE` that are half-done, contradict the spec, lack a test for a required behaviour, or
claim an exit criterion that cannot be met without data. It is scoped to the units that were open
when the run started.

Every finding names its unit, and **the engine reopens it**: the line becomes
`TODO — audit: <finding>`. The coder fixes it, the tests pass again and **the audit runs again**
until it comes back clean. Only an audit without findings ends the run. `BLOCKED` units are never
reopened.

The audit fails closed: a reply that cannot be read — prose only, a truncated or malformed findings
object — is never a clean audit. A reply whose findings list is empty but which wraps it in more
than a short note of prose is sent back once to the same agent to restate as JSON, so findings
written as prose cannot vanish.

Diffs sent to the reviewer and auditor go whole when short (under 2000 characters); when long, only
the `--stat` goes and the read-only agents open the changed files themselves.

---

## Free models — no subscription

The engine ships its own agent (`engine/llm.py`, standard library only) that talks to any
OpenAI-compatible endpoint and gives the model the same tools the CLIs have: list, read and search
files and, for the coder, write and edit files and run commands — so it can run the tests `DONE`
requires. The reviewer and auditor only get the read tools. Outside the repo it can only touch the
spec's and plan's directories and the `--allow-dir` ones.

### Local with Ollama (recommended, free)

```bash
# 1. Install Ollama: https://ollama.com/download (Linux: curl -fsSL https://ollama.com/install.sh | sh)
# 2. Download a model with tool support
ollama pull qwen2.5-coder:7b          # ~5 GB; with 16 GB+ of RAM try qwen2.5-coder:14b
# 3. Give it enough context (Ollama's default cuts long prompts)
OLLAMA_CONTEXT_LENGTH=32768 ollama serve
# 4. Run
mae --free
```

By default it uses `qwen2.5-coder:7b` at `http://localhost:11434/v1`. To change that:

```bash
mae --free --agent coder=ollama:qwen2.5-coder:14b      # a bigger model for the coder only
export MAE_OLLAMA_MODEL=llama3.1:8b                    # another default model
export MAE_OLLAMA_URL=http://other-machine:11434/v1    # Ollama on another machine
```

or in `.mae/config.json`: `"llm": {"ollama": {"url": "...", "model": "..."}}`. Before starting,
`mae` checks that Ollama answers and that the model is downloaded, and if not tells you what to run.

### A free API (the `api` runner)

Any OpenAI-compatible service. On a free tier you pay nothing; mind its per-minute limits (the
engine retries 429s, honouring `Retry-After`).

```json
{
  "agents": {"coder": "api", "reviewer": "api", "auditor": "ollama"},
  "llm": {"api": {"url": "https://openrouter.ai/api/v1",
                  "model": "qwen/qwen-2.5-coder-32b-instruct:free",
                  "key_env": "OPENROUTER_API_KEY"}}
}
```

`key_env` is the name of the environment variable holding the key (default `MAE_API_KEY`); the key
itself never goes in the file. `MAE_API_URL` and `MAE_API_MODEL` work too. For safety, `key_env` can
only be a `MAE_*` name or a known model-provider key (`OPENROUTER_API_KEY`, `GROQ_API_KEY`,
`OPENAI_API_KEY`, …); to use any other variable, allow it yourself in your environment:
`export MAE_ALLOWED_KEY_ENVS=MY_VARIABLE`. `url` must be `http(s)://`, the key never travels over
plain `http` except to this machine, never follows redirects and is never written to the log. At
startup `mae` prints which host each key goes to, with a `WARNING` when that host was chosen by the
repo's own config.

### What to expect

A local 7–14 B model is much slower and less capable than `claude` or `codex`: it does well on small,
well-specified units and struggles with large changes. Split the plan into small units, raise
`--max-iterations`, or mix: a free model for reviewer and auditor and a stronger one for the coder.
The cost recorded for these runners is 0; tokens are still counted.

In `mae-ui`, each role has a runner selector (`ollama`, `api`, `claude`, `codex`), a model selector
listing what the endpoint offers, and the **Use Ollama for all three** button.

---

## Roles, CLIs and prompts

Which CLI plays which role is configuration:

```bash
mae --agent reviewer=claude --agent auditor=claude    # claude only, no codex needed
```

Every function that launches a CLI takes the same arguments (`write`, `add_dirs`, `schema`,
`timeout`), so adding another CLI is one function plus one entry in `RUNNERS` in `engine/cli.py`.
Two limitations the engine warns about: `codex` has no `--add-dir`, and `claude` has no output
schema, so it is asked for JSON in the prompt.

Prompts are files: `engine/prompts/{coder,reviewer,auditor}.md`. To change one for a single
project, copy it to `<repo>/.mae/prompts/<role>.md` and edit it — the engine says when it uses an
override. Placeholders are `{name}`; an unknown one fails at startup with the list of available
names; a literal brace must be doubled `{{ }}`.

---

## Checkpoints

Before the first pass the engine creates a branch in the target repo and commits your uncommitted
work as the starting point, so nothing of yours is lost or mixed into the agent's diffs:

```
[git] working on branch engine/20260913-101500
[git] 37aeaf3 engine: iteration 1 — 3 error(s)
[git] a91c02b engine: iteration 2 — green
```

```bash
git -C <repo> diff HEAD~1          # what the last pass did
git -C <repo> reset --hard <sha>   # go back to any pass
```

Checkpoints never include `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `__pycache__/` or
`.mae/runs/`, even if the repo's `.gitignore` forgets them.

Not a git repo? The run works the same, without protection, and says so. `--no-git` turns it off on
purpose.

---

## The run log

One JSONL file per run in `<repo>/.mae/runs/`:

```bash
tail -f <repo>/.mae/runs/*.jsonl                                                      # live
jq -r 'select(.event=="agent") | "\(.label) \(.seconds)s $\(.cost_usd)"' <repo>/.mae/runs/*.jsonl
jq 'select(.event=="finish") | .usage' <repo>/.mae/runs/*.jsonl
```

Events: `start`, `agent` (per CLI call: seconds, tokens, cost), `gate`, `checkpoint`, `limit`
(usage-limit waits), `finish` / `aborted`. An interrupted run still writes `aborted` and keeps its
checkpoints.

---

## Cost and limits

The coder prompt puts the stable content first (spec + instructions, cached at ~10%) and the
variable content last (queue + findings, full price). Check that caching works in the progress line
— a high `cache_read` is what you want:

```
[coder] done — in=1204 cache_read=2610 out=880 cost=$0.0431
```

Only the agents' final answers enter the graph state, never their transcripts; the tester costs
nothing; the reviewer only runs when something fails and the auditor only with the queue closed
(repeating until clean).

If a CLI stops on a usage limit, the engine waits until the reset time shown in its output (30 min
if there is none), up to 4 times per call:

```
[coder] limit reached — sleeping 47 min, resuming ~15:32
```

Waits longer than 6 hours are refused with the reset date — a session limit is worth waiting out, a
monthly cap is not. Ctrl-C kills the agent's whole process group, and the next run continues from
the status block.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `spec file not found: <repo>/.mae/spec.md` | Create it, or point `--spec` / `"spec"` at yours. The `plan` version of this error prints a starter block to copy. |
| `has nothing left open — every item is DONE or BLOCKED` | Working as intended. Add a `TODO` line or reopen one. |
| `WARNING: unreadable status line, the engine ignores it` | A made-up state (`PENDING`) or a missing colon. Fix the line. |
| You wrote a new section but nothing runs | Prose takes no part in routing. Add a `<name>: TODO` line inside the block. |
| `unknown CLI 'x' for coder` / `unknown role` | A typo in `--agent` or `"agents"`. Roles: coder, reviewer, auditor. Runners: claude, codex, ollama, api. |
| `the claude CLI is not on PATH` and you have no subscription | Use `mae --free` (Ollama) or the `api` runner with a free tier. |
| `Ollama is not reachable at http://localhost:11434/v1` | Ollama is not installed or not running: install it and run `ollama serve`. |
| `Ollama has no model 'qwen2.5-coder:7b'` | `ollama pull qwen2.5-coder:7b`, or pick one you already have with `--agent ROLE=ollama:<model>`. |
| The local model answers without using tools or ignores the plan | Context too short: start Ollama with `OLLAMA_CONTEXT_LENGTH=32768`, or use a bigger model. |
| `llm.api.key_env: X is not a known model API key variable` | The repo's config names a variable that is not a model key. Use `MAE_API_KEY`, or allow it yourself: `export MAE_ALLOWED_KEY_ENVS=X`. |
| `... redirects (301) to https://...` | The endpoint redirects (often `http` → `https`). Put the final URL in the config or `MAE_*_URL`. |
| `another run already holds <plan>` | Another run is working on that plan. Wait for it or stop it. |
| Lint passes on everything | **snap** `ruff` cannot write its cache in `/tmp` and returns `[]` with exit 0. `snap remove ruff && pip install ruff`, or keep the target repo inside `$HOME`. |
| `codex ... failed to refresh available models` | Harmless; the engine hides the 325 KB log line. `npm i -g @openai/codex@latest` fixes it. |
| The coder did not update the plan | It could not reach the file. The spec and plan directories are allowed automatically; anything else needs `--allow-dir`. |
| The coder leaves units `IN_PROGRESS` because it "cannot run pytest" | An older engine version: `claude -p` in `acceptEdits` refuses Bash. The coder now gets `--allowedTools Bash`. |
| `Stopped early: 2 coder passes in a row changed nothing` | The coder is stuck. Read its last output: it usually asks for something (permission, data, a decision). |
| The loop ends with units still open | It hit `--max-iterations`. Raise it above the number of open units and run again — it continues where it left off. |
| `[auditor] reopened: …` | The auditor found problems in `DONE` units; the engine moved them back to `TODO` with the finding as the note. Expected: the coder fixes them and the audit runs again. |
| Ends at the ceiling with `audit_clean=False` | The auditor keeps finding problems. Read the `audit:` notes in the plan and the `problems` printed at the end; fix the spec if it asks for something impossible, or mark the unit `BLOCKED`. |

---

## Good practices

- Commit `<repo>/.mae/` with its project; add `.mae/runs/` to `.gitignore`.
- The status block is the only state that survives between runs; if an agent breaks it,
  `git diff` shows you.
- Delete old run branches once merged or discarded:
  `git -C <repo> branch -D $(git -C <repo> branch --list 'engine/*')`.

---

## Security

The engine runs agents that **execute commands on your machine** with your permissions. Before
using it with specs, plans or repositories that are not yours, read [SECURITY.md](SECURITY.md): it
explains the threat model (per-repo gates and prompts, the web dashboard, API keys, checkpoints) and
how to report a vulnerability privately.

In short: run other people's repos inside a container or VM, review their `.mae/config.json` and
`.mae/prompts/` before launching `mae`, and do not share the `mae-ui` URL (it contains the token).

---

## Development

```
MultiAgentEngine/
├── pyproject.toml       dependencies, `mae`/`mae-ui`, ruff, mypy, pytest and coverage
├── CHANGELOG.md         changes per version
├── CONTRIBUTING.md      how to set up and what a pull request needs
├── SECURITY.md          threat model and how to report a vulnerability
├── engine/
│   ├── __init__.py      __version__ (the single source of the version)
│   ├── __main__.py      `python -m engine`
│   ├── main.py          CLI parsing, config, validation, plan lock, run()
│   ├── graph.py         status-block parsing, routing, the LangGraph graph
│   ├── state.py         GraphState
│   ├── cli.py           claude/codex runners (RUNNERS), usage-limit waits
│   ├── llm.py           the built-in agent for free models (Ollama / OpenAI-compatible API)
│   ├── git.py           per-run branch, checkpoints, diffs
│   ├── prompts.py       prompt loading with per-repo overrides
│   ├── prompts/         coder.md, reviewer.md, auditor.md
│   ├── runlog.py        JSONL log, cost and token totals
│   ├── ui.py            the `mae-ui` server (stdlib), token-protected API
│   ├── ui.html          the `mae-ui` page, no dependencies
│   └── nodes/
│       ├── coder.py     implementation agent
│       ├── tester.py    deterministic gates and output parsing
│       └── reviewer.py  reviewer and auditor (shared schema, fail-closed parsing)
└── tests/
    ├── test_engine.py      plan parsing, routing, output parsing, log
    ├── test_modular.py     role→CLI, prompts, documents, gates, diffs, config
    ├── test_ops.py         checkpoints, plan lock, idle passes
    ├── test_llm.py         free models against a fake OpenAI-compatible server
    ├── test_robustness.py  plan, git and usage-limit edge cases, full runs
    ├── test_hardening.py   confinement, credentials, fail-closed audits, HTTP guards, signals
    └── test_ui.py          the UI API, token, a full run with a stub CLI
```

```bash
pip install -e ".[dev]"
ruff check . && mypy && python -m pytest -q --cov
```

GitHub Actions (`.github/workflows/tests.yml`) repeats this in a clean clone on Python 3.10 to 3.13
(Linux) and 3.12 (macOS) on every push and pull request, requires 80 % coverage, builds the package
and checks that the wheel installs. Pushing a `vX.Y.Z` tag makes `.github/workflows/release.yml`
publish it to PyPI.

Nothing inside `engine/` is project-specific. If you find yourself editing it to target a project,
that belongs in the target repo's `.mae/`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow, the project rules and how a release is cut,
and [CHANGELOG.md](CHANGELOG.md) for the history of changes.

---

## License

[MIT](LICENSE) © 2026 MiltonJoel
