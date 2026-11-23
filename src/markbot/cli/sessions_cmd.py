"""`markbot sessions status|migrate [--dry-run]`：把旧归档收敛到 events.jsonl 单一事实来源。

对每个 ``sessions/<id>/``：

1. 只有 ``trajectory.jsonl`` 没有 ``events.jsonl`` 的老归档：经 ``Session.load`` 反推并补写
   事件日志（与运行时恢复会话的逻辑相同）；
2. 逐条比对「事件投影」与 ``trajectory.jsonl``，一致才删除 ``trajectory.jsonl``；
3. 事件里的压缩段不少于 ``compacted.jsonl`` 时才删除 ``compacted.jsonl``
   （老归档的压缩原文只在旧文件里，保留）。

任何不一致都只报告、不删除。``--dry-run`` 只统计。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from markbot.kernel.config import markbot_home
from markbot.kernel.session import Session
from markbot.schema.session_log import (
    COMPACTED_FILE,
    EVENTS_FILE,
    TRAJECTORY_FILE,
    SessionLog,
    read_jsonl_rows,
)


@dataclass
class MigrationReport:
    sessions: int = 0
    events_written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    freed_bytes: int = 0


def _key(row: dict[str, Any]) -> tuple:
    calls = tuple(
        (str(call.get("id") or ""), str(call.get("name") or ""))
        for call in (row.get("tool_calls") or [])
        if isinstance(call, dict)
    )
    return (
        str(row.get("role") or ""),
        str(row.get("content") or ""),
        row.get("tool_call_id"),
        row.get("name"),
        bool(row.get("is_error")),
        calls,
    )


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def migrate_sessions(home: Path, *, dry_run: bool = False) -> MigrationReport:
    report = MigrationReport()
    root = Path(home) / "sessions"
    if not root.is_dir():
        return report
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        trajectory = directory / TRAJECTORY_FILE
        compacted = directory / COMPACTED_FILE
        if not trajectory.is_file() and not compacted.is_file():
            continue
        report.sessions += 1
        events_path = directory / EVENTS_FILE
        if not events_path.is_file():
            if dry_run:
                report.events_written.append(directory.name)
                continue
            # 与运行时恢复同一条路径：反推事件并补写 events.jsonl
            restored = Session.load(Path(home), directory.name)
            if restored is None or not events_path.is_file():
                report.kept.append(f"{directory.name}: 无法生成事件日志")
                continue
            report.events_written.append(directory.name)

        log = SessionLog(directory)
        if not log.from_events and events_path.is_file():
            report.kept.append(f"{directory.name}: 事件日志为空或损坏")
            continue
        if trajectory.is_file():
            legacy = [_key(row) for row in read_jsonl_rows(trajectory)]
            derived = [_key(row) for row in log.trajectory()]
            if legacy == derived:
                report.removed.append(f"{directory.name}/{TRAJECTORY_FILE}")
                report.freed_bytes += _size(trajectory)
                if not dry_run:
                    trajectory.unlink(missing_ok=True)
            else:
                report.kept.append(
                    f"{directory.name}/{TRAJECTORY_FILE}: 与事件投影不一致"
                    f"（{len(legacy)} vs {len(derived)} 条），未删除"
                )
        if compacted.is_file():
            legacy_segments = len(read_jsonl_rows(compacted))
            derived_segments = sum(1 for event in log.events() if event.type == "compaction")
            if derived_segments >= legacy_segments:
                report.removed.append(f"{directory.name}/{COMPACTED_FILE}")
                report.freed_bytes += _size(compacted)
                if not dry_run:
                    compacted.unlink(missing_ok=True)
            else:
                report.kept.append(
                    f"{directory.name}/{COMPACTED_FILE}: 事件里只有 {derived_segments} 段压缩"
                    f"（旧文件 {legacy_segments} 段），保留"
                )
    return report


def status(home: Path, *, emit=print) -> int:
    root = Path(home) / "sessions"
    counts = {"sessions": 0, "events": 0, TRAJECTORY_FILE: 0, COMPACTED_FILE: 0}
    sizes = {EVENTS_FILE: 0, TRAJECTORY_FILE: 0, COMPACTED_FILE: 0}
    if root.is_dir():
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            counts["sessions"] += 1
            for name in sizes:
                path = directory / name
                if path.is_file():
                    sizes[name] += _size(path)
                    counts["events" if name == EVENTS_FILE else name] += 1
    emit(f"会话目录: {counts['sessions']}，含 events.jsonl: {counts['events']}")
    emit(
        f"派生文件: trajectory.jsonl {counts[TRAJECTORY_FILE]} 个 "
        f"({sizes[TRAJECTORY_FILE] / 1024:.0f} KB)，compacted.jsonl {counts[COMPACTED_FILE]} 个 "
        f"({sizes[COMPACTED_FILE] / 1024:.0f} KB)"
    )
    emit(f"事件日志合计: {sizes[EVENTS_FILE] / 1024:.0f} KB")
    if counts[TRAJECTORY_FILE] or counts[COMPACTED_FILE]:
        emit("可运行 `markbot sessions migrate --dry-run` 预览清理")
    return 0


def sessions_command(argv: list[str]) -> int:
    home = markbot_home()
    sub = argv[0] if argv else "status"
    if sub == "status":
        return status(home)
    if sub == "migrate":
        dry_run = "--dry-run" in argv
        report = migrate_sessions(home, dry_run=dry_run)
        prefix = "（dry-run）将" if dry_run else "已"
        print(f"检查 {report.sessions} 个含派生文件的会话")
        if report.events_written:
            print(f"{prefix}补写事件日志: {len(report.events_written)} 个")
        print(
            f"{prefix}删除派生文件: {len(report.removed)} 个，"
            f"释放 {report.freed_bytes / 1024:.0f} KB"
        )
        for item in report.kept:
            print(f"  保留 {item}")
        return 0
    print("用法: markbot sessions status | migrate [--dry-run]", file=sys.stderr)
    return 2


__all__ = ["MigrationReport", "migrate_sessions", "sessions_command", "status"]
