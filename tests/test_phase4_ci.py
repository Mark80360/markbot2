"""Phase 4（ADR 0016）：评测趋势、回归判定、晋升门禁评测集、eval 命令的门禁参数、CI 配置。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from markbot.cli import eval_cmd
from markbot.cli.eval_cmd import CaseResult
from markbot.optimizer import gate
from markbot.optimizer.evals import Score
from markbot.optimizer.run import OptimizerSettings
from markbot.optimizer.trend import (
    EvalRecord,
    append_record,
    baseline_failures,
    flipped_cases,
    load_baselines,
    load_history,
    record_from_results,
    regressions,
    trend_table,
)

REPO = Path(__file__).resolve().parents[1]


def _results(**cases: bool) -> list[CaseResult]:
    return [CaseResult(name, ok, "通过" if ok else "check 未通过", seconds=1.0, tokens=10) for name, ok in cases.items()]


# ================================================================== 趋势


def test_record_roundtrip_and_mixed_file(tmp_path):
    trend = tmp_path / "logs" / "optimizer.jsonl"
    trend.parent.mkdir(parents=True)
    # 优化器原有的运行记录（没有 kind）与坏行都要跳过
    trend.write_text('{"run_id": "run_1", "promoted": false}\nnot json\n', encoding="utf-8")
    record = record_from_results("coding", _results(a=True, b=False), model="m", revision="abc")
    assert (record.passed, record.total, record.tokens) == (1, 2, 20)
    assert record.reasons == {"b": "check 未通过"}
    append_record(trend, record)
    append_record(trend, record_from_results("browser", _results(x=True)))
    history = load_history(trend, "coding")
    assert len(history) == 1 and history[0].cases == {"a": True, "b": False}
    assert history[0].model == "m" and len(load_history(trend)) == 2
    assert "| coding | 1/2 | 50% | m | abc |" in trend_table(history)


def test_regressions_ratio_and_flips():
    before = EvalRecord("coding", {"a": True, "b": True, "c": False, "d": True})
    same_ratio_flip = EvalRecord("coding", {"a": True, "b": False, "c": True, "d": True})
    assert flipped_cases(before, same_ratio_flip) == ["b"]
    assert any("由通过变为失败" in item for item in regressions(before, same_ratio_flip))
    assert regressions(before, same_ratio_flip, strict_cases=False) == []
    drop = EvalRecord("coding", {"a": True, "b": False, "c": False, "d": False})
    assert any("通过率下降" in item for item in regressions(before, drop, strict_cases=False))
    assert regressions(before, drop, tolerance=0.6, strict_cases=False) == []
    assert regressions(None, drop) == []


def test_baselines(tmp_path):
    repo = load_baselines(REPO / "evals" / "baselines.json")
    assert {"coding", "browser"} <= set(repo)
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"coding": 0.8, "browser": {"min_ratio": 0.5}, "bad": "x"}), encoding="utf-8")
    floors = load_baselines(path)
    assert floors == {"coding": 0.8, "browser": 0.5}
    assert baseline_failures(EvalRecord("coding", {"a": True, "b": False}), floors)
    assert baseline_failures(EvalRecord("browser", {"a": True, "b": False}), floors) == []
    assert load_baselines(tmp_path / "missing.json") == {}


# ================================================================== 晋升门禁


def _dirs(tmp_path):
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    (current / "strategy").mkdir(parents=True)
    (candidate / "strategy").mkdir(parents=True)
    (current / "strategy" / "system_prompt.md").write_text("old\n", encoding="utf-8")
    (candidate / "strategy" / "system_prompt.md").write_text("new\n", encoding="utf-8")
    return candidate, current


def _runner(scores: dict[str, Score], calls: list[str] | None = None):
    async def run(directory: Path) -> Score:
        if calls is not None:
            calls.append(directory.name)
        return scores[directory.name]

    return run


def test_gate_rejects_candidate_that_drops_on_extra_suite(tmp_path):
    candidate, current = _dirs(tmp_path)
    regression = _runner({"current": Score(9, 10), "candidate": Score(10, 10)})
    coding = _runner({"current": Score(4, 5), "candidate": Score(3, 5)})
    result = asyncio.run(gate.evaluate(candidate, current, regression, extra_suites={"coding": coding}))
    assert not result.passed and any("评测集 coding 掉分" in reason for reason in result.reasons)
    assert result.scores == {"regression": "9/10 -> 10/10", "coding": "4/5 -> 3/5"}
    tolerant = asyncio.run(
        gate.evaluate(candidate, current, regression, extra_suites={"coding": coding}, tolerance=0.25)
    )
    assert tolerant.passed


def test_gate_passes_when_extra_suites_hold(tmp_path):
    candidate, current = _dirs(tmp_path)
    regression = _runner({"current": Score(9, 10), "candidate": Score(9, 10)})
    coding = _runner({"current": Score(4, 5), "candidate": Score(5, 5)})
    browser = _runner({"current": Score(0, 0), "candidate": Score(0, 0)})
    result = asyncio.run(
        gate.evaluate(candidate, current, regression, extra_suites={"coding": coding, "browser": browser})
    )
    assert result.passed, result.reasons
    assert result.as_dict()["scores"]["coding"] == "4/5 -> 5/5"


def test_gate_skips_extra_suites_when_already_failing(tmp_path):
    candidate, current = _dirs(tmp_path)
    regression = _runner({"current": Score(9, 10), "candidate": Score(5, 10)})
    calls: list[str] = []
    coding = _runner({"current": Score(1, 1), "candidate": Score(1, 1)}, calls)
    result = asyncio.run(gate.evaluate(candidate, current, regression, extra_suites={"coding": coding}))
    assert not result.passed and calls == []  # 贵的评测集没有白跑


def test_gate_rejects_when_suite_errors(tmp_path):
    candidate, current = _dirs(tmp_path)
    regression = _runner({"current": Score(1, 1), "candidate": Score(1, 1)})

    async def broken(directory: Path) -> Score:
        raise RuntimeError("模型网关不可达")

    result = asyncio.run(gate.evaluate(candidate, current, regression, extra_suites={"coding": broken}))
    assert not result.passed and "保守拒绝" in result.reasons[0]


def test_optimizer_settings_and_config_defaults():
    from markbot.kernel.config import OptimizerConfig

    assert OptimizerSettings().extra_suites == {}
    cfg = OptimizerConfig()
    assert cfg.gate_suites == [] and cfg.gate_max_cases == 5 and cfg.gate_tolerance == 0.0


def test_cli_gate_suites_resolution(tmp_path, capsys):
    from markbot.cli.main import _gate_suites
    from markbot.kernel.config import load_config

    cfg = load_config(home=tmp_path / "home", project_root=REPO)
    cfg.optimizer.gate_suites = ["coding", "nope"]
    suites = _gate_suites(cfg, provider=None, argv=[])
    assert list(suites) == ["coding"]
    assert "未知门禁评测集 nope" in capsys.readouterr().err
    assert _gate_suites(cfg, None, ["--gate-suites", "none"]) == {}
    assert list(_gate_suites(cfg, None, ["--gate-suites", "coding,browser"])) == ["coding", "browser"]


def test_suite_runner_passes_harness_and_limits_cases(tmp_path, monkeypatch):
    seen: list[tuple[str, Path]] = []

    async def fake_run_case(case, *, provider=None, keep=False, learned_dir=None):
        seen.append((case.name, learned_dir))
        return CaseResult(case.name, case.name != "off-by-one", "x")

    monkeypatch.setattr(eval_cmd, "run_case", fake_run_case)
    runner = eval_cmd.suite_runner("coding", REPO / "evals" / "coding", max_cases=2)
    score = asyncio.run(runner(tmp_path / "harness"))
    assert score.total == 2 and len(seen) == 2
    assert all(path == tmp_path / "harness" for _, path in seen)
    with pytest.raises(ValueError):
        eval_cmd.suite_runner("nope", tmp_path)


# ================================================================== eval 配置隔离


def test_case_config_reads_real_home_but_isolates(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (real / "config.toml").write_text('[agent]\nmodel = "eval-model-x"\n', encoding="utf-8")
    monkeypatch.setenv("MARKBOT_HOME", str(real))
    cfg = eval_cmd.case_config(tmp_path / "tmphome", tmp_path / "ws")
    assert cfg.agent.model == "eval-model-x"
    assert cfg.home == tmp_path / "tmphome" and cfg.permissions.profile == "trusted"
    assert cfg.logging.enabled is False


def test_stage_learned_copies_instead_of_sharing(tmp_path, monkeypatch):
    real = tmp_path / "real"
    (real / "learned" / "strategy").mkdir(parents=True)
    (real / "learned" / "strategy" / "memory.md").write_text("记住", encoding="utf-8")
    monkeypatch.setenv("MARKBOT_HOME", str(real))
    staged = eval_cmd.stage_learned(tmp_path / "tmphome")
    assert (staged / "strategy" / "memory.md").read_text(encoding="utf-8") == "记住"
    (staged / "strategy" / "memory.md").write_text("改了", encoding="utf-8")
    assert (real / "learned" / "strategy" / "memory.md").read_text(encoding="utf-8") == "记住"
    other = tmp_path / "harness"
    (other / "strategy").mkdir(parents=True)
    (other / "strategy" / "system_prompt.md").write_text("候选", encoding="utf-8")
    staged2 = eval_cmd.stage_learned(tmp_path / "tmphome2", other)
    assert (staged2 / "strategy" / "system_prompt.md").exists()
    empty = eval_cmd.stage_learned(tmp_path / "tmphome3", tmp_path / "missing")
    assert empty.is_dir()


# ================================================================== eval 命令门禁


@pytest.fixture
def eval_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GITHUB_SHA", "deadbeefcafe0000")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    outcomes: list[list[CaseResult]] = []
    monkeypatch.setattr(eval_cmd, "_run_suite", lambda suite, argv, directory: outcomes.pop(0))
    trend = tmp_path / "trend.jsonl"
    return outcomes, trend, summary


def test_eval_record_and_fail_on_regression(eval_env, capsys):
    outcomes, trend, summary = eval_env
    base = ["--suite", "coding", "--record", "--trend-file", str(trend), "--fail-on-regression"]
    outcomes.append(_results(a=True, b=True))
    assert eval_cmd.eval_command(base) == 0
    outcomes.append(_results(a=True, b=False))
    assert eval_cmd.eval_command(base) == 1
    err = capsys.readouterr().err
    assert "REGRESSION" in err and "通过率下降" in err
    history = load_history(trend, "coding")
    assert [record.passed for record in history] == [2, 1]
    assert history[-1].revision == "deadbeefcafe"
    assert "### 评测 coding：1/2" in summary.read_text(encoding="utf-8")
    # 有容忍度：掉一个用例只警告
    outcomes.append(_results(a=True, b=False))
    outcomes.append(_results(a=False, b=True))
    tolerant = [*base, "--tolerance", "0.6"]
    assert eval_cmd.eval_command(tolerant) == 0
    assert eval_cmd.eval_command(tolerant) == 0
    assert "WARNING" in capsys.readouterr().err


def test_eval_baseline_and_min_ratio(eval_env, tmp_path):
    outcomes, trend, _ = eval_env
    baselines = tmp_path / "b.json"
    baselines.write_text(json.dumps({"suites": {"coding": {"min_ratio": 0.75}}}), encoding="utf-8")
    outcomes.append(_results(a=True, b=True, c=False, d=True))
    assert eval_cmd.eval_command(["--suite", "coding", "--trend-file", str(trend), "--baseline", str(baselines)]) == 0
    outcomes.append(_results(a=True, b=False, c=False, d=True))
    assert eval_cmd.eval_command(["--suite", "coding", "--trend-file", str(trend), "--baseline", str(baselines)]) == 1
    outcomes.append(_results(a=True, b=False))
    assert eval_cmd.eval_command(["--suite", "coding", "--trend-file", str(trend), "--min-ratio", "0.5"]) == 0
    assert not trend.exists()  # 没有 --record 不写趋势


def test_eval_legacy_exit_code_and_trend_view(eval_env, capsys):
    outcomes, trend, _ = eval_env
    outcomes.append(_results(a=True, b=False))
    assert eval_cmd.eval_command(["--suite", "coding", "--trend-file", str(trend), "--record"]) == 1
    capsys.readouterr()
    assert eval_cmd.eval_command(["--trend", "--trend-file", str(trend)]) == 0
    assert "| coding | 1/2 |" in capsys.readouterr().out
    assert eval_cmd.eval_command(["--suite", "nope"]) == 2


# ================================================================== CI 配置


def _workflow(name: str) -> dict:
    path = REPO / ".github" / "workflows" / name
    if not (REPO / ".github").is_dir():
        pytest.skip("工作区里没有 .github/（只同步了 src/tests）；完整仓库里这条测试必须运行")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML 把裸 on 解析成 True
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_ci_workflow_runs_linux_and_windows():
    ci = _workflow("ci.yml")
    assert {"push", "pull_request"} <= set(ci["on"])
    job = ci["jobs"]["test"]
    assert set(job["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "windows-latest"}
    commands = " ".join(step.get("run", "") for step in job["steps"])
    assert "ruff check src tests" in commands
    assert "tests/test_shell_smoke.py" in commands and "pytest -q" in commands
    assert ci["jobs"]["lint-strict"]["continue-on-error"] is True


def test_eval_workflow_is_nightly_and_gated():
    nightly = _workflow("eval.yml")
    assert nightly["on"]["schedule"] and "workflow_dispatch" in nightly["on"]
    job = nightly["jobs"]["eval"]
    assert "MARKBOT_EVAL_ENABLED" in job["if"]
    script = " ".join(step.get("run", "") for step in job["steps"])
    for flag in ("--record", "--fail-on-regression", "--baseline evals/baselines.json", "--trend-file"):
        assert flag in script
    assert "secrets.MARKBOT_EVAL_API_KEY" in job["env"]["MARKBOT_EVAL_API_KEY"]
