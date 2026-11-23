"""会话内的文件读写跟踪（扩展间共享的库模块，加载器会跳过 ``_`` 开头的文件；ADR 0012）。

- 读文件时记下文件状态（mtime + 大小 + 内容哈希），写/改之前检查文件是否在读取后被外部改过，
  避免覆盖用户在编辑器里的改动；
- 记录本会话改过哪些文件、增删多少行（交付报告用）；
- 记录「改了代码之后有没有验证过」，供 Agent Loop 的验证提醒使用。

状态挂在会话对象的属性上，只在进程内有效；不写入会话事件（会话事实仍以 events.jsonl 为准）。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

# 改了这些后缀的文件才需要「验证提醒」；文档、配置以外的都算代码
NON_CODE_SUFFIXES = frozenset(
    {".md", ".markdown", ".txt", ".rst", ".log", ".csv", ".json", ".yaml", ".yml", ".toml",
     ".ini", ".cfg", ".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".pdf"}
)


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def file_state(path: Path, data: bytes | None = None) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    state: dict[str, Any] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    if data is not None:
        state["sha1"] = _digest(data)
    return state


def _reads(session: Any) -> dict[str, dict[str, Any]]:
    reads = getattr(session, "file_reads", None)
    if reads is None:
        reads = {}
        try:
            session.file_reads = reads
        except AttributeError:
            pass
    return reads


def record_read(session: Any, path: Path, data: bytes | None = None) -> None:
    state = file_state(path, data)
    if state is not None:
        _reads(session)[str(path)] = state


def stale_reason(session: Any, path: Path) -> str | None:
    """文件在本会话读取之后被外部修改过时返回原因；没读过或没变化返回 None。"""
    known = _reads(session).get(str(path))
    if not known:
        return None
    current = file_state(path)
    if current is None:
        return f"{path} 在读取之后被删除了"
    if current["mtime_ns"] == known["mtime_ns"] and current["size"] == known["size"]:
        return None
    if "sha1" in known:
        try:
            if _digest(path.read_bytes()) == known["sha1"]:
                return None  # 只是 touch 过，内容没变
        except OSError:
            pass
    return (
        f"{path} 在你上次读取之后被修改过（可能是用户在编辑器里改的）。"
        "请先重新读取文件，确认最新内容后再修改，避免覆盖别人的改动"
    )


def record_change(
    session: Any, path: Path, *, added: int = 0, removed: int = 0, action: str = "edit",
    data: bytes | None = None,
) -> None:
    """记录一次改动，并把新状态记为「已读」（自己的改动不算外部修改）。"""
    changes = getattr(session, "file_changes", None)
    if changes is None:
        changes = {}
        try:
            session.file_changes = changes
        except AttributeError:
            return
    key = str(path)
    entry = changes.setdefault(key, {"added": 0, "removed": 0, "actions": []})
    entry["added"] += added
    entry["removed"] += removed
    if action not in entry["actions"]:
        entry["actions"].append(action)
    entry["at"] = time.time()
    if action == "delete":
        _reads(session).pop(key, None)
    else:
        record_read(session, path, data)
    if Path(key).suffix.lower() not in NON_CODE_SUFFIXES:
        try:
            session.code_changed_at = time.time()
        except AttributeError:
            pass


def mark_verified(session: Any, *, ok: bool, kind: str, summary: str = "") -> None:
    """记录一次验证（测试 / 静态检查 / 构建）。"""
    try:
        session.verified_at = time.time()
        records = getattr(session, "verifications", None)
        if records is None:
            records = []
            session.verifications = records
        records.append({"kind": kind, "ok": ok, "summary": summary[:300], "at": time.time()})
        del records[:-20]
    except AttributeError:
        pass


def needs_verification(session: Any) -> bool:
    changed = getattr(session, "code_changed_at", None)
    if not changed:
        return False
    verified = getattr(session, "verified_at", None) or 0
    return verified < changed


def changes_summary(session: Any, limit: int = 20) -> str:
    changes = getattr(session, "file_changes", None) or {}
    if not changes:
        return ""
    lines = [
        f"- {path}（+{entry['added']} -{entry['removed']}，{'/'.join(entry['actions'])}）"
        for path, entry in list(changes.items())[:limit]
    ]
    if len(changes) > limit:
        lines.append(f"- …另有 {len(changes) - limit} 个文件")
    return "\n".join(lines)


__all__ = [
    "changes_summary",
    "file_state",
    "mark_verified",
    "needs_verification",
    "record_change",
    "record_read",
    "stale_reason",
]
