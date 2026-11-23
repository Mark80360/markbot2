from __future__ import annotations

from pydantic import BaseModel, Field

from markbot.api import Tool, ToolResult
from markbot.schema.models import SessionEvent


class _Params(BaseModel):
    text: str = Field(description="要回显的文本")


def test_tool_schema_is_stable():
    tool = Tool(
        name="echo",
        description="回显文本",
        params=_Params,
        handler=lambda ctx, args: ToolResult(content=args.text),
    )

    assert tool.schema() == {
        "name": "echo",
        "description": "回显文本",
        "input_schema": {
            "properties": {
                "text": {"description": "要回显的文本", "title": "Text", "type": "string"}
            },
            "required": ["text"],
            "title": "_Params",
            "type": "object",
        },
    }


def test_tool_result_error_shape():
    result = ToolResult.error("boom", denied=True)
    assert result.is_error
    assert result.content == "boom"
    assert result.details == {"denied": True}


def test_session_event_schema_is_stable():
    event = SessionEvent(
        seq=3,
        ts="2026-01-01T00:00:00+08:00",
        type="tool_result",
        data={"content": "ok", "tool_call_id": "c1", "name": "echo", "is_error": False},
    )

    assert event.model_dump() == {
        "schema_version": 1,
        "seq": 3,
        "ts": "2026-01-01T00:00:00+08:00",
        "type": "tool_result",
        "data": {
            "content": "ok",
            "tool_call_id": "c1",
            "name": "echo",
            "is_error": False,
        },
    }
