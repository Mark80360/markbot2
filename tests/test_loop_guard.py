from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from markbot.api import Tool, ToolResult
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider


class _Empty(BaseModel):
    pass


def test_streamed_text_survives_provider_failure(make_kernel):
    class StreamThenFail:
        name = "failing"

        async def complete(self, payload, on_delta=None):
            if on_delta is not None:
                on_delta("已经发给用户的部分")
            raise RuntimeError("upstream down")

    chunks: list[str] = []
    kernel = make_kernel(provider=StreamThenFail())
    result = asyncio.run(kernel.run("hi", on_delta=chunks.append))

    assert result.stopped_reason == "error"
    assert result.text == "已经发给用户的部分"
    assert chunks == ["已经发给用户的部分"]


def test_cancelled_tool_batch_still_pairs_results(make_kernel):
    started = asyncio.Event()

    async def slow(ctx, args: _Empty) -> ToolResult:
        started.set()
        await asyncio.sleep(30)
        return ToolResult("done")

    provider = ScriptedProvider(
        [ModelReply(tool_calls=[ToolCall(id="c1", name="slow", args={})])]
    )
    kernel = make_kernel(provider=provider)
    kernel.runtime.tools.register(
        Tool(name="slow", description="slow", params=_Empty, handler=slow),
        origin="test",
    )

    async def main() -> None:
        task = asyncio.create_task(kernel.run("go", archive=False))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    messages = kernel.session.messages
    assert [message.role for message in messages] == ["user", "assistant", "tool"]
    assert messages[1].tool_calls[0].id == "c1"
    assert messages[2].tool_call_id == "c1"
    assert messages[2].content.startswith("回合被中断")


def test_repeated_identical_calls_are_guarded(make_kernel):
    replies = [
        ModelReply(tool_calls=[ToolCall(id=f"c{index}", name="echo", args={"text": "x"})])
        for index in range(5)
    ] + ["结束"]
    kernel = make_kernel(provider=ScriptedProvider(replies))

    result = asyncio.run(kernel.run("go", archive=False))

    assert result.stopped_reason == "completed"
    results = [message for message in kernel.session.messages if message.role == "tool"]
    assert len(results) == 5
    assert sum("guard:" in message.content for message in results) == 2
    assert all(message.tool_call_id for message in results)


def test_consecutive_permission_denials_stop_turn(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall(id=f"c{index}", name="echo", args={"text": f"x{index}"})
                    for index in range(3)
                ]
            )
        ]
    )
    kernel = make_kernel(provider=provider)
    kernel.events.subscribe(
        "tool_call", "test", lambda **kwargs: {"block": True, "reason": "no"}
    )

    result = asyncio.run(kernel.run("go", archive=False))

    assert result.stopped_reason == "guarded"
    assert "连续" in (result.error or "")
    tools = [message for message in kernel.session.messages if message.role == "tool"]
    assert len(tools) == 3
    assert all(message.is_error for message in tools)
    assert all(message.tool_call_id for message in tools)
