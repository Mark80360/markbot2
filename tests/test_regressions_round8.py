"""完整检查发现的二次 bug 的回归测试（ADR 0008）。"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from markbot.api.tool import Tool, ToolResult
from markbot.kernel import os_sandbox
from markbot.kernel.config import PermissionConfig, load_config
from markbot.kernel.paths import PathGuard
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.optimizer.evals import EvalCase

CHANNEL_DIR = Path(__file__).resolve().parents[1] / "channels" / "feishu"


# ---------------------------------------------- 1. 评测会话不能进入 sessions/


def test_eval_run_case_does_not_archive_sessions(home, project, tmp_path):
    from markbot.cli.main import _make_run_case

    cfg = load_config(home=home, project_root=project)
    run_case = _make_run_case(cfg, ScriptedProvider(["评测回答"]))
    learned = tmp_path / "learned"
    learned.mkdir()
    text = asyncio.run(run_case(learned, EvalCase(id="c", prompt="hi")))
    assert text == "评测回答"
    sessions = home / "sessions"
    archived = list(sessions.glob("*/meta.json")) if sessions.exists() else []
    assert archived == [], "评测会话被归档，会污染优化器与记忆整理的输入"


def test_shutdown_without_archive(make_kernel, home):
    kernel = make_kernel(["ok"])
    asyncio.run(kernel.run("hi", archive=False))
    asyncio.run(kernel.shutdown(archive=False))
    sessions = home / "sessions"
    assert not sessions.exists() or not list(sessions.glob("*/meta.json"))


# ---------------------------------------------- 2. trusted 档位下钉死程序的工具免确认


class _Script(BaseModel):
    skill: str
    script: str


class _Cmd(BaseModel):
    command: str


async def _ok(ctx, args):
    return ToolResult(content="ok")


def test_pinned_program_tool_skips_confirmation_in_trusted(tmp_path):
    tool = Tool(name="run_skill_script", description="", params=_Script, handler=_ok,
                capabilities=frozenset({"exec:python"}))
    args = {"skill": "demo", "script": "run.py"}
    trusted = PermissionPolicy(PermissionConfig(profile="trusted"), root=tmp_path)
    assert not trusted.check(tool, args).requires_confirmation
    standard = PermissionPolicy(PermissionConfig(profile="standard"), root=tmp_path)
    assert standard.check(tool, args).requires_confirmation


def test_free_command_still_confirms_in_trusted(tmp_path):
    tool = Tool(name="run_command", description="", params=_Cmd, handler=_ok,
                capabilities=frozenset({"exec:*"}))
    trusted = PermissionPolicy(PermissionConfig(profile="trusted"), root=tmp_path)
    assert trusted.check(tool, {"command": "python3 x.py"}).requires_confirmation


# ---------------------------------------------- 3. OS 隔离下技能目录可读


def test_sandbox_profile_reopens_extra_readable_after_deny(tmp_path):
    root = tmp_path / "root"
    home = tmp_path / "mbhome"
    skill = home / "learned" / "skills" / "demo"
    skill.mkdir(parents=True)
    root.mkdir()
    guard = PathGuard.build(root, home=home)
    profile = os_sandbox.build_profile(guard, extra_readable=(skill,))
    deny = profile.index(f'(deny file-read* file-write* (subpath "{home.resolve()}"))')
    allow = profile.index(f'(allow file-read* (subpath "{skill.resolve()}"))')
    assert deny < allow
    # 只读开放：技能目录没有写权限
    assert f'(allow file-read* file-write* (subpath "{skill.resolve()}"))' not in profile


def test_exec_argv_opens_protected_cwd_readonly(tmp_path, monkeypatch):
    from markbot.kernel.execution import LocalExecution

    captured: dict = {}

    def fake_wrap(argv, guard, *, extra_readable=()):
        captured["readable"] = extra_readable
        return argv

    async def fake_run(argv, *, cwd, timeout_s, env, **_kwargs):
        return SimpleNamespace(ok=True, returncode=0, stdout="", stderr="", timed_out=False)

    import markbot.kernel.execution as execution

    root = tmp_path / "root"
    home = tmp_path / "mbhome"
    skill = home / "learned" / "skills" / "demo"
    skill.mkdir(parents=True)
    root.mkdir()
    env = LocalExecution(root, guard=PathGuard.build(root, home=home))
    env.os_sandbox = "macos"  # 跨平台测试：只验证参数传递
    monkeypatch.setattr(execution.os_sandbox, "wrap_argv", fake_wrap)
    monkeypatch.setattr(execution, "run_sandboxed", fake_run)
    asyncio.run(env.exec_argv(["python", "run.py"], cwd=skill))
    assert Path(skill).resolve() in [Path(p).resolve() for p in captured["readable"]]


# ---------------------------------------------- 6. 飞书渠道按 chat_id 分片并行


def test_feishu_shards_are_stable_per_chat(monkeypatch):
    monkeypatch.syspath_prepend(str(CHANNEL_DIR))
    gateway_module = importlib.import_module("feishu_gateway")
    fake = SimpleNamespace(queues=[object()] * 4)

    def shard(chat: str) -> int:
        data = {"event": {"message": {"chat_id": chat}}, "header": {"event_id": "e"}}
        return gateway_module.Gateway._shard_of(fake, data)

    assert shard("oc_a") == shard("oc_a")
    assert len({shard(f"oc_{i}") for i in range(64)}) > 1  # 不同聊天会分散到多个线程
    assert all(0 <= shard(f"oc_{i}") < 4 for i in range(64))
    single = SimpleNamespace(queues=[object()])
    assert gateway_module.Gateway._shard_of(single, {"event": {}}) == 0


def test_feishu_settings_workers_default(monkeypatch):
    monkeypatch.syspath_prepend(str(CHANNEL_DIR))
    settings_module = importlib.import_module("feishu_settings")
    fields = settings_module.Settings.__dataclass_fields__
    assert fields["workers"].default == 4
