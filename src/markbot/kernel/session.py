"""会话状态与磁盘归档（对齐 Harness² 的 task 概念）。

会话的权威来源是 append-only 事件日志 ``events.jsonl``；内存里的 ``messages``
只是投影缓存，方便模型调用与上下文组装。所有状态变化都必须经过本模块的
``append_*`` / ``apply_compaction``，保证事件与消息不会分叉。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 脱敏实现位于稳定契约 markbot.api.redaction；这里重新导出，保持旧导入路径可用
from markbot.api.redaction import (
    REDACTED,
    clear_registered_secrets,
    redact_known,
    redact_secrets,
    register_secrets,
)
from markbot.schema.models import Message, SessionEvent, SessionMeta, ToolCallRecord
from markbot.schema.session_log import (
    COMPACTED_FILE,
    COMPACTION_PREFIX,
    COMPACTION_SUFFIX,
    EVENTS_FILE,
    LEGACY_FILES,
    TRAJECTORY_FILE,
    compaction_marker,
    project_messages,
    read_events,
)
from markbot.schema.session_log import redacted_message as _redacted_message

_FINAL_NAME = "final.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_session_id() -> str:
    return "sess_" + uuid.uuid4().hex[:12].upper()


def _atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件、fsync，再 ``os.replace``：读者只会看到旧版或新版，不会看到半个文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


_TAIL_BYTES = 64 * 1024


def _last_seq_on_disk(path: Path) -> int | None:
    """读事件文件尾部，返回最后一条完整事件的 seq；文件不存在或尾部不可解析时返回 None。"""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return 0
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None
    if not tail.endswith(b"\n"):
        return None  # 末行被截断：需要重写
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return int(json.loads(line.decode("utf-8")).get("seq"))
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            return None
    return 0


class Session:
    def __init__(
        self,
        request: str,
        *,
        session_id: str | None = None,
        domain: str = "general",
        skills: Any = None,
        runtime: Any = None,
        events_dir: Path | None = None,
        append_request: bool = True,
        request_images: list[str] | None = None,
    ) -> None:
        self.id = session_id or new_session_id()
        self.request = request
        self.domain = domain
        self.skills = skills
        self.runtime = runtime
        # 单次运行附带的元数据（如渠道投递信息、定时任务上下文），不写入归档
        self.metadata: dict[str, Any] = {}
        self.started_at = now_iso()
        self.ended_at: str | None = None
        self.status = "running"
        self.steps = 0
        self.tool_call_count = 0
        self.messages: list[Message] = []
        self.compaction_count = 0
        self.compacted_segments: list[list[dict[str, Any]]] = []
        self.feedback: dict[str, Any] | None = None
        self.archive_dir: Path | None = None
        self._events: list[SessionEvent] = []
        self._seq = 0
        self._subscribers: list[Callable[[SessionEvent], None]] = []
        self._events_path: Path | None = (
            Path(events_dir) / self.id / "events.jsonl" if events_dir else None
        )
        # 实时追加是否失败过：失败后归档时必须整体重写一次，补齐缺失事件
        self._events_dirty = False
        # 首次追加前检查文件末尾：上次崩溃可能留下没有换行的半行，直接追加会把新事件也写坏
        self._tail_checked = False
        if append_request:
            self.append_user(request, images=request_images)

    # ------------------------------------------------------------------ events
    @property
    def events(self) -> list[SessionEvent]:
        return list(self._events)

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def persists_events(self) -> bool:
        """事件是否实时落盘（落盘的会话可以随时移出内存，之后从归档恢复）。"""
        return self._events_path is not None

    def subscribe(self, callback: Callable[[SessionEvent], None]) -> Callable[[], None]:
        """订阅会话事件；返回退订函数。订阅者异常不会影响会话本身。"""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def _append_event(self, type_: str, data: dict[str, Any]) -> SessionEvent:
        self._seq += 1
        event = SessionEvent(
            seq=self._seq,
            ts=now_iso(),
            type=type_,  # type: ignore[arg-type]
            data=_redact_value(data),
        )
        self._events.append(event)
        self._write_event(event)
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - 订阅者失败不影响会话
                continue
        return event

    def _write_event(self, event: SessionEvent) -> None:
        if self._events_path is None:
            return
        try:
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            prefix = ""
            if not self._tail_checked:
                self._tail_checked = True
                if self._events_path.is_file() and self._events_path.stat().st_size > 0:
                    with self._events_path.open("rb") as fh:
                        fh.seek(-1, os.SEEK_END)
                        if fh.read(1) != b"\n":
                            prefix = "\n"  # 隔开残缺的末行；读取时它会被当作坏行跳过
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(prefix + event.model_dump_json() + "\n")
        except OSError:
            # 事件日志写失败不应打断回合；标记后由归档整体重写补齐。
            self._events_dirty = True

    def _write_all_events(self, path: Path) -> None:
        _atomic_write_text(
            path, "".join(event.model_dump_json() + "\n" for event in self._events)
        )
        if self._events_path is not None and _same_path(path, self._events_path):
            self._events_dirty = False
            self._tail_checked = True

    def _events_in_sync(self, path: Path) -> bool:
        """磁盘上的事件日志是否已经完整：同一文件、追加从未失败、末条 seq 与内存一致。"""
        if self._events_path is None or self._events_dirty:
            return False
        if not _same_path(path, self._events_path):
            return False
        return _last_seq_on_disk(path) == self._seq

    def _restore(self, events: list[SessionEvent], messages: list[Message]) -> None:
        self._events = sorted(events, key=lambda item: item.seq)
        self._seq = self._events[-1].seq if self._events else 0
        self.messages = messages
        self.compacted_segments = [
            list(event.data.get("removed_messages") or [])
            for event in self._events
            if event.type == "compaction"
        ]

    # -------------------------------------------------------------------- load
    @classmethod
    def load(
        cls,
        home: Path,
        session_id: str,
        *,
        skills: Any = None,
        runtime: Any = None,
    ) -> "Session | None":
        """从 ``sessions/<id>/`` 归档恢复会话；归档不存在或不可读时返回 None。

        优先读 ``events.jsonl``（append-only 权威日志）；旧归档只有
        ``trajectory.jsonl`` 时，把它反推成事件并补写事件日志。
        """
        target = Path(home) / "sessions" / session_id
        events_path = target / EVENTS_FILE
        trajectory = target / TRAJECTORY_FILE
        if not events_path.is_file() and not trajectory.is_file():
            return None

        meta: dict[str, Any] = {}
        meta_path = target / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        request = str(meta.get("request") or "")

        events: list[SessionEvent] = []
        messages: list[Message] = []
        damaged = 0
        if events_path.is_file():
            events, messages, damaged = _read_events(events_path)
        if not messages:
            events, messages = [], _read_trajectory(trajectory)
            if not messages:
                return None
            events = _events_from_messages(messages)
        if not request:
            request = next(
                (msg.content for msg in messages if msg.role == "user"), ""
            )

        session = cls(
            request,
            session_id=session_id,
            domain=str(meta.get("domain") or "general"),
            skills=skills,
            runtime=runtime,
            events_dir=target.parent,
            append_request=False,
        )
        session._restore(events, messages)
        if damaged:
            # 日志里有坏行（崩溃留下的半行等）：读取时已跳过，下次归档整体重写成干净文件
            session._events_dirty = True
        if not events_path.is_file():
            # 旧归档没有事件日志：补写一份，后续回合继续 append 就不会断代。
            try:
                session._write_all_events(events_path)
            except OSError:
                pass
        session.started_at = str(meta.get("started_at") or session.started_at)
        session.status = str(meta.get("status") or "running")
        session.steps = int(meta.get("steps") or 0)
        session.tool_call_count = int(meta.get("tool_calls") or 0)
        session.compaction_count = int(meta.get("compactions") or 0)
        session.archive_dir = target
        return session

    # ---------------------------------------------------------------- mutation
    def append_user(self, text: str, images: list[str] | None = None) -> None:
        paths = [str(item) for item in images or [] if str(item).strip()]
        self.messages.append(Message(role="user", content=text, images=paths))
        data: dict[str, Any] = {"content": text}
        if paths:
            data["images"] = paths
        self._append_event("user_message", data)

    def append_assistant(self, text: str, tool_calls: list[Any] | None = None) -> None:
        records = [
            ToolCallRecord(id=c.id, name=c.name, arguments=dict(c.args or {}))
            for c in (tool_calls or [])
        ]
        self.messages.append(Message(role="assistant", content=text or "", tool_calls=records))
        self._append_event(
            "assistant_message",
            {
                "content": text or "",
                "tool_calls": [record.model_dump() for record in records],
            },
        )

    def append_tool_results(self, calls: list[Any], results: list[Any]) -> None:
        for call, result in zip(calls, results):
            self.tool_call_count += 1
            raw_images = (getattr(result, "details", None) or {}).get("images") or []
            images = (
                [str(item) for item in raw_images if isinstance(item, str) and item]
                if isinstance(raw_images, list)
                else []
            )
            self.messages.append(
                Message(
                    role="tool",
                    content=result.content,
                    tool_call_id=call.id,
                    name=call.name,
                    is_error=bool(result.is_error),
                    images=images,
                )
            )
            data: dict[str, Any] = {
                "content": result.content,
                "tool_call_id": call.id,
                "name": call.name,
                "is_error": bool(result.is_error),
            }
            if images:
                data["images"] = images
            self._append_event("tool_result", data)

    def record_feedback(self, payload: dict[str, Any]) -> None:
        """用户反馈（点赞/纠正/评分），随会话归档写入 feedback.json。"""
        self.feedback = dict(payload)

    def redact_messages(
        self, indexes: Iterable[int], placeholder: str, *, reason: str = ""
    ) -> list[dict[str, Any]]:
        """把指定消息替换成占位文本（并清空其工具参数），作为 ``redaction`` 事件落盘。

        直接改 ``messages`` 会让内存与 ``events.jsonl`` 分叉：重启后从日志恢复，
        被移除的内容又会回来。返回被替换消息的摘要（不含原文）。
        """
        targets = sorted({i for i in indexes if isinstance(i, int) and 0 <= i < len(self.messages)})
        if not targets:
            return []
        removed: list[dict[str, Any]] = []
        for index in targets:
            message = self.messages[index]
            removed.append(
                {
                    "index": index,
                    "role": message.role,
                    "tool": message.name,
                    "chars": len(message.content),
                }
            )
            self.messages[index] = _redacted_message(message, placeholder)
        self._append_event(
            "redaction",
            {"indexes": targets, "placeholder": placeholder, "reason": reason},
        )
        return removed

    def apply_compaction(self, summary: str, cut: int) -> None:
        """把 ``messages[:cut]`` 换成一条摘要锚点；原文保留在归档里供审计。"""
        removed = [msg.model_dump() for msg in self.messages[:cut]]
        self.compacted_segments.append(removed)
        self.compaction_count += 1
        self.messages = [compaction_marker(summary), *self.messages[cut:]]
        self._append_event(
            "compaction",
            {
                "summary": summary,
                "removed": cut,
                "removed_messages": removed,
            },
        )

    def messages_for_model(self) -> list[dict[str, Any]]:
        return [msg.model_dump() for msg in self.messages]

    def final_text(self) -> str:
        for msg in reversed(self.messages):
            if msg.role == "assistant" and msg.content.strip():
                return msg.content
        return ""

    def meta(self, *, harness_snapshot: str | None = None) -> SessionMeta:
        return SessionMeta(
            id=self.id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            domain=self.domain,
            status=self.status,
            harness_snapshot=harness_snapshot,
            steps=self.steps,
            tool_calls=self.tool_call_count,
            request=self.request,
            compactions=self.compaction_count,
        )

    # 是否继续写派生文件 trajectory.jsonl / compacted.jsonl（ADR 0007）。默认不写：
    # 它们能从 events.jsonl 完整重建。Kernel 按 [sessions] legacy_views 覆盖。
    write_legacy_views: bool = False

    def archive(
        self,
        home: Path,
        *,
        harness_snapshot: str | None = None,
        legacy_views: bool | None = None,
    ) -> Path:
        self.ended_at = self.ended_at or now_iso()
        target = Path(home) / "sessions" / self.id
        target.mkdir(parents=True, exist_ok=True)
        meta = self.meta(harness_snapshot=harness_snapshot).model_copy(
            update={"request": redact_secrets(self.request)}
        )
        # 所有归档文件都原子替换：后台整理器/优化器可能在任意时刻读取它们
        _atomic_write_text(target / "meta.json", redact_secrets(meta.model_dump_json(indent=2)))
        _atomic_write_text(target / "request.md", redact_secrets(self.request))
        # 事件日志是权威来源，平时实时追加；只有不完整（追加失败、末行残缺、
        # 不是同一文件）时才整体原子重写，避免每回合都重写一遍权威日志。
        events_path = target / EVENTS_FILE
        if not self._events_in_sync(events_path):
            self._write_all_events(events_path)
        legacy = self.write_legacy_views if legacy_views is None else legacy_views
        if legacy:
            self._write_legacy_views(target)
        else:
            self._drop_legacy_views(target)
        (target / "files").mkdir(exist_ok=True)
        (target / "deliverable").mkdir(exist_ok=True)
        final = self.final_text()
        if final:
            _atomic_write_text(target / "deliverable" / _FINAL_NAME, redact_secrets(final))
        if self.feedback is not None:
            _atomic_write_text(
                target / "feedback.json",
                redact_secrets(json.dumps(self.feedback, ensure_ascii=False, indent=2)),
            )
        self.archive_dir = target
        return target

    def _drop_legacy_views(self, target: Path) -> None:
        """事件日志已完整落盘后，删除能从它重建的旧派生文件，避免同一份内容存三遍。

        ``trajectory.jsonl`` 就是当前投影，总能重建。``compacted.jsonl`` 只有在事件里的
        压缩段不少于它时才删：ADR 0001 之前的老归档是从 trajectory 反推出事件的，
        没有压缩事件，删掉会丢失被压缩的原文。
        """
        legacy_compacted = target / COMPACTED_FILE
        keep_compacted = False
        if legacy_compacted.is_file():
            try:
                rows = sum(
                    1 for line in legacy_compacted.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            except OSError:
                rows = 0
            keep_compacted = rows > len(self.compacted_segments)
        for name in LEGACY_FILES:
            if name == COMPACTED_FILE and keep_compacted:
                continue
            try:
                (target / name).unlink(missing_ok=True)
            except OSError:
                pass

    def _write_legacy_views(self, target: Path) -> None:
        """兼容旧读取方：从内存投影写出 trajectory.jsonl / compacted.jsonl。"""
        _atomic_write_text(
            target / TRAJECTORY_FILE,
            "".join(
                json.dumps(_redact_row(msg.model_dump()), ensure_ascii=False) + "\n"
                for msg in self.messages
            ),
        )
        if self.compacted_segments:
            _atomic_write_text(
                target / COMPACTED_FILE,
                "".join(
                    json.dumps(
                        {
                            "segment": index,
                            "messages": [_redact_row(row) for row in segment],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                    for index, segment in enumerate(self.compacted_segments, start=1)
                ),
            )


def _same_path(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _read_events(path: Path) -> tuple[list[SessionEvent], list[Message], int]:
    """读取事件日志，返回 ``(事件, 投影消息, 跳过的坏行数)``。"""
    events, damaged = read_events(path)
    return events, project_messages(events), damaged


def _read_trajectory(path: Path) -> list[Message]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    messages: list[Message] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(Message.model_validate(json.loads(line)))
        except (TypeError, ValueError):
            continue
    return messages


def _events_from_messages(messages: list[Message]) -> list[SessionEvent]:
    """把旧轨迹反推成等价事件，用于补写事件日志。"""
    events: list[SessionEvent] = []
    seq = 0
    for message in messages:
        seq += 1
        if message.role == "user":
            type_, data = "user_message", {"content": message.content}
            if message.images:
                data["images"] = list(message.images)
        elif message.role == "assistant":
            type_ = "assistant_message"
            data = {
                "content": message.content,
                "tool_calls": [call.model_dump() for call in message.tool_calls],
            }
        else:
            type_ = "tool_result"
            data = {
                "content": message.content,
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "is_error": message.is_error,
            }
            if message.images:
                data["images"] = list(message.images)
        events.append(
            SessionEvent(seq=seq, ts=now_iso(), type=type_, data=data)  # type: ignore[arg-type]
        )
    return events


def _redact_row(row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("content"), str):
        row["content"] = redact_secrets(row["content"])
    for call in row.get("tool_calls") or []:
        if "arguments" in call:
            call["arguments"] = _redact_value(call["arguments"])
    return row


def _redact_value(value: Any) -> Any:
    """递归脱敏工具参数；不再经过 JSON 文本往返，避免序列化/转义被破坏。"""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def copy_tree(src: Path, dst: Path) -> None:
    if not Path(src).exists():
        return
    if Path(dst).exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


__all__ = [
    "COMPACTION_PREFIX",
    "COMPACTION_SUFFIX",
    "REDACTED",
    "Session",
    "clear_registered_secrets",
    "copy_tree",
    "new_session_id",
    "now_iso",
    "project_messages",
    "redact_known",
    "redact_secrets",
    "register_secrets",
]
