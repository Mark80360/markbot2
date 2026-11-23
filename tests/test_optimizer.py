from __future__ import annotations

import asyncio
import json
from pathlib import Path

from markbot.kernel.providers.echo import EchoProvider
from markbot.optimizer.evals import EvalCase, Score
from markbot.optimizer.gate import evaluate, illegal_paths, new_capabilities
from markbot.optimizer.run import OptimizerSettings, optimize
from markbot.optimizer.snapshots import (
    list_snapshots,
    promote,
    read_tree,
    restore_snapshot,
    write_tree,
)
from markbot.schema.models import SessionMeta


async def _ok(_harness: Path) -> Score:
    return Score(passed=1, total=1)


def _write_skill(root: Path, tools: list[str]) -> None:
    path = root / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "name: demo\n"
        "description: demo\n"
        f"allowed-tools: {json.dumps(tools)}\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )


def test_gate_rejects_identical(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    current.mkdir()
    candidate.mkdir()
    result = asyncio.run(evaluate(candidate, current, _ok))
    assert not result.passed
    assert any("完全相同" in reason for reason in result.reasons)


def test_gate_rejects_illegal_paths(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    current.mkdir()
    candidate.mkdir()
    (candidate / "packages").mkdir()
    (candidate / "packages" / "x.py").write_text("x", encoding="utf-8")
    assert illegal_paths(candidate) == ["packages/x.py"]
    result = asyncio.run(evaluate(candidate, current, _ok))
    assert not result.passed
    assert any("越界路径" in reason for reason in result.reasons)


def test_gate_rejects_new_capabilities(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    current.mkdir()
    candidate.mkdir()
    _write_skill(current, [])
    _write_skill(candidate, ["run_script"])
    assert new_capabilities(candidate, current) == ["skill:demo:run_script"]
    result = asyncio.run(evaluate(candidate, current, _ok))
    assert not result.passed
    assert any("新的权限" in reason for reason in result.reasons)


def test_gate_rejects_regression(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    write_tree(current, {"strategy/system_prompt.md": "a"})
    write_tree(candidate, {"strategy/system_prompt.md": "b"})

    async def scored(harness: Path) -> Score:
        return Score(passed=1, total=1) if harness == current else Score(passed=0, total=1)

    result = asyncio.run(evaluate(candidate, current, scored))
    assert not result.passed
    assert any("回归掉分" in reason for reason in result.reasons)


def test_snapshot_promote_and_rollback(tmp_path):
    learned = tmp_path / "learned"
    snapshots = tmp_path / "snapshots"
    write_tree(learned, {"strategy/system_prompt.md": "base\n"})

    parent, new_hash = promote(
        {"strategy/system_prompt.md": "better\n", "manifest.json": "{}"},
        learned,
        snapshots,
        origin_task="sess_1",
    )
    assert read_tree(learned)["strategy/system_prompt.md"] == "better\n"
    assert (learned / "manifest.json").exists()
    assert list_snapshots(snapshots)

    restored = restore_snapshot(learned, snapshots)
    assert restored == parent
    assert read_tree(learned)["strategy/system_prompt.md"] == "base\n"
    assert new_hash.startswith("sha256:")


def _prepare_home(home: Path, project: Path) -> Path:
    learned = home / "learned"
    write_tree(learned, {"strategy/system_prompt.md": "base\n"})
    session = home / "sessions" / "sess_1"
    session.mkdir(parents=True)
    meta = SessionMeta(
        id="sess_1",
        started_at="2026-01-01T00:00:00+08:00",
        ended_at="2026-01-01T00:05:00+08:00",
        status="completed",
        domain="general",
        steps=2,
        tool_calls=1,
        request="整理文件",
    )
    (session / "meta.json").write_text(meta.model_dump_json(), encoding="utf-8")
    (session / "request.md").write_text("整理文件", encoding="utf-8")
    (session / "trajectory.jsonl").write_text(
        json.dumps({"role": "tool", "is_error": True, "content": "boom"}) + "\n",
        encoding="utf-8",
    )
    cases = project / "evals" / "cases"
    cases.mkdir(parents=True)
    (cases / "case.yaml").write_text(
        "id: c1\nprompt: hi\nchecks:\n  - type: contains\n    value: hello-markbot\n",
        encoding="utf-8",
    )
    return learned


def test_optimize_promotes_and_is_rollbackable(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    learned = _prepare_home(home, project)

    async def run_case(harness_dir: Path, case: EvalCase) -> str:
        return "hello-markbot"

    report = asyncio.run(
        optimize(
            home=home,
            learned_dir=learned,
            evals_dir=project / "evals" / "cases",
            provider=EchoProvider(),
            run_case=run_case,
            # 单元测试只放 1 条用例；真实运行默认要求 >= 10 条
            settings=OptimizerSettings(max_tasks_per_run=5, min_eval_cases=1),
            emit=lambda *_: None,
        )
    )
    assert report.promoted, report.reasons
    assert report.selected in {"inv", "dom"}
    tree = read_tree(learned)
    assert "strategy/system_prompt.md" in tree
    assert "经验约束" in tree["strategy/system_prompt.md"]
    assert (learned / "manifest.json").exists()
    assert list_snapshots(home / "snapshots")

    restore_snapshot(learned, home / "snapshots", report.parent_snapshot)
    assert read_tree(learned)["strategy/system_prompt.md"] == "base\n"


def test_optimize_rejects_when_regression_drops(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    learned = _prepare_home(home, project)

    async def run_case(harness_dir: Path, case: EvalCase) -> str:
        return "" if "candidates" in str(harness_dir) else "hello-markbot"

    report = asyncio.run(
        optimize(
            home=home,
            learned_dir=learned,
            evals_dir=project / "evals" / "cases",
            provider=EchoProvider(),
            run_case=run_case,
            emit=lambda *_: None,
        )
    )
    assert not report.promoted
    assert any("低于基线" in reason for reason in report.reasons)
    assert read_tree(learned)["strategy/system_prompt.md"] == "base\n"


def test_optimize_dry_run_does_not_promote(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    learned = _prepare_home(home, project)

    async def run_case(harness_dir: Path, case: EvalCase) -> str:
        return "hello-markbot"

    report = asyncio.run(
        optimize(
            home=home,
            learned_dir=learned,
            evals_dir=project / "evals" / "cases",
            provider=EchoProvider(),
            run_case=run_case,
            dry_run=True,
            emit=lambda *_: None,
        )
    )
    assert not report.promoted
    assert any("dry-run" in reason for reason in report.reasons)
    assert read_tree(learned)["strategy/system_prompt.md"] == "base\n"


def test_optimize_refuses_with_too_few_cases(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    learned = _prepare_home(home, project)

    async def run_case(harness_dir: Path, case: EvalCase) -> str:
        return "hello-markbot"

    report = asyncio.run(
        optimize(
            home=home,
            learned_dir=learned,
            evals_dir=project / "evals" / "cases",
            provider=EchoProvider(),
            run_case=run_case,
            settings=OptimizerSettings(min_eval_cases=10),
            emit=lambda *_: None,
        )
    )
    assert not report.promoted
    assert any("min_eval_cases" in reason for reason in report.reasons)
    assert read_tree(learned) == {"strategy/system_prompt.md": "base\n"}


def test_skill_candidates_wait_for_approval(tmp_path):
    from markbot.optimizer.run import _skill_changes

    assert _skill_changes({}, {"skills/x/SKILL.md": "a"}) == ["skills/x/SKILL.md"]
    assert _skill_changes({"skills/x/SKILL.md": "a"}, {"skills/x/SKILL.md": "a"}) == []
    assert _skill_changes({"skills/x/SKILL.md": "a"}, {}) == ["skills/x/SKILL.md"]


def test_proposals_do_not_embed_session_text(tmp_path):
    from markbot.optimizer.run import _fallback_prompt, propose_dom

    secret_request = "帮我把合同发给张三 13800000000"
    tree = propose_dom({}, f"request: {secret_request}", "office")
    assert all(secret_request not in text for text in tree.values())
    assert secret_request not in _fallback_prompt("base", f"request: {secret_request}")


def test_llm_judge_is_scored_when_judge_given():
    from markbot.optimizer.evals import Check, score_case

    case = EvalCase(
        id="j",
        prompt="p",
        checks=(Check(type="contains", value="ok"), Check(type="llm_judge", rubric="礼貌")),
    )
    assert asyncio.run(score_case("ok", case)).total == 1  # 无 judge 时跳过
    passed = asyncio.run(score_case("ok", case, judge=lambda text, rubric: "ok" in text))
    assert (passed.passed, passed.total) == (2, 2)

    async def failing(text, rubric):
        raise RuntimeError("judge down")

    failed = asyncio.run(score_case("ok", case, judge=failing))
    assert (failed.passed, failed.total) == (1, 2)
