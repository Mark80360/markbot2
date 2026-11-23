"""晋升门禁：必须真的变了、不越界、不新增权限、回归不掉分。

ADR 0016：除回归集外，还可以挂额外的评测集（编码、浏览器……），每个评测集都**只许变好**：
候选在任何一个评测集上的通过率低于当前 harness（超过 ``tolerance``）就拒绝晋升。
"""

from __future__ import annotations

import tomllib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from markbot.optimizer.evals import Score
from markbot.schema.hashing import tree_hash
from markbot.schema.learned import allowed_path

EPSILON = 1e-9


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    # 各评测集的「当前 -> 候选」分数，如 {"regression": "9/10 -> 10/10", "coding": "4/5 -> 4/5"}
    scores: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "reasons": list(self.reasons), "scores": dict(self.scores)}


def illegal_paths(directory: Path) -> list[str]:
    directory = Path(directory)
    if not directory.exists():
        return []
    bad: list[str] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(directory).as_posix()
        if not allowed_path(rel):
            bad.append(rel)
    return sorted(bad)


def _frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def capabilities_of(directory: Path) -> set[str]:
    """学习层可见的能力声明：包 manifest 与技能 allowed-tools。"""
    directory = Path(directory)
    caps: set[str] = set()
    if not directory.exists():
        return caps
    manifest = directory / "markbot.toml"
    if manifest.exists():
        try:
            with manifest.open("rb") as fh:
                data = tomllib.load(fh)
            for cap in (data.get("permissions") or {}).get("capabilities") or []:
                caps.add(str(cap))
        except (OSError, tomllib.TOMLDecodeError):
            caps.add("invalid:markbot.toml")
    for path in directory.rglob("SKILL.md"):
        data = _frontmatter(path)
        allowed = data.get("allowed-tools") or data.get("allowed_tools") or []
        if isinstance(allowed, str):
            allowed = [part.strip() for part in allowed.split(",")]
        name = str(data.get("name") or path.parent.name)
        for tool in allowed or []:
            caps.add(f"skill:{name}:{tool}")
    return caps


def new_capabilities(candidate_dir: Path, current_dir: Path) -> list[str]:
    return sorted(capabilities_of(candidate_dir) - capabilities_of(current_dir))


EvalRunner = Callable[[Path], Awaitable[Score]]


async def evaluate(
    candidate_dir: Path,
    current_dir: Path,
    run_eval: EvalRunner,
    *,
    extra_suites: Mapping[str, EvalRunner] | None = None,
    tolerance: float = 0.0,
) -> GateResult:
    reasons: list[str] = []
    scores: dict[str, str] = {}

    if tree_hash(Path(candidate_dir)) == tree_hash(Path(current_dir)):
        reasons.append("候选与当前 harness 完全相同")

    bad = illegal_paths(Path(candidate_dir))
    if bad:
        reasons.append("候选包含越界路径: " + ", ".join(bad))

    added = new_capabilities(Path(candidate_dir), Path(current_dir))
    if added:
        reasons.append("候选请求了新的权限: " + ", ".join(added))

    base_score = await run_eval(Path(current_dir))
    cand_score = await run_eval(Path(candidate_dir))
    scores["regression"] = f"{base_score} -> {cand_score}"
    if cand_score.ratio < base_score.ratio - EPSILON:
        reasons.append(f"回归掉分: {base_score} -> {cand_score}")

    # 前面已经不通过时不再跑额外评测集：它们通常要调用真实模型，又慢又贵
    if not reasons:
        for name, runner in (extra_suites or {}).items():
            try:
                base = await runner(Path(current_dir))
                cand = await runner(Path(candidate_dir))
            except Exception as exc:  # noqa: BLE001 - 评测集本身出错时保守拒绝
                reasons.append(f"评测集 {name} 运行失败，保守拒绝: {exc}")
                break
            scores[name] = f"{base} -> {cand}"
            if not base.total and not cand.total:
                continue
            if cand.ratio < base.ratio - max(0.0, tolerance) - EPSILON:
                reasons.append(f"评测集 {name} 掉分: {base} -> {cand}")
                break

    return GateResult(passed=not reasons, reasons=reasons, scores=scores)


__all__ = [
    "EPSILON",
    "GateResult",
    "allowed_path",
    "capabilities_of",
    "evaluate",
    "illegal_paths",
    "new_capabilities",
]
