---
title: MultiAgentEngine
hide:
  - navigation
---

# MultiAgentEngine

**A loop of coding agents that implements, tests and audits your spec until it is done.**

MultiAgentEngine (`mae`) is an orchestrator built on [LangGraph](https://github.com/langchain-ai/langgraph)
that drives coding agents — the `claude` CLI, the `codex` CLI, or a **free model** (local through
[Ollama](https://ollama.com), or any OpenAI-compatible API) — against a repository until an architecture
spec is implemented, tested and audited.

You write two files: the **spec** (what the system must be) and the **plan** (the work queue). The engine
runs the agents, validates every pass with deterministic gates, takes a git checkpoint per pass, and stops
when nothing is left open and the audit comes back clean.

[Get started](getting-started.md){ .md-button .md-button--primary }
[How the loop works](concepts/loop.md){ .md-button }

## In thirty seconds

```bash
pipx install git+https://github.com/MiltonJoel/MultiAgentEngine.git

cd ~/code/myproject && mkdir -p .mae
echo "A FastAPI service with email/password login. Every endpoint has a pytest test." > .mae/spec.md
printf '<!-- ENGINE-STATUS\nLogin flow: TODO\n-->\n' > .mae/plan.md

mae --free          # every role on a free local model through Ollama
```

When it finishes, the work is on a new branch with one commit per pass, and `mae` prints the exact
`git log` command to review it.

## What you get

<div class="grid cards" markdown>

-   **Evaluator-optimizer loop**

    ---

    coder → tester → reviewer (on red) or auditor (on green) → coder, until the queue is closed. The
    auditor reopens `DONE` units that do not meet the spec. [The loop](concepts/loop.md)

-   **Deterministic gates**

    ---

    pytest and ruff by default, or any command — `npm test`, `tsc`, `eslint`, `go vet`. Their output becomes
    structured `file:line` errors. [Configuration](reference/config.md)

-   **Any model in any role**

    ---

    `claude`, `codex`, Ollama or an OpenAI-compatible API, mixed per role. No paid subscription needed.
    [Free models](guide/free-models.md)

-   **Safe by design**

    ---

    One branch and one commit per pass, secrets kept out of commits, file tools confined to the repo,
    API keys protected, clean Ctrl-C and `SIGTERM`. [Security](security.md)

-   **Observable**

    ---

    A JSONL log per run with timings, tokens and cost, and exit codes designed for CI.
    [Run log](reference/run-log.md) · [CI](guide/ci.md)

-   **Local dashboard**

    ---

    `mae-ui` edits the queue and spec, starts and stops runs, streams them live and browses past runs
    with their diffs. [Dashboard](guide/dashboard.md)

</div>

## Requirements

- Python 3.10+ on Linux or macOS (on Windows, inside [WSL](https://learn.microsoft.com/windows/wsl/)).
- At least one model provider: Ollama with a downloaded model, an OpenAI-compatible endpoint, or the
  logged-in `claude` / `codex` CLIs.
- Optional: `git` in the target repo (checkpoints and rollback), `pytest` and `ruff` (the default gates).

!!! warning "The agents run commands on your machine"
    The coder executes shell commands with your user's permissions. Read [Security](security.md) before
    pointing the engine at a repository, spec or plan you did not write.
