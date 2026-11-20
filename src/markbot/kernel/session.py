"""会话状态与磁盘归档（对齐 Harness² 的 task 概念）。"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from markbot.schema.models import Message, SessionMeta, ToolCallRecord

_FINAL_NAME = "final.md"
REDACTED = "[REDACTED]"
COMPACTION_PREFIX = "[此前对话已压缩为以下摘要，继续任务时以此为准]\n<summary>\n"
COMPACTION_SUFFIX = "\n</summary>"

# 归档前对密钥形状的字符串做脱敏；宁可多遮，不写入明文。
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk-ant-[a-z0-9_\-]{16,})"),
    re.compile(r"(?i)\b(sk-[a-z0-9_\-]{16,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    # 前缀允许下划线等字符：``CRAWL4AI_API_TOKEN=`` 这类环境变量名里 ``TOKEN`` 前
    # 没有词边界，旧写法 ``\btoken\b`` 会漏过，导致密钥以明文落盘。
    # 值里排除反斜杠：脱敏对象常常是 JSON 文本，``token=xxx\"`` 的 ``\`` 是转义符，
    # 一旦被吞掉就会留下裸引号，把整段 JSON 破坏成非法格式（归档时 json.loads 崩溃）。
    re.compile(
        r"(?i)[A-Za-z0-9_.\-]*(?:api[_-]?key|access[_-]?token|token|secret|password|passwd)"
        r"\b\s*[:=]\s*['\"]?([^\s'\"`,;\\]{8,})"
    ),
    re.compile(r"(?i)\bbearer\s+([a-z0-9._\-]{16,})"),
)


def redact_secrets(text: str) -> str:
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(
            lambda match: match.group(0).replace(match.group(1), REDACTED), out
        )
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_session_id() -> str:
    return "sess_" + uuid.uuid4().hex[:12].upper()


class Session:
    def __init__(
        self,
        request: str,
        *,
        session_id: str | None = None,
        domain: str = "general",
        skills: Any = None,
        runtime: Any = None,
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
        self.append_user(request)

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

        只有显式传入 ``session_id`` 的调用方（渠道等）会走这里，用于服务重启后
        继续沿用同一会话的上下文。本地交互式会话不传 id，仍然每次全新开始。
        """
        target = Path(home) / "sessions" / session_id
        trajectory = target / "trajectory.jsonl"
        if not trajectory.is_file():
            return None
        messages: list[Message] = []
        try:
            lines = trajectory.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(Message.model_validate(json.loads(line)))
            except (TypeError, ValueError):
                continue
        if not messages:
            return None

        meta: dict[str, Any] = {}
        meta_path = target / "meta.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
        request = str(meta.get("request") or "")
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
        )
        session.messages = messages
        session.started_at = str(meta.get("started_at") or session.started_at)
        session.status = str(meta.get("status") or "running")
        session.steps = int(meta.get("steps") or 0)
        session.tool_call_count = int(meta.get("tool_calls") or 0)
        session.compaction_count = int(meta.get("compactions") or 0)
        session.archive_dir = target
        return session

    def append_user(self, text: str) -> None:
        self.messages.append(Message(role="user", content=text))

    def append_assistant(self, text: str, tool_calls: list[Any] | None = None) -> None:
        records = [
            ToolCallRecord(id=c.id, name=c.name, arguments=dict(c.args or {}))
            for c in (tool_calls or [])
        ]
        self.messages.append(Message(role="assistant", content=text or "", tool_calls=records))

    def append_tool_results(self, calls: list[Any], results: list[Any]) -> None:
        for call, result in zip(calls, results):
            self.tool_call_count += 1
            self.messages.append(
                Message(
                    role="tool",
                    content=result.content,
                    tool_call_id=call.id,
                    name=call.name,
                    is_error=bool(result.is_error),
                )
            )

    def record_feedback(self, payload: dict[str, Any]) -> None:
        """用户反馈（点赞/纠正/评分），随会话归档写入 feedback.json。"""
        self.feedback = dict(payload)

    def apply_compaction(self, summary: str, cut: int) -> None:
        """把 ``messages[:cut]`` 换成一条摘要锚点；原文保留在归档里供审计。"""
        removed = [msg.model_dump() for msg in self.messages[:cut]]
        self.compacted_segments.append(removed)
        self.compaction_count += 1
        marker = Message(
            role="user",
            content=f"{COMPACTION_PREFIX}{summary.strip()}{COMPACTION_SUFFIX}",
        )
        self.messages = [marker, *self.messages[cut:]]

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

    def archive(self, home: Path, *, harness_snapshot: str | None = None) -> Path:
        self.ended_at = self.ended_at or now_iso()
        target = Path(home) / "sessions" / self.id
        target.mkdir(parents=True, exist_ok=True)
        meta = self.meta(harness_snapshot=harness_snapshot).model_copy(
            update={"request": redact_secrets(self.request)}
        )
        (target / "meta.json").write_text(
            redact_secrets(meta.model_dump_json(indent=2)), encoding="utf-8"
        )
        (target / "request.md").write_text(redact_secrets(self.request), encoding="utf-8")
        with (target / "trajectory.jsonl").open("w", encoding="utf-8") as fh:
            for msg in self.messages:
                fh.write(json.dumps(_redact_row(msg.model_dump()), ensure_ascii=False) + "\n")
        if self.compacted_segments:
            with (target / "compacted.jsonl").open("w", encoding="utf-8") as fh:
                for index, segment in enumerate(self.compacted_segments, start=1):
                    fh.write(
                        json.dumps(
                            {
                                "segment": index,
                                "messages": [_redact_row(row) for row in segment],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
        (target / "files").mkdir(exist_ok=True)
        (target / "deliverable").mkdir(exist_ok=True)
        final = self.final_text()
        if final:
            (target / "deliverable" / _FINAL_NAME).write_text(
                redact_secrets(final), encoding="utf-8"
            )
        if self.feedback is not None:
            (target / "feedback.json").write_text(
                redact_secrets(json.dumps(self.feedback, ensure_ascii=False, indent=2)),
                encoding="utf-8",
            )
        self.archive_dir = target
        return target


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


__all__ = ["Session", "copy_tree", "new_session_id", "now_iso", "redact_secrets"]
