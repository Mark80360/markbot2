"""第 5 轮回归：会话串扰与常驻上限、定时任务独立会话、回合内压缩、记忆索引容量、逐步流式。"""

from __future__ import annotations

import asyncio
from typing import Any

from markbot.kernel import app as app_mod
from markbot.kernel.compaction import Compactor, select_cut
from markbot.kernel.config import load_config
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.session import COMPACTION_PREFIX, Session
from markbot.memory.store import INDEX_LIMIT, MemoryStore, render_index
from markbot.schema.models import Message, ToolCallRecord

# ============================================================ 会话串扰


def test_channel_style_new_session_does_not_end_other_sessions(make_kernel):
    kernel = make_kernel(["一", "二", "三"])
    ended: list[str] = []
    kernel.events.subscribe("session_end", "test", lambda **kw: ended.append(kw["session"].id))

    first = asyncio.run(kernel.run("聊天 A", new_session=True, end_previous=False))
    second = asyncio.run(kernel.run("聊天 B", new_session=True, end_previous=False))
    assert first.session_id != second.session_id
    assert ended == []  # 旧实现：聊天 B 开新会话时会把聊天 A 结束掉

    again = asyncio.run(kernel.run("A 继续", session_id=first.session_id))
    assert again.session_id == first.session_id
    contents = [m.content for m in kernel._sessions[first.session_id].messages if m.role == "user"]
    assert contents == ["聊天 A", "A 继续"]


def test_cli_new_session_still_ends_previous(make_kernel):
    kernel = make_kernel(["一", "二"])
    ended: list[str] = []
    kernel.events.subscribe("session_end", "test", lambda **kw: ended.append(kw["session"].id))
    first = asyncio.run(kernel.run("one", archive=False))
    asyncio.run(kernel.run("two", archive=False, new_session=True))
    assert ended == [first.session_id]


def test_resident_sessions_are_bounded_and_restorable(make_kernel, monkeypatch):
    monkeypatch.setattr(app_mod, "MAX_RESIDENT_SESSIONS", 2)
    kernel = make_kernel([f"回复{i}" for i in range(6)])
    ids = [
        asyncio.run(kernel.run(f"聊天{i}", new_session=True, end_previous=False)).session_id
        for i in range(4)
    ]
    assert len(kernel._sessions) <= 2
    assert ids[0] not in kernel._sessions
    assert set(kernel._activity) <= set(kernel._sessions)

    restored = asyncio.run(kernel.run("还记得吗", session_id=ids[0]))
    assert restored.session_id == ids[0]
    users = [m.content for m in kernel._sessions[ids[0]].messages if m.role == "user"]
    assert users == ["聊天0", "还记得吗"]


def test_forget_session_clears_bookkeeping(make_kernel):
    kernel = make_kernel(["x"])
    result = asyncio.run(kernel.run("a", new_session=True, end_previous=False))
    assert kernel.forget_session(result.session_id)
    assert result.session_id not in kernel._sessions
    assert result.session_id not in kernel._activity
    assert kernel.session is None
    assert not kernel.forget_session(result.session_id)


# ============================================================ 回合内压缩


def _user(text: str) -> Message:
    return Message(role="user", content=text)


def _assistant_call(index: int) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=[ToolCallRecord(id=f"c{index}", name="fetch", arguments={})],
    )


def _tool(index: int, size: int) -> Message:
    return Message(role="tool", content="x" * size, tool_call_id=f"c{index}", name="fetch")


def _long_turn(steps: int = 8, size: int = 6000) -> list[Message]:
    marker = _user(f"{COMPACTION_PREFIX}旧摘要\n</summary>")
    messages = [marker, _user("查一下香港沙田的酒店")]
    for index in range(steps):
        messages += [_assistant_call(index), _tool(index, size)]
    return messages


def test_select_cut_skips_marker_only_prefix():
    # 旧实现在这里返回 1：只移除上一份摘要再重新摘要一遍，每步白花一次模型调用
    assert select_cut(_long_turn(), 24_000) is None


def test_select_cut_enters_long_turn_at_assistant_boundary():
    messages = _long_turn()
    cut = select_cut(messages, 24_000, 36_000)
    assert cut is not None and cut > 1
    assert messages[cut].role == "assistant"  # 工具调用与结果不被拆开


def test_select_cut_prefers_user_boundary_across_turns():
    messages = [
        _user("q1"),
        Message(role="assistant", content="r" * 6000),
        _user("q2"),
        Message(role="assistant", content="r" * 6000),
        _user("q3"),
        _assistant_call(0),
        _tool(0, 10),
    ]
    cut = select_cut(messages, 5000, 36_000)
    assert messages[cut].role == "user"


def test_compaction_inside_turn_keeps_request_verbatim():
    class Summarizer:
        name = "s"

        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, payload: dict[str, Any], on_delta=None) -> ModelReply:
            self.calls += 1
            return ModelReply(text="## 目标\n查酒店")

    session = Session("占位", append_request=False)
    session.messages = _long_turn()
    provider = Summarizer()
    outcome = asyncio.run(Compactor(provider, budget_chars=60_000).compact(session))
    assert outcome is not None and outcome.removed > 2
    assert provider.calls == 1
    marker = session.messages[0].content
    assert marker.startswith(COMPACTION_PREFIX)
    assert "当前请求原文" in marker and "查一下香港沙田的酒店" in marker
    assert session.messages[1].role == "assistant"


def test_compaction_noop_does_not_call_model():
    calls: list[int] = []

    class Counting:
        name = "c"

        async def complete(self, payload, on_delta=None):
            calls.append(1)
            return ModelReply(text="摘要")

    session = Session("占位", append_request=False)
    session.messages = _long_turn()
    compactor = Compactor(Counting(), budget_chars=60_000)
    compactor.max_keep_chars = 10**9  # 保留整个回合 => 前缀只剩锚点 => 不压缩
    assert asyncio.run(compactor.compact(session)) is None
    assert calls == []


# ============================================================ 记忆索引容量


def test_memory_index_capacity_uses_rendered_size(tmp_path):
    store = MemoryStore(tmp_path / "learned")
    written = 0
    for index in range(12):
        receipt = store.remember(
            f"主题{index} 的事实：这是一条中等长度的记忆，用于检验索引容量口径 {index}",
            target="wiki",
            topic=f"topic-{index:02d}",
        )
        assert receipt.ok, receipt.message
        written += 1
    assert written == 12  # 旧口径（index.json 字节数）在第 5~6 页就会拒绝
    index = store._read_index()
    assert len(render_index(index)) <= INDEX_LIMIT


def test_memory_index_full_message_is_actionable(tmp_path):
    store = MemoryStore(tmp_path / "learned")
    long = "非常长的摘要" * 30
    receipt = None
    for index in range(60):
        receipt = store.remember(f"{long}{index}", target="wiki", topic=f"t{index:02d}")
        if not receipt.ok:
            break
    assert receipt is not None and not receipt.ok
    assert "不要原样重试" in receipt.message
    assert "memory_forget" in receipt.message


# ============================================================ 逐步流式


def test_every_step_streams_with_separator(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(text="我先查一下", tool_calls=[ToolCall(id="c1", name="echo", args={"text": "x"})]),
            "查到了：x",
        ]
    )
    kernel = make_kernel(provider=provider)
    chunks: list[str] = []
    result = asyncio.run(kernel.run("查", archive=False, on_delta=chunks.append))
    assert result.text == "查到了：x"
    # 旧实现只流式第 1 步，最终答复不会出现在 chunks 里
    assert chunks == ["我先查一下", "\n\n", "查到了：x"]


def test_cron_config_unaffected_by_round5(home, project):
    # 防御：新的 run() 参数不应改变配置加载
    cfg = load_config(home=home, project_root=project)
    assert cfg.cron.enabled
