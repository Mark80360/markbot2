"""回归集：读取 cases、对最终文本做可验证检查。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

Judge = Callable[[str, str], Awaitable[bool] | bool]


@dataclass(frozen=True)
class Check:
    type: str
    value: str = ""
    rubric: str = ""


@dataclass(frozen=True)
class EvalCase:
    id: str
    prompt: str
    domain: str = "general"
    files: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()


@dataclass
class Score:
    passed: int = 0
    total: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def __str__(self) -> str:
        return f"{self.passed}/{self.total}"


def load_cases(directory: Path) -> list[EvalCase]:
    directory = Path(directory)
    if not directory.exists():
        return []
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or not data.get("id") or not data.get("prompt"):
            continue
        checks = tuple(
            Check(
                type=str(item.get("type", "")),
                value=str(item.get("value", "")),
                rubric=str(item.get("rubric", "")),
            )
            for item in data.get("checks") or []
            if isinstance(item, dict)
        )
        cases.append(
            EvalCase(
                id=str(data["id"]),
                prompt=str(data["prompt"]),
                domain=str(data.get("domain", "general")),
                files=tuple(str(f) for f in data.get("files") or []),
                checks=checks,
            )
        )
    return cases


def score_text(text: str, case: EvalCase) -> Score:
    score = Score()
    for check in case.checks:
        if check.type == "contains":
            score.total += 1
            ok = check.value in text
            score.passed += int(ok)
            score.details.append(f"{case.id}:contains={ok}")
        elif check.type == "not_contains":
            score.total += 1
            ok = check.value not in text
            score.passed += int(ok)
            score.details.append(f"{case.id}:not_contains={ok}")
        elif check.type == "regex":
            score.total += 1
            try:
                ok = bool(re.search(check.value, text, re.MULTILINE))
            except re.error:
                ok = False
            score.passed += int(ok)
            score.details.append(f"{case.id}:regex={ok}")
        # llm_judge 在没有 judge 时跳过：未评分不等于 0 分
    return score


class EvalSuite:
    """把「用某个 harness 跑一条用例」的职责留给调用方。"""

    def __init__(
        self,
        cases: list[EvalCase],
        run_case: Callable[[Path, EvalCase], Awaitable[str]],
        *,
        judge: Judge | None = None,
    ) -> None:
        self.cases = cases
        self.run_case = run_case
        self.judge = judge

    async def score(self, harness_dir: Path) -> Score:
        total = Score()
        for case in self.cases:
            try:
                text = await self.run_case(Path(harness_dir), case)
            except Exception as exc:  # noqa: BLE001 - 单条用例失败不影响其它
                total.total += len(case.checks)
                total.details.append(f"{case.id}: error={exc}")
                continue
            partial = score_text(text, case)
            total.passed += partial.passed
            total.total += partial.total
            total.details.extend(partial.details)
        return total


def case_files(case: EvalCase, base: Path) -> list[Path]:
    return [base / rel for rel in case.files]


__all__ = ["Check", "EvalCase", "EvalSuite", "Score", "case_files", "load_cases", "score_text"]
