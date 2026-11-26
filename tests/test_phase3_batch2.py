"""Phase 3 第二批（ADR 0015）：多模态图片、view_image、桌面控制、系统运维工具、浏览器评测集。

桌面测试用假后端替换真实的鼠标键盘与截图，不会动这台电脑；真实桌面冒烟测试用
``MARKBOT_DESKTOP_SMOKE=1`` 打开（只截图，不点击）。
"""

from __future__ import annotations

import asyncio
import os
import socket
import urllib.request
from pathlib import Path

import pytest

from markbot.api.media import image_block, prepare_images, user_images, vision_enabled
from markbot.api.ui import NullUI
from markbot.cli import eval_browser
from markbot.extensions import desktop, sysops, vision
from markbot.extensions.desktop import backends
from markbot.kernel.providers import anthropic, openai_compat
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import SourceSet
from markbot.kernel.session import Session
from markbot.schema.session_log import project_messages

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (64).to_bytes(4, "big") + (32).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00" + b"\x00" * 16
REPO = Path(__file__).resolve().parents[1]


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    return path


class _YesUI(NullUI):
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def confirm(self, title: str, message: str) -> bool:
        self.asked.append(message)
        return True


def _ctx(root: Path, profile: str = "trusted", ui=None, vision_fn=None, home: Path | None = None):
    from markbot.kernel.config import PermissionConfig
    from markbot.kernel.permissions import PermissionPolicy
    from markbot.kernel.tools import KernelToolContext

    policy = PermissionPolicy(PermissionConfig(profile=profile), root=root, home=home)
    return KernelToolContext(session=Session("测试"), policy=policy, ui=ui, vision=vision_fn)


def _setup(module, config=None) -> dict:
    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI(), config=config or {})
    module.setup(api)
    api.commit()
    return {tool.name: tool for tool in source.tools}


def _call(tool, ctx, **kwargs):
    async def invoke():
        outcome = tool.handler(ctx, tool.params(**kwargs))
        if hasattr(outcome, "__await__"):
            return await outcome
        return outcome

    return asyncio.run(invoke())


# ================================================================== 图片基础


def test_image_block_sniffs_and_limits(tmp_path):
    good = _png(tmp_path / "a.png")
    kind, data = image_block(good)
    assert kind == "image/png" and data
    fake = tmp_path / "b.png"
    fake.write_bytes(b"not an image")
    assert image_block(fake) is None
    assert image_block(tmp_path / "missing.png") is None
    assert image_block(good, max_bytes=10) is None


def test_prepare_images_keeps_recent_and_placeholders_old():
    messages = [
        {"role": "user", "content": "看图", "images": ["/a.png", "/b.png"]},
        {"role": "tool", "content": "截图", "images": ["/c.png"]},
        {"role": "assistant", "content": "好"},
    ]
    out = prepare_images(messages, vision=True, max_images=2)
    assert out[0]["images"] == ["/b.png"] and "/a.png" in out[0]["content"]
    assert out[1]["images"] == ["/c.png"]
    assert messages[0]["images"] == ["/a.png", "/b.png"]  # 不修改原消息
    off = prepare_images(messages, vision=False)
    assert all(not item.get("images") for item in off)
    assert "view_image" in off[0]["content"]


def test_user_images_only_from_allowed_roots(tmp_path):
    media = tmp_path / "home" / "media"
    inside = _png(media / "feishu" / "x.png")
    outside = _png(tmp_path / "elsewhere.png")
    text = f"看看这个\n[image: {inside}]\n[image: {outside}]\n[image: {media / 'nope.png'}]"
    assert user_images(text, [media]) == [str(inside.resolve())]


def test_vision_enabled_auto_detection():
    assert vision_enabled("auto", "anthropic", "anything")
    assert vision_enabled("auto", "openai_compat", "qwen-vl-max")
    assert vision_enabled("auto", "openai_compat", "gpt-4o-mini")
    assert not vision_enabled("auto", "openai_compat", "sensenova-6.8-flash")
    assert vision_enabled(True, "openai_compat", "sensenova-6.8-flash")
    assert not vision_enabled("false", "anthropic", "claude")


# ================================================================== 会话与 provider


def test_session_records_images_in_events(tmp_path):
    session = Session("看图", append_request=False)
    session.append_user("看图", images=["/x.png"])

    class Call:
        id, name, args = "c1", "browser_screenshot", {}

    class Result:
        content, is_error, details = "截图", False, {"images": ["/y.png"]}

    session.append_assistant("", tool_calls=[Call()])
    session.append_tool_results([Call()], [Result()])
    restored = project_messages(session.events)
    assert restored[0].images == ["/x.png"]
    assert restored[-1].images == ["/y.png"]
    assert session.messages_for_model()[-1]["images"] == ["/y.png"]


def test_openai_messages_attach_tool_images_after_tool_results(tmp_path):
    shot = str(_png(tmp_path / "s.png"))
    payload = {
        "messages": [
            {"role": "user", "content": "截个图", "images": []},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "name": "browser_screenshot", "arguments": {}},
                {"id": "b", "name": "read_file", "arguments": {}},
            ]},
            {"role": "tool", "tool_call_id": "a", "name": "browser_screenshot", "content": "ok", "images": [shot]},
            {"role": "tool", "tool_call_id": "b", "name": "read_file", "content": "txt", "images": []},
            {"role": "assistant", "content": "看到了"},
        ]
    }
    out = openai_compat._messages(payload)
    roles = [item["role"] for item in out]
    assert roles == ["user", "assistant", "tool", "tool", "user", "assistant"]
    parts = out[4]["content"]
    assert parts[0]["type"] == "text" and "browser_screenshot" in parts[0]["text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out[0]["content"] == "截个图"


def test_openai_user_images_and_unreadable_note(tmp_path):
    good = str(_png(tmp_path / "g.png"))
    out = openai_compat._messages(
        {"messages": [{"role": "user", "content": "这是什么", "images": [good, str(tmp_path / "bad.png")]}]}
    )
    content = out[0]["content"]
    assert content[0]["type"] == "text" and "无法读取" in content[0]["text"]
    assert len(content) == 2 and content[1]["type"] == "image_url"


def test_anthropic_tool_result_carries_image_blocks(tmp_path):
    shot = str(_png(tmp_path / "s.png"))
    out = anthropic._messages(
        [
            {"role": "user", "content": "看图", "images": [shot]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "t", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "a", "content": "截图", "images": [shot]},
        ]
    )
    assert out[0]["content"][1]["type"] == "image"
    result = out[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["content"][0] == {"type": "text", "text": "截图"}
    assert result["content"][1]["source"]["media_type"] == "image/png"


def test_kernel_attaches_user_images_and_describes(make_kernel, home):
    kernel = make_kernel(["收到", "一张测试图"])
    picture = _png(home / "media" / "feishu" / "p.png")
    asyncio.run(kernel.run(f"这张图是什么\n[image: {picture}]", archive=False))
    sent = kernel.provider.calls[0]["messages"][-1]
    assert sent["images"] == [str(picture.resolve())]
    assert kernel.vision  # 默认模型 claude-* -> 能看图
    text = asyncio.run(kernel.describe_image(str(picture), "描述"))
    assert text == "一张测试图"
    assert kernel.provider.calls[-1]["messages"][0]["images"] == [str(picture)]


def test_kernel_describe_image_without_vision_model(make_kernel, home):
    from markbot.kernel.config import load_config

    cfg = load_config(home=home, project_root=home.parent / "project")
    cfg.agent.vision = "false"
    kernel = make_kernel(config=cfg)
    picture = _png(home / "media" / "q.png")
    with pytest.raises(RuntimeError, match="视觉模型"):
        asyncio.run(kernel.describe_image(str(picture)))
    # 不能看图时，上下文里的图片换成文字占位
    asyncio.run(kernel.run(f"看图 [image: {picture}]", archive=False))
    sent = kernel.provider.calls[0]["messages"][-1]
    assert sent["images"] == [] and "view_image" in sent["content"]


# ================================================================== view_image


def test_view_image_attaches_when_main_model_sees(tmp_path):
    tool = _setup(vision)["view_image"]
    _png(tmp_path / "a.png")
    ctx = _ctx(tmp_path)
    ctx.main_vision = True
    result = _call(tool, ctx, path="a.png")
    assert not result.is_error and result.details["images"] == [str(tmp_path / "a.png")]


def test_view_image_delegates_to_vision_model(tmp_path):
    seen: list[tuple[str, str]] = []

    async def describe(path, question):
        seen.append((path, question))
        return "图里是一只猫"

    tool = _setup(vision)["view_image"]
    _png(tmp_path / "a.png")
    result = _call(tool, _ctx(tmp_path, vision_fn=describe), path="a.png", question="什么动物")
    assert "一只猫" in result.content and seen == [(str(tmp_path / "a.png"), "什么动物")]
    assert "images" not in result.details
    assert _call(tool, _ctx(tmp_path), path="a.txt").is_error
    assert _call(tool, _ctx(tmp_path), path="missing.png").is_error


# ================================================================== 桌面控制


class _FakeDesk:
    def __init__(self, monkeypatch, title="记事本", process="notepad.exe"):
        self.actions: list[tuple] = []
        self.window = backends.Window(handle="42", title=title, process=process, active=True, rect=(0, 0, 800, 600))
        monkeypatch.setattr(backends, "has_display", lambda: True)
        monkeypatch.setattr(backends, "active_window", lambda: self.window)
        monkeypatch.setattr(backends, "list_windows", lambda: [self.window])
        monkeypatch.setattr(backends, "cursor_position", lambda: (400, 200))
        monkeypatch.setattr(backends, "screenshot", self._shot)
        monkeypatch.setattr(backends, "click", lambda x, y, button="left", clicks=1: self.actions.append(("click", x, y, button, clicks)))
        monkeypatch.setattr(backends, "type_text", lambda text: self.actions.append(("type", text)))
        monkeypatch.setattr(backends, "press_keys", lambda keys: self.actions.append(("keys", tuple(keys))))
        monkeypatch.setattr(backends, "focus_window", lambda handle: self.actions.append(("focus", handle)))

    def _shot(self, target, max_width=1600):
        _png(Path(target))
        # 屏幕 3200x1800，缩放到 1600x900
        return backends.Shot(str(target), 1600, 900, 3200, 1800)


def _desk(tmp_path, ui=None, **settings):
    config = {"tools": {"desktop": {"enabled": True, "settle_s": 0, **settings}}}
    return _setup(desktop, config), _ctx(tmp_path, ui=ui, home=tmp_path / "home")


def test_desktop_disabled_by_default():
    assert _setup(desktop) == {}
    assert desktop.parse_settings({}).enabled is False


def test_desktop_click_maps_screenshot_coordinates(tmp_path, monkeypatch):
    fake = _FakeDesk(monkeypatch)
    ui = _YesUI()
    tools, ctx = _desk(tmp_path, ui=ui)
    shot = _call(tools["desktop_screenshot"], ctx)
    assert shot.details["images"] and "1600x900" in shot.content
    assert "鼠标位置: (200, 100)" in shot.content
    clicked = _call(tools["desktop_click"], ctx, x=100, y=50)
    assert not clicked.is_error, clicked.content
    assert fake.actions[0] == ("click", 200, 100, "left", 1)
    assert clicked.details["images"]  # 动作后自动截图
    _call(tools["desktop_type"], ctx, text="你好", enter=True, screenshot=False)
    assert ("type", "你好") in fake.actions and ("keys", ("enter",)) in fake.actions
    assert len(ui.asked) == 1  # confirm = first：每个会话只问一次
    out_of_range = _call(tools["desktop_click"], ctx, x=5000, y=10)
    assert out_of_range.is_error


def test_desktop_confirm_all_and_unattended_denial(tmp_path, monkeypatch):
    _FakeDesk(monkeypatch)
    ui = _YesUI()
    tools, ctx = _desk(tmp_path, ui=ui, confirm="all")
    _call(tools["desktop_key"], ctx, keys="ctrl+s", screenshot=False)
    _call(tools["desktop_key"], ctx, keys="esc", screenshot=False)
    assert len(ui.asked) == 2
    tools, unattended = _desk(tmp_path)  # NullUI：无人值守
    denied = _call(tools["desktop_click"], unattended, x=1, y=1)
    assert denied.is_error and denied.details.get("denied")


def test_desktop_blocked_apps(tmp_path, monkeypatch):
    fake = _FakeDesk(monkeypatch, title="KeePass - 数据库", process="KeePass.exe")
    tools, ctx = _desk(tmp_path, ui=_YesUI())
    result = _call(tools["desktop_type"], ctx, text="x")
    assert result.is_error and "blocked_apps" in result.content
    assert fake.actions == []
    only = desktop.DesktopSettings(allowed_apps=["notepad"])
    assert desktop.app_denial(only, "计算器", "calc.exe")
    assert desktop.app_denial(only, "无标题 - 记事本", "notepad.exe") is None


def test_desktop_type_secret_requires_registration(tmp_path, monkeypatch):
    _FakeDesk(monkeypatch)
    tools, ctx = _desk(tmp_path, ui=_YesUI())
    result = _call(tools["desktop_type"], ctx, secret="APP_PIN")
    assert result.is_error and "没有在 [tools.desktop] secrets 中登记" in result.content


def test_parse_keys():
    assert desktop.parse_keys("Ctrl + Shift + S") == ["ctrl", "shift", "s"]
    assert desktop.parse_keys("ctrl+plus") == ["ctrl", "+"]
    assert desktop.parse_keys("enter") == ["enter"]


def test_desktop_launch_declares_exec_and_strict_denies(tmp_path):
    tools, _ = _desk(tmp_path)
    assert "exec:*" in tools["desktop_launch"].capabilities
    strict = _ctx(tmp_path, profile="strict")
    assert not strict.policy.check(tools["desktop_click"], {"x": 1, "y": 1}).allowed
    standard = _ctx(tmp_path, profile="standard")
    decision = standard.policy.check(tools["desktop_launch"], {"program": "notepad"})
    assert decision.allowed and decision.requires_confirmation


@pytest.mark.skipif(os.environ.get("MARKBOT_DESKTOP_SMOKE") != "1", reason="需要图形界面：MARKBOT_DESKTOP_SMOKE=1")
def test_desktop_screenshot_smoke(tmp_path):
    shot = backends.screenshot(tmp_path / "s.png")
    assert Path(shot.path).stat().st_size > 0 and shot.width > 0
    assert backends.capabilities()["display"] == "yes"


# ================================================================== 系统运维


def test_sysops_registration_and_info(tmp_path):
    tools = _setup(sysops)
    assert {"system_info", "list_processes", "disk_usage", "check_port", "service_status", "kill_process"} <= set(tools)
    info = _call(tools["system_info"], _ctx(tmp_path))
    assert not info.is_error and "CPU" in info.content and "磁盘" in info.content
    assert _setup(sysops, {"tools": {"sysops": {"enabled": False}}}) == {}


def test_sysops_processes_and_disk(tmp_path):
    tools = _setup(sysops)
    procs = _call(tools["list_processes"], _ctx(tmp_path), limit=5)
    assert not procs.is_error and "PID" in procs.content
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "f.bin").write_bytes(b"x" * 4096)
    disk = _call(tools["disk_usage"], _ctx(tmp_path), path=".", top=3)
    assert "big/" in disk.content


def test_check_port(tmp_path):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        tool = _setup(sysops)["check_port"]
        ok = _call(tool, _ctx(tmp_path), host="127.0.0.1", port=port)
        assert not ok.is_error and ok.details["open"] is True
    finally:
        server.close()
    closed = _call(tool, _ctx(tmp_path), host="127.0.0.1", port=port, timeout_s=0.5)
    assert closed.is_error and closed.details["open"] is False


def test_kill_process_guards(tmp_path):
    tool = _setup(sysops)["kill_process"]
    assert "exec:*" in tool.capabilities
    own = _call(tool, _ctx(tmp_path), pid=os.getpid())
    assert own.is_error and own.details.get("denied")
    strict = _ctx(tmp_path, profile="strict")
    assert not strict.policy.check(tool, {"pid": 12345}).allowed
    # 自身已做危险进程守卫：standard / trusted 档位直接执行，不再要求确认
    for profile in ("standard", "trusted"):
        decision = _ctx(tmp_path, profile=profile).policy.check(tool, {"pid": 12345})
        assert decision.allowed and not decision.requires_confirmation, decision.reason
    assert sysops.human(1536) == "1.5 KB"


def test_service_status_rejects_injection(tmp_path):
    tool = _setup(sysops)["service_status"]
    assert _call(tool, _ctx(tmp_path), name="x; rm -rf /").is_error


# ================================================================== 浏览器评测集


def test_browser_eval_cases_load():
    cases = eval_browser.load_browser_cases(REPO / "evals" / "browser")
    names = {case.name for case in cases}
    assert {"js-table", "form-submit", "pagination", "login-flow", "search-filter"} <= names
    assert all("{base_url}" in case.prompt for case in cases)


def test_browser_eval_judge():
    case = eval_browser.BrowserCase(name="x", path=Path("."), prompt="p", expect=["共7条"], expect_any=["a", "b"], forbid=["错误"])
    assert eval_browser.judge("ERROR 共 7 条，a", case) is None
    assert "缺少" in eval_browser.judge("共 8 条 a", case)
    assert "任一" in eval_browser.judge("共7条", case)
    assert "禁止" in eval_browser.judge("共7条 a 错误", case)


def test_browser_eval_raw_and_server():
    raw = eval_browser.browser_eval_raw({"tools": {"web": {"allow_private": ["lan:1"]}}}, 8123, Path("/tmp/p"))
    assert raw["tools"]["web"]["allow_private"] == ["lan:1", "127.0.0.1:8123"]
    assert raw["tools"]["browser"]["confirm"] == "none" and raw["tools"]["desktop"]["enabled"] is False
    server, base = eval_browser.serve(REPO / "evals" / "browser" / "js-table" / "site")
    try:
        with urllib.request.urlopen(base + "/index.html", timeout=5) as resp:  # noqa: S310 - 本地测试服务器
            assert "Gamma" in resp.read().decode("utf-8")
    finally:
        server.shutdown()
