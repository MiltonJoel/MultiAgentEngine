import json
import tempfile
from pathlib import Path

from engine.cli import CODEX_NOISE, DEFAULT_WAIT, _claude_result, _claude_usage, _retry_delay
from engine.graph import (
    MAX_ITERATIONS,
    after_audit,
    after_review,
    after_tests,
    archive_done,
    plan_done,
    plan_open,
    plan_states,
    plan_unlisted,
    plan_unparsed,
)
from engine.nodes.reviewer import _parse_problems
from engine.nodes.tester import FAIL_LINE, _json_errors, _text_errors, parse_cmd


def test_after_tests():
    with tempfile.TemporaryDirectory() as d:
        todo, done = Path(d) / "todo.txt", Path(d) / "done.txt"
        todo.write_text("<!-- ENGINE-STATUS\nFase 1: DONE\nFase 2: TODO\n-->\n")
        done.write_text("<!-- ENGINE-STATUS\nFase 1: DONE\nFase 2: DONE\n-->\n")

        def route(tests_passed, plan, audited=False, iteration=0):
            return after_tests({"tests_passed": tests_passed, "plan": str(plan), "audited": audited,
                                "iteration": iteration, "max_iterations": MAX_ITERATIONS})

        # red build -> analyse the failure
        assert route(False, done) == "reviewer"
        # green but plan unfinished -> keep working, don't declare victory
        assert route(True, todo) == "coder"
        # green and plan finished, never audited -> one audit pass before ending
        assert route(True, done) == "auditor"
        # already audited -> really done
        assert route(True, done, audited=True) == "__end__"
        # a red build still goes to the reviewer even after an audit
        assert route(False, done, audited=True) == "reviewer"
        # iteration cap wins over everything, so no edge back to coder can spin
        assert route(True, todo, iteration=MAX_ITERATIONS) == "__end__"
        assert route(True, done, iteration=MAX_ITERATIONS) == "__end__"


def test_after_audit():
    finding = [{"file": "a.py", "line": 1, "description": "stub", "suggested_fix": "implement"}]
    # findings go back to the coder
    assert after_audit({"problems": finding, "iteration": 0, "max_iterations": MAX_ITERATIONS}) == "coder"
    # clean audit ends the run
    assert after_audit({"problems": [], "iteration": 0, "max_iterations": MAX_ITERATIONS}) == "__end__"
    # cap still bounds it
    assert after_audit({"problems": finding, "iteration": MAX_ITERATIONS, "max_iterations": MAX_ITERATIONS}) == "__end__"


def test_plan_done():
    prose = "\n### Fase 1 — Event Bus\n\n* Implementar el bus.\n\n```python\nx: int = 1  # a colon in code\n```\n"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "plan.txt"
        for text, expected in [
            # no block yet (first run) -> not done, or the loop would never start
            (prose, False),
            ("<!-- ENGINE-STATUS\nFase 1: TODO\n-->" + prose, False),
            ("<!-- ENGINE-STATUS\nFase 1: DONE\nFase 2: IN_PROGRESS\n-->" + prose, False),
            ("<!-- ENGINE-STATUS\nFase 1: DONE\nFase 2: DONE\n-->" + prose, True),
            # BLOCKED can't be unblocked by the loop, so it counts as finished —
            # with or without the reason the coder is told to append
            ("<!-- ENGINE-STATUS\nFase 1: DONE\nFase 2: BLOCKED need API key\n-->" + prose, True),
            ("<!-- ENGINE-STATUS\nFase 1: DONE\nFase 2: BLOCKED\n-->" + prose, True),
            # a reason must not mask a still-open item
            ("<!-- ENGINE-STATUS\nFase 1: BLOCKED waiting\nFase 2: TODO ver notas\n-->" + prose, False),
            # em-dashes and colons inside the item name must not break parsing
            ("<!-- ENGINE-STATUS\nFase 6 — OMS: máquina de estados: DONE\n-->" + prose, True),
        ]:
            p.write_text(text)
            assert plan_done(str(p)) is expected, text[:70]

        # prose outside the block is never parsed as status, even if it looks like one
        p.write_text("Fase 9: TODO\n" + prose)
        assert plan_states(str(p)) == []


def test_plan_unparsed():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "plan.txt"
        p.write_text(
            "<!-- ENGINE-STATUS\n"
            "Fase 1: DONE\n"
            "Fase 2: PENDIENTE\n"          # unknown state -> silently dropped before
            "Fase 3 sin dos puntos TODO\n"  # missing colon
            "Fase 4: TODO revisar seccion 5\n"
            "-->\n")
        assert plan_states(str(p)) == ["DONE", "TODO"]
        assert plan_unparsed(str(p)) == ["Fase 2: PENDIENTE", "Fase 3 sin dos puntos TODO"]
        # a well-formed block reports nothing
        p.write_text("<!-- ENGINE-STATUS\nFase 1: DONE\n-->\n")
        assert plan_unparsed(str(p)) == []


def test_plan_open_names_the_units():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "plan.txt"
        p.write_text(
            "<!-- ENGINE-STATUS\n"
            "Fase 1 — Event Bus: DONE\n"
            "Fase 6 — OMS: maquina de estados: TODO — cambie el timeout\n"
            "Fase 7: IN_PROGRESS\n"
            "Fase 8: BLOCKED faltan claves\n"
            "-->\n")
        # only genuinely open units, with the note stripped off the name
        assert plan_open(str(p)) == ["Fase 6 — OMS: maquina de estados", "Fase 7"]


def test_archive_done_loses_nothing():
    body = "\n### Fase 1\nprosa del usuario que no se debe tocar\n"
    with tempfile.TemporaryDirectory() as d:
        plan, arch = Path(d) / "plan.txt", Path(d) / "plan.txt.archive"
        plan.write_text(
            "<!-- ENGINE-STATUS\n"
            "Fase 1: DONE\n"
            "Fase 2: TODO\n"
            "Fase 3: BLOCKED faltan claves\n"
            "Fase 4: PENDIENTE\n"   # unparsed: must never be swept away
            "-->" + body)
        moved, kept = archive_done(str(plan), str(arch))
        assert (moved, kept) == (1, 3)
        after = plan.read_text()
        assert "Fase 1: DONE" not in after          # moved out
        assert "Fase 1: DONE" in arch.read_text()   # and preserved verbatim
        for survivor in ("Fase 2: TODO", "Fase 3: BLOCKED", "Fase 4: PENDIENTE"):
            assert survivor in after, survivor
        assert after.endswith(body)                 # the user's prose is untouched
        # nothing DONE left -> second run is a no-op, not a corruption
        assert archive_done(str(plan), str(arch)) == (0, 3)

    # archiving everything must leave a ledger line, or the block reads as "never started"
    with tempfile.TemporaryDirectory() as d:
        plan, arch = Path(d) / "p.txt", Path(d) / "p.archive"
        plan.write_text("<!-- ENGINE-STATUS\nA: DONE\nB: DONE\n-->" + body)
        assert archive_done(str(plan), str(arch)) == (2, 1)
        assert plan_done(str(plan)) is True
        assert plan_states(str(plan)) == ["DONE"]


def test_after_review():
    assert after_review({"iteration": MAX_ITERATIONS, "max_iterations": MAX_ITERATIONS}) == "__end__"
    assert after_review({"iteration": 1, "max_iterations": MAX_ITERATIONS}) == "coder"


def test_fail_line_parses_pytest_tb_line():
    out = (
        "=================================== FAILURES ===================================\n"
        "/home/x/proj/test_bot.py:42: AssertionError: assert 1 == 2\n"
        "tests/test_api.py:7: TypeError: unsupported operand\n"
        "1 failed, 2 passed in 0.03s\n"
    )
    hits = [(m["file"], int(m["line"]), m["desc"]) for m in FAIL_LINE.finditer(out)]
    assert hits == [
        ("/home/x/proj/test_bot.py", 42, "AssertionError: assert 1 == 2"),
        ("tests/test_api.py", 7, "TypeError: unsupported operand"),
    ]


def test_parse_problems_tolerates_fences_and_junk():
    payload = {"problems": [{"file": "bot.py", "line": 42, "description": "d", "suggested_fix": "f"}]}
    assert _parse_problems(json.dumps(payload)) == payload["problems"]
    assert _parse_problems(f"```json\n{json.dumps(payload)}\n```") == payload["problems"]
    fallback = _parse_problems("codex had a bad day")
    assert fallback[0]["description"] == "codex had a bad day"


def test_codex_noise_is_recognised():
    real = ("2026-08-10T21:37:57Z ERROR codex_models_manager::manager: failed to refresh "
            'available models: unknown variant `max`; body: {"models":[...]}')
    assert CODEX_NOISE.search(real)
    # and it must not swallow ordinary output
    assert not CODEX_NOISE.search("models refreshed, 3 available\n")


def test_retry_delay():
    now = 1_800_000_000.0
    # not a limit error -> don't sleep
    assert _retry_delay("TypeError: bad operand", now) == 0
    # epoch reset, 10 minutes out
    assert _retry_delay("Claude AI usage limit reached|1800000600", now) == 600 + 30
    # ISO reset from the billing-error message (now == 2027-01-15T08:00Z)
    assert _retry_delay("spend limit reached (monthly; resets 2027-02-01 00:00 UTC)", now) > 86400
    # limit with no parseable reset -> fixed fallback
    assert _retry_delay("API overloaded, please retry", now) == DEFAULT_WAIT
    # a reset already in the past still waits a floor, never a negative sleep
    assert _retry_delay("rate limit; resetsAt 1799999000", now) == 60


def test_claude_result_keeps_only_the_final_answer():
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "noise"}]}}) + "\n",
        json.dumps({"type": "result", "result": "what I changed", "usage": {"input_tokens": 10}}) + "\n",
    ]
    assert _claude_result(lines) == "what I changed"
    assert _claude_result(["not json\n"]) == ""



def test_plan_unlisted_also_catches_plain_prose():
    """A request written as a paragraph is still a request. Only headings were
    detected before, so free prose slipped through with no warning at all."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "trabajo.txt"
        p.write_text(
            "<!-- ENGINE-STATUS\nFase 1: DONE\n-->\n\n"
            "# comentario nuestro, no cuenta\n\n"
            "necesito que implementes un backtesting sin sesgos\n")
        assert plan_unlisted(str(p)) == ["necesito que implementes un backtesting sin sesgos"]

        # once queued, it stops being reported
        p.write_text(
            "<!-- ENGINE-STATUS\nnecesito que implementes un backtesting sin sesgos: TODO\n-->\n\n"
            "necesito que implementes un backtesting sin sesgos\n")
        assert plan_unlisted(str(p)) == []


def test_fail_line_reaches_beyond_python():
    """The gate is configurable now, so the parser has to survive whatever the
    project's own tools print — not just pytest --tb=line."""
    out = (
        "src/app.ts:12:5: error TS2345: Argument of type 'string'\n"   # tsc / eslint compact
        "main.go:8: undefined: foo\n"                                  # go vet
        "engine/cli.py:3:1: F401 unused import\n"                      # ruff text
        "2026-08-10T21:37:57Z ERROR codex: nope\n"                     # a timestamp is not a file
        "Traceback (most recent call last):\n"
    )
    assert [(m["file"], m["line"]) for m in FAIL_LINE.finditer(out)] == [
        ("src/app.ts", "12"), ("main.go", "8"), ("engine/cli.py", "3"),
    ]
    assert _text_errors("tests/test_api.py:7: TypeError: bad operand", "/repo") == [
        {"file": "tests/test_api.py", "line": 7, "description": "TypeError: bad operand"}
    ]


def test_json_errors_reads_ruff_and_eslint_and_declines_the_rest():
    ruff = json.dumps([{"filename": "/repo/a.py", "location": {"row": 4}, "code": "F401", "message": "unused"}])
    assert _json_errors(ruff, "/repo") == [{"file": "a.py", "line": 4, "description": "F401: unused"}]
    eslint = json.dumps([{"filePath": "/repo/a.js", "messages": [{"line": 9, "ruleId": "eqeqeq", "message": "use ==="}]}])
    assert _json_errors(eslint, "/repo") == [{"file": "a.js", "line": 9, "description": "eqeqeq: use ==="}]
    # a file with no findings is not a failure, and unknown shapes fall through to text parsing
    assert _json_errors(json.dumps([{"filePath": "/repo/a.js", "messages": []}]), "/repo") == []
    assert _json_errors("pytest output, not json", "/repo") is None
    assert _json_errors(json.dumps([{"weird": 1}]), "/repo") is None


def test_parse_cmd_distinguishes_default_from_disabled():
    assert parse_cmd(None, "pytest -q") == ["pytest", "-q"]   # not configured -> default
    assert parse_cmd("", "pytest -q") == []                   # explicitly disabled
    assert parse_cmd("npm test -- --ci", "pytest -q") == ["npm", "test", "--", "--ci"]


def test_claude_usage_is_read_off_the_result_event():
    lines = [
        json.dumps({"type": "assistant", "message": {"content": []}}) + "\n",
        json.dumps({"type": "result", "result": "ok", "total_cost_usd": 0.42,
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}}) + "\n",
    ]
    assert _claude_usage(lines) == {"cost_usd": 0.42, "input_tokens": 10,
                                    "cache_read_input_tokens": 90, "output_tokens": 5}
    assert _claude_usage(["not json\n"]) == {}  # a killed run must not crash the logger


def test_runlog_accumulates_money_and_writes_one_line_per_event():
    from engine import runlog
    with tempfile.TemporaryDirectory() as d:
        path = runlog.start(Path(d) / "runs")
        runlog.log("agent", label="coder", cost_usd=0.10, input_tokens=5)
        runlog.log("agent", label="coder", cost_usd=0.05, input_tokens=7, seconds=3)
        runlog.log("gate", errors=0)
        events = [json.loads(line) for line in path.read_text().splitlines()]
        assert [e["event"] for e in events] == ["agent", "agent", "gate"]
        assert all("ts" in e for e in events)
        # seconds must not be summed into the money totals, only *_usd / *_tokens
        assert runlog.totals() == {"cost_usd": 0.15000000000000002, "input_tokens": 12}
        assert "$0.15" in runlog.summary()
