"""会话事件日志的读取与投影（纯函数，只依赖 ``schema.models``）。

``sessions/<id>/events.jsonl`` 是会话的唯一事实来源（ADR 0001 / 0007）。旧版归档额外
写了两份派生文件：

- ``trajectory.jsonl``：当前投影（= ``project_messages(events)``）；
- ``compacted.jsonl``：每次压缩移除的原文（= 各 ``compaction`` 事件的 ``removed_messages``）。

它们都能从事件日志完整重建，因此默认不再写入。kernel、memory、optimizer 统一通过这里
读取「轨迹」与「压缩段」视图：有事件日志就从事件投影，只有旧文件的旧归档才回退去读旧文件。

事件在追加时已经脱敏（``Session._append_event``），这里不重复脱敏。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from markbot.schema.models import Message, SessionEvent, ToolCallRecord

EVENTS_FILE = "events.jsonl"
TRAJECTORY_FILE = "trajectory.jsonl"
COMPACTED_FILE = "compacted.jsonl"
LEGACY_FILES = (TRAJECTORY_FILE, COMPACTED_FILE)

COMPACTION_PREFIX = "[此前对话已压缩为以下摘要，继续任务时以此为准]\n<summary>\n"
COMPACTION_SUFFIX = "\n</summary>"
# 与 markbot.api.redaction.REDACTED 同值；redaction 事件总是自带 placeholder，这里只是兜底
DEFAULT_PLACEHOLDER = "[REDACTED]"


# ------------------------------------------------------------------ 投影
def compaction_marker(summary: str) -> Message:
    return Message(
        role="user",
        content=f"{COMPACTION_PREFIX}{summary.strip()}{COMPACTION_SUFFIX}",
    )


def redacted_message(message: Message, placeholder: str) -> Message:
    """占位替换正文并清空工具参数；保留角色、调用 id 与名称，维持 call/result 配对。"""
    return message.model_copy(
        update={
            "content": placeholder,
            "tool_calls": [
                call.model_copy(update={"arguments": {}}) for call in message.tool_calls
            ],
        }
    )


def project_messages(events: Iterable[SessionEvent]) -> list[Message]:
    """把事件日志投影成消息列表（纯函数，可回放）。

    每类事件恰好对应一条消息；压缩事件按 ``removed`` 数替换前缀，和
    ``Session.apply_compaction`` 的语义完全一致。
    """
    messages: list[Message] = []
    for event in sorted(events, key=lambda item: item.seq):
        data = event.data or {}
        if event.type == "user_message":
            messages.append(Message(role="user", content=str(data.get("content") or "")))
        elif event.type == "assistant_message":
            records = [
                ToolCallRecord.model_validate(call) for call in (data.get("tool_calls") or [])
            ]
            messages.append(
                Message(
                    role="assistant",
                    content=str(data.get("content") or ""),
                    tool_calls=records,
                )
            )
        elif event.type == "tool_result":
            messages.append(
                Message(
                    role="tool",
                    content=str(data.get("content") or ""),
                    tool_call_id=data.get("tool_call_id"),
                    name=data.get("name"),
                    is_error=bool(data.get("is_error")),
                )
            )
        elif event.type == "compaction":
            removed = max(0, int(data.get("removed") or 0))
            messages = [compaction_marker(str(data.get("summary") or "")), *messages[removed:]]
        elif event.type == "redaction":
            placeholder = str(data.get("placeholder") or DEFAULT_PLACEHOLDER)
            for index in data.get("indexes") or []:
                if isinstance(index, int) and 0 <= index < len(messages):
                    messages[index] = redacted_message(messages[index], placeholder)
    return messages


def compacted_segments(events: Iterable[SessionEvent]) -> list[list[dict[str, Any]]]:
    """每次压缩被移除的原文（与旧 ``compacted.jsonl`` 的 ``messages`` 字段一致）。"""
    return [
        [row for row in (event.data.get("removed_messages") or []) if isinstance(row, dict)]
        for event in sorted(events, key=lambda item: item.seq)
        if event.type == "compaction"
    ]


# ------------------------------------------------------------------ 读取
def read_events(path: Path) -> tuple[list[SessionEvent], int]:
    """读取事件日志，返回 ``(按 seq 排序的事件, 跳过的坏行数)``；文件不可读返回空。"""
    events: list[SessionEvent] = []
    damaged = 0
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(SessionEvent.model_validate(json.loads(line)))
        except (TypeError, ValueError):
            damaged += 1
    events.sort(key=lambda item: item.seq)
    return events, damaged


def read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


class SessionLog:
    """一个会话目录的只读视图：轨迹与压缩段都从事件日志派生，旧归档回退旧文件。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._events: list[SessionEvent] | None = None

    @property
    def events_path(self) -> Path:
        return self.directory / EVENTS_FILE

    def events(self) -> list[SessionEvent]:
        if self._events is None:
            self._events = read_events(self.events_path)[0] if self.events_path.is_file() else []
        return self._events

    @property
    def from_events(self) -> bool:
        """事件日志存在且能投影出消息：此时旧文件即使存在也被忽略。"""
        return bool(self.events()) and bool(project_messages(self.events()))

    def exists(self) -> bool:
        return self.from_events or (self.directory / TRAJECTORY_FILE).is_file()

    def trajectory(self) -> list[dict[str, Any]]:
        """当前投影，每条是 ``Message.model_dump()``（与旧 ``trajectory.jsonl`` 行一致）。"""
        if self.from_events:
            return [message.model_dump() for message in project_messages(self.events())]
        return read_jsonl_rows(self.directory / TRAJECTORY_FILE)

    def compacted(self) -> list[dict[str, Any]]:
        """压缩段，每条 ``{"segment": n, "messages": [...]}``（与旧 ``compacted.jsonl`` 行一致）。

        ADR 0001 之前的老归档：事件是从 trajectory 反推的，没有压缩事件，被压缩的原文
        只在 ``compacted.jsonl`` 里；旧文件的段数更多时以旧文件为准（归档时也不会删它）。
        """
        legacy = read_jsonl_rows(self.directory / COMPACTED_FILE)
        if not self.from_events:
            return legacy
        derived = [
            {"segment": index, "messages": segment}
            for index, segment in enumerate(compacted_segments(self.events()), start=1)
        ]
        return legacy if len(legacy) > len(derived) else derived

    def legacy_files(self) -> list[Path]:
        return [self.directory / name for name in LEGACY_FILES if (self.directory / name).is_file()]


def trajectory_text(directory: Path) -> str:
    """轨迹的 JSONL 文本（调试、测试与人工查看用）。"""
    return "".join(
        json.dumps(row, ensure_ascii=False) + "\n" for row in SessionLog(directory).trajectory()
    )


__all__ = [
    "COMPACTED_FILE",
    "COMPACTION_PREFIX",
    "COMPACTION_SUFFIX",
    "DEFAULT_PLACEHOLDER",
    "EVENTS_FILE",
    "LEGACY_FILES",
    "TRAJECTORY_FILE",
    "SessionLog",
    "compacted_segments",
    "compaction_marker",
    "project_messages",
    "read_events",
    "read_jsonl_rows",
    "redacted_message",
    "trajectory_text",
]
