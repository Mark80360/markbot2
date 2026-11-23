from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from markbot.optimizer.adapter import AssistantAdapter, SessionStore
from markbot.optimizer.evals import (
    Check,
    EvalCase,
    EvalSuite,
    Score,
    case_files,
    load_cases,
    score_text,
)
from markbot.schema.models import SessionMeta


def _session(
    root: Path,
    session_id: str,
    *,
    status: str = "completed",
    started_at: str = "2026-01-01T00:00:00+08:00",
    ended_at: str | None = "2026-01-01T00:05:00+08:00",
    domain: str = "general",
    request: str = "请求",
    trajectory: str | None = None,
) -> Path:
    directory = root / session_id
    directory.mkdir(parents=True)
    meta = SessionMeta(
        id=session_id,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        domain=domain,
        request=request,
    )
    (directory / "meta.json").write_text(meta.model_dump_json(), encoding="utf-8")
    (directory / "request.md").write_text(request, encoding="utf-8")
    if trajectory is not None:
        (directory / "trajectory.jsonl").write_text(trajectory, encoding="utf-8")
    return directory


def test_session_store_completed_filters_and_sorts(tmp_path):
    store = SessionStore(tmp_path)
    assert store.completed() == []

    root = tmp_path / "sessions"
    _session(root, "sess_b", ended_at="2026-01-02T00:00:00+08:00")
    _session(root, "sess_a", ended_at="2026-01-03T00:00:00+08:00")
    _session(root, "sess_running", status="running")
    broken = root / "sess_broken"
    broken.mkdir()
    (broken / "meta.json").write_text("{not json", encoding="utf-8")
    (root / "no-meta").mkdir()

    assert [meta.id for meta in store.completed()] == ["sess_b", "sess_a"]
    assert [meta.id for meta in store.completed(limit=1)] == ["sess_b"]
    assert [
        meta.id
        for meta in store.completed(before="2026-01-03T00:00:00+08:00")
    ] == ["sess_b"]
    assert store.completed(before="2026-01-02T00:00:00+08:00") == []


def test_session_store_reads_meta_request_and_trajectory(tmp_path):
    _session(
        tmp_path / "sessions",
        "sess_1",
        domain="code",
        request="整理文件",
        trajectory='{"role": "user"}\nnot-json\n{"role": "assistant"}\n',
    )
    store = SessionStore(tmp_path)

    assert store.meta("sess_1")["domain"] == "code"
    assert store.read("sess_1", "request.md") == "整理文件"
    assert store.path("sess_1", "request.md").exists()
    assert store.trajectory("sess_1") == [{"role": "user"}, {"role": "assistant"}]
    assert store.trajectory("missing") == []


def test_assistant_adapter_maps_sessions_and_deliverables(tmp_path):
    root = tmp_path / "sessions"
    _session(root, "sess_1", domain="code", request="写代码")
    adapter = AssistantAdapter(
        store=SessionStore(tmp_path),
        run_started_at="2026-01-02T00:00:00+08:00",
        learned_dir=tmp_path / "learned",
    )
    assert adapter.tasks() == ["sess_1"]
    assert adapter.domain_of("sess_1") == "code"
    assert adapter.task_prompt("sess_1") == "写代码"
    assert adapter.task_files("sess_1") is None
    (root / "sess_1" / "files").mkdir()
    assert adapter.task_files("sess_1") == root / "sess_1" / "files"

    assert adapter.base_harness() == tmp_path / "learned"
    assert adapter.spec.runs_root == tmp_path / "optimizer" / "runs"
    assert asyncio.run(adapter.score(tmp_path, "sess_1")) is None

    run_dir = tmp_path / "run"
    assert not adapter.delivered(run_dir)
    assert adapter.deliverable_text(run_dir) == ""
    (run_dir / "deliverable").mkdir(parents=True)
    (run_dir / "deliverable" / "final.md").write_text("结果", encoding="utf-8")
    assert adapter.delivered(run_dir)
    assert adapter.deliverable_text(run_dir) == "结果"


def test_assistant_adapter_execute_requires_runner(tmp_path):
    adapter = AssistantAdapter(
        store=SessionStore(tmp_path),
        run_started_at="2026-01-02T00:00:00+08:00",
        learned_dir=tmp_path / "learned",
    )
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.execute("sess_1", tmp_path, "run-1"))

    calls: list[tuple] = []

    async def runner(task_id: str, harness_dir: Path, run_id: str) -> Path:
        calls.append((task_id, harness_dir, run_id))
        return harness_dir / "out"

    adapter.runner = runner
    result = asyncio.run(adapter.execute("sess_1", tmp_path, "run-1"))
    assert result == tmp_path / "out"
    assert calls == [("sess_1", tmp_path, "run-1")]


def test_assistant_adapter_validate_tree(tmp_path):
    adapter = AssistantAdapter(
        store=SessionStore(tmp_path),
        run_started_at="2026-01-02T00:00:00+08:00",
        learned_dir=tmp_path / "learned",
    )
    adapter.validate_tree(
        {
            "strategy/system_prompt.md": "prompt",
            "skills/demo/SKILL.md": "skill",
            "memory/wiki/index.json": "{}",
        }
    )
    with pytest.raises(ValueError, match="越界路径"):
        adapter.validate_tree({"packages/demo.py": "x"})


def test_load_cases_skips_invalid_files(tmp_path):
    (tmp_path / "ok.yaml").write_text(
        "id: c1\n"
        "prompt: 你好\n"
        "domain: code\n"
        "files: [a.txt]\n"
        "checks:\n"
        "  - type: contains\n"
        "    value: ok\n"
        "  - bad\n",
        encoding="utf-8",
    )
    (tmp_path / "broken.yaml").write_text("id: [", encoding="utf-8")
    (tmp_path / "no-id.yaml").write_text("prompt: x\n", encoding="utf-8")
    (tmp_path / "no-prompt.yaml").write_text("id: y\n", encoding="utf-8")
    (tmp_path / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")

    cases = load_cases(tmp_path)
    assert [case.id for case in cases] == ["c1"]
    case = cases[0]
    assert case.domain == "code"
    assert case.files == ("a.txt",)
    assert len(case.checks) == 1
    assert case_files(case, tmp_path) == [tmp_path / "a.txt"]


def test_score_text_checks_and_ratio():
    case = EvalCase(
        id="c1",
        prompt="p",
        checks=(
            Check(type="contains", value="hello"),
            Check(type="not_contains", value="bad"),
            Check(type="regex", value=r"^hi"),
            Check(type="regex", value="["),
            Check(type="llm_judge", value="", rubric="r"),
        ),
    )
    score = score_text("hi hello world", case)
    assert score.passed == 3
    assert score.total == 4
    assert score.ratio == 0.75
    assert str(score) == "3/4"
    assert score.details[-1] == "c1:regex=False"
    assert Score().ratio == 0.0


def test_eval_suite_continues_after_case_error():
    cases = [
        EvalCase(id="c1", prompt="p", checks=(Check(type="contains", value="ok"),)),
        EvalCase(id="c2", prompt="p", checks=(Check(type="contains", value="ok"),)),
    ]

    async def run_case(harness: Path, case: EvalCase) -> str:
        if case.id == "c2":
            raise RuntimeError("boom")
        return "ok"

    suite = EvalSuite(cases, run_case)
    score = asyncio.run(suite.score(Path("/tmp/harness")))
    assert score.passed == 1
    assert score.total == 2
    assert any("c2: error=boom" in detail for detail in score.details)
