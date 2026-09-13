"""Prompts are files: engine/prompts/<name>.md, overridable per repo at <repo>/.mae/prompts/<name>.md."""
from pathlib import Path

BUILTIN = Path(__file__).resolve().parent / "prompts"
OVERRIDE = ".mae/prompts"


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
    try:
        return source.read_text(encoding="utf8").format(**fields)
    except KeyError as exc:
        raise SystemExit(
            f"{source}: unknown placeholder {exc} — available: {', '.join(sorted(fields))}. "
            "A literal brace in a prompt must be doubled: {{ }}"
        )
