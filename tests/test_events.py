from __future__ import annotations

import asyncio

from markbot.kernel.events import EventBus


def test_handlers_run_in_order_and_merge_data():
    bus = EventBus()
    seen: list[str] = []

    bus.subscribe("x", "a", lambda **kw: seen.append("a"))
    bus.subscribe("x", "b", lambda **kw: {"data": {"second": True}})
    bus.subscribe("x", "c", lambda **kw: seen.append("c"))

    outcome = asyncio.run(bus.emit("x", value=1))
    assert seen == ["a", "c"]
    assert outcome.data == {"second": True}
    assert not outcome.blocked
    assert bus.handler_count("x") == 3
    assert bus.handler_count() == 3


def test_handler_exception_is_isolated():
    bus = EventBus()
    called: list[str] = []

    def broken(**kwargs):
        raise RuntimeError("boom")

    bus.subscribe("x", "broken", broken)
    bus.subscribe("x", "ok", lambda **kw: called.append("ok"))

    outcome = asyncio.run(bus.emit("x"))
    assert called == ["ok"]
    assert outcome.data["errors"] == [{"owner": "broken", "error": "boom"}]


def test_block_short_circuits_later_handlers():
    bus = EventBus()
    later: list[str] = []
    bus.subscribe("x", "first", lambda **kw: {"block": True, "reason": "denied"})
    bus.subscribe("x", "second", lambda **kw: later.append("second"))

    outcome = asyncio.run(bus.emit("x"))
    assert outcome.blocked
    assert outcome.block_reason == "denied"
    assert later == []


def test_async_handler_is_awaited():
    bus = EventBus()
    seen: list[dict] = []

    async def handler(**kwargs):
        seen.append(kwargs)
        return {"data": {"async": True}}

    bus.subscribe("x", "async", handler)
    outcome = asyncio.run(bus.emit("x", value=2))
    assert seen == [{"value": 2}]
    assert outcome.data == {"async": True}


def test_unsubscribe_only_removes_its_handler():
    bus = EventBus()
    calls: list[str] = []
    off_a = bus.subscribe("x", "a", lambda **kw: calls.append("a"))
    bus.subscribe("x", "b", lambda **kw: calls.append("b"))

    off_a()
    off_a()  # 重复退订是安全的
    asyncio.run(bus.emit("x"))
    assert calls == ["b"]
    assert bus.handler_count("x") == 1


def test_emit_sync_outside_loop_runs_immediately():
    bus = EventBus()
    calls: list[str] = []
    bus.subscribe("x", "a", lambda **kw: calls.append("a"))

    outcome = bus.emit_sync("x")
    assert calls == ["a"]
    assert not outcome.blocked


def test_emit_sync_inside_loop_schedules_background_task():
    bus = EventBus()
    calls: list[str] = []

    async def handler(**kwargs):
        calls.append("a")

    bus.subscribe("x", "a", handler)

    async def main():
        outcome = bus.emit_sync("x")
        assert not outcome.blocked
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert calls == ["a"]
