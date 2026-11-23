"""学习层与运行时数据的保护：文件工具、执行环境、命令扫描与技能索引的一致性。

对应 2026-11-21 服务器日志：模型被拒绝写 ~/.markbot/workspace 后改用 run_command，
把技能直接写进 learned/，绕过了 memory 工具与优化器门禁。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from markbot.api.ui import NullUI
from markbot.extensions import fs, skills
from markbot.kernel.config import PermissionConfig
from markbot.kernel.execution import LocalExecution
from markbot.kernel.paths import PathGuard
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import SkillIndex, SourceSet
from markbot.kernel.session import Session
from markbot.kernel.tools import KernelToolContext


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "mbhome"
    project = tmp_path / "project"
    for path in (
        home / "learned" / "skills" / "demo",
        home / "workspace",
        home / "media" / "feishu",
        home / "sessions" / "sess_1" / "files",
        project / "src",
        project / ".markbot",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("[agent]\n", encoding="utf-8")
    (home / "learned" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    return home.resolve(), project.resolve()


def _policy(layout, profile="trusted", **kwargs) -> PermissionPolicy:
    home, project = layout
    return PermissionPolicy(PermissionConfig(profile=profile, **kwargs), root=project, home=home)


class _Tool:
    def __init__(self, *caps: str) -> None:
        self.capabilities = frozenset(caps)


READ = _Tool("fs.read:./**")
WRITE = _Tool("fs.write:./**")
NARROW_WRITE = _Tool("fs.write:./output/**")
EXEC = _Tool("exec:*")


# ------------------------------------------------------------ 文件路径


def test_home_is_protected_but_workspace_and_media_are_open(layout):
    home, _ = layout
    policy = _policy(layout)
    assert not policy.check(WRITE, {"path": str(home / "learned/skills/demo/SKILL.md")}).allowed
    denied = policy.check(READ, {"path": str(home / "config.toml")})
    assert not denied.allowed and denied.reason.startswith("受保护路径")
    assert policy.check(WRITE, {"path": str(home / "workspace/ps5/a.py")}).allowed
    assert policy.check(READ, {"path": str(home / "media/feishu/a.png")}).allowed
    assert not policy.check(WRITE, {"path": str(home / "media/feishu/a.png")}).allowed
    assert policy.check(READ, {"path": str(home / "sessions/sess_1/files/tool-1.txt")}).allowed
    assert not policy.check(READ, {"path": str(home / "sessions/sess_1/trajectory.jsonl")}).allowed


def test_project_markbot_dir_is_protected(layout):
    policy = _policy(layout)
    assert not policy.check(WRITE, {"path": ".markbot/trust.json"}).allowed
    assert policy.check(WRITE, {"path": "src/a.py"}).allowed


def test_symlink_into_protected_dir_is_denied(layout):
    home, project = layout
    (project / "link").symlink_to(home / "learned")
    assert not _policy(layout).check(READ, {"path": "link/skills/demo/SKILL.md"}).allowed


def test_narrow_capability_does_not_extend_to_workspace(layout):
    home, _ = layout
    assert not _policy(layout).check(NARROW_WRITE, {"path": str(home / "workspace/a")}).allowed


def test_extra_dirs_from_config(layout, tmp_path):
    scratch = tmp_path / "scratch"
    old = tmp_path / "old_home"
    scratch.mkdir()
    old.mkdir()
    policy = _policy(
        layout, extra_writable_dirs=[str(scratch)], extra_protected_dirs=[str(old)]
    )
    assert policy.check(WRITE, {"path": str(scratch / "a.txt")}).allowed
    assert not policy.check(EXEC, {"command": f"cat {old}/config.json"}).allowed


def test_local_execution_uses_guard(layout):
    home, project = layout
    guard = PathGuard.build(project, home=home)
    env = LocalExecution(project, guard=guard)
    path = env.write_text(str(home / "workspace" / "a.txt"), "x")
    assert path.read_text(encoding="utf-8") == "x"
    with pytest.raises(PermissionError):
        env.write_text(str(home / "learned" / "x.md"), "x")
    with pytest.raises(PermissionError):
        env.read_text(str(home / "config.toml"))
    # 只读例外目录不可写
    with pytest.raises(PermissionError):
        env.write_text(str(home / "media" / "a.png"), "x")


def test_fs_tool_can_write_workspace(layout):
    home, project = layout
    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI())
    fs.setup(api)
    api.commit()
    write_file = {tool.name: tool for tool in source.tools}["write_file"]
    policy = _policy(layout)
    ctx = KernelToolContext(session=Session("t"), policy=policy)
    target = str(home / "workspace" / "hk" / "crawl.py")
    assert policy.check(write_file, {"path": target}).allowed
    result = asyncio.run(write_file.handler(ctx, write_file.params(path=target, content="print(1)")))
    assert not result.is_error
    assert (home / "workspace" / "hk" / "crawl.py").exists()


# ------------------------------------------------------------ 命令扫描


@pytest.mark.parametrize(
    "command",
    [
        "cat {home}/config.toml",
        "cp a.md {home}/learned/skills/demo/SKILL.md",
        "cat > {home}/learned/skills/hk/SKILL.md << 'EOF'\nx\nEOF",
        "ls {home}",
        "cat {home}/lea*/skills/demo/SKILL.md",
        "cat $MARKBOT_HOME/config.toml",
        "cd .. && cat mbhome/learned/x",
        "python3 x.py --out={home}/learned/a",
        "sed -i s/a/b/ {home}/trust.json",
    ],
)
def test_command_touching_protected_paths_is_denied(layout, command, monkeypatch):
    home, _ = layout
    monkeypatch.setenv("MARKBOT_HOME", str(home))
    decision = _policy(layout).check(EXEC, {"command": command.format(home=home)})
    assert not decision.allowed
    assert "受保护路径" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p {home}/workspace/hk && cat > {home}/workspace/hk/a.py << 'EOF'\nprint(1)\nEOF",
        "TMPZIP={home}/workspace/a.zip; curl -o $TMPZIP http://example.com/a",
        "python3 {home}/workspace/ps5/a.py",
        "ls {home}/media/feishu",
        "find {home}/sessions/sess_1/files -type f",
        "curl -s http://192.168.71.90:11235/health",
        "sed 's/=.*/=<redacted>/' a.txt",
        "grep -rn '.*foo' src | head",
        "git status",
    ],
)
def test_ordinary_commands_are_not_blocked(layout, command):
    home, _ = layout
    assert _policy(layout).check(EXEC, {"command": command.format(home=home)}).allowed


def test_command_scan_applies_even_in_trusted_profile(layout):
    home, _ = layout
    decision = _policy(layout, "trusted").check(EXEC, {"command": f"cat {home}/config.toml"})
    assert not decision.allowed


# ------------------------------------------------------------ 技能索引


def test_read_skill_explains_skipped_skill(tmp_path):
    root = tmp_path / "skills"
    (root / "bad").mkdir(parents=True)
    (root / "bad" / "SKILL.md").write_text("---\nname: other\ndescription: d\n---\n", "utf-8")
    index = SkillIndex()
    index.add_root(root, origin="learned", priority=10)

    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI())
    skills.setup(api)
    api.commit()
    read_skill = {tool.name: tool for tool in source.tools}["read_skill"]
    ctx = KernelToolContext(
        session=Session("t", skills=index),
        policy=PermissionPolicy(PermissionConfig(), root=tmp_path),
    )
    result = asyncio.run(read_skill.handler(ctx, read_skill.params(name="bad")))
    assert result.is_error
    assert "存在但未进入索引" in result.content
    missing = asyncio.run(read_skill.handler(ctx, read_skill.params(name="nope")))
    assert "当前可用技能" in missing.content


def test_session_follows_runtime_after_reload(make_kernel):
    kernel = make_kernel(["一", "二"])
    asyncio.run(kernel.run("第一问", archive=False))
    session = kernel.session
    kernel.reload()
    asyncio.run(kernel.run("第二问", archive=False))
    assert kernel.session is session
    assert session.skills is kernel.runtime.skills
    assert session.runtime is kernel.runtime


def test_memory_written_skill_is_visible_next_turn(make_kernel, home):
    """memory 工具写入技能后，同一会话下一轮 read_skill 必须能读到。"""
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="memory_remember",
                        args={
                            "target": "skill",
                            "topic": "hk-map-lookup",
                            "text": "查香港地点的标准流程：先验存在性与性质，再规划路线",
                            "steps": ["核验存在性", "规划路线"],
                        },
                    )
                ]
            ),
            "已保存",
            ModelReply(tool_calls=[ToolCall(id="c2", name="read_skill", args={"name": "hk-map-lookup"})]),
            "读到了",
        ]
    )
    kernel = make_kernel(provider=provider)
    asyncio.run(kernel.run("保存技能", archive=False))
    kernel.reload()  # 模拟后台监视器检测到 learned/ 变化后的重载
    asyncio.run(kernel.run("读取技能", archive=False))
    results = [m for m in kernel.session.messages if m.role == "tool"]
    assert results[-1].name == "read_skill"
    assert not results[-1].is_error, results[-1].content


# ------------------------------------------------------------ 监视器


def test_watcher_ignores_hot_reloaded_learned_paths(make_kernel, home):
    from markbot.cli.main import _runtime_fingerprint

    kernel = make_kernel()
    learned = home / "learned"
    (learned / "strategy").mkdir(parents=True, exist_ok=True)
    before = _runtime_fingerprint(kernel)

    (learned / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    (learned / "skills" / "demo" / "SKILL.md").write_text("x", encoding="utf-8")
    (learned / "memory" / "wiki").mkdir(parents=True, exist_ok=True)
    (learned / "memory" / "wiki" / "a.md").write_text("x", encoding="utf-8")
    (learned / "strategy" / "memory.md").write_text("- x\n", encoding="utf-8")
    assert _runtime_fingerprint(kernel) == before

    (learned / "strategy" / "system_prompt.md").write_text("新的系统提示", encoding="utf-8")
    assert _runtime_fingerprint(kernel) != before


@pytest.mark.skipif(os.name != "posix", reason="路径语义按 POSIX 编写")
def test_guard_build_dedupes_and_resolves(tmp_path):
    guard = PathGuard.build(tmp_path, home=tmp_path / "h", extra_writable=[tmp_path / "h" / "workspace"])
    assert guard.writable.count((tmp_path / "h" / "workspace").resolve()) == 1
