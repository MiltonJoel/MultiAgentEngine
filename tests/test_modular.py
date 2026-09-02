"""The three seams: which CLI runs a role, where prompts come from, what gates the coder."""
import subprocess
import tempfile
from pathlib import Path

import pytest

from engine import git, prompts
from engine.cli import DEFAULT_AGENTS, RUNNERS, run_agent
from engine.main import _agents, build_parser
from engine.nodes.reviewer import NO_DIFF, _diff
from engine.nodes.tester import DEFAULT_TEST_CMD, build_gates


def test_run_agent_dispatches_by_name_and_refuses_typos():
    seen = {}
    RUNNERS["fake"] = lambda prompt, cwd, label, **kw: seen.update(prompt=prompt, cwd=cwd, label=label, **kw) or "ok"
    try:
        assert run_agent("fake", "p", "/tmp", label="reviewer", write=True) == "ok"
        assert seen == {"prompt": "p", "cwd": "/tmp", "label": "reviewer", "write": True}
        with pytest.raises(SystemExit):
            run_agent("gpt5", "p", "/tmp", label="coder")
    finally:
        del RUNNERS["fake"]


def test_every_role_has_a_runner_and_a_prompt():
    for role, cli in DEFAULT_AGENTS.items():
        assert cli in RUNNERS
        assert prompts.path(role).is_file(), role


def test_agents_resolve_from_config_then_flags():
    parser = build_parser()
    assert _agents(parser, {}, []) == DEFAULT_AGENTS
    # config first, flag on top of it
    assert _agents(parser, {"reviewer": "claude"}, ["coder=codex"]) == {
        **DEFAULT_AGENTS, "reviewer": "claude", "coder": "codex"}
    for bad in (["coder"], ["tester=claude"], ["coder=gpt5"]):
        with pytest.raises(SystemExit):
            _agents(parser, {}, bad)
    with pytest.raises(SystemExit):
        _agents(parser, {"tester": "claude"}, [])


def test_prompt_override_lives_in_the_target_repo():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        assert prompts.path("coder", repo) == prompts.BUILTIN / "coder.md"
        override = repo / prompts.OVERRIDE / "coder.md"
        override.parent.mkdir(parents=True)
        override.write_text("spec is {spec}\n")
        assert prompts.path("coder", repo) == override
        assert prompts.load("coder", repo, spec="S") == "spec is S\n"
        # a prompt asking for something the node doesn't provide must say so, not KeyError
        override.write_text("{nonsense}")
        with pytest.raises(SystemExit):
            prompts.load("coder", repo, spec="S")


def test_builtin_prompts_only_use_placeholders_the_nodes_pass():
    provided = {
        "coder": {"spec_path", "spec", "plan_path", "plan", "errors", "problems"},
        "reviewer": {"spec", "plan", "errors", "diff"},
        "auditor": {"spec", "plan", "focus", "diff"},
    }
    for name, fields in provided.items():
        # renders with exactly those fields, so a stale placeholder fails here, not at 3am
        prompts.load(name, None, **dict.fromkeys(fields, "x"))


def test_build_gates_shortcut_and_general_form():
    default = build_gates(None, None, None)
    assert [g["cmd"][0] for g in default] == ["pytest", "ruff"]
    assert default[0]["ok_codes"] == (0, 5)  # pytest: "no tests collected" is not a failure
    # a disabled gate disappears instead of running an empty command
    assert build_gates(None, "", "") == []
    assert build_gates(None, None, "")[0]["cmd"] == DEFAULT_TEST_CMD.split()
    # the general form: any number of tools, in order
    gates = build_gates([{"cmd": "npm test", "ok_codes": [0]}, {"cmd": "tsc --noEmit"}], None, None)
    assert [g["cmd"] for g in gates] == [["npm", "test"], ["tsc", "--noEmit"]]
    assert gates[1]["ok_codes"] == (0,)
    with pytest.raises(SystemExit):
        build_gates([{"command": "npm test"}], None, None)


def test_diff_reaches_the_reviewer_and_degrades_without_git():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        (repo / "a.py").write_text("x = 1\n")
        git.checkpoint(str(repo), "base")
        base = git.head(str(repo))
        (repo / "a.py").write_text("x = 2\n")
        git.checkpoint(str(repo), "engine: iteration 1")

        state = {"repo": str(repo), "git_branch": "engine/x", "base_sha": base}
        diff = _diff(state, "HEAD~1")
        assert "-x = 1" in diff and "+x = 2" in diff
        assert _diff(state, base) == diff  # one pass since the run started
        # no git, or no commit to compare against: say so instead of sending ""
        assert _diff({**state, "git_branch": None}, "HEAD~1") == NO_DIFF
        assert _diff(state, None) == NO_DIFF


def test_long_diffs_are_truncated_before_they_reach_a_prompt():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for key, value in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        (repo / "a.py").write_text("x = 1\n")
        git.checkpoint(str(repo), "base")
        (repo / "a.py").write_text("\n".join(f"line {i}" for i in range(5000)))
        git.checkpoint(str(repo), "big")
        body = git.diff(str(repo))
        assert len(body) < git.MAX_DIFF_CHARS + 200
        assert "truncated" in body
