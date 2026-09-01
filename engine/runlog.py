"""One JSONL line per event, so a 90-minute run leaves something behind.

Cost, tokens, findings and checkpoints only existed in the terminal scrollback
before. The file is append-only and one line per event, so `tail -f` works while
the run is going and `jq` works after it.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

# ponytail: process-global sink — one run per process, and threading it through
# every node signature buys nothing.
_sink = None
_totals: dict[str, float] = {}


def start(log_dir: str | Path) -> Path:
    global _sink
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{datetime.now(timezone.utc).astimezone().strftime('%Y%m%d-%H%M%S')}.jsonl"
    _sink = path.open("a", encoding="utf8", buffering=1)  # line-buffered: readable live
    return path


def log(event: str, **fields) -> None:
    """Record one event. Numeric fields ending in _usd or _tokens accumulate."""
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
    cost = total.get("cost_usd", 0)
    return (f"${cost:.2f} — in={int(total.get('input_tokens', 0))} "
            f"cached={int(total.get('cache_read_input_tokens', 0))} "
            f"out={int(total.get('output_tokens', 0))}")


class Timer:
    """`with Timer() as t:` … `t.seconds` — for the duration field on agent events."""

    def __enter__(self):
        self.start = time.monotonic()
        return self

    def __exit__(self, *exc):
        self.seconds = round(time.monotonic() - self.start, 1)
        return False
