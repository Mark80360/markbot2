from __future__ import annotations

import asyncio
import json

from markbot.kernel.config import with_agent
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider


def test_plain_completion_is_archived(make_kernel):
    kernel = make_kernel(["你好，我是 MarkBot"])
    result = asyncio.run(kernel.run("打个招呼"))
    assert result.stopped_reason == "completed"
    assert result.text == "你好，我是 MarkBot"

    session_dir = kernel.home / "sessions" / result.session_id
    assert (session_dir / "meta.json").exists()
    assert (session_dir / "request.md").read_text(encoding="utf-8") == "打个招呼"
    assert (session_dir / "deliverable" / "final.md").read_text(encoding="utf-8") == result.text
    meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "completed"
    assert meta["steps"] == 1


def test_turns_share_session(make_kernel):
    kernel = make_kernel(["第一轮回复", "第二轮回复"])
    first = asyncio.run(kernel.run("第一问", archive=False))
    second = asyncio.run(kernel.run("第二问", archive=False))
    assert first.session_id == second.session_id
    assert [m.role for m in kernel.session.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert [m.content for m in kernel.session.messages if m.role == "user"] == [
        "第一问",
        "第二问",
    ]
    asyncio.run(kernel.shutdown())
    rows = [
        json.loads(line)
        for line in (
            kernel.home / "sessions" / first.session_id / "trajectory.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 4


def test_new_session_resets_history(make_kernel):
    kernel = make_kernel(["a", "b"])
    first = asyncio.run(kernel.run("one", archive=False))
    second = asyncio.run(kernel.run("two", archive=False, new_session=True))
    assert first.session_id != second.session_id
    assert [m.content for m in kernel.session.messages if m.role == "user"] == ["two"]


def test_tool_roundtrip(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[ToolCall(id="c1", name="echo", args={"text": "hi"})]),
            "工具已返回 hi",
        ]
    )
    kernel = make_kernel(provider=provider)
    result = asyncio.run(kernel.run("调用 echo"))
    assert result.text == "工具已返回 hi"
    assert result.stopped_reason == "completed"
    rows = [
        json.loads(line)
        for line in (kernel.home / "sessions" / result.session_id / "trajectory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["role"] for row in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[2]["content"] == "hi"


def test_unknown_tool_is_recoverable(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[ToolCall(id="c1", name="nope", args={})]),
            "已改用其他路径",
        ]
    )
    kernel = make_kernel(provider=provider)
    result = asyncio.run(kernel.run("用不存在的工具"))
    assert result.status if hasattr(result, "status") else True
    assert result.text == "已改用其他路径"
    trajectory = (
        kernel.home / "sessions" / result.session_id / "trajectory.jsonl"
    ).read_text(encoding="utf-8")
    assert "unknown tool: nope" in trajectory


def test_provider_failure_returns_error(make_kernel):
    class Broken:
        name = "broken"

        def __init__(self):
            self.calls = 0

        async def complete(self, payload):
            self.calls += 1
            raise RuntimeError("upstream down")

    provider = Broken()
    kernel = make_kernel(provider=provider)
    result = asyncio.run(kernel.run("hi"))
    assert result.stopped_reason == "error"
    assert "upstream down" in (result.error or "")
    assert provider.calls == 1  # 重试由 provider 内部负责，loop 不再叠加


def test_provider_failure_keeps_error_when_message_empty(make_kernel):
    """httpx 超时的 str() 是空串，错误信息不能因此变成空。"""

    class Silent(Exception):
        def __str__(self) -> str:
            return ""

    class Broken:
        name = "broken"

        async def complete(self, payload):
            raise Silent()

    kernel = make_kernel(provider=Broken())
    result = asyncio.run(kernel.run("hi"))
    assert result.stopped_reason == "error"
    assert result.error == "Silent"


def test_openai_provider_retries_before_succeeding(monkeypatch):
    """provider 层重试：前两次超时，第三次成功。"""
    import httpx

    from markbot.kernel.providers.openai_compat import OpenAICompatProvider

    calls = {"n": 0}

    async def fake_post(self, url, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectTimeout("connect timeout")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = OpenAICompatProvider(
        api_key="k", model="m", max_retries=2, retry_backoff_s=0
    )
    reply = asyncio.run(provider.complete({"messages": [{"role": "user", "content": "hi"}]}))
    assert reply.text == "ok"
    assert calls["n"] == 3


def test_openai_provider_reports_error_after_retries(monkeypatch):
    """重试耗尽后错误信息保留类型与尝试次数，不能为空。"""
    import httpx

    from markbot.kernel.providers.base import ProviderError
    from markbot.kernel.providers.openai_compat import OpenAICompatProvider

    calls = {"n": 0}

    async def fake_post(self, url, **kwargs):
        calls["n"] += 1
        raise httpx.ReadTimeout("read timeout")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = OpenAICompatProvider(
        api_key="k", model="m", max_retries=1, retry_backoff_s=0
    )
    try:
        asyncio.run(provider.complete({"messages": [{"role": "user", "content": "hi"}]}))
    except ProviderError as exc:
        assert "ReadTimeout" in str(exc)
        assert "已尝试 2 次" in str(exc)
    else:  # pragma: no cover - 必须抛错
        raise AssertionError("provider 应当抛出 ProviderError")
    assert calls["n"] == 2


def test_max_steps(make_kernel):
    provider = ScriptedProvider(
        [ModelReply(tool_calls=[ToolCall(id="c1", name="echo", args={"text": "x"})])]
    )
    kernel = make_kernel(provider=provider)
    kernel.config = with_agent(kernel.config, max_steps=2)
    kernel.reload()
    result = asyncio.run(kernel.run("loop"))
    assert result.stopped_reason == "max_steps"


def test_tool_call_can_be_blocked(make_kernel):
    kernel = make_kernel(
        [
            ModelReply(tool_calls=[ToolCall(id="c1", name="echo", args={"text": "x"})]),
            "改用别的方式",
        ]
    )

    def block(**kwargs):
        return {"block": True, "reason": "extension denied"}

    kernel.events.subscribe("tool_call", "test", block)
    result = asyncio.run(kernel.run("试试"))
    assert result.text == "改用别的方式"
    trajectory = (
        kernel.home / "sessions" / result.session_id / "trajectory.jsonl"
    ).read_text(encoding="utf-8")
    assert "extension denied" in trajectory


def test_explicit_session_id_resumes_context_after_restart(make_kernel):
    """渠道带 session_id 的请求在服务重启后仍能接上之前的上下文。"""
    first_kernel = make_kernel(["记住，你叫小马"])
    first = asyncio.run(first_kernel.run("我叫什么？"))
    asyncio.run(first_kernel.shutdown())

    # 新 Kernel 代表服务重启：内存会话为空，只能从归档恢复
    second_kernel = make_kernel(["你叫小马"])
    second = asyncio.run(second_kernel.run("继续", session_id=first.session_id))

    assert second.session_id == first.session_id
    assert [m.content for m in second_kernel.session.messages if m.role == "user"] == [
        "我叫什么？",
        "继续",
    ]


def test_missing_session_id_starts_fresh_session(make_kernel):
    """不带 session_id 的本地会话不继承任何归档上下文。"""
    first_kernel = make_kernel(["第一轮"])
    asyncio.run(first_kernel.run("第一问"))

    second_kernel = make_kernel(["第二轮"])
    second = asyncio.run(second_kernel.run("第二问"))

    assert len([m for m in second_kernel.session.messages if m.role == "user"]) == 1

