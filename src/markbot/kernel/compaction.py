"""上下文压缩：超过字符预算时，把较早的消息摘要成一条锚点消息。

参考 Pi 的 compaction 策略：
- 保留最近一段原文，只压缩更早的前缀；
- 切点落在 user / assistant 边界，避免拆散 tool call 与 tool result；
- 优先用模型生成结构化摘要，失败时退化为确定性摘要。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from markbot.kernel.providers.base import Provider
from markbot.kernel.session import COMPACTION_PREFIX, COMPACTION_SUFFIX, Session
from markbot.schema.models import Message

TOOL_RESULT_MAX_CHARS = 2000
TRIGGER_RATIO = 0.8
KEEP_RATIO = 0.4
MIN_KEEP_CHARS = 4000


def spill_tool_result(
    text: str, files_dir: Path, cap: int = 16_000
) -> tuple[str, Path | None]:
    """超限工具结果落盘，上下文只留路径与预览；返回 ``(替换文本, 落盘路径)``。"""
    if len(text) <= cap:
        return text, None
    files_dir = Path(files_dir)
    files_dir.mkdir(parents=True, exist_ok=True)
    name = f"tool-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}.txt"
    path = files_dir / name
    path.write_text(text, encoding="utf-8")
    head = max(100, min(3000, cap // 2))
    tail = max(50, min(1000, cap // 4))
    preview = (
        f"[结果已落盘：{path}]\n[总长 {len(text)} 字符]\n"
        f"[开头]\n{text[:head]}\n[... 中间省略 ...]\n[结尾]\n{text[-tail:]}"
    )
    return preview, path


SUMMARIZATION_SYSTEM_PROMPT = (
    "你是上下文摘要助手。请阅读用户与 AI 助手的对话，"
    "输出结构化摘要，供另一个 LLM 继续完成任务。\n"
    "不要继续对话，不要回答对话中的问题，只输出摘要。"
)

SUMMARIZATION_PROMPT = """以上是需要压缩的对话。请生成结构化上下文摘要。

严格使用以下格式：

## 目标
[用户想完成什么；可能包含多个任务]

## 约束与偏好
- [用户提出的约束、偏好或要求]
- [没有则写“(无)”]

## 进展
### 已完成
- [x] [已完成的任务/改动]

### 进行中
- [ ] [当前工作]

### 受阻
- [遇到但未解决的问题，没有则写“(无)”]

## 关键决策
- **[决策]**：[简要理由]

## 下一步
1. [按顺序列出接下来应该做什么]

## 关键上下文
- [继续工作所需的数据、示例或引用]
- [没有则写“(无)”]

保持简洁，保留确切的文件路径、函数名与报错信息。"""


def message_chars(message: Message) -> int:
    size = len(message.content or "")
    for call in message.tool_calls:
        size += len(call.name) + len(json.dumps(call.arguments, ensure_ascii=False)) + 32
    return size + 32


def estimate_chars(payload: dict[str, Any]) -> int:
    total = len(payload.get("system") or "")
    for message in payload.get("messages") or []:
        total += len(str(message.get("content") or ""))
        for call in message.get("tool_calls") or []:
            total += len(json.dumps(call.get("arguments") or {}, ensure_ascii=False)) + 32
    for tool in payload.get("tools") or []:
        total += len(json.dumps(tool, ensure_ascii=False))
    return total


def select_cut(messages: list[Message], keep_chars: int) -> int | None:
    """从尾部向前累计，找到保留区起点；只在 user 消息处切。"""
    candidates = [
        index for index, message in enumerate(messages) if message.role == "user"
    ]
    if not candidates:
        return None
    kept = 0
    for index in range(len(messages) - 1, -1, -1):
        kept += message_chars(messages[index])
        if kept < keep_chars:
            continue
        cut = next((item for item in candidates if item >= index), candidates[-1])
        return cut if cut > 0 else None
    return None


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... 已截断 {len(text) - limit} 字符]"


def serialize_messages(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        if message.role == "user":
            if message.content.strip():
                parts.append(f"[User]: {message.content}")
        elif message.role == "assistant":
            if message.content.strip():
                parts.append(f"[Assistant]: {message.content}")
            calls = [
                f"{call.name}({json.dumps(call.arguments, ensure_ascii=False)})"
                for call in message.tool_calls
            ]
            if calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(calls)}")
        elif message.role == "tool":
            if message.content.strip():
                parts.append(
                    f"[Tool result]: {_truncate(message.content, TOOL_RESULT_MAX_CHARS)}"
                )
    return "\n\n".join(parts)


def fallback_summary(messages: list[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        if message.role == "user" and message.content.strip():
            lines.append(f"- 用户: {_truncate(message.content.strip(), 240)}")
        elif message.role == "assistant" and message.content.strip():
            lines.append(f"- 助手: {_truncate(message.content.strip(), 160)}")
        elif message.role == "tool":
            lines.append(f"- 工具 {message.name or ''}: {_truncate(message.content.strip(), 120)}")
    if not lines:
        lines.append("- （更早的对话没有可提取的文本）")
    return "（模型摘要不可用，以下为确定性摘录）\n" + "\n".join(lines)


async def summarize(provider: Provider, messages: list[Message]) -> str:
    conversation = serialize_messages(messages)
    prompt = (
        f"<conversation>\n{conversation}\n</conversation>\n\n{SUMMARIZATION_PROMPT}"
    )
    payload = {
        "system": SUMMARIZATION_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [],
    }
    try:
        reply = await provider.complete(payload)
        text = (getattr(reply, "text", "") or "").strip()
        if text:
            return text
    except Exception:  # noqa: BLE001 - 摘要失败不能让主循环失败
        pass
    return fallback_summary(messages)


@dataclass
class CompactionOutcome:
    before: int
    kept: int
    removed: int
    summary_chars: int


class Compactor:
    def __init__(self, provider: Provider, *, budget_chars: int = 60_000) -> None:
        self.provider = provider
        self.budget_chars = max(2000, int(budget_chars))
        self.trigger_chars = int(self.budget_chars * TRIGGER_RATIO)
        self.keep_chars = max(MIN_KEEP_CHARS, int(self.budget_chars * KEEP_RATIO))

    def should_compact(self, payload: dict[str, Any]) -> bool:
        return estimate_chars(payload) > self.trigger_chars

    async def compact(self, session: Session) -> CompactionOutcome | None:
        before = len(session.messages)
        cut = select_cut(session.messages, self.keep_chars)
        if cut is None:
            return None
        summary = await summarize(self.provider, session.messages[:cut])
        session.apply_compaction(summary, cut)
        return CompactionOutcome(
            before=before,
            kept=len(session.messages),
            removed=cut,
            summary_chars=len(summary),
        )


__all__ = [
    "COMPACTION_PREFIX",
    "COMPACTION_SUFFIX",
    "CompactionOutcome",
    "Compactor",
    "estimate_chars",
    "fallback_summary",
    "select_cut",
    "serialize_messages",
    "summarize",
]
