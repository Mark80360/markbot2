"""共用传输层（providers/http.py）：流式累积、重试与「已输出不重放」。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from markbot.kernel.providers.anthropic import AnthropicProvider, _AnthropicStream
from markbot.kernel.providers.base import ContentRejectedError, ProviderError
from markbot.kernel.providers.http import int_items, sse_event
from markbot.kernel.providers.openai_compat import OpenAICompatProvider, _OpenAIStream


def test_sse_event_parsing():
    assert sse_event('data: {"a": 1}') == {"a": 1}
    assert sse_event("data: [DONE]") is None
    assert sse_event("event: ping") is None
    assert sse_event("data: not-json") is None
    assert sse_event("data: [1, 2]") is None


def test_int_items_filters_non_ints():
    assert int_items({"a": 1, "b": "x", "c": {"d": 1}, "e": True}) == {"a": 1}
    assert int_items(None) == {}


def test_openai_accumulator_joins_text_and_tool_calls():
    acc = _OpenAIStream()
    assert acc.result() is None
    assert acc.feed({"choices": [{"delta": {"content": "he"}}]}) == "he"
    assert acc.feed({"choices": [{"delta": {"content": "llo"}}]}) == "llo"
    acc.feed(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "c1", "function": {"name": "ec", "arguments": '{"t'}}
                        ]
                    }
                }
            ]
        }
    )
    acc.feed(
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "ho",
                                                                        "arguments": '":1}'}}]}}]}
    )
    acc.feed({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}})
    reply = acc.result()
    assert reply.text == "hello"
    assert reply.tool_calls[0].name == "echo" and reply.tool_calls[0].args == {"t": 1}
    assert reply.usage == {"prompt_tokens": 3, "completion_tokens": 4}
    with pytest.raises(ProviderError):
        acc.feed({"error": {"message": "boom"}})


def test_anthropic_accumulator_handles_blocks_and_usage():
    acc = _AnthropicStream()
    acc.feed({"type": "message_start", "message": {"usage": {"input_tokens": 5}}})
    assert acc.feed({"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": "hi"}}) == "hi"
    acc.feed({"type": "content_block_start", "index": 1,
              "content_block": {"type": "tool_use", "id": "c1", "name": "echo"}})
    acc.feed({"type": "content_block_delta", "index": 1,
              "delta": {"type": "input_json_delta", "partial_json": '{"x":'}})
    acc.feed({"type": "content_block_delta", "index": 1,
              "delta": {"type": "input_json_delta", "partial_json": "2}"}})
    acc.feed({"type": "message_delta", "usage": {"output_tokens": 7}})
    reply = acc.result()
    assert reply.text == "hi"
    assert reply.tool_calls[0].args == {"x": 2}
    assert reply.usage == {"input_tokens": 5, "output_tokens": 7}
    with pytest.raises(ProviderError):
        acc.feed({"type": "error", "error": {"message": "overloaded"}})


def _sse(*events: str) -> bytes:
    return "".join(f"data: {e}\n\n" for e in events).encode()


def _mock(monkeypatch, handler):
    real = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)


def test_openai_stream_retries_5xx_then_streams(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(
            200,
            content=_sse('{"choices":[{"delta":{"content":"ok"}}]}', "[DONE]"),
        )

    _mock(monkeypatch, handler)
    provider = OpenAICompatProvider(api_key="k", model="m", max_retries=1, retry_backoff_s=0)
    seen: list[str] = []
    reply = asyncio.run(provider.complete({"messages": []}, on_delta=seen.append))
    assert reply.text == "ok" and seen == ["ok"] and calls["n"] == 2


def test_stream_moderation_rejection_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text='{"error":{"message":"content policy"}}')

    _mock(monkeypatch, handler)
    provider = AnthropicProvider(api_key="k", model="m", max_retries=3, retry_backoff_s=0)
    with pytest.raises(ContentRejectedError):
        asyncio.run(provider.complete({"messages": []}, on_delta=lambda _t: None))
    assert calls["n"] == 1


def test_empty_stream_is_retried_then_fails(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, content=_sse("[DONE]"))

    _mock(monkeypatch, handler)
    provider = OpenAICompatProvider(api_key="k", model="m", max_retries=2, retry_backoff_s=0)
    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(provider.complete({"messages": []}, on_delta=lambda _t: None))
    assert calls["n"] == 3
    assert "模型返回为空" in str(excinfo.value)


def test_openai_non_stream_unparseable_is_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "fine"}}]})

    _mock(monkeypatch, handler)
    provider = OpenAICompatProvider(api_key="k", model="m", max_retries=1, retry_backoff_s=0)
    assert asyncio.run(provider.complete({"messages": []})).text == "fine"
    assert calls["n"] == 2
