# MultiAgentEngine

A LangGraph loop that drives two coding agents — the `claude` CLI and the `codex`
CLI — against a repository until an architecture spec is implemented, tested and
audited.

You write the spec and the work queue. The engine runs the agents, gates them on a
deterministic test step, keeps the work queue up to date, and stops when there is
nothing open left.

---

## Requirements

| Thing | Why | Notes |
|---|---|---|
| `claude` CLI, logged in | the coder | must be on `PATH`, already authenticated |
| `codex` CLI, logged in | the reviewer and auditor | must be on `PATH`, already authenticated |
| `pytest` | the default test gate | run inside the **target** repo; swap it with `--test-cmd` |
| `ruff` | the default lint gate | optional — skipped if not installed; swap it with `--lint-cmd` |
| `git` | checkpoints in the target repo | optional — without it the run has no rollback point |
| `pip install -e .` | `langgraph`, and the `mae` command | for this engine, not the target repo |

No API keys are configured here. The engine shells out to CLIs you have already
logged into.

```bash
pip install -e .     # or: pipx install -e .  → `mae` on PATH, runnable from any repo
```

---

## The two files you own

The engine deliberately splits what changes from what does not.

### `--spec` — the architecture (frozen)

Your blueprint: what the system must be. Requirements, interfaces, dataclasses,
diagrams, invariants. Any format — prose is fine.

**The engine never edits this file.** Only you do. It is sent first in every
prompt and does not change between iterations, so it sits in the model's cached
prefix and costs ~10% after the first call.

### `--plan` — the work queue (churns)

Holds the **status block**, an HTML comment the engine parses:

```
<!-- ENGINE-STATUS
Fase 1 — Event Bus: DONE
Fase 2 — InstrumentRegistry: IN_PROGRESS
Fase 3 — Broker Interface: TODO — cambié max_slippage a 0.001
Fase 4 — Trading Calendar: BLOCKED faltan credenciales PPI
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
- The name may contain colons and dashes (`Fase 6 — OMS: máquina de estados: DONE`
  parses correctly).
- Position and order are irrelevant — the block is an index, not a sequence.
- A line the parser cannot read is **reported, not silently dropped**.
- Deleting `<!-- ENGINE-STATUS` or `-->` wipes the engine's memory of what is done.

---

## Running it

```bash
mae --repo ~/Desktop/BOT-ARG        # or: python3 -m engine.main --repo ...
```

`--spec` and `--plan` default to `workspace/arquitectura.md` and
`workspace/trabajo.txt`, resolved from the project directory — so the command works
from any shell cwd, and you only pass them to point somewhere else.

| Flag | Meaning |
|---|---|
| `--spec FILE` | architecture doc (default: `workspace/arquitectura.md`) |
| `--plan FILE` | work queue with the status block (default: `workspace/trabajo.txt`) |
| `--repo DIR` | the repository being built (default `.`) |
| `--allow-dir DIR` | extra directory the coder may read; repeatable |
| `--test-cmd CMD` | test gate to run in the repo; `''` disables it |
| `--lint-cmd CMD` | lint gate to run in the repo; `''` disables it |
| `--max-iterations N` | ceiling on coder passes (default 15) |
| `--log-dir DIR` | where the JSONL run log goes (default `<plan dir>/runs`) |
| `--no-git` | skip the per-run branch and the per-iteration commits |
| `--archive` | compact the status block and exit; no agent runs |

Only one run per plan file: the second one exits immediately instead of
interleaving its writes into the same status block.

`--allow-dir` matters more than it looks: the coder runs with `--repo` as its
working directory and **cannot read or write outside it** otherwise. Point it at
any codebase your spec tells the agent to consult:

```bash
mae --repo ~/Desktop/BOT-ARG --allow-dir ~/Desktop/FreqBot
```

The spec's and plan's own directories are added automatically.

Before doing anything the engine prints what it thinks, so a bad edit costs a
Ctrl-C instead of a run:

```
Plan: BLOCKED=1 DONE=16 TODO=1
This run: Fase 11 — Reintentos idempotentes
```

---

## The loop

```
                   ┌──────────────────────────────────────────┐
                   ↓                                          │
 spec + queue → CODER → TESTER → tests pass? ── no → REVIEWER ─┘
                                     │                        │
                                     yes                 (iteration < 15)
                                     ↓
                              queue has open items? ── yes ────┘
                                     │
                                     no
                                     ↓
                              already audited? ── no → AUDITOR ── findings? ─┐
                                     │                                  yes  │
                                     yes                                ─────┘
                                     ↓                                   no
                                    END ←────────────────────────────────┘
```

**CODER** (`claude`, `acceptEdits`, 90 min) — implements open items and updates the
status block *incrementally*, flipping each unit to `DONE` as its code lands. It
does not run the tests; that is the tester's job. It is the only node that writes.

**TESTER** (no LLM, free) — runs the project's own gates (by default
`pytest -q --tb=line --no-header` and `ruff check . --output-format json`) and turns
their output into structured errors:

```json
[{"file": "argbot/oms/order_manager.py", "line": 84, "description": "assert 0 == 2"}]
```

Capped at 20 errors. `pytest` exit code 5 ("no tests collected") counts as a pass,
so a repo without tests doesn't loop forever. A missing tool is skipped, not failed.

The **commands are configuration, not code**, so the same engine drives a Python, a
JS or a Go repo: `--test-cmd "npm test"`, `--lint-cmd "eslint . -f json"`, or a
`.mae.json` in the target repo (below). Parsing tries, in order, ruff/eslint JSON,
then `path:line[:col]: message` — which covers pytest `--tb=line`, ruff, mypy, tsc,
eslint compact, `go vet` and gcc — then falls back to the last 800 characters of raw
output. An unrecognised tool degrades to a blunt error; it never breaks the loop.

It also **commits after every pass**, red ones included: each iteration becomes a
rollback point and a readable diff.

**REVIEWER** (`codex`, read-only) — runs **only on a red build**. Sees the spec,
the queue and the errors; returns root causes as JSON via `--output-schema`:

```json
{"problems": [{"file": "...", "line": 84, "description": "why it fails",
               "suggested_fix": "what to change"}]}
```

**AUDITOR** (`codex`, read-only) — runs **once per run**, when the tests pass and
the queue is closed. Checks the code against the spec, because the coder marked its
own homework and passing tests do not prove the spec was delivered. It looks for
units marked `DONE` that are stubbed, contradict the spec, or have no test covering
required behaviour. Its findings go back to the coder as `problems`.

The audit is **scoped to the units that were open when the run started**, so
adding one small item does not re-audit everything.

`--max-iterations` (default 15) bounds every edge back to the coder. It is a
ceiling, not a target — the loop exits as soon as nothing is open. It must exceed
the number of units in the plan.

---

## Checkpoints in the target repo

The coder writes for hours with `acceptEdits`. Before the first pass the engine
branches:

```
[git] working on branch engine/20260901-1939
[git] 37aeaf3 engine: iteration 1 — 3 error(s)
[git] a91c02b engine: iteration 2 — green
```

`checkout -b` keeps your working tree, so uncommitted work carries over and is
captured by the first checkpoint rather than lost. Afterwards:

```bash
git -C <repo> log --oneline main..engine/20260901-1939   # what the run did
git -C <repo> diff HEAD~1                                # what the last pass did
git -C <repo> reset --hard <sha>                         # undo back to any pass
```

If the target isn't a git repo the run still works, unprotected, and says so.
`--no-git` opts out deliberately.

---

## Per-repo settings — `.mae.json`

Drop it in the target repo so daily use across projects isn't a wall of flags:

```json
{
  "test_cmd": "npm test -- --run",
  "lint_cmd": "eslint . -f json",
  "max_iterations": 20,
  "allow_dirs": ["~/Desktop/FreqBot"]
}
```

CLI flags override it.

---

## The run log

Every run appends one JSONL file under `<plan dir>/runs/`, so a 90-minute night
run leaves something behind besides scrollback:

```bash
tail -f workspace/runs/20260901-193914.jsonl               # live
jq -r 'select(.event=="agent") | "\(.label) \(.seconds)s $\(.cost_usd)"' workspace/runs/*.jsonl
jq 'select(.event=="finish") | .usage' workspace/runs/*.jsonl
```

Events: `start`, `agent` (per CLI call: seconds, tokens, cost), `gate` (errors
found), `checkpoint` (sha), `limit` (rate-limit sleeps), `finish` / `aborted`.
A killed run still writes its `aborted` line and keeps its checkpoints.

---

## Adding and changing work

**The spec is the content; the status block is the routing.** The coder reads the
whole spec, but only runs if the block has something open. Editing prose alone,
with everything `DONE`, changes nothing — that is the one silent failure mode.

**A small change is one line.** Do not write a prose section for it:

```
Reintentos idempotentes en reconciliación: TODO
```

**A correction to finished work** — flip it back with a note saying what changed:

```
Fase 7 — Aggressive Limit Policy: TODO — max_slippage baja a 0.001, exige book de 3 niveles
```

The coder is told that a `TODO` whose code already exists means you edited
something, and to reconcile rather than assume it is finished.

**A genuinely new subsystem** — describe it in the spec (or below the block in the
work file) and add its line. Match the names so the agent can find the detail.

**When the block gets long:**

```bash
mae --archive
```

Moves `DONE` lines into `workspace/trabajo.txt.archive` with a timestamp. Deterministic, no
agent, no cost. Nothing is lost: moved lines are appended verbatim, and lines the
parser doesn't understand stay put. If everything was archived it leaves one
summary line behind — an empty block would read as "never started" and make the
coder rebuild the plan from scratch. The archive is never sent to any model.

---

## Cost behaviour

Roughly, for a ~10k-char spec:

```
coder prompt
├─ spec + instructions ── ~2,660 tokens ── identical every call → cached (~10%)
└─ queue + findings ────── ~450 tokens ── volatile, full price
```

Stable content is deliberately first and volatile content last. Watch the progress
line to confirm the cache is working:

```
[coder] done — in=1204 cache_read=2610 out=880 cost=$0.0431
```

A high `cache_read` means the split is paying off. Other cost decisions baked in:

- Only the agents' **final answers** enter the graph state, never their transcripts
  — otherwise each iteration's prompt would carry the previous one's full log.
- The tester is deterministic, so the loop's gate costs nothing.
- The reviewer runs only on failure; the auditor only once.
- Test output is capped so a traceback can't flood three prompts.

---

## Usage limits

If a CLI dies on a usage or rate limit, the engine sleeps until it resets and
retries — the run survives instead of dying halfway:

```
[coder] limit reached — sleeping 47 min, resuming ~15:32
```

- Only a **non-zero exit** is inspected, so an agent that merely *mentions* rate
  limits in its output can't trigger a spurious sleep.
- The reset time is read from the CLI output (unix epoch or `resets <ISO> UTC`);
  with no parseable time it waits 30 minutes.
- Waits longer than **6 hours** are refused — a session limit is worth waiting out,
  a monthly spend cap is not. You get the reset date instead.
- At most 4 waits per call.

Work is not lost if you kill the run: the code is on disk and the status block is
updated incrementally, so the next run picks up from there — and Ctrl-C kills the
agent's whole process group, so nothing keeps editing the repo behind your back.

---

## Files

```
MultiAgentEngine/
├── pyproject.toml           deps, and the `mae` entry point
├── conftest.py              puts the project root on sys.path for pytest
├── engine/                  the engine itself — nothing user-editable
│   ├── main.py              entry point: CLI, validation, run lock, --archive
│   ├── graph.py             LangGraph wiring, plan parsing, routing
│   ├── state.py             GraphState
│   ├── git.py               per-run branch, per-iteration checkpoints
│   ├── runlog.py            JSONL run log, cost and token totals
│   ├── cli.py               claude/codex wrappers, limit handling, output parsing
│   └── nodes/
│       ├── coder.py         implementation agent
│       ├── tester.py        deterministic, configurable test + lint gate
│       └── reviewer.py      failure analyst **and** auditor (shared schema)
├── workspace/               your documents — the only files you edit
│   ├── arquitectura.md      --spec  (default)
│   ├── trabajo.txt          --plan  (default)
│   ├── trabajo.txt.archive  written by --archive, never read by a model
│   ├── legacy/              pre-split originals, safe to delete
│   └── runs/                JSONL run logs (gitignored)
└── tests/
    ├── test_engine.py       plan parsing, routing, output parsing
    └── test_ops.py          checkpoints, run lock, per-repo config
```

Run the suite with:

```bash
python3 -m pytest tests/ -q
```

---

## Troubleshooting

**`codex ... failed to refresh available models: unknown variant 'max'`**
Harmless — codex falls back to defaults and exits 0. The engine suppresses the
(325 KB) log line and prints one note instead. Fix the cause with
`npm i -g @openai/codex@latest`.

**Lint silently passes on everything**
If `ruff` came from snap it cannot write its cache under `/tmp` and returns `[]`
with exit 0 — lint appears green while checking nothing. Install it with pip, or
keep the target repo under `$HOME`.

**The coder didn't update the plan**
Almost always a permissions issue: the plan lives outside `--repo` and its
directory wasn't allowed. The engine adds it automatically now; if the spec points
at another codebase, add it with `--allow-dir`.

**`has nothing left open — every item is DONE or BLOCKED`**
Working as intended. Add a line, or flip one back to `TODO`.

**An item vanished from the tally**
Its line didn't parse — an invented state (`PENDIENTE`) or a missing colon. The
engine warns about these at startup, before the run.

**You added a section but the engine says nothing is open**
You wrote prose below the block without adding a status line, so the engine can't
see it. The error lists the sections it found and the exact lines to paste:

```
These sections are written below the block but no status line mentions them,
so the engine cannot see them. Add a line inside <!-- ENGINE-STATUS ... --> for each:

    Capa de Persistencia: TODO
    Observabilidad y Logging Estructurado: TODO
```

---

## Suggested hygiene

Keep `workspace/` under git — it is committed with the engine. The status block is
the only state that survives between runs; if an agent ever mangles it, `git diff`
shows you in a second.

Delete stale `engine/*` branches in the target repo once you have merged or
discarded them: `git -C <repo> branch -D $(git -C <repo> branch --list 'engine/*')`.
