from __future__ import annotations

import asyncio

import httpx
import pytest

from markbot.kernel.config import Config
from markbot.kernel.providers import build_provider
from markbot.kernel.providers.anthropic import AnthropicProvider, _messages, _parse
from markbot.kernel.providers.base import ContentRejectedError, ProviderError
from markbot.kernel.providers.echo import EchoProvider
from markbot.kernel.providers.openai_compat import OpenAICompatProvider


def test_anthropic_messages_convert_tool_calls_and_results():
    messages = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [
                {"id": "c1", "name": "echo", "arguments": {"text": "x"}},
                {"id": "c2", "name": "echo", "arguments": {}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "x"},
        {"role": "tool", "tool_call_id": "c2", "content": "empty", "is_error": True},
    ]
    out = _messages(messages)
    assert out[0] == {"role": "user", "content": "hi"}
    assert out[1]["role"] == "assistant"
    assert out[1]["content"][0] == {"type": "text", "text": "thinking"}
    assert out[1]["content"][1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "echo",
        "input": {"text": "x"},
    }
    # 连续的工具结果合并到同一个 user 消息里
    assert out[2]["role"] == "user"
    assert [block["type"] for block in out[2]["content"]] == ["tool_result", "tool_result"]
    assert out[2]["content"][1]["is_error"] is True


def test_anthropic_parse_text_tools_and_usage():
    reply = _parse(
        {
            "content": [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
                {"type": "tool_use", "id": "c1", "name": "echo", "input": {"text": "x"}},
            ],
            "usage": {"input_tokens": 3, "output_tokens": 4, "nested": {"x": 1}},
        }
    )
    assert reply.text == "hello world"
    assert reply.tool_calls[0].id == "c1"
    assert reply.tool_calls[0].args == {"text": "x"}
    assert reply.usage == {"input_tokens": 3, "output_tokens": 4}


def test_anthropic_complete_builds_request(monkeypatch):
    captured: dict = {}

    async def fake_post(self, url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "pong"},
                    {"type": "tool_use", "id": "c1", "name": "echo", "input": {"text": "x"}},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = AnthropicProvider(
        api_key="key",
        model="claude-test",
        base_url="https://example.test/",
        max_tokens=128,
        retry_backoff_s=0,
    )

    reply = asyncio.run(
        provider.complete(
            {
                "system": "sys",
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "ping"}],
                "tools": [{"name": "echo", "description": "d", "input_schema": {"type": "object"}}],
            }
        )
    )

    assert captured["url"] == "https://example.test/v1/messages"
    assert captured["json"]["model"] == "claude-test"
    assert captured["json"]["max_tokens"] == 8
    assert captured["json"]["system"] == "sys"
    assert captured["json"]["tools"][0]["name"] == "echo"
    assert captured["headers"]["x-api-key"] == "key"
    assert reply.text == "pong"
    assert reply.tool_calls[0].name == "echo"


def test_anthropic_retries_server_errors_then_succeeds(monkeypatch):
    calls = {"n": 0}

    async def fake_post(self, url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = AnthropicProvider(
        api_key="key", model="m", max_retries=1, retry_backoff_s=0
    )
    reply = asyncio.run(provider.complete({"messages": []}))
    assert reply.text == "ok"
    assert calls["n"] == 2


def test_anthropic_moderation_rejection_is_not_retried(monkeypatch):
    calls = {"n": 0}

    async def fake_post(self, url, **kwargs):
        calls["n"] += 1
        return httpx.Response(
            400,
            text='{"error":{"message":"Inappropriate input rejected","code":"18"}}',
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = AnthropicProvider(
        api_key="key", model="m", max_retries=3, retry_backoff_s=0
    )
    with pytest.raises(ContentRejectedError):
        asyncio.run(provider.complete({"messages": []}))
    assert calls["n"] == 1


def test_anthropic_client_error_raises_provider_error(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(401, text="bad key")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = AnthropicProvider(api_key="key", model="m", max_retries=0)
    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(provider.complete({"messages": []}))
    assert "401" in str(excinfo.value)


def test_build_provider_selects_implementation():
    cfg = Config()
    cfg.agent.model = "claude-sonnet"
    cfg.agent.api_key = "k"
    assert isinstance(build_provider(cfg), AnthropicProvider)

    cfg = Config()
    cfg.agent.provider = "openai_compat"
    cfg.agent.model = "m"
    cfg.secrets = {"OPENAI_API_KEY": "k"}
    provider = build_provider(cfg)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.api_key == "k"

    cfg = Config()
    cfg.agent.provider = "echo"
    assert isinstance(build_provider(cfg), EchoProvider)

    cfg = Config()
    cfg.agent.provider = "openai_compat"
    with pytest.raises(ProviderError):
        build_provider(cfg)

    cfg = Config()
    cfg.agent.provider = "unknown"
    with pytest.raises(ProviderError):
        build_provider(cfg)
