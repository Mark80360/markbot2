from __future__ import annotations

import asyncio
from typing import Any

from markbot.kernel.compaction import select_cut
from markbot.kernel.config import load_config
from markbot.kernel.providers.base import ModelReply
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.session import COMPACTION_PREFIX, Session
from markbot.schema.session_log import SessionLog


class _SummaryAwareProvider:
    name = "test"

    async def complete(self, payload: dict[str, Any]) -> ModelReply:
        system = str(payload.get("system") or "")
        if "上下文摘要助手" in system:
            return ModelReply(text="## 目标\n继续测试")
        return ModelReply(text="ok")


def test_select_cut_only_at_user_boundary():
    session = Session("u1")
    session.append_assistant("a1")
    session.append_user("u2")
    session.append_assistant("a2")
    assert select_cut(session.messages, 1) == 2
    assert select_cut(session.messages, 10_000) is None


def test_apply_compaction_replaces_prefix():
    session = Session("旧问题")
    session.append_assistant("旧回答")
    session.append_user("最近问题")
    session.apply_compaction("摘要内容", 2)

    assert session.compaction_count == 1
    assert session.messages[0].content.startswith(COMPACTION_PREFIX)
    assert "摘要内容" in session.messages[0].content
    assert session.messages[1].content == "最近问题"
    assert session.compacted_segments[0][0]["content"] == "旧问题"


def test_auto_compaction_when_budget_exceeded(make_kernel, home, project):
    cfg = load_config(home=home, project_root=project)
    cfg.agent.context_budget_chars = 2000
    kernel = make_kernel(provider=_SummaryAwareProvider(), config=cfg)

    result = None
    for index in range(4):
        result = asyncio.run(
            kernel.run(f"第{index}轮 " + "内容" * 800, archive=False)
        )
    assert result is not None
    assert result.stopped_reason == "completed"
    assert kernel.session.compaction_count >= 1
    assert kernel.session.messages[0].content.startswith(COMPACTION_PREFIX)
    assert "继续测试" in kernel.session.messages[0].content

    asyncio.run(kernel.shutdown())
    session_dir = kernel.home / "sessions" / result.session_id
    # 压缩原文在事件日志的 compaction 事件里，不再单独写 compacted.jsonl（ADR 0007）
    assert SessionLog(session_dir).compacted()
    assert not (session_dir / "compacted.jsonl").exists()


def test_scripted_provider_still_works_after_compaction(make_kernel, home, project):
    """压缩关闭时（预算足够大）不会额外调用模型。"""
    cfg = load_config(home=home, project_root=project)
    cfg.agent.context_budget_chars = 1_000_000
    provider = ScriptedProvider(["结果"])
    kernel = make_kernel(provider=provider, config=cfg)
    result = asyncio.run(kernel.run("普通请求", archive=False))
    assert result.text == "结果"
    assert kernel.session.compaction_count == 0
    assert len(provider.calls) == 1
