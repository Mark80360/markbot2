"""日志保留策略：按天数与总大小清理 ``logs/`` 与 ``gateway/`` 下的按日文件。

旧行为：运行日志、交互日志（含完整模型报文）、网关日志只增不删，交互日志一天就能到
数 MB。这里只清理文件名形如 ``YYYY-MM-DD.*`` 的按日文件，今天的文件永不删除；
会话归档（``sessions/``）不在清理范围内，它们是优化器与记忆整理的输入。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

_logger = logging.getLogger("markbot.retention")
_DAY = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(md|log)$")

# 目录 -> 配置里对应的保留天数字段；交互日志体积最大，单独设置
CATEGORIES = {
    "logs/interaction": "interaction_retention_days",
    "logs/runtime": "retention_days",
    "gateway": "retention_days",
}


@dataclass
class PruneReport:
    deleted: list[str] = field(default_factory=list)
    freed_bytes: int = 0
    total_bytes: int = 0


def _day_of(path: Path) -> date | None:
    match = _DAY.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _files(home: Path) -> list[tuple[str, Path, date, int]]:
    out: list[tuple[str, Path, date, int]] = []
    for rel in CATEGORIES:
        directory = Path(home) / rel
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            day = _day_of(path)
            if day is None or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            out.append((rel, path, day, size))
    return out


def prune_logs(
    home: Path,
    *,
    retention_days: int = 30,
    interaction_retention_days: int = 7,
    max_total_mb: float = 500,
    today: date | None = None,
) -> PruneReport:
    """删除过期的按日日志；总量仍超上限时从最旧的开始删（交互日志优先）。"""
    today = today or datetime.now().astimezone().date()
    limits = {
        "retention_days": int(retention_days),
        "interaction_retention_days": int(interaction_retention_days),
    }
    report = PruneReport()
    remaining: list[tuple[str, Path, date, int]] = []
    for rel, path, day, size in _files(home):
        keep_days = limits[CATEGORIES[rel]]
        if day < today and keep_days > 0 and day <= today - timedelta(days=keep_days):
            if _unlink(path):
                report.deleted.append(f"{rel}/{path.name}")
                report.freed_bytes += size
                continue
        remaining.append((rel, path, day, size))

    budget = int(float(max_total_mb) * 1024 * 1024) if max_total_mb and max_total_mb > 0 else 0
    total = sum(size for *_, size in remaining)
    if budget and total > budget:
        # 交互日志先删，再按日期从旧到新；今天的文件不删
        order = sorted(
            (item for item in remaining if item[2] < today),
            key=lambda item: (item[0] != "logs/interaction", item[2]),
        )
        for rel, path, _day, size in order:
            if total <= budget:
                break
            if _unlink(path):
                report.deleted.append(f"{rel}/{path.name}")
                report.freed_bytes += size
                total -= size
    report.total_bytes = total
    if report.deleted:
        _logger.info(
            "日志清理 deleted=%d freed_mb=%.1f remaining_mb=%.1f",
            len(report.deleted),
            report.freed_bytes / 1024 / 1024,
            total / 1024 / 1024,
        )
    return report


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError as exc:
        _logger.warning("日志清理失败 path=%s error=%s", path, exc)
        return False


__all__ = ["CATEGORIES", "PruneReport", "prune_logs"]
