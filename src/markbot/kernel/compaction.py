"""上下文压缩：超过字符预算时，把较早的消息摘要成一条锚点消息。

参考 Pi 的 compaction 策略：
- 保留最近一段原文，只压缩更早的前缀；
- 优先切在 user 边界；当前回合本身就超出预算时（长工具循环只有一条 user 消息），
  退到回合内的 assistant 边界——切在 assistant 前面不会拆散 tool call 与 tool result；
- 被移除的部分只剩上一次的摘要锚点时不压缩：那只是重新摘要一遍摘要，白花一次模型调用；
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
# 保留区的上限：超过它说明保留下来的仍会很快再次触发压缩，需要切进当前回合
MAX_KEEP_RATIO = 0.6
MIN_KEEP_CHARS = 4000
CURRENT_REQUEST_MAX_CHARS = 2000


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


def is_compaction_marker(message: Message) -> bool:
    return message.role == "user" and (message.content or "").startswith(COMPACTION_PREFIX)


def _last_request_index(messages: list[Message]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user" and not is_compaction_marker(messages[index]):
            return index
    return None


def select_cut(
    messages: list[Message], keep_chars: int, max_keep_chars: int | None = None
) -> int | None:
    """从尾部向前累计，找到保留区起点（``messages[cut:]`` 原样保留）。

    1. 优先切在 user 边界：取保留量达到 ``keep_chars`` 处之后的第一条 user 消息；
    2. 没有这样的 user 边界时保留整个最近回合；但若最近回合本身超过
       ``max_keep_chars``，就切进回合内部，落在 assistant 消息前面（其 tool 结果紧随其后，
       调用与结果不会被拆开）；
    3. 被移除的前缀只有压缩锚点时返回 ``None``：没有可节省的内容。
    """
    users = [index for index, message in enumerate(messages) if message.role == "user"]
    if not users:
        return None
    start: int | None = None
    kept = 0
    for index in range(len(messages) - 1, -1, -1):
        kept += message_chars(messages[index])
        if kept >= keep_chars:
            start = index
            break
    if start is None:
        return None

    cut = next((item for item in users if item >= start), None)
    if cut is None:
        cut = users[-1]
        turn_chars = sum(message_chars(message) for message in messages[cut:])
        if max_keep_chars is not None and turn_chars > max_keep_chars:
            inner = [
                index
                for index, message in enumerate(messages)
                if index > cut and message.role == "assistant"
            ]
            if inner:
                cut = next((item for item in inner if item >= start), inner[-1])
    if cut <= 0:
        return None
    if all(is_compaction_marker(message) for message in messages[:cut]):
        return None
    return cut


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
        self.max_keep_chars = max(self.keep_chars, int(self.budget_chars * MAX_KEEP_RATIO))

    def should_compact(self, payload: dict[str, Any]) -> bool:
        return estimate_chars(payload) > self.trigger_chars

    async def compact(self, session: Session) -> CompactionOutcome | None:
        before = len(session.messages)
        cut = select_cut(session.messages, self.keep_chars, self.max_keep_chars)
        if cut is None:
            return None
        summary = await summarize(self.provider, session.messages[:cut])
        request_index = _last_request_index(session.messages)
        if request_index is not None and request_index < cut:
            # 切进了当前回合：用户这一轮的原始请求会被摘要掉，原文附在摘要后，避免目标走样
            request = _truncate(session.messages[request_index].content, CURRENT_REQUEST_MAX_CHARS)
            summary = f"{summary.rstrip()}\n\n## 当前请求原文\n{request}"
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
    "is_compaction_marker",
    "select_cut",
    "serialize_messages",
    "summarize",
]
