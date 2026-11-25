---
title: Cost and limits
---

# Cost and limits

This page explains what a run spends, which roles spend it and when, how the prompts are ordered so that the
provider's prompt cache can help, how `mae` waits out a usage limit instead of failing, and where to read the
numbers in the run log.

## What a call costs

Each role runs one of the four runners. The cost of a call depends on the runner:

| Runner | Cost in the run log | Tokens in the run log |
|---|---|---|
| `claude` | `cost_usd` from the CLI's result event | input, cache read, output |
| `codex` | no cost: the CLI reports none, so nothing is added | not recorded |
| `ollama`, `api` | `0.0` | input and output, when the endpoint reports usage |

The `$` figure in the summary is the sum of the `claude` costs, so it leaves out `codex` altogether. If you use
`codex` for a role, the summary undercounts. Check the provider's billing for that part.

## Which roles run, and when

| Role | When it runs | Model calls |
|---|---|---|
| coder | every pass | one per pass |
| tester | after every coder pass | none; it runs the gates |
| reviewer | only when the build is red | one per red pass, plus one restatement if its reply is ambiguous |
| auditor | only when the build is green and every unit is `DONE` or `BLOCKED` | one per audit, plus one restatement if its reply is ambiguous; repeats after each fix until an audit is clean |

The coder usually costs the most, because it runs on every pass and does the most work. The reviewer
does not run on a green build. The auditor does not run until the queue is closed, and then it runs again after
every round of fixes until it comes back clean.

The loop also stops early when it is not making progress. Two coder passes in a row that change nothing end the
run, and so does the pass limit. See [The loop](loop.md).

### What enters the graph state

The state the graph carries between nodes is small. The loop does not carry transcripts from one agent to the
next:

- Only the agent's final answer comes back from a call. For `claude` that is the result event's text. For
  `ollama` and `api` it is the last assistant message. The tool calls and results of a model run are discarded
  when the run ends.
- The coder's output is not used. The coder node only returns the pass counter and the idle counter.
- The reviewer and the auditor return parsed findings, not the reply. The raw reply stays in a local variable
  until it is parsed.
- The tester returns at most 20 errors, each with a file, a line and a description.

So a long run does not grow the prompt by carrying old answers forward. The growth comes from the agents' own
tool use within one call.

## Prompt order and caching

The provider can reuse the start of a prompt that it has seen before, and charges less for those tokens. The
engine does not set cache markers itself. The `claude` CLI and the provider decide what can be cached. What the
engine controls is the order of the text, which is why the stable parts come first in each prompt:

| Prompt | Order of the variable parts |
|---|---|
| coder | spec first, then the fixed instructions; the plan, the errors and the findings come last |
| reviewer | spec, then plan, then errors, then the diff |
| auditor | spec, then plan, then the scope, then the diff |

The plan changes on almost every coder pass, because the coder updates the status block. So for the coder, only the
part before the plan is cached from one pass to the next. The errors and findings change every pass and come last,
so they never break the cache for the text before them.

An edit to `spec.md` during a run changes the start of every prompt, so nothing after the spec is cached for that
pass. Make spec changes between runs when cost matters.

The run log's `cache_read_input_tokens` shows what was reused. A high value means the cache worked. A progress
line from the `claude` runner looks like this:

```text
[coder] done — in=1204 cache_read=2610 out=880 cost=$0.0431
```

## Usage limits and waits

A CLI runner (`claude`, `codex`) can stop because the account has hit a usage limit. The engine treats that as a
wait, not a failure, when the output says so. It looks for these phrases in the last 30 lines of a failed
transcript: `limit reached`, `rate limit`, `rate-limit` or `ratelimit`, or `overloaded`. Messages the agent wrote
earlier in the transcript are ignored, so code that mentions a rate limit does not start a wait.

The wait time comes from the output, in one of two forms:

- a Unix time of 10 digits after `reset`, `resets`, `reached` and the like;
- `resets YYYY-MM-DD HH:MM UTC`.

With a reset time, the engine waits until that time plus 30 seconds of clock margin, and at least 60 seconds. If the
output gives no reset time, it waits 30 minutes.

| Limit | Value | Meaning |
|---|---|---|
| `MAX_WAITS` | 4 | at most four waits for one call; after the fifth limit the run fails with `<label> failed (<reason>)` |
| `MAX_SLEEP` | 6 hours | a longer wait is refused: `hit a limit that resets ~YYYY-MM-DD HH:MM (X.Xh away) — too long to wait, re-run after it clears` |
| `DEFAULT_WAIT` | 30 minutes | the wait when the output has no reset time |

A refused wait ends the run with exit code `1`. A session limit usually resets within hours, so the engine waits
it out. A monthly cap does not, so the engine stops and tells you when it resets.

While it waits, the console shows:

```text
[coder] limit reached — sleeping 47 min, resuming ~15:32
```

The run log gets a `limit` event with the label, the sleep in minutes and the time it resumes. Each retry after a
wait starts a fresh attempt with its own timeout, so the wait itself does not use up the coder's 90 minutes.

### Retries on the HTTP runners

`ollama` and `api` do not sleep on a session limit. They retry a request that gets `429`, `500`, `502`, `503` or
`504`, up to four times, with the waits described in [Free models](../guide/free-models.md#retries). They use one
budget for the whole call, so the retries and waits count against the time limit of that call. The run log gets a
`limit` event with `sleep_min`, but no `resume` time.

## Reading the cost from the run log

Every run writes one JSON object per line to `.mae/runs/<stamp>.jsonl`. Three events carry cost:

- `agent`: one per call attempt, with `label` (`coder`, `reviewer`, `auditor`, `reviewer-restate`, ...), `agent`
  (the runner), `seconds`, `prompt_chars`, and the usage fields its runner reports.
- `finish`: the run's totals in `usage`, written when the run ends.
- `aborted`: the same totals, when the run stops with an error or an interrupt.

For the CLI runners, each attempt logs an `agent` event, so a call that was retried after a limit logs one per
attempt. The HTTP runners log one event per call, however many retries it took.

The console prints the totals at the end, in this form (the numbers here are an example):

```text
Usage: $0.42 — in=180310 cached=152004 out=21877
```

To see the cost of each call:

```bash
jq -r 'select(.event=="agent")
       | "\(.label)\t\(.agent)\t\(.seconds)s\t$\(.cost_usd // "n/a")"' .mae/runs/*.jsonl
```

`codex` calls show `$n/a`, because the engine has no cost for them.

To see the totals of each run:

```bash
jq -c 'select(.event=="finish" or .event=="aborted") | .usage' .mae/runs/*.jsonl
```

To see how many limit waits a run took and how long it slept:

```bash
jq -c 'select(.event=="limit")' .mae/runs/<stamp>.jsonl
```

The run log is described in [Run log](../reference/run-log.md).

## Keeping the cost down

- Give the cheap work to a cheap runner. A free runner for the reviewer and the auditor, for example, costs nothing
  in the `$` figure (see [Free models](../guide/free-models.md)). Set it with `--agent reviewer=ollama`.
- Keep the spec stable during a run, so the coder's prompt cache keeps working.
- Split the plan into small units. A unit that is too large makes the coder run more passes, and each pass costs
  a full prompt.
- Set `--max-iterations` to the number of passes you are willing to pay for. The loop stops at the ceiling and
  leaves the rest of the queue open for the next run.

## Related pages

- [The loop](loop.md) describes the graph that decides which role runs next.
- [Run log](../reference/run-log.md) lists every event and field.
- [Troubleshooting](../troubleshooting.md) covers what to do when a run stops on a limit.
