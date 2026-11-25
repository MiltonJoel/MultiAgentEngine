---
title: Run log
---

# Run log

Every run appends a JSONL file: one JSON object per line, one line per event. You can `tail -f` it while
the engine works, read it with `jq` afterwards, or load it into the dashboard's History view.

## Location and naming

The default directory is `<repo>/.mae/runs/`; `--log-dir` moves it. The engine creates the directory and a
`.gitignore` containing `*` inside it, and checkpoint commits exclude `.mae/runs/**` regardless of the
target repository's own `.gitignore`.

The file is named from the local start time:

```text
<repo>/.mae/runs/20260913-101500.jsonl
```

Two runs that start in the same second do not share a file: the second gets a `-<pid>-<n>` suffix, for
example `20260913-101500-48213-1.jsonl`. The log is opened with exclusive create, so a collision is retried
rather than appended to.

Each record has two fields of its own:

| Field | Type | Meaning |
|---|---|---|
| `ts` | string | Local time with UTC offset, seconds precision, e.g. `2026-09-13T10:15:00+02:00`. |
| `event` | string | The event name; see the table below. |

## Events

| Event | When it is written | Fields |
|---|---|---|
| `start` | After the branch and baseline commit, before the graph runs. | `repo`, `plan`, `spec`, `branch`, `base_sha`, `focus`, `agents`, `max_iterations`, `gates` |
| `agent` | After a CLI attempt, failed attempts included; once at the end of an `ollama`/`api` call. | `label`, `agent`, `seconds`, `prompt_chars`; runner-specific usage fields |
| `gate` | After the gates run for a coder pass. | `iteration`, `errors`, `failures` |
| `checkpoint` | When the tester commits a pass and git checkpoints are on. | `iteration`, `sha`, `status` |
| `limit` | When a usage limit or HTTP back-off forces a wait. | `label`, `sleep_min`; `resume` for the CLIs |
| `reopen` | When the auditor's findings move units back to `TODO`. | `units` |
| `stalled` | When the idle-pass limit stops the loop. | `idle_passes` |
| `finish` | When the graph returns normally. | `iterations`, `tests_passed`, `audit_clean`, `errors`, `problems`, `clean`, `exit_code`, `caveats`, `usage` |
| `aborted` | On any exception, including Ctrl-C and `SIGTERM`. | `error`, `usage` |

A normal file starts with `start`, contains a sequence of `agent`, `gate`, `checkpoint`, `limit`, `reopen`
and `stalled` events, and ends with `finish` or `aborted`.

### `start`

| Field | Meaning |
|---|---|
| `repo`, `plan`, `spec` | Absolute paths used for the run. |
| `branch` | The `engine/<stamp>` branch, or `null` with `--no-git` or outside a git repository. |
| `base_sha` | The commit the run started from, or `null`. The auditor diffs against it. |
| `focus` | Unit names that were open when the run started. Empty when the plan had no status block yet or no unit was open. |
| `agents` | Object mapping each role to its binding, e.g. `{"coder": "claude", "reviewer": "codex", "auditor": "codex"}`. |
| `max_iterations` | The effective coder-pass ceiling. |
| `gates` | The gate commands as strings, in order. Empty when gates are disabled. |

### `agent`

`label` is the role or call name: `coder`, `reviewer`, `auditor`, or `reviewer-restate` /
`auditor-restate` when an ambiguous findings reply was sent back once to be restated as JSON. `agent` is the
runner name (`claude`, `codex`, `ollama`, `api`). `seconds` is wall-clock time for that attempt and
`prompt_chars` the length of the prompt in characters.

The remaining fields depend on the runner:

| Runner | Extra fields |
|---|---|
| `claude` | `cost_usd`, `input_tokens`, `cache_read_input_tokens`, `output_tokens`, when its final `result` event reported them. |
| `codex` | None: codex reports no cost of its own, so a codex-only run has no usage totals. |
| `ollama`, `api` | `model`, `cost_usd` (`0.0`), `input_tokens`, `output_tokens`. |

### `gate`

`iteration` is the coder pass that just finished. `errors` is the number of structured errors after the
20-error cap, and `failures` is one string per error in the form `file:line description`, with the
description truncated to 120 characters. Problems in the `ENGINE-STATUS` block are included as errors, with
the plan path as the file.

### `checkpoint`

Written only when git checkpoints are active. `iteration` is the pass, `sha` the short commit hash and
`status` either `green` or `N error(s)`. Red passes are committed too, so every pass is a rollback point.

### `limit`

`sleep_min` is the wait in minutes: an integer for the `claude`/`codex` CLIs and possibly a fractional
number for the HTTP runners. The CLIs also write `resume`, the local `HH:MM` when the call is expected to
continue.

### `reopen` and `stalled`

`reopen` carries `units`, the names the auditor found problems in and the engine moved back to `TODO`.
`stalled` carries `idle_passes` and is written when the engine gives up because consecutive coder passes
changed nothing.

### `finish` and `aborted`

`finish` is written once, when the graph returns. `errors` and `problems` are the final structured lists
(the same JSON the terminal prints when non-empty). `clean` is `true` only for exit code `0`, `exit_code` is
the numeric code, and `caveats` lists reasons for exit code `2` such as `BLOCKED: Login flow` or
`no gate ran — green checked nothing`. `usage` is the totals object described below.

`aborted` is written instead when an exception stops the run, including Ctrl-C and `SIGTERM`. `error` is the
exception type and message, and `usage` holds the totals up to that point. The checkpoints stay.

## Totals and usage

As each event is written, numeric fields whose name ends in `_usd` or `_tokens` are added to an in-process
total. That means `cost_usd`, `input_tokens`, `cache_read_input_tokens` and `output_tokens`. The `finish`
and `aborted` events include those totals as `usage`:

```json
"usage": {"cost_usd": 0.0431, "input_tokens": 1204, "cache_read_input_tokens": 2610, "output_tokens": 880}
```

The terminal summary comes from the same object: `$0.04 — in=1204 cached=2610 out=880`, or
`no usage reported` when nothing accumulated. Codex reports no cost or token counts, so a run using only
codex has an empty `usage`.

The log has no separate totals event. Read `usage` from the final event, or sum the `agent` events.

## API keys are scrubbed

When the engine reads a runner's key it registers the value with the run log. Every string in every event
is then scanned and the value is replaced with `[redacted]` before JSON escaping, including inside nested
objects and lists and inside error bodies echoed by an endpoint. Keys are never written to the log. Values
shorter than 8 characters are not registered, because scrubbing them would replace ordinary words.

## `jq` recipes

```bash
# follow a run live
tail -f .mae/runs/*.jsonl

# one line per agent call
jq -r 'select(.event=="agent") | "\(.label)\t\(.agent)\t\(.seconds)s\t$\(.cost_usd // 0)"' .mae/runs/*.jsonl

# the totals recorded at the end
jq 'select(.event=="finish") | .usage' .mae/runs/*.jsonl

# gate failures from the newest run
jq -r 'select(.event=="gate" and .errors > 0) | .failures[]' "$(ls -t .mae/runs/*.jsonl | head -1)"

# count events
jq -r '.event' .mae/runs/*.jsonl | sort | uniq -c

# the five slowest agent calls
jq -s 'map(select(.event=="agent")) | sort_by(.seconds) | reverse | .[:5] | .[] | {label: .label, seconds: .seconds}' .mae/runs/*.jsonl

# exit status and caveats
jq -r 'select(.event=="finish") | "exit=\(.exit_code) clean=\(.clean) caveats=\(.caveats | join("; "))"' .mae/runs/*.jsonl
```

The dashboard's History view reads the same files: it shows one row per log with status, passes and cost,
and its **Clear** button deletes all logs except the one belonging to a run in progress.

## Related pages

- [Command line](cli.md) — `--log-dir` and the exit codes recorded in `finish`.
- [Configuration](config.md) — where the default log directory sits and what is excluded from commits.
- [Cost and limits](../concepts/cost.md) — how tokens, cost and usage-limit waits work.
- [CI, Docker and signals](../guide/ci.md) — reading the log and exit codes from a pipeline.
