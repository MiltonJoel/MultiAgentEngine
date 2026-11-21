# Contributing

Thanks for helping. The engine is small on purpose: stdlib plus LangGraph, no web framework,
no front-end build. Please keep it that way.

## Setup

```bash
git clone https://github.com/MiltonJoel/MultiAgentEngine.git
cd MultiAgentEngine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install            # optional: runs ruff on every commit
```

Use a `pip`-installed `ruff`, not the snap build (it can report no findings while checking
nothing — see the README's troubleshooting table).

## Before you open a pull request

```bash
ruff check .                  # lint
mypy                          # types (engine/ only)
python -m pytest -q --cov     # tests + coverage; CI fails under 80 %
python -m build && twine check --strict dist/*   # only if you touched packaging
```

CI runs all of this on Python 3.10–3.13 (Linux) and 3.12 (macOS).

## Ground rules

- **Every behaviour change has a test.** Tests never call a real model: use the fake CLIs
  and the scripted OpenAI-compatible server already in `tests/`.
- **Nothing project-specific goes in `engine/`.** Anything about a particular target repo
  belongs in that repo's `.mae/`.
- **Engine messages are English and actionable**: say what is wrong and what to do about it.
- **Agents' transcripts never enter the graph state** — only their final answers. This keeps
  the cost per pass flat.
- Add a line under *Unreleased* in `CHANGELOG.md`.

## Releasing (maintainers)

1. Bump `__version__` in `engine/__init__.py` and move *Unreleased* in `CHANGELOG.md` under
   the new version.
2. Commit, then `git tag vX.Y.Z && git push --tags`. The `release` workflow checks that the
   tag matches the version, builds, and publishes to PyPI through trusted publishing.
