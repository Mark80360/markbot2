"""Provider 抽象：只负责一次模型调用。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# 流式回调：模型每产出一段文本就调用一次，用于渠道侧渐进展示。
DeltaCallback = Callable[[str], None]


class ProviderError(RuntimeError):
    pass


class ContentRejectedError(ProviderError):
    """上游内容安全策略拒绝了请求。

    这类 4xx 与参数错误不同：请求内容不变时重试必然同样被拒，因此不重试，
    交由上层决定是否从上下文中剔除触发内容后重发。
    """


# 各家网关的拒绝文案不统一，按已知特征匹配；只在 4xx 分支使用，避免误伤。
_MODERATION_MARKERS = (
    "inappropriate input/output",
    "security reasons",
    '"code":"18"',
    '"code": "18"',
    "content policy",
    "content_policy",
    "content moderation",
    "内容安全",
    "内容审核",
    "敏感内容",
)


def is_moderation_rejection(body: str) -> bool:
    text = (body or "").lower()
    return any(marker in text for marker in _MODERATION_MARKERS)


def describe_error(exc: BaseException | None) -> str:
    """异常的可读描述。

    ``httpx.ReadTimeout`` 这类异常的 ``str()`` 是空串，直接记录会得到
    空白的错误信息（日志里就成了 ``"error": ""``），因此这里始终带上异常类型。
    """
    if exc is None:
        return "未知错误"
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    # 参数无法解析为 JSON 对象时的原因；非空时 Agent Loop 直接返回错误，不执行工具
    parse_error: str | None = None
    raw_arguments: str = ""


def parse_tool_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    """把 provider 返回的工具参数解析成 dict；失败返回 ``({}, 原因)``，不静默吞掉。

    常见失败：流式输出被截断（``max_tokens`` 用尽）导致 JSON 不完整，或模型给出
    非对象 JSON。空串/``None`` 视为无参数调用。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    value: Any = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError as exc:
            return {}, f"参数不是合法 JSON（{exc}）；可能是输出被截断"
    if not isinstance(value, dict):
        return {}, f"参数必须是 JSON 对象，实际是 {type(value).__name__}"
    return value, None


def make_tool_call(call_id: str, name: str, raw_arguments: Any) -> ToolCall:
    args, error = parse_tool_arguments(raw_arguments)
    raw_text = raw_arguments if isinstance(raw_arguments, str) else ""
    if error and not raw_text:
        try:
            raw_text = json.dumps(raw_arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_text = str(raw_arguments)
    return ToolCall(
        id=call_id, name=name, args=args, parse_error=error, raw_arguments=raw_text if error else ""
    )


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class Provider(Protocol):
    name: str

    async def complete(
        self, payload: dict[str, Any], on_delta: DeltaCallback | None = None
    ) -> ModelReply: ...


__all__ = [
    "ContentRejectedError",
    "DeltaCallback",
    "ModelReply",
    "Provider",
    "ProviderError",
    "ToolCall",
    "describe_error",
    "is_moderation_rejection",
    "make_tool_call",
    "parse_tool_arguments",
]
