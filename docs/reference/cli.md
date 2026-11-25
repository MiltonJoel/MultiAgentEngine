---
title: Command line
---

# Command line

`mae` runs one engine session against a repository. `mae-ui` serves a local dashboard that launches the
same `mae` as a subprocess. Both commands are installed as console scripts and can also be run as modules.

## `mae`

```bash
mae --repo ~/code/myproject
python -m engine --repo ~/code/myproject     # the same entry point
```

### Flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `-h`, `--help` | — | — | Print the help text and exit. |
| `--version` | — | — | Print the installed version (`engine.__version__`) and exit. |
| `--spec FILE` | path | `<repo>/.mae/spec.md` | Architecture document. The engine reads it but never edits it. A config key `spec` can set the default. |
| `--plan FILE` | path | `<repo>/.mae/plan.md` | Work queue holding the `ENGINE-STATUS` block. A config key `plan` can set the default. |
| `--repo REPO` | path | `.` | Target repository. Expanded (`~`) and resolved before use. |
| `--allow-dir DIR` | path, repeatable | empty | Extra directory the coder may read. May be given more than once. |
| `--gate CMD` | string, repeatable | config `gates`, otherwise pytest then ruff | Gate command run in the repository after every coder pass. Repeating the flag replaces the whole list. `--gate ''` disables all gates. |
| `--agent ROLE=CLI` | string, repeatable | coder=`claude`, reviewer=`codex`, auditor=`codex` | Bind a role to a runner, optionally with a knob after `:`. See [Agent bindings](#agent-bindings). |
| `--free` | flag | off | Start from the free local Ollama model (`qwen2.5-coder:7b` by default) for every role. Config `agents` and `--agent` still override individual roles. |
| `--max-iterations N` | integer | `15` | Ceiling on coder passes. It is a ceiling, not a target; the loop can end earlier. |
| `--log-dir DIR` | path | `<repo>/.mae/runs` | Directory for the JSONL run log. |
| `--no-git` | flag | off | Skip the per-run branch and the per-iteration commits, so there are no rollback points. |

`--spec` and `--plan` paths given on the command line are resolved from the directory you launch `mae` in.
Relative paths from `.mae/config.json` are resolved from the repository. In both cases `~` is expanded and
the file must already exist.

!!! note "One run per plan"
    The engine takes an exclusive advisory lock on the plan file. A second `mae` pointed at the same plan
    exits at once with code `3` and `another run already holds <plan> — wait for it or kill it`. The lock is advisory,
    so a human editor is unaffected.

### Agent bindings

The syntax is `ROLE=RUNNER` or `ROLE=RUNNER:KNOB`.

| Part | Values |
|---|---|
| `ROLE` | `coder`, `reviewer`, `auditor` |
| `RUNNER` | `claude`, `codex`, `ollama`, `api` |
| `KNOB` | a claude model, a codex reasoning effort or model, or an ollama/api model |

The knob is everything after the first `:`, so model names that themselves contain colons work. It must be
one word with no whitespace.

```bash
mae --agent coder=claude:opus
mae --agent reviewer=codex:high
mae --agent coder=ollama:qwen2.5-coder:14b
mae --agent auditor=api:qwen/qwen-2.5-coder-32b-instruct:free
mae --agent reviewer=claude --agent auditor=claude    # claude only, no codex
```

Bindings are applied in this order, each step overriding the previous one for a role:

1. `DEFAULT_AGENTS` (or `FREE_AGENTS` when `--free` is given)
2. the `agents` object in `.mae/config.json`
3. each `--agent` flag, in the order given

A repeated `--agent` for the same role therefore keeps the last one. Roles and runners are validated before
the run starts; see [Startup checks](#startup-checks).

### Exit codes

| Code | Constant | Meaning |
|---|---|---|
| `0` | `EXIT_CLEAN` | Every unit is `DONE`, at least one gate ran and passed, and the audit found nothing. |
| `1` | `EXIT_FAILED` | The run did not finish: it stopped at the pass ceiling or idle limit before a clean end, an agent or checkpoint failed, or the audit was not clean. |
| `2` | `EXIT_NEEDS_REVIEW` | The queue is closed and the audit is clean, but a human should look: some units are `BLOCKED`, or no gate ran at all. |
| `3` | `EXIT_REFUSED` | The run refused to start. No agent ran, no log was written, no branch was created and nothing was spent. The message on stderr says why. |
| `130` | `EXIT_INTERRUPTED` | Interrupted with Ctrl-C (`SIGINT`). |
| `143` | `EXIT_TERMINATED` | Stopped with `SIGTERM` (`docker stop`, systemd, a cancelled CI job). |

A run is "finished" only when the tester passed, the auditor was clean and the plan has nothing open.
`BLOCKED` counts as closed for that test, but it adds the caveat `BLOCKED: <units>` and turns the exit code
into `2`. So does a run in which the gate list was empty: `no gate ran — green checked nothing`.

!!! note "Every refusal exits 3"
    Anything that stops `mae` before the first agent starts exits with `3`, and prints `mae: error: <message>`
    on stderr. That covers bad arguments (an unknown flag, a bad value, an unknown role or runner, a missing
    gate tool, a bad `--max-iterations`, an `--allow-dir` that is not a directory, a repo mid-merge or
    mid-rebase, a plan with nothing open, a `--log-dir` that cannot be created, …), an invalid, ill-typed or
    unreadable `.mae/config.json`, an unreadable plan or prompt override, a broken prompt placeholder, a
    symlink loop in a path, an unwritable plan, a plan another run already holds, and running on Windows. No
    log file is written, no branch is created and nothing is spent. `--help` and `--version` exit `0`.

    One failure happens after the branch exists but still before any agent: the baseline commit of your
    uncommitted work (a failing pre-commit hook, missing git identity, signing). It exits `1`, logs an
    `aborted` event, and prints the command to delete the empty branch.

    Exit `1` only ever means a run that started and did not finish, and `2` only means a finished run that
    needs review.

On Ctrl-C or `SIGTERM` the engine kills the agent's process group, logs `aborted`, keeps the checkpoints and
exits with the matching code. A signal that arrives during a git checkpoint waits for the checkpoint to
finish (each git call is bounded by its 300-second timeout), so it does not leave `.git/index.lock` behind.

### Startup checks

`main()` validates the whole run before the first agent is called. In order. Any failure in these checks exits
with `3` (refused to start), and none of them starts a run:

1. **Platform** — Linux or macOS only. On Windows it exits with `mae runs on Linux and macOS only (it
   manages process groups and POSIX file locks) — on Windows, run it inside WSL`.
2. **Repository** — `--repo` must be a directory: `--repo is not a directory: <path>`.
3. **Config** — `.mae/config.json` must be a JSON object; the keys it validates (`spec`, `plan`, `gates`,
   `agents`, `llm`, `allow_dirs`) must have the right types. It prints `Config: <path>` when the file
   exists.
4. **Documents** — the spec and plan must exist and must be different files. A missing document produces
   `spec file not found: <path>` (or `plan file not found: <path>`) with the places to put it; for a plan it
   also prints a starter status block.
5. **Gates** — every configured gate command is split with `shlex.split`, and its first word is looked up.
   A relative tool with a slash (`scripts/check.sh`) is looked up inside the repository, anything else on
   `PATH`. A **configured** gate whose tool is missing is an error. A **default** gate whose tool is missing
   is dropped with `Gate skipped, <tool> is not installed: <cmd>`. A snap build prints a warning.
6. **LLM settings** — the `llm` section is validated: known runners, known settings, a valid `key_env`, an
   `http(s)://` URL. See [Configuration](config.md).
7. **Agents** — every role must be known, every runner must be in `RUNNERS`, and a knob must be one word.
   For `ollama` and `api`, `llm.check()` contacts the endpoint and verifies the URL, the model, the key and
   (for Ollama) that the model is downloaded. For `claude` and `codex`, the CLI must be on `PATH`. When a
   key is configured, it prints which host the key goes to, with a `WARNING` when that host was chosen by
   the repository's own config.
8. **Iterations** — `--max-iterations` or the config value must be a positive integer. Booleans are
   rejected.
9. **Allowed directories** — every `--allow-dir` and `allow_dirs` entry must be an existing directory.
10. **Plan warnings** — unreadable status lines, multiple `ENGINE-STATUS` blocks and an early `-->` are
    printed as `WARNING: ...`. They do not stop the run. A unit that disappears later is caught by the
    tester and makes that pass red.
11. **Prompt overrides** — every `.mae/prompts/<role>.md` is formatted with empty placeholders, so a bad
    placeholder fails now rather than hours later.
12. **Git state** — unless `--no-git` is set, and only when the target is a git repository, a merge,
    rebase, cherry-pick or stale `index.lock` stops the run with `<repo>: <reason> — finish or abort it
    first, or run with --no-git`.
13. **Plan not finished** — a plan whose every unit is `DONE` or `BLOCKED` is refused: `<plan> has nothing
    left open — every item is DONE or BLOCKED. Add a `<name>: TODO` line inside the ENGINE-STATUS block.`
14. **Plan lock** — the plan must be writable and no other run may hold it.

Only then does `run()` print the plan tally, the units for this run, the log path, the agent bindings and
the gates, create the git branch and baseline commit, log `start`, and invoke the graph.

### What the run prints

```text
Plan: TODO=3
This run: Login flow, Password reset, Rate limiting
Log: /home/you/code/myproject/.mae/runs/20260913-101500.jsonl
Agents: coder=claude, reviewer=codex, auditor=codex
Gates: pytest -q --tb=line --no-header ; ruff check . --output-format json
[git] working on branch engine/20260913-101500
```

At the end it prints the iteration count, `tests_passed`, `audit_clean`, the final plan tally, the usage
summary and, when checkpoints exist, the `git log` command to review the branch. When there are errors or
audit problems it prints them as JSON.

## `mae-ui`

```bash
mae-ui --repo ~/code/myproject
mae-ui --port 9000 --no-browser
python -m engine.ui --repo ~/code/myproject     # the same entry point
```

### Flags

| Flag | Type | Default | Description |
|---|---|---|---|
| `-h`, `--help` | — | — | Print the help text and exit. |
| `--version` | — | — | Print the installed version and exit. |
| `--repo REPO` | path | `.` | Repository to open first. Expanded and resolved. |
| `--port PORT` | integer | `8765` | Port to listen on. `0` lets the operating system choose a free port. |
| `--no-browser` | flag | off | Do not open a browser. The URL is still printed. |

The server binds `127.0.0.1` only. Each launch generates a random token and prints a URL of the form
`http://127.0.0.1:<port>/?token=<token>#<repo>`; every API call must carry that token, and requests whose
`Host` or `Origin` is not the loopback address are rejected. Do not share the URL: it contains the token.

The dashboard runs one engine subprocess at a time and launches it with the Python interpreter that runs
`mae-ui`: `python -u -m engine.main --repo <repo>` plus the flags chosen in the browser (`--agent`,
`--gate`, `--max-iterations`, `--no-git`). Ctrl-C in the terminal stops a running engine as well.

## Related pages

- [Configuration](config.md) — `.mae/config.json` keys, gates and the `llm` section.
- [Roles, runners and prompts](runners.md) — what each runner does and how to bind it.
- [Run log](run-log.md) — the JSONL events and `jq` recipes.
- [CI, Docker and signals](../guide/ci.md) — using the exit codes in a pipeline.
