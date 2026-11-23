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
    (learned / "skills" / "manual-only").mkdir(parents=True)
    (learned / "skills" / "manual-only" / "SKILL.md").write_text(MANUAL, encoding="utf-8")
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


def test_skill_metadata_contract(make_kernel, tmp_path):
    learned = tmp_path / "learned"
    root = learned / "skills"
    (root / "bad-name").mkdir(parents=True)
    (root / "bad-name" / "SKILL.md").write_text(
        "---\nname: other\ndescription: 名称与目录不一致\n---\nBODY\n",
        encoding="utf-8",
    )
    (root / "long-desc").mkdir()
    (root / "long-desc" / "SKILL.md").write_text(
        "---\nname: long-desc\ndescription: " + "x" * 1025 + "\n---\nBODY\n",
        encoding="utf-8",
    )
    kernel = make_kernel(learned_dir=learned)
    skills = kernel.runtime.skills
    assert "bad-name" not in skills.names()
    assert "long-desc" not in skills.names()
    assert any("name 必须与目录名一致" in warning for warning in skills.warnings)
    assert any("description 必须是" in warning for warning in skills.warnings)


def test_learned_skill_visible_without_reload(make_kernel, tmp_path):
    learned = tmp_path / "learned"
    kernel = make_kernel(learned_dir=learned)
    skills = kernel.runtime.skills
    assert skills.get("late") is None

    target = learned / "skills" / "late"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: late\ndescription: 运行期写入的技能\n---\nLATE-BODY\n",
        encoding="utf-8",
    )

    assert skills.get("late") is not None
    assert "late" in skills.index_prompt()
    _, body = skills.read_body("late")
    assert "LATE-BODY" in body


def test_skill_body_substitutes_base_dir(make_kernel, tmp_path):
    learned = tmp_path / "learned"
    skill = learned / "skills" / "with-script"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: with-script\ndescription: 带脚本的技能\n---\n"
        "Run {baseDir}/scripts/run.sh\n",
        encoding="utf-8",
    )
    kernel = make_kernel(learned_dir=learned)
    _, body = kernel.runtime.skills.read_body("with-script")
    assert "{baseDir}" not in body
    assert str(skill.resolve()) in body


def test_skill_requires_marks_unavailable(make_kernel, tmp_path):
    learned = tmp_path / "learned"
    root = learned / "skills"
    (root / "needs-bin").mkdir(parents=True)
    (root / "needs-bin" / "SKILL.md").write_text(
        "---\n"
        "name: needs-bin\n"
        "description: 依赖不存在的命令\n"
        "requires:\n"
        "  bins: [markbot-definitely-missing-binary]\n"
        "---\nBODY\n",
        encoding="utf-8",
    )
    (root / "ready").mkdir()
    (root / "ready" / "SKILL.md").write_text(
        "---\nname: ready\ndescription: 可直接使用\n---\nBODY\n",
        encoding="utf-8",
    )
    kernel = make_kernel(learned_dir=learned)
    index = kernel.runtime.skills.index_prompt()
    available_part = index.split("不可用技能")[0]
    assert "ready" in available_part
    assert "needs-bin" not in available_part
    assert "不可用技能" in index and "needs-bin" in index
