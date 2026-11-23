from __future__ import annotations

import asyncio
import json

import pytest

from markbot.api.ui import NullUI
from markbot.kernel.app import guess_domain
from markbot.kernel.providers.echo import ScriptedProvider

SKILL = """---
name: demo
description: Demo skill
allowed-tools: []
---
STEP-BODY
"""


def _learned(tmp_path):
    learned = tmp_path / "learned"
    (learned / "skills" / "demo").mkdir(parents=True)
    (learned / "skills" / "demo" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return learned


def test_blocked_input_ends_turn_without_session(make_kernel):
    kernel = make_kernel(["unused"])
    kernel.events.subscribe("input", "test", lambda **kw: {"block": True, "reason": "no"})

    result = asyncio.run(kernel.run("hi"))
    assert result.stopped_reason == "blocked"
    assert result.error == "no"
    assert kernel.session is None


def test_run_text_returns_final_text(make_kernel):
    kernel = make_kernel(["最终答案"])
    assert asyncio.run(kernel.run_text("问题")) == "最终答案"


def test_run_skill_injects_body_and_arguments(make_kernel, tmp_path):
    provider = ScriptedProvider(["已执行"])
    kernel = make_kernel(provider=provider, learned_dir=_learned(tmp_path))

    result = asyncio.run(kernel.run_skill("demo", "额外要求"))

    assert result.stopped_reason == "completed"
    assert result.text == "已执行"
    prompt = json.dumps(provider.calls[0]["messages"], ensure_ascii=False)
    assert "STEP-BODY" in prompt
    assert "用户补充：额外要求" in prompt


def test_run_skill_unknown_name_does_not_call_provider(make_kernel, tmp_path):
    provider = ScriptedProvider(["不应该出现"])
    kernel = make_kernel(provider=provider, learned_dir=_learned(tmp_path))

    result = asyncio.run(kernel.run_skill("missing"))

    assert result.stopped_reason == "error"
    assert "未知技能" in (result.error or "")
    assert provider.calls == []


def test_explicit_skill_from_channel_is_injected_for_one_turn(make_kernel, tmp_path):
    provider = ScriptedProvider(["已执行"])
    kernel = make_kernel(provider=provider, learned_dir=_learned(tmp_path))

    result = asyncio.run(kernel.run("$demo 帮我执行", archive=False))

    assert result.stopped_reason == "completed"
    system = provider.calls[0]["system"]
    assert "STEP-BODY" in system
    assert "用户显式指定的技能" in system
    assert "_active_skills" not in kernel.session.metadata
    assert "STEP-BODY" not in json.dumps(
        kernel.session.messages_for_model(), ensure_ascii=False
    )


def test_run_metadata_is_visible_during_turn_then_restored(make_kernel):
    kernel = make_kernel(["ok"])
    seen: list[dict] = []
    kernel.events.subscribe(
        "turn_start", "test", lambda session=None, **kw: seen.append(dict(session.metadata))
    )

    asyncio.run(
        kernel.run(
            "hi",
            archive=False,
            metadata={"cron": {"job_id": "job-1"}},
        )
    )

    assert seen[0]["cron"] == {"job_id": "job-1"}
    assert "cron" not in kernel.session.metadata


def test_existing_metadata_is_restored_after_turn(make_kernel):
    kernel = make_kernel(["ok"])
    asyncio.run(kernel.run("first", archive=False))
    kernel.session.metadata["keep"] = "old"

    asyncio.run(kernel.run("second", archive=False, metadata={"keep": "new"}))
    assert kernel.session.metadata["keep"] == "old"


def test_end_session_without_session_and_archives(make_kernel):
    kernel = make_kernel(["ok"])

    assert asyncio.run(kernel.end_session()) is None
    asyncio.run(kernel.run("hi", archive=False))
    assert asyncio.run(kernel.end_session(archive=False)) is None
    assert kernel.session is None

    asyncio.run(kernel.run("again", archive=False))
    path = asyncio.run(kernel.end_session())
    assert path is not None and path.exists()


def test_set_ui_returns_previous_and_updates_loop(make_kernel):
    kernel = make_kernel(["ok"])
    original = kernel.ui

    previous = kernel.set_ui(NullUI())
    assert previous is original
    assert kernel.ui is kernel.loop._ui

    kernel.set_ui(original)
    assert kernel.ui is original


def test_reload_rebuilds_runtime(make_kernel):
    kernel = make_kernel(["ok"])
    before = kernel.runtime
    after = kernel.reload()
    assert after is kernel.runtime
    assert after is not before
    assert "echo" in after.tools
    assert kernel.summary()["tools"] == len(after.tools)


@pytest.mark.parametrize(
    ("text", "domain"),
    [
        ("帮我处理 xlsx 表格", "office"),
        ("检查这个 git 仓库", "code"),
        ("抓取网页 http://x", "web"),
        ("提取 pdf 内容", "pdf"),
        ("随便聊聊", "general"),
    ],
)
def test_guess_domain(text, domain):
    assert guess_domain(text) == domain
