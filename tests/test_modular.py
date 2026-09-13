"""The seams: which CLI runs a role, where prompts and documents come from, what gates the coder."""
import subprocess
import tempfile
from pathlib import Path

import pytest

from engine import git, prompts
from engine.cli import DEFAULT_AGENTS, RUNNERS, run_agent
from engine.main import DEFAULT_PLAN, DEFAULT_SPEC, MAE_DIR, _agents, _doc, _repo_config, build_parser
from engine.nodes.reviewer import NO_DIFF, _diff
from engine.nodes.tester import DEFAULT_GATES, build_gates


def _init(repo: Path):
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)


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
        prompts.load(name, None, **dict.fromkeys(fields, "x"))


def test_build_gates():
    default = build_gates(None)
    assert [g["cmd"][0] for g in default] == ["pytest", "ruff"]
    assert default[0]["ok_codes"] == (0, 5)  # "no tests collected" is not a failure
    assert build_gates([]) == build_gates([""]) == []  # disabled
    gates = build_gates(["npm test", {"cmd": "tsc --noEmit", "ok_codes": [0, 1]}])
    assert [g["cmd"] for g in gates] == [["npm", "test"], ["tsc", "--noEmit"]]
    assert [g["ok_codes"] for g in gates] == [(0,), (0, 1)]
    for bad in ([{"command": "npm test"}], [42]):
        with pytest.raises(SystemExit):
            build_gates(bad)
    assert build_gates(DEFAULT_GATES) == default


def test_diff_reaches_the_reviewer_and_degrades_without_git():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _init(repo)
        (repo / "a.py").write_text("x = 1\n")
        git.checkpoint(str(repo), "base")
        base = git.head(str(repo))
        (repo / "a.py").write_text("x = 2\n")
        git.checkpoint(str(repo), "engine: iteration 1")

        state = {"repo": str(repo), "git_branch": "engine/x", "base_sha": base}
        diff = _diff(state, "HEAD~1")
        assert "-x = 1" in diff and "+x = 2" in diff
        assert _diff(state, base) == diff
        assert _diff({**state, "git_branch": None}, "HEAD~1") == NO_DIFF
        assert _diff(state, None) == NO_DIFF


def test_long_diffs_are_truncated_before_they_reach_a_prompt():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        _init(repo)
        (repo / "a.py").write_text("x = 1\n")
        git.checkpoint(str(repo), "base")
        (repo / "a.py").write_text("\n".join(f"line {i}" for i in range(5000)))
        git.checkpoint(str(repo), "big")
        body = git.diff(str(repo))
        assert len(body) < git.MAX_DIFF_CHARS + 200
        assert "truncated" in body


def test_documents_live_in_the_target_repo_not_in_the_package():
    parser = build_parser()
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        with pytest.raises(SystemExit):
            _doc(parser, None, None, repo, DEFAULT_PLAN, "plan")

        default = repo / MAE_DIR / DEFAULT_SPEC
        default.parent.mkdir()
        default.write_text("arch")
        assert _doc(parser, None, None, repo, DEFAULT_SPEC, "spec") == default

        (repo / "docs").mkdir()
        (repo / "docs" / "arch.md").write_text("arch")
        assert _doc(parser, None, "docs/arch.md", repo, DEFAULT_SPEC, "spec") == repo / "docs" / "arch.md"

        elsewhere = repo / "elsewhere.md"
        elsewhere.write_text("arch")
        assert _doc(parser, str(elsewhere), "docs/arch.md", repo, DEFAULT_SPEC, "spec") == elsewhere


def test_repo_config_is_optional_and_loud_when_broken():
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        assert _repo_config(repo) == {}
        (repo / MAE_DIR).mkdir()
        (repo / MAE_DIR / "config.json").write_text('{"max_iterations": 9}')
        assert _repo_config(repo) == {"max_iterations": 9}
        (repo / MAE_DIR / "config.json").write_text("{not json")
        with pytest.raises(SystemExit):
            _repo_config(repo)
