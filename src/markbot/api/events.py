"""事件名常量。事件字符串同时是扩展的订阅键。"""

from __future__ import annotations


class EventName:
    EXTENSION_LOADED = "extension_loaded"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    BEFORE_PROVIDER_REQUEST = "before_provider_request"
    AFTER_PROVIDER_RESPONSE = "after_provider_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    BEFORE_COMPACTION = "before_compaction"
    INPUT = "input"
    SHUTDOWN = "shutdown"


BLOCKABLE = frozenset(
    {
        EventName.BEFORE_PROVIDER_REQUEST,
        EventName.TOOL_CALL,
        EventName.BEFORE_COMPACTION,
        EventName.INPUT,
    }
)

ALL_EVENTS = frozenset(
    {
        EventName.EXTENSION_LOADED,
        EventName.SESSION_START,
        EventName.SESSION_END,
        EventName.TURN_START,
        EventName.TURN_END,
        EventName.BEFORE_PROVIDER_REQUEST,
        EventName.AFTER_PROVIDER_RESPONSE,
        EventName.TOOL_CALL,
        EventName.TOOL_RESULT,
        EventName.BEFORE_COMPACTION,
        EventName.INPUT,
        EventName.SHUTDOWN,
    }
)

__all__ = ["ALL_EVENTS", "BLOCKABLE", "EventName"]
