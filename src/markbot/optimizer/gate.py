"""晋升门禁：必须真的变了、不越界、不新增权限、回归不掉分。"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from markbot.optimizer.evals import Score
from markbot.schema.hashing import tree_hash

EPSILON = 1e-9

_ALLOWED = [
    re.compile(r"^manifest\.json$"),
    re.compile(r"^strategy/(system_prompt|guardrails|memory)\.md$"),
    # 长期记忆的按需知识页与索引（由 memory 工具写入）
    re.compile(r"^memory/wiki/[a-z0-9][a-z0-9_\-]*\.md$"),
    re.compile(r"^memory/wiki/archive/[a-z0-9][a-z0-9._\-]*\.md$"),
    re.compile(r"^memory/wiki/index\.json$"),
    re.compile(r"^tool_notes/[a-z0-9][a-z0-9_\-]*\.md$"),
    re.compile(r"^skills/[a-z0-9][a-z0-9_\-]*/SKILL\.md$"),
    re.compile(r"^skills/[a-z0-9][a-z0-9_\-]*/(scripts|references|assets)/[A-Za-z0-9_.\-]+$"),
    re.compile(r"^sub_agents/[a-z0-9][a-z0-9_\-]*\.md$"),
]


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "reasons": list(self.reasons)}


def allowed_path(rel: str) -> bool:
    return any(rx.match(rel) for rx in _ALLOWED)


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
) -> GateResult:
    reasons: list[str] = []

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
    if cand_score.ratio < base_score.ratio - EPSILON:
        reasons.append(f"回归掉分: {base_score} -> {cand_score}")

    return GateResult(passed=not reasons, reasons=reasons)


__all__ = [
    "EPSILON",
    "GateResult",
    "allowed_path",
    "capabilities_of",
    "evaluate",
    "illegal_paths",
    "new_capabilities",
]
