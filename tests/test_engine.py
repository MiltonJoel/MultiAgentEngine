import json
import tempfile
from pathlib import Path

from engine.cli import CODEX_NOISE, DEFAULT_WAIT, _claude_result, _claude_usage, _retry_delay
from engine.graph import (
    MAX_ITERATIONS,
    after_audit,
    after_review,
    after_tests,
    audit_node,
    plan_done,
    plan_items,
    plan_open,
    plan_states,
    plan_unparsed,
)
from engine.nodes.reviewer import _parse_problems
from engine.nodes.tester import FAIL_LINE, _json_errors, _text_errors


def test_after_tests():
    with tempfile.TemporaryDirectory() as d:
        todo, done = Path(d) / "todo.md", Path(d) / "done.md"
        todo.write_text("<!-- ENGINE-STATUS\nLogin: DONE\nSignup: TODO\n-->\n")
        done.write_text("<!-- ENGINE-STATUS\nLogin: DONE\nSignup: DONE\n-->\n")

        def route(tests_passed, plan, clean=False, iteration=0):
            return after_tests({"tests_passed": tests_passed, "plan": str(plan), "audit_clean": clean,
                                "iteration": iteration, "max_iterations": MAX_ITERATIONS})

        assert route(False, done) == "reviewer"               # red -> analyse
        assert route(True, todo) == "coder"                   # green, unfinished -> keep working
        assert route(True, done) == "auditor"                 # green, finished, not clean yet -> audit (again)
        assert route(True, done, clean=True) == "__end__"     # clean audit -> done
        assert route(False, done, clean=True) == "reviewer"   # red still gets reviewed
        assert route(True, todo, iteration=MAX_ITERATIONS) == "__end__"  # cap wins over everything
        assert route(True, done, iteration=MAX_ITERATIONS) == "__end__"


def test_after_audit_and_review():
    finding = [{"file": "a.py", "line": 1, "description": "stub", "suggested_fix": "implement"}]
    assert after_audit({"problems": finding, "iteration": 0, "max_iterations": MAX_ITERATIONS}) == "coder"
    assert after_audit({"problems": [], "iteration": 0, "max_iterations": MAX_ITERATIONS}) == "__end__"
    assert after_audit({"problems": finding, "iteration": MAX_ITERATIONS, "max_iterations": MAX_ITERATIONS}) == "__end__"
    assert after_review({"iteration": MAX_ITERATIONS, "max_iterations": MAX_ITERATIONS}) == "__end__"
    assert after_review({"iteration": 1, "max_iterations": MAX_ITERATIONS}) == "coder"


def test_plan_done():
    prose = "\n### Login\n\n* Implement it.\n\n```python\nx: int = 1  # a colon in code\n```\n"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "plan.md"
        for text, expected in [
            (prose, False),  # no block yet (first run) -> not done, or the loop would never start
            ("<!-- ENGINE-STATUS\nLogin: TODO\n-->" + prose, False),
            ("<!-- ENGINE-STATUS\nLogin: DONE\nSignup: IN_PROGRESS\n-->" + prose, False),
            ("<!-- ENGINE-STATUS\nLogin: DONE\nSignup: DONE\n-->" + prose, True),
            # BLOCKED counts as finished, with or without a reason
            ("<!-- ENGINE-STATUS\nLogin: DONE\nSignup: BLOCKED need API key\n-->" + prose, True),
            ("<!-- ENGINE-STATUS\nLogin: DONE\nSignup: BLOCKED\n-->" + prose, True),
            # a reason must not mask a still-open item
            ("<!-- ENGINE-STATUS\nLogin: BLOCKED waiting\nSignup: TODO see notes\n-->" + prose, False),
            # dashes and colons inside the name must not break parsing
            ("<!-- ENGINE-STATUS\nStep 6 — Orders: state machine: DONE\n-->" + prose, True),
        ]:
            p.write_text(text)
            assert plan_done(str(p)) is expected, text[:70]

        # prose outside the block is never parsed as status
        p.write_text("Step 9: TODO\n" + prose)
        assert plan_states(str(p)) == []


def test_plan_unparsed():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "plan.md"
        p.write_text(
            "<!-- ENGINE-STATUS\n"
            "Login: DONE\n"
            "Signup: PENDING\n"          # unknown state
            "Billing no colon TODO\n"    # missing colon
            "Search: TODO review section 5\n"
            "-->\n")
        assert plan_states(str(p)) == ["DONE", "TODO"]
        assert plan_unparsed(str(p)) == ["Signup: PENDING", "Billing no colon TODO"]
        p.write_text("<!-- ENGINE-STATUS\nLogin: DONE\n-->\n")
        assert plan_unparsed(str(p)) == []


def test_plan_open_names_the_units():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "plan.md"
        p.write_text(
            "<!-- ENGINE-STATUS\n"
            "Step 1 — Login: DONE\n"
            "Step 6 — Orders: state machine: TODO — changed the timeout\n"
            "Step 7: IN_PROGRESS\n"
            "Step 8: BLOCKED missing keys\n"
            "-->\n")
        assert plan_open(str(p)) == ["Step 6 — Orders: state machine", "Step 7"]


def test_fail_line_parses_many_tools():
    out = (
        "/home/x/proj/test_bot.py:42: AssertionError: assert 1 == 2\n"   # pytest --tb=line
        "src/app.ts:12:5: error TS2345: Argument of type 'string'\n"    # tsc / eslint compact
        "main.go:8: undefined: foo\n"                                   # go vet
        "engine/cli.py:3:1: F401 unused import\n"                       # ruff text
        "2026-08-10T21:37:57Z ERROR codex: nope\n"                      # a timestamp is not a file
        "1 failed, 2 passed in 0.03s\n"
    )
    assert [(m["file"], m["line"]) for m in FAIL_LINE.finditer(out)] == [
        ("/home/x/proj/test_bot.py", "42"), ("src/app.ts", "12"), ("main.go", "8"), ("engine/cli.py", "3"),
    ]
    assert _text_errors("tests/test_api.py:7: TypeError: bad operand", "/repo") == [
        {"file": "tests/test_api.py", "line": 7, "description": "TypeError: bad operand"}
    ]


def test_json_errors_reads_ruff_and_eslint_and_declines_the_rest():
    ruff = json.dumps([{"filename": "/repo/a.py", "location": {"row": 4}, "code": "F401", "message": "unused"}])
    assert _json_errors(ruff, "/repo") == [{"file": "a.py", "line": 4, "description": "F401: unused"}]
    eslint = json.dumps([{"filePath": "/repo/a.js", "messages": [{"line": 9, "ruleId": "eqeqeq", "message": "use ==="}]}])
    assert _json_errors(eslint, "/repo") == [{"file": "a.js", "line": 9, "description": "eqeqeq: use ==="}]
    assert _json_errors(json.dumps([{"filePath": "/repo/a.js", "messages": []}]), "/repo") == []
    assert _json_errors("pytest output, not json", "/repo") is None
    assert _json_errors(json.dumps([{"weird": 1}]), "/repo") is None


def test_parse_problems_tolerates_fences_and_junk():
    payload = {"problems": [{"file": "bot.py", "line": 42, "description": "d", "suggested_fix": "f"}]}
    expected = [{"unit": "", **payload["problems"][0]}]
    assert _parse_problems(json.dumps(payload)) == expected
    assert _parse_problems(f"```json\n{json.dumps(payload)}\n```") == expected
    assert _parse_problems("codex had a bad day")[0]["description"] == "unreadable reply: codex had a bad day"


def test_codex_noise_is_recognised():
    real = ("2026-08-10T21:37:57Z ERROR codex_models_manager::manager: failed to refresh "
            'available models: unknown variant `max`; body: {"models":[...]}')
    assert CODEX_NOISE.search(real)
    assert not CODEX_NOISE.search("models refreshed, 3 available\n")


def test_retry_delay():
    now = 1_800_000_000.0
    assert _retry_delay("TypeError: bad operand", now) == 0
    assert _retry_delay("Claude AI usage limit reached|1800000600", now) == 600 + 30
    assert _retry_delay("spend limit reached (monthly; resets 2027-02-01 00:00 UTC)", now) > 86400
    assert _retry_delay("API overloaded, please retry", now) == DEFAULT_WAIT
    assert _retry_delay("rate limit; resetsAt 1799999000", now) == 60  # past reset -> floor, never negative


def test_claude_result_and_usage_come_from_the_final_event():
    lines = [
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "noise"}]}}) + "\n",
        json.dumps({"type": "result", "result": "what I changed", "total_cost_usd": 0.42,
                    "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}}) + "\n",
    ]
    assert _claude_result(lines) == "what I changed"
    assert _claude_usage(lines) == {"cost_usd": 0.42, "input_tokens": 10,
                                    "cache_read_input_tokens": 90, "output_tokens": 5}
    assert _claude_result(["not json\n"]) == ""
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


def test_audit_findings_reopen_done_units_until_the_audit_is_clean():
    from engine.cli import RUNNERS
    body = "\nNotes the user wrote.\n"
    findings = {"problems": [
        {"unit": "Step 1 — Login", "file": "a.py", "line": 3, "description": "no test\ncovers --> lockout",
         "suggested_fix": "add one"},
        {"unit": "step 1 — login", "file": "a.py", "line": 9, "description": "second finding", "suggested_fix": "x"},
        {"unit": "Step 3", "file": "b.py", "line": 1, "description": "stub", "suggested_fix": "x"},  # BLOCKED
        {"unit": "Nope", "file": "c.py", "line": 1, "description": "unknown unit", "suggested_fix": "x"},
    ]}
    with tempfile.TemporaryDirectory() as d:
        spec, plan = Path(d) / "spec.md", Path(d) / "plan.md"
        spec.write_text("# Spec\n")
        plan.write_text("<!-- ENGINE-STATUS\nStep 1 — Login: DONE\nStep 2: DONE\nStep 3: BLOCKED no keys\n-->" + body)
        state = {"repo": d, "spec": str(spec), "plan": str(plan), "agents": {"auditor": "fake"},
                 "git_branch": None, "base_sha": None, "focus": [], "iteration": 1, "max_iterations": MAX_ITERATIONS}

        reply = findings
        RUNNERS["fake"] = lambda prompt, cwd, label, **kw: json.dumps(reply)
        try:
            update = audit_node(state)
            assert update["audit_clean"] is False and after_audit({**state, **update}) == "coder"
            assert dict(plan_items(str(plan))) == {"Step 1 — Login": "TODO", "Step 2": "DONE", "Step 3": "BLOCKED"}
            text = plan.read_text()
            assert "Step 1 — Login: TODO — audit: no test covers → lockout\n" in text  # one line, block intact
            assert plan_unparsed(str(plan)) == [] and text.endswith(body)

            # coder re-marks it DONE -> a green build must be audited again, not end
            plan.write_text(text.replace("Step 1 — Login: TODO — audit: no test covers → lockout", "Step 1 — Login: DONE"))
            assert after_tests({**state, **update, "tests_passed": True}) == "auditor"

            reply = {"problems": []}
            update = audit_node(state)
            assert update["audit_clean"] is True and after_audit({**state, **update}) == "__end__"
            assert after_tests({**state, **update, "tests_passed": True}) == "__end__"
        finally:
            del RUNNERS["fake"]
