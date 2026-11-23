from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from markbot.api.ui import NullUI
from markbot.extensions import memory
from markbot.kernel.config import PermissionConfig
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import SourceSet
from markbot.kernel.session import Session
from markbot.kernel.tools import KernelToolContext


class YesUI(NullUI):
    async def confirm(self, title: str, message: str) -> bool:
        return True


def _tools():
    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("memory", source=source, ui=NullUI())
    memory.setup(api)
    api.commit()
    return {tool.name: tool for tool in source.tools}


def _ctx(tmp_path: Path, *, learned: Path | None = None, ui=None) -> KernelToolContext:
    runtime = SimpleNamespace(learned_dir=learned)
    return KernelToolContext(
        session=Session("记忆测试", runtime=runtime if learned else None),
        policy=PermissionPolicy(PermissionConfig(profile="trusted"), root=tmp_path),
        ui=ui,
    )


def _call(tool, ctx, **kwargs):
    return asyncio.run(tool.handler(ctx, tool.params(**kwargs)))


def test_memory_tools_require_learned_dir(tmp_path):
    tools = _tools()
    ctx = _ctx(tmp_path)
    calls = {
        "memory_remember": {"text": "x"},
        "memory_search": {"query": "x"},
        "memory_forget": {"text": "x"},
        "memory_status": {},
    }
    for name, kwargs in calls.items():
        result = _call(tools[name], ctx, **kwargs)
        assert result.is_error
        assert "没有长期记忆目录" in result.content


def test_memory_off_blocks_reads_and_writes(tmp_path):
    learned = tmp_path / "learned"
    learned.mkdir()
    tools = _tools()
    ctx = _ctx(tmp_path, learned=learned)
    ctx.session.metadata["memory"] = "off"

    assert _call(tools["memory_remember"], ctx, text="x").is_error
    assert _call(tools["memory_search"], ctx, query="x").is_error
    assert _call(tools["memory_forget"], ctx, text="x").is_error


def test_remember_search_status_and_duplicates(tmp_path):
    learned = tmp_path / "learned"
    learned.mkdir()
    tools = _tools()
    ctx = _ctx(tmp_path, learned=learned)

    written = _call(tools["memory_remember"], ctx, text="用户喜欢简洁回答")
    assert not written.is_error
    assert "已写入" in written.content
    assert (learned / written.details["path"]).exists()

    duplicate = _call(tools["memory_remember"], ctx, text="用户喜欢简洁回答")
    assert not duplicate.is_error
    assert "无变化" in duplicate.content

    wiki = _call(
        tools["memory_remember"], ctx, text="部署使用蓝绿发布", target="wiki", topic="发布"
    )
    assert not wiki.is_error
    assert (learned / wiki.details["path"]).exists()

    skill = _call(
        tools["memory_remember"],
        ctx,
        text="部署流程",
        target="skill",
        topic="deploy",
        steps=["构建", "发布"],
    )
    assert not skill.is_error

    found = _call(tools["memory_search"], ctx, query="简洁")
    assert "用户喜欢简洁回答" in found.content
    assert found.details["hits"][0]["source"] == "memory"

    empty = _call(tools["memory_search"], ctx, query="不存在的关键词")
    assert "未找到匹配" in empty.content

    status = _call(tools["memory_status"], ctx)
    assert "常驻记忆" in status.content
    assert status.details["resident"]["chars"] > 0


def test_remember_skill_requires_topic_and_steps(tmp_path):
    learned = tmp_path / "learned"
    learned.mkdir()
    tools = _tools()
    ctx = _ctx(tmp_path, learned=learned)

    no_topic = _call(tools["memory_remember"], ctx, text="x", target="skill")
    assert no_topic.is_error
    assert "需要 topic" in no_topic.content

    no_steps = _call(tools["memory_remember"], ctx, text="x", target="skill", topic="demo")
    assert no_steps.is_error
    assert "需要 steps" in no_steps.content


def test_replacing_memory_requires_confirmation(tmp_path):
    learned = tmp_path / "learned"
    learned.mkdir()
    tools = _tools()
    deny_ctx = _ctx(tmp_path, learned=learned)
    _call(tools["memory_remember"], deny_ctx, text="旧偏好")

    denied = _call(
        tools["memory_remember"], deny_ctx, text="新偏好", replaces="旧偏好"
    )
    assert not denied.is_error
    assert "未确认" in denied.content
    assert denied.details["verified"] is False
    assert denied.details["written"] == []

    yes_ctx = _ctx(tmp_path, learned=learned, ui=YesUI())
    replaced = _call(
        tools["memory_remember"], yes_ctx, text="新偏好", replaces="旧偏好"
    )
    assert not replaced.is_error
    assert "替换 1 条旧内容" in replaced.content


def test_forget_memory_and_wiki_page(tmp_path):
    learned = tmp_path / "learned"
    learned.mkdir()
    tools = _tools()
    ctx = _ctx(tmp_path, learned=learned)

    _call(tools["memory_remember"], ctx, text="临时偏好")
    forgotten = _call(tools["memory_forget"], ctx, text="临时偏好")
    assert not forgotten.is_error
    assert "已从" in forgotten.content

    missing = _call(tools["memory_forget"], ctx, text="临时偏好")
    assert "未找到" in missing.content

    _call(tools["memory_remember"], ctx, text="知识", target="wiki", topic="部署")
    denied = _call(tools["memory_forget"], ctx, topic="部署")
    assert denied.is_error
    assert "用户未确认" in denied.content

    yes_ctx = _ctx(tmp_path, learned=learned, ui=YesUI())
    removed = _call(tools["memory_forget"], yes_ctx, topic="部署")
    assert not removed.is_error
    assert "已从" in removed.content
