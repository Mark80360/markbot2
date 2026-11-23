"""评测趋势（ADR 0016）：把每次评测的通过情况追加到 ``<home>/logs/optimizer.jsonl``，并与上一次比较。

``optimizer.jsonl`` 原来只记录优化器运行（``OptimizationReport``，没有 ``kind`` 字段）；
评测记录带 ``"kind": "eval"``，两类记录可以共存，读取时按 ``kind`` 过滤。

回归判定（``regressions``）：
- 通过率比上一次同套件记录下降超过 ``tolerance``；
- ``strict_cases=True`` 时，上一次通过、这一次失败的用例也算回归（``flipped_cases``）。
  模型输出有随机性，夜间评测通常给一点容忍度并把翻转用例只作为警告。
基线（``baseline_failures``）：``evals/baselines.json`` 里每个套件的最低通过率。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVAL_KIND = "eval"


@dataclass
class EvalRecord:
    suite: str
    cases: dict[str, bool] = field(default_factory=dict)
    ts: str = ""
    model: str = ""
    revision: str = ""
    seconds: float = 0.0
    tokens: int = 0
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> int:
        return sum(1 for ok in self.cases.values() if ok)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def ratio(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": EVAL_KIND,
            "ts": self.ts or _now(),
            "suite": self.suite,
            "passed": self.passed,
            "total": self.total,
            "ratio": round(self.ratio, 4),
            "cases": dict(self.cases),
            "reasons": {name: text[:200] for name, text in self.reasons.items()},
            "model": self.model,
            "revision": self.revision,
            "seconds": round(self.seconds, 1),
            "tokens": self.tokens,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvalRecord:
        cases = data.get("cases") if isinstance(data.get("cases"), dict) else {}
        reasons = data.get("reasons") if isinstance(data.get("reasons"), dict) else {}
        return cls(
            suite=str(data.get("suite") or ""),
            cases={str(name): bool(ok) for name, ok in cases.items()},
            ts=str(data.get("ts") or ""),
            model=str(data.get("model") or ""),
            revision=str(data.get("revision") or ""),
            seconds=float(data.get("seconds") or 0),
            tokens=int(data.get("tokens") or 0),
            reasons={str(name): str(text) for name, text in reasons.items()},
        )


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def record_from_results(suite: str, results: Iterable[Any], **meta: Any) -> EvalRecord:
    """``CaseResult`` 列表（有 name / passed / reason / seconds / tokens）-> 记录。"""
    record = EvalRecord(suite=suite, **meta)
    for item in results:
        name = str(getattr(item, "name", ""))
        record.cases[name] = bool(getattr(item, "passed", False))
        if not record.cases[name]:
            record.reasons[name] = str(getattr(item, "reason", ""))
        record.seconds += float(getattr(item, "seconds", 0) or 0)
        record.tokens += int(getattr(item, "tokens", 0) or 0)
    return record


def append_record(path: Path, record: EvalRecord) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")


def load_history(path: Path, suite: str | None = None) -> list[EvalRecord]:
    """读取评测记录（按文件顺序）；坏行与非评测记录跳过。"""
    path = Path(path)
    if not path.is_file():
        return []
    records: list[EvalRecord] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("kind") != EVAL_KIND:
            continue
        if suite is not None and data.get("suite") != suite:
            continue
        records.append(EvalRecord.from_dict(data))
    return records


def flipped_cases(previous: EvalRecord | None, current: EvalRecord) -> list[str]:
    """上一次通过、这一次失败的用例。"""
    if previous is None:
        return []
    return sorted(name for name, ok in current.cases.items() if not ok and previous.cases.get(name) is True)


def describe_flips(current: EvalRecord, names: list[str]) -> str:
    detail = "；".join(f"{name}（{current.reasons.get(name, '失败')}）" for name in names[:10])
    return f"{current.suite} 由通过变为失败：{detail}"


def regressions(
    previous: EvalRecord | None,
    current: EvalRecord,
    *,
    tolerance: float = 0.0,
    strict_cases: bool = True,
) -> list[str]:
    if previous is None or not previous.total:
        return []
    problems: list[str] = []
    if current.ratio < previous.ratio - max(0.0, tolerance) - 1e-9:
        problems.append(
            f"{current.suite} 通过率下降：{previous.passed}/{previous.total}（{previous.ratio:.0%}）"
            f" -> {current.passed}/{current.total}（{current.ratio:.0%}）"
        )
    flipped = flipped_cases(previous, current)
    if flipped and strict_cases:
        problems.append(describe_flips(current, flipped))
    return problems


def load_baselines(path: Path) -> dict[str, float]:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    suites = data.get("suites", data) if isinstance(data, dict) else {}
    out: dict[str, float] = {}
    for name, value in (suites or {}).items():
        if isinstance(value, dict):
            value = value.get("min_ratio")
        try:
            out[str(name)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def baseline_failures(record: EvalRecord, baselines: Mapping[str, float]) -> list[str]:
    floor = baselines.get(record.suite)
    if floor is None or record.ratio >= floor - 1e-9:
        return []
    return [f"{record.suite} 通过率 {record.ratio:.0%} 低于基线 {floor:.0%}"]


def trend_table(history: list[EvalRecord], limit: int = 10) -> str:
    """最近几次记录的 Markdown 表（CI 摘要与 ``markbot eval --trend`` 用）。"""
    rows = ["| 时间 | 套件 | 通过 | 通过率 | 模型 | 版本 |", "|---|---|---|---|---|---|"]
    for record in history[-limit:]:
        rows.append(
            f"| {record.ts[:16].replace('T', ' ')} | {record.suite} | {record.passed}/{record.total} | "
            f"{record.ratio:.0%} | {record.model or '-'} | {record.revision[:8] or '-'} |"
        )
    return "\n".join(rows)


__all__ = [
    "EVAL_KIND",
    "EvalRecord",
    "append_record",
    "baseline_failures",
    "describe_flips",
    "flipped_cases",
    "load_baselines",
    "load_history",
    "record_from_results",
    "regressions",
    "trend_table",
]
