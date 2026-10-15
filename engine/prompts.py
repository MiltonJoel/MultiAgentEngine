"""Prompts are files: engine/prompts/<name>.md, overridable per repo at <repo>/.mae/prompts/<name>.md."""
from pathlib import Path

BUILTIN = Path(__file__).resolve().parent / "prompts"
OVERRIDE = ".mae/prompts"
# The placeholders each prompt is filled with; check() formats every override against them at startup.
FIELDS = {"coder": ("spec_path", "spec", "plan_path", "plan", "errors", "problems"),
          "reviewer": ("spec", "plan", "errors", "diff"),
          "auditor": ("spec", "plan", "focus", "diff")}


def path(name: str, repo: str | Path | None = None) -> Path:
    if repo:
        override = Path(repo) / OVERRIDE / f"{name}.md"
        if override.is_file():
            return override
    return BUILTIN / f"{name}.md"


def load(name: str, repo: str | Path | None = None, **fields) -> str:
    """Read the prompt and fill its {placeholders}; an unknown one fails with the list of what's available."""
    source = path(name, repo)
    if source != BUILTIN / f"{name}.md":
        print(f"[{name}] using prompt override {source}", flush=True)
    return _format(source, fields)


def _format(source: Path, fields: dict) -> str:
    try:
        return source.read_text(encoding="utf8").format(**fields)
    except (KeyError, IndexError, ValueError) as exc:
        raise SystemExit(
            f"{source}: bad placeholder ({type(exc).__name__}: {exc}) — available: {', '.join(sorted(fields))}. "
            "A literal brace in a prompt must be doubled: {{ }}"
        )


def check(repo: str | Path) -> None:
    """Fail before the run spends anything if an override would only blow up hours in (the auditor runs last)."""
    for name, fields in FIELDS.items():
        source = path(name, repo)
        if source != BUILTIN / f"{name}.md":
            _format(source, dict.fromkeys(fields, ""))
