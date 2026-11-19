"""The multimillion idea lives in ideas/ and shows its iterations."""

import re
from pathlib import Path

IDEA = Path(__file__).resolve().parent.parent / "ideas" / "multimillion_idea.md"


def test_idea_is_iterated_and_reaches_millions():
    text = IDEA.read_text()
    iterations = [int(n) for n in re.findall(r"^## Iteration (\d+)", text, re.M)]
    assert len(iterations) >= 2 and iterations == list(range(1, len(iterations) + 1))
    m = re.search(r"\$([\d.]+)M ARR", text)
    assert m and float(m.group(1)) >= 1
