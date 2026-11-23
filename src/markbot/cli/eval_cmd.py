"""编码评测：``markbot eval --suite coding``（ADR 0013，T2-26）；浏览器评测见 ``eval_browser``（ADR 0015）。

每个用例是 ``evals/coding/<name>/``：

- ``case.yaml``：``prompt``（交给 Agent 的任务）、``check``（判定命令，默认
  ``python -m pytest -q``）、``max_steps``（可选）、``allowed``（允许修改的文件 glob 列表，可选，
  默认不限制）、``timeout_s``（可选）；
- ``repo/``：一个小仓库（通常带一个失败的测试）。

执行：把 ``repo/`` 复制到临时目录作为工作区，用当前配置的模型跑一个回合（trusted 档位、工作区内
命令免确认、临时 home，不写入真实的 sessions/ 与 learned/），然后在工作区运行 ``check``：
退出码为 0 且没有越界修改（``allowed`` 之外的文件）算通过。``python`` 开头的命令会替换为当前解释器。

模型配置与密钥读自真实的 MarkBot home（``MARKBOT_HOME`` 或 ``~/.markbot``），学习层复制一份到临时 home，
评测不会写入真实的 sessions/、learned/ 与日志。

趋势与门禁（ADR 0016）::

    markbot eval --suite coding --record                 # 结果追加到 <home>/logs/optimizer.jsonl
    markbot eval --suite coding --record --fail-on-regression [--tolerance 0.2]
    markbot eval --suite coding --baseline evals/baselines.json
    markbot eval --suite coding --min-ratio 0.6
    markbot eval --trend [--suite coding]                # 只打印最近的趋势

指定 ``--fail-on-regression`` / ``--baseline`` / ``--min-ratio`` 时，退出码只按这些条件判定；
否则保持旧行为（全部通过才返回 0）。``--trend-file PATH`` 改用别的趋势文件（CI 缓存用）。
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from markbot.kernel.app import Kernel
from markbot.kernel.config import Config, load_config, markbot_home
from markbot.optimizer.evals import Score
from markbot.optimizer.trend import (
    append_record,
    baseline_failures,
    describe_flips,
    flipped_cases,
    load_baselines,
    load_history,
    record_from_results,
    regressions,
    trend_table,
)

DEFAULT_CHECK = "python -m pytest -q -p no:cacheprovider"
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".git"}


@dataclass
class CodingCase:
    name: str
    path: Path
    prompt: str
    check: str = DEFAULT_CHECK
    max_steps: int = 30
    allowed: list[str] = field(default_factory=list)
    timeout_s: float = 900.0


@dataclass
class CaseResult:
    name: str
    passed: bool
    reason: str
    steps: int = 0
    tool_calls: int = 0
    seconds: float = 0.0
    tokens: int = 0
    changed: list[str] = field(default_factory=list)
    check_output: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "passed": self.passed, "reason": self.reason, "steps": self.steps,
            "tool_calls": self.tool_calls, "seconds": round(self.seconds, 1), "tokens": self.tokens,
            "changed": self.changed,
        }


def load_cases(directory: Path) -> list[CodingCase]:
    cases: list[CodingCase] = []
    for case_dir in sorted(Path(directory).iterdir() if Path(directory).is_dir() else []):
        spec_path = case_dir / "case.yaml"
        if not spec_path.is_file() or not (case_dir / "repo").is_dir():
            continue
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        cases.append(
            CodingCase(
                name=case_dir.name,
                path=case_dir,
                prompt=str(spec.get("prompt") or "").strip(),
                check=str(spec.get("check") or DEFAULT_CHECK),
                max_steps=int(spec.get("max_steps") or 30),
                allowed=[str(item) for item in (spec.get("allowed") or [])],
                timeout_s=float(spec.get("timeout_s") or 900),
            )
        )
    return [case for case in cases if case.prompt]


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file() and not (SKIP_PARTS & set(path.relative_to(root).parts)):
            out[path.relative_to(root).as_posix()] = hashlib.sha1(path.read_bytes()).hexdigest()
    return out


def _changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))


def _check_argv(command: str) -> list[str]:
    argv = shlex.split(command, posix=True)
    if argv and argv[0] in ("python", "python3"):
        argv[0] = sys.executable
    return argv


def run_check(command: str, cwd: Path, timeout_s: float = 300) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            _check_argv(command), cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return 124, "check 超时"
    except OSError as exc:
        return 127, f"check 无法执行: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr)[-3000:]


def eval_config(base: Config, home: Path, workspace: Path) -> Config:
    """评测用配置：沿用模型设置，权限放宽到工作区内免确认，home 与日志隔离。"""
    cfg = base
    cfg.home = home
    cfg.project_root = workspace
    cfg.permissions.profile = "trusted"
    cfg.permissions.trusted_exec = "all"
    cfg.logging.enabled = False
    cfg.memory.auto_consolidate = False
    cfg.cron.enabled = False
    cfg.projects = {}
    return cfg


def case_config(tmp_home: Path, workspace: Path) -> Config:
    """读真实 home 的模型配置与密钥（旧实现读的是空的临时 home，真实评测拿不到 API key），再隔离 home。"""
    return eval_config(load_config(home=markbot_home(), project_root=workspace), tmp_home, workspace)


def stage_learned(tmp_home: Path, learned_dir: Path | None = None) -> Path:
    """把要评测的学习层（默认真实 home 的 learned/）复制进临时 home：评测期间的记忆写入不影响原件。"""
    source = Path(learned_dir) if learned_dir is not None else markbot_home() / "learned"
    target = Path(tmp_home) / "learned"
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".lock", "*.lock"))
    else:
        target.mkdir(parents=True, exist_ok=True)
    return target


async def run_case(
    case: CodingCase, *, provider: Any = None, keep: bool = False, learned_dir: Path | None = None
) -> CaseResult:
    started = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix=f"markbot-eval-{case.name}-"))
    workspace = tmp / "repo"
    shutil.copytree(case.path / "repo", workspace)
    before = _snapshot(workspace)
    try:
        cfg = case_config(tmp / "home", workspace)
        cfg.agent.max_steps = case.max_steps
        kernel = Kernel.build(
            config=cfg,
            provider=provider,
            include_packages=False,
            workspace=workspace,
            learned_dir=stage_learned(tmp / "home", learned_dir),
        )
        try:
            result = await asyncio.wait_for(kernel.run(case.prompt, archive=False), timeout=case.timeout_s)
        except asyncio.TimeoutError:
            return CaseResult(case.name, False, "Agent 超时", seconds=time.monotonic() - started)
        finally:
            await kernel.shutdown(archive=False)
        changed = _changed(before, _snapshot(workspace))
        tokens = int(result.total_usage.get("tokens_in", 0)) + int(result.total_usage.get("tokens_out", 0))
        common = dict(
            steps=getattr(kernel.session, "steps", 0) if kernel.session else 0,
            tool_calls=result.tool_calls,
            seconds=time.monotonic() - started,
            tokens=tokens,
            changed=changed,
        )
        if case.allowed:
            outside = [path for path in changed if not any(fnmatch.fnmatch(path, glob) for glob in case.allowed)]
            if outside:
                return CaseResult(case.name, False, f"越界修改: {', '.join(outside[:5])}", **common)
        code, output = run_check(case.check, workspace)
        if code != 0:
            return CaseResult(case.name, False, f"check 未通过（退出码 {code}）", check_output=output, **common)
        if not changed:
            return CaseResult(case.name, False, "没有修改任何文件", check_output=output, **common)
        return CaseResult(case.name, True, "通过", check_output=output, **common)
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def render(results: list[CaseResult]) -> str:
    lines = ["用例                          结果   步数  工具  耗时(s)  token    说明"]
    for item in results:
        lines.append(
            f"{item.name:<28}  {'PASS' if item.passed else 'FAIL'}  {item.steps:>4}  {item.tool_calls:>4}  "
            f"{item.seconds:>7.1f}  {item.tokens:>7}  {item.reason}"
        )
    passed = sum(item.passed for item in results)
    lines.append(f"\n通过率 {passed}/{len(results)}" + (f"（{passed / len(results):.0%}）" if results else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------ 门禁评测集（ADR 0016）
SUITES = ("coding", "browser")


def score_results(results: list[CaseResult]) -> Score:
    score = Score(passed=sum(item.passed for item in results), total=len(results))
    score.details = [f"{item.name}:{'pass' if item.passed else item.reason}" for item in results]
    return score


def suite_runner(suite: str, directory: Path, *, provider: Any = None, max_cases: int = 0):
    """``harness_dir -> Score``：用指定学习层跑一个评测集，供优化器晋升门禁使用。"""
    if suite not in SUITES:
        raise ValueError(f"未知评测集: {suite}")

    async def run(harness_dir: Path) -> Score:
        results: list[CaseResult] = []
        if suite == "coding":
            cases = load_cases(directory)
            for case in cases[:max_cases] if max_cases > 0 else cases:
                results.append(await run_case(case, provider=provider, learned_dir=Path(harness_dir)))
        else:
            from markbot.cli.eval_browser import load_browser_cases, run_browser_case

            browser_cases = load_browser_cases(directory)
            for case in browser_cases[:max_cases] if max_cases > 0 else browser_cases:
                results.append(await run_browser_case(case, provider=provider, learned_dir=Path(harness_dir)))
        return score_results(results)

    return run


# ------------------------------------------------------------------ 命令行
def _flag(argv: list[str], name: str, default: str | None = None) -> str | None:
    if name not in argv:
        return default
    index = argv.index(name)
    return argv[index + 1] if index + 1 < len(argv) and not argv[index + 1].startswith("--") else default


def _revision() -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    if sha:
        return sha[:12]
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _model() -> str:
    try:
        cfg = load_config(home=markbot_home())
    except Exception:  # noqa: BLE001 - 只用于记录
        return ""
    return str(cfg.agent.model or "")


def _step_summary(text: str) -> None:
    """在 GitHub Actions 里把结果写进作业摘要。"""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def _run_suite(suite: str, argv: list[str], directory: Path) -> list[CaseResult] | None:
    max_cases = int(_flag(argv, "--max-cases", "0") or 0)
    if suite == "browser":
        from markbot.cli.eval_browser import run_browser_suite

        results = run_browser_suite(argv, directory)
        return results[:max_cases] if results and max_cases > 0 else results
    cases = load_cases(directory)
    if "--case" in argv:
        wanted = argv[argv.index("--case") + 1]
        cases = [case for case in cases if case.name == wanted]
    if max_cases > 0:
        cases = cases[:max_cases]
    if not cases:
        print(f"error: {directory} 下没有找到评测用例", file=sys.stderr)
        return None
    keep = "--keep" in argv
    results: list[CaseResult] = []
    for case in cases:
        print(f"运行 {case.name} …", file=sys.stderr)
        results.append(asyncio.run(run_case(case, keep=keep)))
        if not results[-1].passed and results[-1].check_output and "--verbose" in argv:
            print(results[-1].check_output, file=sys.stderr)
    return results


def eval_command(argv: list[str]) -> int:
    suite = _flag(argv, "--suite", "coding") or "coding"
    trend_file = Path(_flag(argv, "--trend-file") or markbot_home() / "logs" / "optimizer.jsonl")
    if "--trend" in argv:
        history = load_history(trend_file, suite if "--suite" in argv else None)
        print(trend_table(history, limit=int(_flag(argv, "--limit", "20") or 20)) if history else f"（{trend_file} 没有评测记录）")
        return 0
    if suite not in SUITES:
        print(f"error: 未知评测集 {suite}（当前支持 coding、browser；回归集用 markbot optimize --dry-run）", file=sys.stderr)
        return 2
    directory = Path(_flag(argv, "--cases") or Path.cwd() / "evals" / suite)
    results = _run_suite(suite, argv, directory)
    if results is None:
        return 2
    if "--json" in argv:
        print(json.dumps([item.as_dict() for item in results], ensure_ascii=False, indent=2))
    else:
        print(render(results))

    record = record_from_results(suite, results, model=_model(), revision=_revision())
    history = load_history(trend_file, suite)
    previous = history[-1] if history else None
    problems: list[str] = []
    warnings: list[str] = []
    gating = False
    tolerance = float(_flag(argv, "--tolerance", "0") or 0)
    if "--fail-on-regression" in argv:
        gating = True
        # 给了容忍度时，单个用例翻转只作为警告（模型输出有随机性），整体掉分超过容忍度才标红
        problems += regressions(previous, record, tolerance=tolerance, strict_cases=tolerance <= 0)
    flips = flipped_cases(previous, record)
    if flips and not any("由通过变为失败" in item for item in problems):
        warnings.append(describe_flips(record, flips))
    if "--baseline" in argv:
        gating = True
        problems += baseline_failures(record, load_baselines(Path(_flag(argv, "--baseline") or "evals/baselines.json")))
    if "--min-ratio" in argv:
        gating = True
        floor = float(_flag(argv, "--min-ratio", "1") or 1)
        if record.ratio < floor - 1e-9:
            problems.append(f"{suite} 通过率 {record.ratio:.0%} 低于 --min-ratio {floor:.0%}")
    if "--record" in argv:
        append_record(trend_file, record)
        print(f"已记录到 {trend_file}", file=sys.stderr)

    summary = [f"### 评测 {suite}：{record.passed}/{record.total}（{record.ratio:.0%}）"]
    if previous is not None:
        summary.append(f"上一次：{previous.passed}/{previous.total}（{previous.ratio:.0%}，{previous.ts[:16]}）")
    summary += [f"- ❌ {problem}" for problem in problems]
    summary += [f"- ⚠️ {warning}" for warning in warnings]
    failed = [item for item in results if not item.passed]
    if failed:
        summary.append("失败用例：" + "，".join(f"`{item.name}`（{item.reason}）" for item in failed))
    _step_summary("\n\n".join(summary))
    for problem in problems:
        print(f"REGRESSION: {problem}", file=sys.stderr)
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    if gating:
        return 1 if problems else 0
    return 0 if all(item.passed for item in results) else 1


__all__ = [
    "SUITES",
    "CaseResult",
    "CodingCase",
    "case_config",
    "eval_command",
    "load_cases",
    "render",
    "run_case",
    "run_check",
    "score_results",
    "stage_learned",
    "suite_runner",
]
