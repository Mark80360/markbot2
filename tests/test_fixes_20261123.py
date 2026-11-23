"""2026-11-23 服务器日志排查后的修复（docs/incident-2026-11-23-stop-image-fences.md）。

1. /stop：排在同一会话后面、等锁期间被取消的任务不再照常执行；取消回合时结束本会话的后台进程；
   飞书工作线程最多等 task_wait_s 就放手，不再让后续消息排队 10 分钟。
2. 飞书发图：请求带上渠道名，<env> 告诉模型可以用 ![..](本地路径) 发图片/文件；后台推送也带媒体。
3. 围栏：view_image / disk_usage / browser_upload 的 fs.read:* 能力读不了工作区子目录、共享工作目录与
   会话临时目录（一律「越权文件访问」）；验证提醒只看本回合、工作区内的代码改动；浏览器支持 display。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

from markbot.api import EventName
from markbot.api.ui import NullUI
from markbot.extensions import browser, process, sysops, vision
from markbot.kernel.config import PermissionConfig
from markbot.kernel.context import channel_line, environment_block
from markbot.kernel.execution import LocalExecution
from markbot.kernel.loop import TurnResult, _needs_verification
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import DEFAULT_GUARDRAILS, SourceSet
from markbot.kernel.session import Session
from markbot.kernel.tools import KernelToolContext
from markbot.kernel.turn import RunGate
from markbot.server.http import execute_chat, parse_chat_request

REPO = Path(__file__).resolve().parents[1]
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


def _setup(module, config=None):
    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI(), config=config or {})
    module.setup(api)
    api.commit()
    return {tool.name: tool for tool in source.tools}, source


def _ctx(root: Path, home: Path | None = None, session: Session | None = None):
    policy = PermissionPolicy(PermissionConfig(profile="trusted", trusted_exec="all"), root=root, home=home)
    env = LocalExecution(root, guard=policy.guard)
    return KernelToolContext(session=session or Session("测试"), policy=policy, env=env)


def _call(tool, ctx, **kwargs):
    async def invoke():
        outcome = tool.handler(ctx, tool.params(**kwargs))
        return await outcome if hasattr(outcome, "__await__") else outcome

    return asyncio.run(invoke())


@pytest.fixture
def layout(tmp_path):
    root = (tmp_path / "project").resolve()
    home = (tmp_path / "home").resolve()
    (root / "shots").mkdir(parents=True)
    (home / "workspace" / "douyin-login").mkdir(parents=True)
    (home / "sessions" / "sess_1" / "scratch" / "screenshots").mkdir(parents=True)
    return root, home


# ================================================================== 围栏：读取能力


def test_view_image_reads_workspace_subdirs_shared_workspace_and_scratch(layout):
    root, home = layout
    tool = _setup(vision)[0]["view_image"]
    ctx = _ctx(root, home)
    targets = [
        root / "shots" / "a.png",
        home / "workspace" / "douyin-login" / "dy_qr.png",
        home / "sessions" / "sess_1" / "scratch" / "screenshots" / "s.png",
    ]
    for path in targets:
        path.write_bytes(PNG)
        decision = ctx.policy.check(tool, {"path": str(path)})
        assert decision.allowed, (path, decision.reason)


def test_outside_workspace_denial_explains_where_to_put_files(layout, tmp_path):
    root, home = layout
    tool = _setup(vision)[0]["view_image"]
    outside = tmp_path / "elsewhere" / "x.png"
    decision = _ctx(root, home).policy.check(tool, {"path": str(outside)})
    assert not decision.allowed
    assert "不在工作区内" in decision.reason and str(home / "workspace") in decision.reason
    # 受保护目录（配置、日志）仍然拒绝
    assert not _ctx(root, home).policy.check(tool, {"path": str(home / "config.toml")}).allowed


def test_read_tools_use_workspace_level_capability():
    assert _setup(vision)[0]["view_image"].capabilities == frozenset({"fs.read:./**"})
    assert "fs.read:./**" in _setup(sysops)[0]["disk_usage"].capabilities
    upload = _setup(browser, {"tools": {"browser": {"enabled": True}}})[0]["browser_upload"]
    assert "fs.read:./**" in upload.capabilities


# ================================================================== 围栏：验证提醒


def test_verify_reminder_only_for_this_turn_and_workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    session = Session("x", append_request=False)
    session.code_changed_at = 200.0
    session.file_changes = {
        str(tmp_path / "home" / "workspace" / "dy_login.py"): {"at": 200.0},
        str(root / "notes.md"): {"at": 200.0},
    }
    assert not _needs_verification(session, since=100.0, root=root)  # 工作区外的脚本 + 文档
    session.file_changes[str(root / "app.py")] = {"at": 50.0}
    assert not _needs_verification(session, since=100.0, root=root)  # 上一回合改的
    session.file_changes[str(root / "app.py")] = {"at": 150.0}
    assert _needs_verification(session, since=100.0, root=root)
    session.verified_at = 160.0
    assert not _needs_verification(session, since=100.0, root=root)
    # 旧调用方式不变
    legacy = Session("y", append_request=False)
    legacy.code_changed_at = 10.0
    assert _needs_verification(legacy)


# ================================================================== /stop


def test_queued_task_cancelled_while_waiting_does_not_run(make_kernel):
    kernel = make_kernel(["不应该执行"])
    result = execute_chat(
        kernel, RunGate(2), "token", "你好", None, [], {}, {}, should_run=lambda: False
    )
    assert result.stopped_reason == "cancelled"
    assert kernel.provider.calls == []


def test_cancelled_turn_stops_session_processes(tmp_path):
    root = tmp_path.resolve()
    tools, source = _setup(process)
    ctx = _ctx(root)
    started = _call(
        tools["process_start"], ctx, command=f'"{sys.executable}" -c "import time; time.sleep(60)"', wait_s=1
    )
    assert not started.is_error, started.content
    handlers = [handler for event, _owner, handler in source.subscriptions if event == EventName.TURN_END]
    assert handlers
    done = TurnResult(stopped_reason="completed", text="好")
    asyncio.run(handlers[0](session=ctx.session, result=done))
    # 正常完成的回合不结束后台进程
    listing = _call(tools["process_list"], ctx)
    assert started.details["handle"] in listing.content
    cancelled = TurnResult(stopped_reason="cancelled", text="")
    asyncio.run(handlers[0](session=ctx.session, result=cancelled))
    assert "已结束本会话的 1 个后台进程" in cancelled.text
    assert started.details["handle"] not in _call(tools["process_list"], ctx).content


def test_stop_on_cancel_can_be_disabled(tmp_path):
    _tools, source = _setup(process, {"tools": {"process": {"stop_on_cancel": False}}})
    handler = next(h for event, _o, h in source.subscriptions if event == EventName.TURN_END)
    result = TurnResult(stopped_reason="cancelled")
    asyncio.run(handler(session=Session("x", append_request=False), result=result))
    assert result.text == ""


# ================================================================== 飞书发图


def test_channel_is_parsed_and_reaches_environment(make_kernel):
    parsed, error = parse_chat_request({"prompt": "hi", "channel": "feishu"})
    assert error is None and parsed["channel"] == "feishu"
    assert parse_chat_request({"prompt": "hi", "channel": "bad channel!"})[0]["channel"] is None
    session = Session("x", append_request=False)
    session.metadata["channel"] = "feishu"
    assert "![说明](本地绝对路径)" in channel_line(session)
    assert channel_line(Session("y", append_request=False)) == ""
    kernel = make_kernel(["好"])
    execute_chat(kernel, RunGate(2), "token", "截图发我", None, [], {}, {}, channel="feishu")
    system = kernel.provider.calls[0]["system"]
    assert "对话渠道: 飞书" in system
    assert "channel" not in (kernel.session.metadata if kernel.session else {})  # 只对本回合生效


def test_environment_block_without_channel_unchanged(tmp_path):
    block = environment_block(LocalExecution(tmp_path), Session("x", append_request=False))
    assert "对话渠道" not in block


def test_guardrails_allow_user_provided_credentials_and_forbid_guessing_limits():
    assert "用户在对话里主动提供" in DEFAULT_GUARDRAILS
    assert "不要臆测" in DEFAULT_GUARDRAILS


@pytest.fixture
def feishu(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "home"))
    monkeypatch.syspath_prepend(str(REPO / "channels" / "feishu"))
    gateway_module = importlib.import_module("feishu_gateway")
    settings_module = importlib.import_module("feishu_settings")
    store_module = importlib.import_module("feishu_store")
    secret_field = "app_" + "secret"
    settings = settings_module.Settings(
        app_id="a", **{secret_field: "s" + "x"}, verification_token="", host="127.0.0.1", port=8790,
        path="/e", markbot_url="http://127.0.0.1:8787", markbot_timeout_s=600.0, group_policy="open",
        reply_to_message=False, topic_isolation=True, react_emoji="", done_emoji="", allow_from=(),
        media_dir=tmp_path / "media", session_db=tmp_path / "f.db", workers=1,
    )
    store = store_module.Store(settings.session_db)
    return gateway_module, settings, gateway_module.Gateway(settings, store)


def test_feishu_payload_carries_channel(feishu, monkeypatch):
    gateway_module, settings, gateway = feishu
    seen: dict = {}

    def fake_run_task(payload, *, chat_key, on_delta):
        seen.update(payload)
        return {"text": "ok"}

    monkeypatch.setattr(gateway, "_run_task", fake_run_task)
    gateway._ask_markbot("hi", None, chat_key="k", chat_id="oc_1")
    assert seen["channel"] == "feishu"


def test_feishu_notify_sends_local_media(feishu, monkeypatch, tmp_path):
    _module, _settings, gateway = feishu
    picture = tmp_path / "qr.png"
    picture.write_bytes(PNG)
    calls: list = []

    def fake_send_answer(chat_id, text, *, media=None, **kwargs):
        calls.append((chat_id, media))
        return True

    monkeypatch.setattr(gateway._client, "send_answer", fake_send_answer)
    assert gateway.notify("oc_1", f"二维码在这里：![二维码]({picture})")
    assert calls == [("oc_1", [str(picture.resolve())])]
    gateway.notify("oc_1", "没有图片")
    assert calls[-1] == ("oc_1", None)


def test_feishu_task_wait_caps_soft_timeout(feishu, monkeypatch):
    _module, settings, gateway = feishu
    assert settings.task_wait_s == 120.0
    import dataclasses

    gateway._settings = dataclasses.replace(settings, task_wait_s=30.0)  # Settings 是 frozen
    clock = {"t": 0.0}
    monkeypatch.setattr(_module.time, "monotonic", lambda: clock["t"])

    def fake_call(method, path, payload=None, timeout=None):
        if method == "POST" and path == "/tasks":
            return 202, {"task_id": "task_1", "session_id": "sess_1"}
        if path.endswith("/detach"):
            return 200, {"state": "running"}
        clock["t"] += 31.0  # 一次长轮询之后就超过 30 秒
        return 200, {"events": [], "next": 0}

    monkeypatch.setattr(gateway, "_markbot_call", fake_call)
    result = gateway._run_task({"prompt": "x"}, chat_key="", on_delta=None)
    assert result["stopped_reason"] == "detached"


def test_browser_display_setting(monkeypatch):
    settings = browser.parse_settings({"tools": {"browser": {"headless": False, "display": ":99"}}})
    assert settings.display == ":99" and settings.headless is False
    tools, _ = _setup(browser, {"tools": {"browser": {"enabled": True, "headless": False, "display": ":99"}}})
    assert "有界面模式（DISPLAY=:99）" in tools["browser_open"].description
    tools, _ = _setup(browser, {"tools": {"browser": {"enabled": True}}})
    assert "无头模式" in tools["browser_open"].description
