from __future__ import annotations

import asyncio

from markbot.kernel.session import Session
from markbot.kernel.tools import KernelToolContext

SKILL = """---
name: demo
description: Demo skill for tests
allowed-tools: []
---
STEP-BODY: do the thing
"""

MANUAL = """---
name: manual-only
description: 只能人工调用
disable-model-invocation: true
---
MANUAL-BODY
"""


def _learned(tmp_path):
    learned = tmp_path / "learned"
    (learned / "skills" / "demo").mkdir(parents=True)
    (learned / "skills" / "demo" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (learned / "skills" / "manual").mkdir(parents=True)
    (learned / "skills" / "manual" / "SKILL.md").write_text(MANUAL, encoding="utf-8")
    return learned


def test_skill_index_only(make_kernel, tmp_path):
    kernel = make_kernel(learned_dir=_learned(tmp_path))
    skills = kernel.runtime.skills
    assert set(skills.names()) == {"demo", "manual-only"}
    index = skills.index_prompt()
    assert "demo" in index
    assert "STEP-BODY" not in index
    assert "manual-only" not in index

    meta, body = skills.read_body("demo")
    assert meta.source == "learned"
    assert "STEP-BODY" in body
    assert body.startswith("STEP-BODY")


def test_skill_script_requires_declared_permission(make_kernel, tmp_path):
    kernel = make_kernel(learned_dir=_learned(tmp_path))
    tool = kernel.runtime.tools.get("run_skill_script")
    assert tool is not None
    session = Session("x", skills=kernel.runtime.skills)
    ctx = KernelToolContext(session=session, policy=kernel.policy)
    params = tool.params(skill="demo", script="run.py")
    result = asyncio.run(_call(tool.handler, ctx, params))
    assert result.is_error
    assert "run_script" in result.content


async def _call(handler, ctx, params):
    outcome = handler(ctx, params)
    if hasattr(outcome, "__await__"):
        return await outcome
    return outcome
