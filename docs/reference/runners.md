---
title: Roles, runners and prompts
---

# Roles, runners and prompts

The loop has three roles. A role is a job to do; a runner is the program or endpoint that does it. The
defaults are one runner per role, but you can mix them freely. Prompts are files, and each repository can
override them.

## Roles

| Role | Job | Writes? | Prompt |
|---|---|---|---|
| `coder` | Implements the open units, writes their tests, runs those tests, maintains the `ENGINE-STATUS` block. | Yes: files and shell commands. | `coder.md` |
| `reviewer` | Diagnoses a red build from the spec, the queue, the errors and the failing pass's diff. | No | `reviewer.md` |
| `auditor` | Checks `DONE` units against the spec after the queue closes; its findings reopen units. | No | `auditor.md` |

The coder is the only role that writes. The reviewer runs only when the gates are red. The auditor runs only
when the gates are green and the queue is closed, and the loop ends cleanly only after an audit with no
findings. See [The loop](../concepts/loop.md) for the routing.

## Runners

| Runner | Kind | Default for | Notes |
|---|---|---|---|
| `claude` | The `claude` CLI, already authenticated on this machine. | `coder` | Streams progress, reports tokens and cost. |
| `codex` | The `codex` CLI, already authenticated on this machine. | `reviewer`, `auditor` | Reports no cost or token counts. |
| `ollama` | A local Ollama server over its OpenAI-compatible API. | `--free` | No account, no key, no cost. |
| `api` | Any hosted OpenAI-compatible endpoint. | — | Reads its key from an environment variable. |

The four built-in names are the keys of `RUNNERS` in `engine/cli.py`. `ollama` and `api` are also in
`MODEL_RUNNERS`, the runners that talk HTTP and take an optional model.

### Knobs after `:`

Every binding is `ROLE=RUNNER` or `ROLE=RUNNER:KNOB`. The knob is everything after the first colon and must
be one word.

| Binding | What the knob does |
|---|---|
| `claude:opus` | Passed as `--model opus`. The dashboard offers `sonnet`, `opus` and `haiku` (`CLAUDE_MODELS`); any other model name is accepted and passed through. |
| `codex:high` | A reasoning effort: `low`, `medium` or `high` (`CODEX_EFFORTS`), passed as `-c model_reasoning_effort=high`. |
| `codex:<model>` | Anything that is not one of those three efforts is passed as `-m <model>`. |
| `ollama:qwen2.5-coder:14b` | The model requested from the Ollama endpoint. Overrides the config and environment defaults. |
| `api:qwen/qwen-2.5-coder-32b-instruct:free` | The model requested from the API endpoint. |

Internally, `_claude_model_flags` turns the knob into `--model <knob>`, and `_codex_model_flags` turns an
effort into `-c model_reasoning_effort=<knob>` or anything else into `-m <knob>`.

Examples:

```bash
mae --agent coder=claude:opus
mae --agent reviewer=codex:high --agent auditor=codex:high
mae --agent coder=ollama:qwen2.5-coder:14b --free
mae --agent reviewer=claude --agent auditor=claude
```

`--free` binds every role to `ollama`; config `agents` and `--agent` still override single roles. See
[Configuration](config.md) for the config file form.

### Defaults

```python
DEFAULT_AGENTS = {"coder": "claude", "reviewer": "codex", "auditor": "codex"}
FREE_AGENTS = {"coder": "ollama", "reviewer": "ollama", "auditor": "ollama"}
```

`DEFAULT_AGENTS` applies without `--free`. `FREE_AGENTS` is the base when `--free` is given, with the
Ollama model from `SETTINGS` (`qwen2.5-coder:7b` unless configured otherwise). Config `agents` overrides
either base, and `--agent` overrides the config per role.

## Timeouts

| Work | Timeout | Source |
|---|---|---|
| One coder pass | 5400 s (90 min) | `TIMEOUT` in `engine/nodes/coder.py`, passed explicitly to the runner. |
| One reviewer or auditor call | The runner's default: `claude` 600 s (10 min), `codex` 1200 s (20 min), `ollama`/`api` 1800 s (30 min). | `run_claude`, `run_codex`, `run_model` defaults. |
| One restatement call | The same runner default. | The `_settle` call passes no explicit timeout. |
| One gate | 900 s (15 min) | `GATE_TIMEOUT` in `engine/nodes/tester.py`. |
| One git command | 300 s | `TIMEOUT` in `engine/git.py`. |
| One model `run_command` | 900 s, capped by the remaining pass budget. | `COMMAND_TIMEOUT` in `engine/llm.py`. |
| A usage-limit wait | 30 min when the CLI gives no reset time, at most 4 waits per call; a wait longer than 6 h is refused. | `DEFAULT_WAIT`, `MAX_WAITS`, `MAX_SLEEP` in `engine/cli.py`. |

The coder's 5400-second budget applies whichever runner plays the coder. The reviewer and auditor use the
default of whichever runner they are bound to, so `--agent reviewer=claude` gives them 10 minutes, while
the default codex gives them 20.

## What each runner does

### `claude`

```text
claude -p --permission-mode <plan|acceptEdits> --output-format stream-json --verbose
```

- `write=True` uses `acceptEdits` and adds `--allowedTools Bash`, because a headless `acceptEdits` session
  would otherwise refuse the shell commands the coder needs to run tests. Read-only passes use `plan`.
- `--add-dir` is added for the spec's directory, the plan's directory and every `--allow-dir`, but only
  on the coder's pass — the reviewer and auditor get the repository only, since the spec and plan text is
  already in their prompt.
- A model knob becomes `--model`.
- The final answer is taken from the `result` event of the stream, not from the whole transcript.
- When the `result` event reports usage, the `agent` log event records `cost_usd`, `input_tokens`,
  `cache_read_input_tokens` and `output_tokens`.

### `codex`

```text
codex exec -C <repo> -s <read-only|workspace-write> --skip-git-repo-check -o <temp-file>
```

- `write=True` uses the `workspace-write` sandbox; read-only passes use `read-only`.
- A reasoning effort becomes `-c model_reasoning_effort=<effort>`; any other knob becomes `-m <model>`.
- A schema is written to a temporary file and passed with `--output-schema`.
- The final answer is read from the file passed to `-o`.
- Codex reports no cost or token counts of its own, so its `agent` events carry only timing and prompt
  size.

### `ollama` and `api`

Both use the built-in agent in `engine/llm.py`, a small tool-using loop over an OpenAI-compatible
`/chat/completions` endpoint. The model gets:

- `list_files`, `read_file` and `search` in every role;
- `write_file`, `replace_in_file` and `run_command` only when the pass is allowed to write, which means the
  coder.

The file tools are confined to the workspace. For the coder that is the repository, the spec's and plan's
directories and the `--allow-dir` directories; the read-only roles only get the repository. `run_command`
is a real shell with your user's permissions. The loop allows at most 200 model turns per call, clips large
tool results and ages older ones so a small context does not overflow. Cost is recorded as `0.0`; prompt
and completion tokens are counted.

## Known runner limitations

| Runner | Limitation |
|---|---|
| `codex` | No `--add-dir`. Extra directories are not readable, and the engine prints a warning when they are requested. |
| `codex` | No cost or token reporting, so a codex-only run has no usage totals. |
| `codex` | May print a large `failed to refresh available models` line; the engine filters it out. |
| `claude` | No output-schema flag. The JSON schema is restated in the prompt, and the reviewer/auditor may ask the agent once to restate an ambiguous reply as JSON. |
| `ollama` | The server must be running and the model pulled; startup checks both and tells you the `ollama pull` command. Small models are slower and less reliable with tools. Ollama's default context can cut long prompts; start it with a larger `OLLAMA_CONTEXT_LENGTH`. |
| `api` | Needs a URL, a model and a key; the key variable must pass the allow-list described in [Configuration](config.md). Plain `http` is allowed only to this machine, redirects are not followed, and 429/5xx responses are retried with back-off. |
| All model runners | 200 model turns per call; the coder's `run_command` runs with your user's rights. |
| All runners | Linux and macOS only. The `claude` and `codex` CLIs must be installed, on `PATH` and already logged in. |

## Prompts

The built-in prompts are `engine/prompts/coder.md`, `engine/prompts/reviewer.md` and
`engine/prompts/auditor.md`. A repository can override any of them by placing a file at
`<repo>/.mae/prompts/<role>.md`; the engine prints `[<role>] using prompt override <path>` when it does.

Each prompt is filled with placeholders. The table lists exactly what each one receives:

| Prompt | Placeholders |
|---|---|
| `coder` | `spec_path`, `spec`, `plan_path`, `plan`, `errors`, `problems` |
| `reviewer` | `spec`, `plan`, `errors`, `diff` |
| `auditor` | `spec`, `plan`, `focus`, `diff` |

- `spec` and `plan` are the file contents, re-read at each call. `spec_path` and `plan_path` are absolute
  paths, so the coder can point at them.
- `errors` is the tester's JSON list of `{file, line, description}`.
- `problems` is the reviewer's or auditor's JSON list of findings.
- `focus` scopes the audit to the units that were open when the run started; if the run started without a
  status block, it tells the auditor to cover every unit.
- `diff` is the diff against the previous pass (reviewer) or against the run's base commit (auditor). A diff
  longer than 2000 characters (`FULL_DIFF_CHARS` in `engine/git.py`) is replaced by its stat plus an
  instruction to read the changed files directly.

At startup every override is formatted with empty placeholders, so a typo fails before the run spends
anything. An unknown placeholder exits with the list of available names. A literal brace in a prompt must
be doubled: `{{ }}`. Overrides must be valid UTF-8.

## Adding a runner

A runner is a function with the same keyword-only arguments as the existing ones, plus one entry in
`RUNNERS`:

```python
def run_mycli(prompt, cwd, label, *, write=False, timeout=600, add_dirs=None, schema=None, model=""):
    """Run the CLI with the prompt on stdin; return only its final answer."""
    # build the argv: write mode, model, extra directories, schema file
    # start it in its own process group, stream its output, kill the group on timeout
    # raise RuntimeError or TimeoutError when the call cannot be completed
    ...
```

Then register it in `engine/cli.py`:

```python
RUNNERS = {"claude": run_claude, "codex": run_codex, "ollama": run_ollama, "api": run_api,
           "mycli": run_mycli}
```

A CLI runner is then available to `--agent ROLE=mycli`, to the config `agents` object and to the dashboard.
At startup the engine checks it with `shutil.which`; the runner itself must fail with `RuntimeError` or
`TimeoutError` when the call cannot be completed, which the engine reports as an aborted run.

Every runner should log an `agent` event with `runlog.log("agent", ...)` so the run log and its usage totals
include the call. The built-in runners already do this.

For an HTTP runner, also:

1. Add it to `MODEL_RUNNERS` in `engine/cli.py`.
2. Add a `SETTINGS` entry (`url`, `model`, `key_env`) and an `ENV` entry (`url`, `model` variable names) in
   `engine/llm.py`, and implement it through `run_model` or an equivalent loop. `run_model` logs the
   `agent` event for you; a custom loop must do it itself.

Prompts belong to roles, not runners, so a new runner for an existing role needs no new prompt. Keep a
runner read-only when `write` is false, kill its process group on timeout, and never let it write outside
`cwd` and `add_dirs`.

## Related pages

- [Command line](cli.md) — `--agent`, `--free` and startup validation.
- [Configuration](config.md) — the `agents` and `llm` sections.
- [Free models](../guide/free-models.md) — Ollama setup and hosted endpoints.
- [Cost and limits](../concepts/cost.md) — tokens, cost and usage-limit waits.
- [Contributing](../contributing.md) — the project's code and test conventions.
