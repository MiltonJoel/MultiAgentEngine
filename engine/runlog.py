"""Append-only JSONL run log, one line per event (`tail -f` while running, `jq` after)."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# ponytail: process-global sink — one run per process.
_sink = None
_totals: dict[str, float] = {}
_secrets: set[str] = set()


def secret(value: str) -> None:
    """Register a value (an API key) that must never reach a run log, even inside an echoed error body."""
    if value and len(value) >= 8:  # a short value would scrub ordinary words out of the log
        _secrets.add(value)


def _scrub(line: str) -> str:
    for value in _secrets:
        line = line.replace(value, "[redacted]")
    return line


def start(log_dir: str | Path) -> Path:
    global _sink
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    if not ignore.exists():  # logs must never ride along in a checkpoint commit
        ignore.write_text("*\n", encoding="utf8")
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    if _sink is not None:
        _sink.close()
    _totals.clear()
    # exclusive create: runs started in the same second (other plans, other processes) never share one log
    for suffix in ("", *(f"-{os.getpid()}-{n}" for n in range(1, 1000))):
        path = directory / f"{stamp}{suffix}.jsonl"
        try:
            _sink = path.open("x", encoding="utf8", buffering=1)
        except FileExistsError:
            continue
        return path
    raise RuntimeError(f"cannot create a new run log in {directory}")


def close() -> None:
    global _sink
    if _sink is not None:
        _sink.close()
        _sink = None


def log(event: str, **fields) -> None:
    """Record one event. Numeric fields ending in _usd or _tokens accumulate into totals()."""
    for key, value in fields.items():
        if isinstance(value, int | float) and key.endswith(("_usd", "_tokens")):
            _totals[key] = _totals.get(key, 0) + value
    if _sink is None:
        return
    _sink.write(_scrub(json.dumps({"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                                   "event": event, **fields}, ensure_ascii=False)) + "\n")


def totals() -> dict:
    return dict(_totals)


def summary() -> str:
    total = _totals
    if not total:
        return "no usage reported"
    return (f"${total.get('cost_usd', 0):.2f} — in={int(total.get('input_tokens', 0))} "
            f"cached={int(total.get('cache_read_input_tokens', 0))} "
            f"out={int(total.get('output_tokens', 0))}")
