# MultiAgentEngine

A LangGraph loop that drives two coding agents — the `claude` CLI and the `codex`
CLI — against a repository until an architecture spec is implemented, tested and
audited.

You write the spec and the work queue. The engine runs the agents, gates them on a
deterministic test step, keeps the work queue up to date, and stops when there is
nothing open left.

---

## Quick start

### 1. Install once

```bash
cd MultiAgentEngine
pip install -e .          # or: pipx install -e .  → puts `mae` on PATH
```

Check the tools it shells out to:

```bash
which mae claude codex pytest ruff git
```

| Thing | Role | Notes |
|---|---|---|
| `claude` CLI, logged in | the coder | on `PATH`, already authenticated |
| `codex` CLI, logged in | reviewer and auditor | on `PATH`, already authenticated — or bind those roles to `claude` |
| `pytest` | default test gate | runs inside the **target** repo; swap with `--gate` |
| `ruff` | default lint gate | skipped if missing. **Not the snap build** — see [Troubleshooting](#troubleshooting) |
| `git` | checkpoints in the target repo | optional — without it there is no rollback point |

No API keys are configured here. The engine uses CLIs you have already logged into.

### 2. Put two files in the repo you want built

```bash
cd ~/code/myproject
mkdir -p .mae
echo ".mae/runs/" >> .gitignore
```

**`.mae/spec.md`** — what the system must be. Any format, prose is fine. The engine
never edits it.

```markdown
# Spec
A FastAPI service with email/password login, password reset by emailed token,
and per-user rate limiting of 100 requests/minute. Postgres via SQLAlchemy.
Every endpoint has a pytest test.
```

**`.mae/plan.md`** — the work queue. The block at the top is what the engine routes
on. One line per unit of work:

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

### 3. Run it

```bash
mae --repo ~/code/myproject        # or cd there and just run: mae
```

It prints what it is about to do before any agent starts — Ctrl-C now if it is wrong:

```
Plan: TODO=3
This run: Login flow, Password reset, Rate limiting
Log: ~/code/myproject/.mae/runs/20260913-101500.jsonl
Agents: coder=claude, reviewer=codex, auditor=codex
Gates: pytest -q --tb=line --no-header ; ruff check . --output-format json
[git] working on branch engine/20260913-101500
```

Then it loops (coder → tests → reviewer or auditor → coder …) until every line is
`DONE` or `BLOCKED` and the audit comes back clean. A run can take hours; leave it.

### 4. Review what it did

At the end it prints the exact command:

```bash
git -C ~/code/myproject log --oneline <base>..engine/20260913-101500   # one commit per pass
git -C ~/code/myproject diff <base>                                     # the whole run
```

Merge the branch if you like it, `git reset --hard <sha>` to any pass if you don't.

### 5. Ask for more

Add or reopen a line in the block and run `mae` again:

```
Rate limiting: TODO — limit is per user, not per IP
Audit log for logins: TODO
```

That is the whole daily workflow: **edit the block, run `mae`, review the branch.**

---

## The two files you own

They live **in the target repo**, not in this one. The engine ships no documents of
its own, so one install drives any number of projects.

```
<repo>/.mae/
├── spec.md        --spec (default)
├── plan.md        --plan (default)
├── config.json    per-repo settings (optional)
├── prompts/       per-repo prompt overrides (optional)
└── runs/          JSONL run logs (gitignore this one)
```

Anywhere else works too — `--spec`/`--plan`, or the `spec`/`plan` keys in
`config.json`.

### `spec.md` — the architecture (frozen)

Requirements, interfaces, data models, diagrams, invariants. Only you edit it. It is
sent first in every prompt and does not change between iterations, so it sits in the
model's cached prefix and costs ~10% after the first call.

### `plan.md` — the work queue (churns)

Holds the **status block**, an HTML comment the engine parses:

```
<!-- ENGINE-STATUS
Login flow: DONE
Password reset: IN_PROGRESS
Rate limiting: TODO — changed the window to 60s
Payment webhook: BLOCKED need the provider's sandbox keys
-->
```

Everything outside the block is yours; the coder is told not to touch it.

| State | Meaning | Engine behaviour |
|---|---|---|
| `TODO` | not started, or you reopened it | keeps working |
| `IN_PROGRESS` | started | keeps working |
| `DONE` | code is written | leaves it alone |
| `BLOCKED` | needs a human (credentials, a product decision) | stops retrying — put the reason on the line |

Rules that matter:

- One item per line, `<name>: <STATE>`. Text after the state is a free-form note
  and is preserved.
- The name may contain colons and dashes (`Step 6 — Orders: state machine: DONE`
  parses correctly).
- Order is irrelevant — the block is an index, not a sequence.
- A line the parser cannot read is **reported at startup, not silently dropped**.
- Deleting `<!-- ENGINE-STATUS` or `-->` wipes the engine's memory of what is done.

### Adding and changing work

**The spec is the content; the status block is the routing.** The coder reads the
whole spec, but only runs if the block has something open. Editing prose alone,
with everything `DONE`, changes nothing.

| You want | Do this |
|---|---|
| A small change | One line: `Idempotent retries on the sync job: TODO` |
| To correct finished work | Flip it back with a note: `Rate limiting: TODO — per user, not per IP`. The coder treats a `TODO` whose code exists as "reconcile with the new note". |
| A new subsystem | Describe it in the spec (or below the block), then add its line. Use the same name in both so the agent can find the detail. |
| A shorter block | Delete old `DONE` lines. Keep at least one line — an empty block reads as "never started" and the coder rebuilds the plan from scratch. |

---

## Flags

| Flag | Meaning |
|---|---|
| `--repo DIR` | the repository being built (default `.`) |
| `--spec FILE` | architecture doc (default `<repo>/.mae/spec.md`) |
| `--plan FILE` | work queue (default `<repo>/.mae/plan.md`) |
| `--allow-dir DIR` | extra directory the coder may read; repeatable |
| `--gate CMD` | gate command run after every coder pass; repeatable, replaces the config list; `--gate ''` disables all gates |
| `--agent ROLE=CLI` | bind a role to a CLI, e.g. `--agent reviewer=claude`; repeatable |
| `--max-iterations N` | ceiling on coder passes (default 15); must exceed the number of open units |
| `--log-dir DIR` | JSONL run log directory (default `<repo>/.mae/runs`) |
| `--no-git` | skip the per-run branch and per-iteration commits |

Only one run per plan file: a second one exits immediately instead of interleaving
its writes into the same status block.

`--allow-dir` matters more than it looks: the coder runs with `--repo` as its
working directory and **cannot read outside it** otherwise. The spec's and plan's
directories are added automatically; add any reference codebase yourself:

```bash
mae --repo ~/code/myproject --allow-dir ~/code/reference-lib
```

---

## Per-repo settings — `.mae/config.json`

Optional. Saves typing flags on every run. CLI flags override it.

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
  "max_iterations": 20,
  "allow_dirs": ["~/code/reference-lib"]
}
```

- `spec` / `plan` — resolved against the repo; set only to move them out of `.mae/`.
- `gates` — run in order. An entry is a command string (exit 0 = pass), or
  `{"cmd", "ok_codes"}` when other exit codes also count as a pass. `[]` = no gates.
  Omitted = pytest then ruff.
- `agents` — roles are `coder`, `reviewer`, `auditor`; CLIs are `claude`, `codex`.

A gate passed with `--gate` only accepts exit 0. pytest exits 5 when it collects no
tests, so for a repo without tests yet use the config form
`{"cmd": "pytest -q --tb=line", "ok_codes": [0, 5]}` (the default gates already do this).

---

## The loop

```
                   ┌──────────────────────────────────────────┐
                   ↓                                          │
 spec + queue → CODER → TESTER → tests pass? ── no → REVIEWER ─┘
                                     │
                                     yes
                                     ↓
                              queue has open items? ── yes → CODER
                                     │
                                     no
                                     ↓
                              already audited? ── no → AUDITOR ── findings? ── yes → CODER
                                     │                                  │
                                     yes                                no
                                     ↓                                  ↓
                                    END ←───────────────────────────────┘
```

Every edge back to the coder stops at `--max-iterations`. It is a ceiling, not a
target — the loop exits as soon as nothing is open.

**CODER** (`claude`, can edit files, 90 min budget) — implements open items and
flips each line to `DONE` as its code lands, so a killed session loses nothing. It
does not run the tests. It is the only node that writes.

**TESTER** (no LLM, free) — runs the gates in order and turns their output into
structured errors, capped at 20:

```json
[{"file": "app/auth/login.py", "line": 84, "description": "assert 0 == 2"}]
```

Parsing tries ruff/eslint JSON, then `path:line[:col]: message` (pytest `--tb=line`,
ruff, mypy, tsc, eslint compact, `go vet`, gcc), then the last 800 characters of raw
output. An unknown tool degrades to a blunt error; it never breaks the loop. A tool
that isn't installed is skipped. It **commits after every pass**, red ones included.

**REVIEWER** (`codex`, read-only) — runs **only on a red build**. Sees the spec,
the queue, the errors and the diff of the pass that just failed; returns root causes
as JSON:

```json
{"problems": [{"file": "...", "line": 84, "description": "why it fails",
               "suggested_fix": "what to change"}]}
```

**AUDITOR** (`codex`, read-only) — runs **once per run**, when tests pass and the
queue is closed. The coder marked its own homework, so the auditor reads the diff of
the whole run against the spec and reports units marked `DONE` that are stubbed,
contradict the spec, or have no test for required behaviour. Scoped to the units that
were open when the run started, so one small addition doesn't re-audit everything.
Findings go back to the coder.

Diffs sent to the reviewer and auditor are capped at 20 000 characters, `--stat` first.

---

## Roles, CLIs and prompts

Which CLI fills a role is configuration:

```bash
mae --agent reviewer=claude --agent auditor=claude    # claude only, no codex needed
```

Every runner takes the same arguments (`write`, `add_dirs`, `schema`, `timeout`), so
adding a third CLI is one function plus one entry in `RUNNERS` in `engine/cli.py`.
Two caveats the engine warns about: `codex` has no `--add-dir`, and `claude` has no
output schema, so it is asked for JSON in the prompt instead.

The prompts are files: `engine/prompts/{coder,reviewer,auditor}.md`. To change one
for a single project, copy it to `<repo>/.mae/prompts/<role>.md` and edit — the
engine prints when it uses an override. Placeholders are `{name}`; an unknown one
fails at startup with the list of available names; a literal brace must be doubled
`{{ }}`.

---

## Checkpoints

Before the first pass the engine branches in the target repo and commits any
uncommitted work as a baseline, so nothing of yours is lost or mixed into the
agent's diffs:

```
[git] working on branch engine/20260913-101500
[git] 37aeaf3 engine: iteration 1 — 3 error(s)
[git] a91c02b engine: iteration 2 — green
```

```bash
git -C <repo> diff HEAD~1          # what the last pass did
git -C <repo> reset --hard <sha>   # undo back to any pass
```

Not a git repo? The run still works, unprotected, and says so. `--no-git` opts out.

---

## The run log

One JSONL file per run under `<repo>/.mae/runs/`:

```bash
tail -f <repo>/.mae/runs/*.jsonl                                                      # live
jq -r 'select(.event=="agent") | "\(.label) \(.seconds)s $\(.cost_usd)"' <repo>/.mae/runs/*.jsonl
jq 'select(.event=="finish") | .usage' <repo>/.mae/runs/*.jsonl
```

Events: `start`, `agent` (per CLI call: seconds, tokens, cost), `gate`, `checkpoint`,
`limit` (rate-limit sleeps), `finish` / `aborted`. A killed run still writes
`aborted` and keeps its checkpoints.

---

## Cost and limits

The coder prompt puts stable content first (spec + instructions, cached at ~10%) and
volatile content last (queue + findings, full price). Confirm the cache is working
from the progress line — a high `cache_read` is what you want:

```
[coder] done — in=1204 cache_read=2610 out=880 cost=$0.0431
```

Only agents' final answers enter the graph state, never their transcripts; the
tester costs nothing; the reviewer runs only on failure and the auditor only once.

If a CLI dies on a usage or rate limit, the engine sleeps until the reset time in
its output (30 min if none), up to 4 times per call:

```
[coder] limit reached — sleeping 47 min, resuming ~15:32
```

Waits over 6 hours are refused with the reset date — a session limit is worth
waiting out, a monthly cap is not. Ctrl-C kills the agent's whole process group, and
the next run resumes from the status block.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `spec file not found: <repo>/.mae/spec.md` | Create it, or point `--spec` / `"spec"` at yours. The `plan` version of this error prints a starter block to paste. |
| `has nothing left open — every item is DONE or BLOCKED` | Working as intended. Add a `TODO` line or reopen one. |
| `WARNING: unreadable status line, it will be ignored` | An invented state (`PENDING`) or a missing colon. Fix the line. |
| You wrote a new section but nothing runs | Prose is invisible to routing. Add a `<name>: TODO` line inside the block. |
| `unknown CLI 'x' for coder` / `unknown role` | Typo in `--agent` or `"agents"`. Roles: coder, reviewer, auditor. CLIs: claude, codex. |
| `another run already holds <plan>` | A run is still going on that plan. Wait, or kill it. |
| Lint passes on everything | `ruff` from **snap** can't write its cache under `/tmp` and returns `[]` with exit 0. `snap remove ruff && pip install ruff`, or keep the target repo under `$HOME`. |
| `codex ... failed to refresh available models` | Harmless; the engine hides the 325 KB log line. `npm i -g @openai/codex@latest` fixes it. |
| The coder didn't update the plan | It couldn't reach the file. Spec/plan directories are allowed automatically; anything else needs `--allow-dir`. |
| Loop ends with units still open | Hit `--max-iterations`. Raise it above the number of open units and re-run — it resumes. |

---

## Hygiene

- Commit `<repo>/.mae/` with its project; gitignore `.mae/runs/`.
- The status block is the only state between runs; if an agent mangles it, `git diff`
  shows you.
- Delete old run branches once merged or discarded:
  `git -C <repo> branch -D $(git -C <repo> branch --list 'engine/*')`.

---

## Working on the engine itself

```
MultiAgentEngine/
├── pyproject.toml       deps, and the `mae` entry point
├── conftest.py          puts the project root on sys.path for pytest
├── engine/
│   ├── main.py          CLI parsing, config, validation, run lock, run()
│   ├── graph.py         status-block parsing, routing, LangGraph wiring
│   ├── state.py         GraphState
│   ├── cli.py           claude/codex runners (RUNNERS), rate-limit sleeps
│   ├── git.py           per-run branch, checkpoints, diffs
│   ├── prompts.py       prompt loading with per-repo override
│   ├── prompts/         coder.md, reviewer.md, auditor.md
│   ├── runlog.py        JSONL run log, cost and token totals
│   └── nodes/
│       ├── coder.py     implementation agent
│       ├── tester.py    deterministic gates and output parsing
│       └── reviewer.py  reviewer and auditor (shared schema)
└── tests/
    ├── test_engine.py   plan parsing, routing, output parsing, run log
    ├── test_modular.py  role→CLI, prompts, documents, gates, diffs, config
    └── test_ops.py      checkpoints, run lock
```

```bash
python3 -m pytest -q && ruff check .
```

Nothing under `engine/` is project-specific. If you find yourself editing it to point
at a project, that belongs in the target repo's `.mae/` instead.
