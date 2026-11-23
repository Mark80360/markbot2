"""第二阶段：回合上下文（TurnContext）与读写闸门（RunGate）。"""

from __future__ import annotations

import asyncio
import threading
import time

from pydantic import BaseModel

from markbot.api.tool import Tool, ToolResult
from markbot.api.ui import NullUI
from markbot.kernel.config import PermissionConfig
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.turn import RunGate, TurnContext, bind_turn, session_scope, turn_ui


class _Empty(BaseModel):
    pass


def _exec_tool() -> Tool:
    async def handler(ctx, args):
        return ToolResult(content="ok")

    return Tool(
        name="t", description="", params=_Empty, handler=handler, capabilities=frozenset({"exec:*"})
    )


# ------------------------------------------------------------ TurnContext


def test_turn_grants_are_scoped_to_the_turn(tmp_path):
    policy = PermissionPolicy(PermissionConfig(profile="standard"), root=tmp_path)
    tool = _exec_tool()
    assert policy.check(tool, {"command": "curl -V"}).requires_confirmation
    with bind_turn(TurnContext.create(grants=["curl"])):
        assert not policy.check(tool, {"command": "curl -V"}).requires_confirmation
    assert policy.check(tool, {"command": "curl -V"}).requires_confirmation


def test_turn_grants_do_not_leak_across_threads(tmp_path):
    policy = PermissionPolicy(PermissionConfig(profile="standard"), root=tmp_path)
    tool = _exec_tool()
    seen: dict[str, bool] = {}
    inside = threading.Event()
    done = threading.Event()

    def granted() -> None:
        with bind_turn(TurnContext.create(grants=["curl"])):
            inside.set()
            done.wait(5)

    def other() -> None:
        inside.wait(5)
        seen["other"] = policy.check(tool, {"command": "curl -V"}).requires_confirmation
        done.set()

    threads = [threading.Thread(target=granted), threading.Thread(target=other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert seen == {"other": True}


def test_turn_ui_defaults_and_override():
    default = NullUI()
    override = NullUI()
    assert turn_ui(default) is default
    with bind_turn(TurnContext.create(ui=override)):
        assert turn_ui(default) is override
    assert turn_ui(default) is default


def test_kernel_run_does_not_mutate_shared_ui_or_grants(make_kernel):
    kernel = make_kernel(["ok"])
    original_ui = kernel.loop._ui
    result = asyncio.run(kernel.run("hi", ui=NullUI(), grants=["curl"], new_session=True))
    assert result.stopped_reason == "completed"
    assert kernel.loop._ui is original_ui
    assert kernel.policy._grants == set()
    assert not kernel.is_running(result.session_id)


# ------------------------------------------------------------ RunGate


def test_gate_runs_different_sessions_concurrently():
    gate = RunGate(max_concurrent=4)
    barrier = threading.Barrier(2, timeout=5)
    errors: list[BaseException] = []

    def work(sid: str) -> None:
        try:
            with gate.session(sid):
                barrier.wait()  # 两个回合必须同时在里面，否则超时
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(sid,)) for sid in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert not errors


def test_gate_serializes_same_session():
    gate = RunGate(max_concurrent=4)
    inside = 0
    peak = 0
    lock = threading.Lock()

    def work() -> None:
        nonlocal inside, peak
        with gate.session("same"):
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.05)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert peak == 1
    assert gate.stats()["sessions"] == 0  # 会话锁用完即回收


def test_gate_exclusive_waits_for_turns_and_blocks_new_ones():
    gate = RunGate(max_concurrent=4)
    order: list[str] = []
    in_turn = threading.Event()
    release_turn = threading.Event()

    def turn() -> None:
        with gate.session("s1"):
            in_turn.set()
            release_turn.wait(5)
            order.append("turn-end")

    def maintenance() -> None:
        in_turn.wait(5)
        with gate:
            order.append("maintenance")

    t1 = threading.Thread(target=turn)
    t2 = threading.Thread(target=maintenance)
    t1.start()
    t2.start()
    time.sleep(0.1)
    assert order == []  # 维护任务在等在途回合
    assert not gate.acquire(blocking=False)
    release_turn.set()
    t1.join(5)
    t2.join(5)
    assert order == ["turn-end", "maintenance"]
    assert gate.acquire(blocking=False)
    gate.release()


def test_gate_limits_total_concurrency():
    gate = RunGate(max_concurrent=2)
    inside = 0
    peak = 0
    lock = threading.Lock()

    def work(sid: str) -> None:
        nonlocal inside, peak
        with gate.session(sid):
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.05)
            with lock:
                inside -= 1

    threads = [threading.Thread(target=work, args=(f"s{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert peak <= 2


def test_session_scope_falls_back_to_plain_lock():
    lock = threading.Lock()
    with session_scope(lock, "x"):
        assert lock.locked()
    assert not lock.locked()
