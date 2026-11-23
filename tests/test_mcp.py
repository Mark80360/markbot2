from __future__ import annotations

import pytest
from pydantic import BaseModel

from markbot.api import Tool, ToolResult
from markbot.extensions import mcp


def test_tool_names_are_sanitized_and_bounded():
    assert mcp._sanitize_mcp_tool_name("mcp_srv_a b/c:d") == "mcp_srv_a_b_c_d"

    long_name = "mcp_" + "x" * 100
    limited = mcp._sanitize_mcp_tool_name(long_name)
    assert len(limited) == 64
    assert limited.startswith("mcp_")
    assert limited != mcp._sanitize_mcp_tool_name(long_name + "y")


def test_local_refs_and_nullable_are_normalized_for_provider():
    schema = {
        "type": "object",
        "properties": {
            "target": {"$ref": "#/definitions/Node"},
            "note": {"type": ["string", "null"]},
        },
        "definitions": {
            "Node": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }

    normalized = mcp._normalize_schema_for_provider(schema)

    assert normalized["properties"]["target"]["$ref"].startswith("#/$defs/ref_")
    assert normalized["properties"]["note"] == {"type": "string", "nullable": True}
    assert any(name.startswith("ref_") for name in normalized["$defs"])


def test_parse_servers_accepts_aliases_and_infers_transport(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "secret-token")
    config = {
        "tools": {
            "mcpServers": {
                "local": {
                    "command": "npx",
                    "args": ["-y", "server"],
                    "enabledTools": ["read_file"],
                    "toolTimeout": 12,
                },
                "remote": {
                    "url": "https://example.com/sse",
                    "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
                },
                "broken": {"url": "file:///tmp/socket"},
            }
        }
    }

    servers = mcp._parse_servers(config)

    assert set(servers) == {"local", "remote"}
    assert servers["local"].transport == "stdio"
    assert servers["local"].enabled_tools == ["read_file"]
    assert servers["local"].tool_timeout == 12
    assert servers["remote"].transport == "sse"
    assert servers["remote"].headers["Authorization"] == "Bearer secret-token"


def test_redact_url_hides_credentials_and_query():
    redacted = mcp._redact_url("https://user:token@example.com:8443/sse?token=abc#frag")

    assert redacted == "https://example.com:8443/..."
    assert "token" not in redacted


class _Args(BaseModel):
    text: str


def _handler(ctx, args) -> ToolResult:
    return ToolResult(content=str(args))


def test_tool_parse_args_uses_raw_schema_when_present():
    dynamic = Tool(
        name="mcp_demo",
        description="demo",
        params=_Args,
        handler=_handler,
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    assert dynamic.schema()["input_schema"] == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }
    assert dynamic.parse_args({"any": "value"}) == {"any": "value"}

    static = Tool(name="echo", description="echo", params=_Args, handler=_handler)
    assert static.parse_args({"text": "hi"}).text == "hi"
    with pytest.raises(Exception):
        static.parse_args({})
