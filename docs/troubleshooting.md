---
title: Troubleshooting
---

# Troubleshooting

The messages below are quoted as the engine prints them. Angle brackets stand for a value: `<plan>`
is the plan file's path, `<role>` a role name, `<name>` a unit or runner name, `<model>` a model tag.
Each entry gives the usual cause and the fix.

## Setup

| Message or symptom | Cause | Fix |
|---|---|---|
| `spec file not found: <path>` | There is no spec at the default `.mae/spec.md`, at the path in `--spec`, or at the `"spec"` key in `.mae/config.json`. | Create the file, point `--spec` at yours, or set `"spec"` in the config. |
| `plan file not found: <path>` | Same as above for the work queue. The message also prints a starter block to copy. | Create `.mae/plan.md`, point `--plan` at it, or set `"plan"`. The starter block has one `TODO` line and a notes heading. |
| `another run already holds <plan> — wait for it or kill it` | Another `mae` process has the plan file locked. The engine allows one run per plan file so two runs cannot interleave writes to the status block. | Wait for the other run to finish, or stop it. The lock is advisory: editing the plan by hand still works. |

## Plan

| Message or symptom | Cause | Fix |
|---|---|---|
| `<plan>` has nothing left open — every item is DONE or BLOCKED. Add a `<name>: TODO` line inside the ENGINE-STATUS block. | The queue is closed. Working as intended, but there is nothing for the coder to do. | Add a `TODO` line, or reopen a finished one with a note. |
| `WARNING: unreadable status line, the engine ignores it: <line>` | A line in the status block has a made-up state such as `PENDING`, a lower-case state, or no colon before the state. The line is ignored, so that unit silently does not exist. | Fix the line to `<name>: <STATE>`, using `TODO`, `IN_PROGRESS`, `DONE` or `BLOCKED`. |
| You wrote a new section but nothing runs | Prose takes no part in routing. Only lines inside the status block decide what the coder works on. | Add a `<name>: TODO` line inside the block. Put the detail in the spec or below the block, under the same name. |
| The coder did not update the plan | The coder could not read a file the unit needs, so it could not finish the work and flip the line. The plan directory itself is always allowed; other directories are not. | Put the referenced material inside the repo, or add its directory with `--allow-dir`. |

See [Plan and spec](guide/plan-and-spec.md) for the block syntax and the states.

## Models and keys

| Message or symptom | Cause | Fix |
|---|---|---|
| `unknown CLI '<name>' for <role> — known: api, claude, codex, ollama` or `unknown role '<name>' — known: coder, reviewer, auditor` | A typo in `--agent` or in the `"agents"` object of `.mae/config.json`. | Use a role from `coder`, `reviewer`, `auditor` and a runner from `claude`, `codex`, `ollama`, `api`. |
| `the claude CLI is not on PATH` and you have no subscription | The default coder is the paid `claude` CLI, which is not installed or not on the `PATH`. | Install and log in, bind the role elsewhere with `--agent`, or run everything free with `mae --free`. |
| `Ollama is not reachable at http://localhost:11434/v1` | Ollama is not installed, or the server is not running. | Install Ollama from <https://ollama.com>, start it with `ollama serve`, and pull a model. |
| `Ollama has no model '<model>'` | The configured model tag is not downloaded on this machine. | Run `ollama pull <model>`, or choose one you have with `--agent ROLE=ollama:<model>`. |
| The local model answers without using tools, or ignores the plan | The context window is too short for the prompt, so the tool instructions and plan fall out of view. | Start Ollama with `OLLAMA_CONTEXT_LENGTH=32768`, or use a larger model. |
| `llm.api.key_env: <VAR> is not a known model API key variable` | A repository's `.mae/config.json` names an environment variable that is not a model-provider key. The engine refuses to send an ambient credential to an endpoint the repository chose. | Use `MAE_API_KEY`, or allow the variable yourself in your own environment: `export MAE_ALLOWED_KEY_ENVS=<VAR>`. A repository cannot set that allow-list. |
| `<url> redirects (301) to <url> — model endpoints are not followed through redirects; configure that URL instead` | The endpoint redirects, often from `http` to `https`. The engine never follows a redirect, because the `Authorization` header would travel to the new host. | Put the final URL in `"llm"` or in `MAE_OLLAMA_URL` / `MAE_API_URL`. |
| `failed to refresh available models` | A non-fatal `codex` log line that carries a large models JSON dump. The engine filters it out of the stream. | Harmless. Upgrade with `npm i -g @openai/codex@latest` if it bothers you. |

Endpoint, model and key settings are described in [Free models](guide/free-models.md) and
[Configuration](reference/config.md).

## Gates

| Message or symptom | Cause | Fix |
|---|---|---|
| Lint passes on everything, and `WARNING: <path> is a snap build; snap ruff can report no findings while checking nothing` | The snap build of `ruff` is confined and cannot write its cache, so it returns an empty result with exit code 0. A green gate then checked nothing. | Remove the snap build and install ruff with pip: `snap remove ruff && pip install ruff`. |
| The coder leaves units `IN_PROGRESS` because it “cannot run pytest” | An older engine version ran `claude -p` in `acceptEdits` mode, which refuses Bash, so the coder could not run tests. Current versions pass `--allowedTools Bash`. | Upgrade the engine. If it still happens, check that the test command is a configured gate and that its tool exists in the target repo. |

The default gates are `pytest -q --tb=line --no-header` (exit code 5, “no tests collected”, counts
as a pass) and `ruff check . --output-format json`. Change them with `--gate` or the `"gates"` key.
See [Command line](reference/cli.md) and [Configuration](reference/config.md).

## The loop

| Message or symptom | Cause | Fix |
|---|---|---|
| `Stopped early: 2 coder passes in a row changed nothing. Read the last coder output above.` | The coder is stuck. It is usually asking for something it did not get: a permission, data, or a decision. | Read the last coder output and answer it. The engine also prints `[engine] 2 coder passes in a row changed nothing — stopping`. |
| The loop ends with units still open | If it did not stop early, it hit the pass ceiling. `--max-iterations` defaults to 15 and is a ceiling on coder passes, not a target. | Raise it above the number of open units and run again; the next run continues from the status block. |
| `[auditor] reopened: <names>` | The auditor found problems in `DONE` units. The engine moved those lines back to `TODO` with the finding as the note. | Expected. Let the coder fix them and the audit run again. If the same units keep reopening, read the `audit:` notes. |
| The run ends at the ceiling with `audit_clean=False` | The auditor keeps finding problems, so the queue never closes cleanly. | Read the `audit:` notes in the plan and the `problems` JSON printed at the end. Fix the spec if it asks for something impossible, or mark the unit `BLOCKED` with the reason. |

The routing behind these messages is explained in [The loop](concepts/loop.md). For exit codes and
what each one means in CI, see [Command line](reference/cli.md).
