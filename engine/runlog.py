"""Append-only JSONL run log, one line per event (`tail -f` while running, `jq` after)."""
import json
from datetime import datetime, timezone
from pathlib import Path

# ponytail: process-global sink — one run per process.
_sink = None
_totals: dict[str, float] = {}


def start(log_dir: str | Path) -> Path:
    global _sink
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now(timezone.utc).astimezone().strftime('%Y%m%d-%H%M%S')}.jsonl"
    _sink = path.open("a", encoding="utf8", buffering=1)
    return path


def log(event: str, **fields) -> None:
    """Record one event. Numeric fields ending in _usd or _tokens accumulate into totals()."""
    for key, value in fields.items():
        if isinstance(value, (int, float)) and key.endswith(("_usd", "_tokens")):
            _totals[key] = _totals.get(key, 0) + value
    if _sink is None:
        return
    _sink.write(json.dumps({"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                            "event": event, **fields}, ensure_ascii=False) + "\n")


def totals() -> dict:
    return dict(_totals)


def summary() -> str:
    total = _totals
    if not total:
        return "no usage reported"
    return (f"${total.get('cost_usd', 0):.2f} — in={int(total.get('input_tokens', 0))} "
            f"cached={int(total.get('cache_read_input_tokens', 0))} "
            f"out={int(total.get('output_tokens', 0))}")
