# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Documentation site (`docs/`, built with Zensical, published to GitHub Pages by
  `.github/workflows/docs.yml`): getting started, guides, concepts, CLI/config/run-log/runner reference,
  security, troubleshooting, with dashboard screenshots. CI builds it in strict mode on every pull request.
- English README (`README.en.md`), with language links between both versions.

### Fixed
- `test_documents_live_in_the_target_repo_not_in_the_package` failed on macOS, where the temp directory
  sits behind a symlink (`/var` → `/private/var`); the macOS CI job was red because of it.
- README: the troubleshooting table quoted the old wording of the unreadable-status-line warning.
- Dashboard: on a narrow board column, a blocked unit's Delete button overflowed its card.

### Changed
- **Breaking for CI scripts:** every refusal to start now exits **3** (`EXIT_REFUSED`) with a
  `mae: error: …` message — bad flags or values, a bad `.mae/config.json`, a missing CLI or gate tool, a
  broken prompt override, a busy repo, a plan held by another run or with nothing left open. Before, these
  exited 2 (indistinguishable from "finished, needs review") or 1 (indistinguishable from a failed run).
  Exit 1 now only means a run that started and did not finish; exit 2 only means it finished but needs
  review. `mae --help` lists the codes. Unreadable config/plan/prompt files, symlink loops, every broken
  placeholder form and an unusable `--log-dir` are refusals too, with a one-line message instead of a traceback.
- A failing baseline commit (before any agent) now logs `aborted` and prints a clean message instead of a
  traceback; a missing gate tool is reported once instead of twice.
- The READMEs are now short overviews; the full reference moved to the documentation site.

## [0.3.0] — 2026-11-21

### Security
- The built-in agent (`ollama`/`api` runners) no longer follows symlinks out of the workspace in
  `search` and `list_files`; every file is confined to the repository and `--allow-dir` directories,
  as `read_file` already was.
- A repository's `.mae/config.json` can no longer aim an ambient credential at an endpoint it chose.
  `llm.*.key_env` is default-deny: it must be a `MAE_*` name or a known model-provider key
  (`OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY`, …); anything else (`GITHUB_TOKEN`, `HF_TOKEN`,
  `AWS_*`, …) only if the user lists it in `MAE_ALLOWED_KEY_ENVS` in their own environment.
  `llm.*.url` must be `http(s)://`.
- API keys are never sent over plain `http` to a host other than this machine, and model endpoints are
  never followed through HTTP redirects (urllib would forward the `Authorization` header to any host).
- API keys are scrubbed from endpoint error messages and from every run-log field (before JSON escaping).
- `mae` prints a `WARNING` when a key goes to an endpoint chosen by the repo's own config.
- A redirecting endpoint now says where it redirects (`configure that URL instead`) at startup.
- The dashboard's automatic runner check (which runs as soon as a repo is opened) no longer sends the
  API key to an endpoint set by the repo's config; the key is sent at launch, and `mae` prints where it goes.
- `mae-ui` rejects requests whose `Host`/`Origin` is not its own loopback address (DNS-rebinding
  protection; `--port 80` accepts the port-less form browsers send), caps request bodies at 4 MiB, times out stalled clients, compares the token in constant
  time over bytes, and serves the page with a strict Content-Security-Policy, `X-Frame-Options: DENY`
  and `Referrer-Policy: no-referrer` (the page URL carries the token).

### Fixed
- An auditor reply that quoted an empty `{"problems": []}` example before its real findings was read as
  a clean audit. All `problems` objects in a reply are now merged (wrapped and nested ones included); a
  malformed, truncated or decoder-exhausting (deeply nested) one fails closed. A `problems` key deeper in the
  reply that is not a findings list (an echoed schema, a count) is ignored rather than failing the audit.
- Findings written as prose beside an empty `{"problems": []}` vanished. Such an ambiguous reply is now sent
  back once to the same agent to restate as JSON (a short prompt with no repo context); only an unambiguous
  answer counts, so a chatty but genuinely clean audit still ends the run.
- Ctrl-C or `SIGTERM` during a checkpoint SIGKILLed git mid-write and left `.git/index.lock`, so every later
  run was refused. Signals now wait for the checkpoint to finish (recorded by a temporary handler, not a
  blocked signal mask, so a hung git hook can still be interrupted); a checkpoint error raised meanwhile is
  kept as the cause of the interrupt.
- The built-in agent's pass died on a symlink loop in the repository (Python 3.10).
- A failed `git add` (or failed secret un-staging) could still produce a checkpoint commit of a tree the
  gates never ran on. It now fails the checkpoint (and the run, in strict mode). git's harmless exit 1 for
  an exclude that names a gitignored path (`.mae/runs/`) is still accepted.
- Children an agent CLI left running after it exited (a dev server, a watcher) kept going and could keep
  editing the repo; the CLI's process group is now killed after every pass, and is signalled by its id so
  this does not depend on the CLI process still existing.
- Stopping a run from the dashboard could signal a run started a moment later.
- HTTP 429/5xx back-off for the model runners could sleep past the pass's time budget.
- A non-JSON answer from a model endpoint (a proxy error page) crashed with a traceback.
- `run_command` ran for one more second after the pass's budget was spent.
- Two runs started in the same second on different plans shared one run-log file.
- A non-UTF-8 prompt override, or a read-only plan, crashed with a traceback instead of a message.

### Added
- `SIGTERM` (systemd, `docker stop`, a cancelled CI job) is handled like Ctrl-C: the agent's process group
  is killed, `aborted` is logged, checkpoints stay, and `mae` exits **143**.
- `mae --version`, `mae-ui --version`, and `python -m engine`.
- A clear message on Windows (`mae` needs Linux or macOS; use WSL) instead of an import error.
- Dashboard: model picker built from the endpoint's own model list; **Clear** in History deletes run logs.
- `py.typed`, project URLs and classifiers; `SECURITY.md`, `CONTRIBUTING.md`, issue/PR templates,
  `.pre-commit-config.yaml`, `.editorconfig`, Dependabot; `tests/` is a regular package.
- CI: lint (ruff with bugbear/bandit/pyupgrade rules) and mypy jobs, tests on Python 3.10–3.13 plus macOS
  with an 80 % coverage floor, a build job (`twine check --strict`, wheel install smoke test), and a tag-driven
  release workflow publishing to PyPI through trusted publishing.

### Changed
- README reorganised: overview, feature list, table of contents, exit-code table, and Security,
  Development and License sections; dashboard descriptions match the current UI.
- **Breaking for some configs:** a repo config whose `llm.api.key_env` names a variable outside the
  allowed set now fails at startup with a message naming `MAE_ALLOWED_KEY_ENVS`.
- The `claude`, `codex` and model runners take everything after `(prompt, cwd, label)` as keyword-only
  arguments.
- The version has a single source, `engine.__version__`; SPDX `license = "MIT"` (setuptools ≥ 77).

### Removed
- Leftover artifacts of a demo run of the engine on its own repository (`ideas/`, `tests/test_idea.py`,
  the root `.mae/`) and the empty root `conftest.py` (pytest now gets `pythonpath` from `pyproject.toml`).

## [0.2.0]

- Free models through Ollama or any OpenAI-compatible API (`--free`, `ollama`/`api` runners).
- `mae-ui` dashboard.
- Portable install (`pipx`), checkpoints, run logs, usage-limit waits.
