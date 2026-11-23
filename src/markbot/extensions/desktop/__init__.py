"""桌面控制（ADR 0015）：截图、鼠标键盘、窗口、Windows 控件树。

默认**关闭**：``[tools.desktop] enabled = true`` 才注册工具。只在有图形界面的机器上可用
（Windows / macOS / 带 DISPLAY 的 Linux）。

工作方式（computer-use 循环）：``desktop_screenshot`` 看屏幕 → 按截图像素坐标
``desktop_click`` / ``desktop_type`` / ``desktop_key`` → 动作默认自动再截一张图附给模型，
模型据此判断是否成功、下一步做什么。Windows 上还可以用 ``desktop_inspect`` 读控件树，
按控件编号（c1、c2…）点击，比坐标可靠。

安全：
- ``confirm = "first"``（默认）：每个会话第一次控制鼠标键盘前请用户确认；``"all"`` 每个动作都确认；
  ``"none"`` 不确认。无人值守的定时任务无法确认 → 拒绝；
- ``blocked_apps``：前台窗口标题或进程名包含这些词时拒绝操作（默认包含常见密码管理器）；
  ``allowed_apps`` 非空时只允许在这些应用里操作；
- 输入密码用 ``desktop_type(secret=<名字>)``（须登记在 ``[tools.desktop] secrets``），值不进入上下文；
- 启动程序 ``desktop_launch`` 额外声明 ``exec:*``，按命令执行的权限规则确认；
- 装了 pyautogui 时，鼠标甩到屏幕左上角可紧急中止；飞书 ``/stop`` 会取消整个回合。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from markbot.api import EventName, ExtensionAPI, Tool, ToolResult
from markbot.extensions.desktop import backends
from markbot.extensions.desktop.backends import DesktopError

logger = logging.getLogger("markbot.desktop")

DEFAULT_BLOCKED = ["1password", "keepass", "bitwarden", "lastpass", "密码管理"]


@dataclass
class DesktopSettings:
    enabled: bool = False
    confirm: str = "first"
    max_width: int = 1600
    settle_s: float = 0.8
    blocked_apps: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED))
    allowed_apps: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def parse_settings(config: Any) -> DesktopSettings:
    tools = (config or {}).get("tools") if isinstance(config, dict) else None
    raw = (tools or {}).get("desktop") if isinstance(tools, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    settings = DesktopSettings(enabled=_truthy(raw.get("enabled", False)))
    confirm = str(raw.get("confirm") or "first").strip().lower()
    settings.confirm = confirm if confirm in {"first", "all", "none"} else "first"
    if "max_width" in raw:
        settings.max_width = max(0, int(raw["max_width"]))
    if "settle_s" in raw:
        settings.settle_s = max(0.0, float(raw["settle_s"]))
    if "blocked_apps" in raw:
        settings.blocked_apps = [str(item).strip().lower() for item in raw["blocked_apps"] or [] if str(item).strip()]
    settings.allowed_apps = [str(item).strip().lower() for item in raw.get("allowed_apps") or [] if str(item).strip()]
    settings.secrets = [str(item).strip() for item in raw.get("secrets") or [] if str(item).strip()]
    return settings


def app_denial(settings: DesktopSettings, title: str, process: str) -> str | None:
    """按前台窗口判断是否允许操作；允许时返回 None。"""
    haystack = f"{title} {process}".lower()
    for word in settings.blocked_apps:
        if word and word in haystack:
            return f"前台窗口「{title or process}」命中 blocked_apps（{word}），禁止自动操作"
    if settings.allowed_apps and not any(word in haystack for word in settings.allowed_apps):
        return f"前台窗口「{title or process}」不在 allowed_apps 中，禁止自动操作"
    return None


def parse_keys(spec: str) -> list[str]:
    """``"ctrl+shift+s"`` -> ``["ctrl", "shift", "s"]``；``"+"`` 键写作 ``plus``。"""
    parts = [part.strip().lower() for part in re.split(r"\s*\+\s*", spec.strip()) if part.strip()]
    return ["+" if part == "plus" else part for part in parts]


@dataclass
class _State:
    shot: backends.Shot | None = None
    approved: bool = False
    controls: dict[str, backends.Control] = field(default_factory=dict)


# ------------------------------------------------------------------ 参数
class ShotIn(BaseModel):
    pass


class WindowsIn(BaseModel):
    pass


class FocusIn(BaseModel):
    target: str = Field(description="窗口句柄（desktop_windows 给出的 id）或标题中的关键词")


class InspectIn(BaseModel):
    window: str = Field(default="", description="窗口句柄或标题关键词；默认前台窗口")
    all_controls: bool = Field(default=False, description="包括不可交互的控件（默认只列按钮、输入框等）")
    limit: int = Field(default=120, ge=10, le=400)


class ClickIn(BaseModel):
    x: int | None = Field(default=None, description="横坐标（最近一次截图的像素坐标）")
    y: int | None = Field(default=None, description="纵坐标（最近一次截图的像素坐标）")
    ref: str = Field(default="", description="改为点击 desktop_inspect 给出的控件编号，如 c12")
    button: Literal["left", "right", "middle"] = "left"
    double: bool = False
    screenshot: bool = Field(default=True, description="动作后自动截图附给模型")


class DragIn(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int
    screenshot: bool = True


class TypeIn(BaseModel):
    text: str = Field(default="", description="要输入的文本（支持中文；会输入到当前焦点位置）")
    secret: str = Field(default="", description="改为输入这个密钥（[tools.desktop] secrets 中登记的名字）")
    enter: bool = Field(default=False, description="输入后按回车")
    screenshot: bool = True


class KeyIn(BaseModel):
    keys: str = Field(description="按键或组合键，如 enter、esc、ctrl+s、alt+tab、ctrl+shift+t")
    repeat: int = Field(default=1, ge=1, le=20)
    screenshot: bool = True


class ScrollIn(BaseModel):
    amount: int = Field(description="滚轮格数：正数向上，负数向下", ge=-50, le=50)
    x: int | None = None
    y: int | None = None
    screenshot: bool = True


class LaunchIn(BaseModel):
    program: str = Field(description="程序名或路径，如 notepad、code、/usr/bin/gedit", json_schema_extra={"x-markbot-role": "program"})
    args: list[str] = Field(default_factory=list)
    wait_s: float = Field(default=2.0, ge=0, le=30, description="启动后等待几秒再截图")


class WaitIn(BaseModel):
    seconds: float = Field(default=2.0, ge=0.1, le=60)


# ------------------------------------------------------------------ 工具
def setup(api: ExtensionAPI) -> None:
    config = getattr(api, "config", None)
    settings = parse_settings(config)
    if not settings.enabled:
        return
    states: dict[str, _State] = {}

    def _sid(ctx: Any) -> str:
        return str(getattr(getattr(ctx, "session", None), "id", "") or "default")

    def _state(ctx: Any) -> _State:
        return states.setdefault(_sid(ctx), _State())

    def _shots_dir(ctx: Any) -> Path:
        home = getattr(getattr(getattr(ctx, "env", None), "guard", None), "home", None)
        base = Path(home) / "sessions" / _sid(ctx) / "scratch" if home else Path(ctx.env.root) / ".markbot-scratch"
        return base / "screenshots"

    def _to_screen(state: _State, x: int, y: int) -> tuple[int, int]:
        shot = state.shot
        if shot is None:
            return int(x), int(y)
        return shot.left + round(x * shot.scale), shot.top + round(y * shot.scale)

    def _to_image(state: _State, x: int, y: int) -> tuple[int, int]:
        shot = state.shot
        if shot is None:
            return int(x), int(y)
        return round((x - shot.left) / shot.scale), round((y - shot.top) / shot.scale)

    async def _take(ctx: Any) -> tuple[backends.Shot, str]:
        state = _state(ctx)
        target = _shots_dir(ctx) / f"desk-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
        shot = await asyncio.to_thread(backends.screenshot, target, settings.max_width)
        state.shot = shot
        window = await asyncio.to_thread(backends.active_window)
        cursor = await asyncio.to_thread(backends.cursor_position)
        cx, cy = _to_image(state, *cursor)
        lines = [
            f"截图 {shot.width}x{shot.height}（屏幕 {shot.screen_width}x{shot.screen_height}"
            + (f"，已缩放 1/{shot.scale:.2f}" if shot.scale > 1.01 else "") + "）：" + shot.path,
            "坐标按这张截图的像素给出（左上角为 0,0）。",
            f"鼠标位置: ({cx}, {cy})",
        ]
        if window is not None:
            lines.append(f"前台窗口: {window.title}" + (f"（{window.process}）" if window.process else ""))
        return shot, "\n".join(lines)

    async def _guard(ctx: Any, action: str) -> ToolResult | None:
        if not backends.has_display():
            return ToolResult.error("这台机器没有图形界面（未设置 DISPLAY / WAYLAND_DISPLAY），无法控制桌面")
        window = await asyncio.to_thread(backends.active_window)
        if window is not None:
            reason = app_denial(settings, window.title, window.process)
            if reason:
                return ToolResult.error(reason, denied=True)
        state = _state(ctx)
        need = settings.confirm == "all" or (settings.confirm == "first" and not state.approved)
        if need:
            where = f"\n当前前台窗口：{window.title}" if window is not None else ""
            message = (
                f"{action}{where}"
                if settings.confirm == "all"
                else f"MarkBot 要在本会话中控制这台电脑的鼠标和键盘（第一个动作：{action}）。{where}\n"
                "确认后本会话内不再逐次询问；随时发送 /stop 中止。"
            )
            confirm = getattr(ctx, "confirm", None)
            if not (confirm and await confirm("确认桌面操作", message)):
                return ToolResult.error(
                    "permission denied: 用户未确认桌面操作（无人值守的任务无法确认）", denied=True
                )
            state.approved = True
        logger.info("桌面动作 session=%s %s", _sid(ctx), action)
        return None

    async def _after(ctx: Any, text: str, want_shot: bool, delay: float | None = None) -> ToolResult:
        if not want_shot:
            return ToolResult(content=text)
        await asyncio.sleep(settings.settle_s if delay is None else delay)
        try:
            shot, summary = await _take(ctx)
        except DesktopError as exc:
            return ToolResult(content=f"{text}\n（动作后截图失败: {exc}）")
        return ToolResult(content=f"{text}\n{summary}", details={"images": [shot.path], "path": shot.path})

    def _wrap(fn: Any) -> Any:
        async def handler(ctx: Any, args: Any) -> ToolResult:
            try:
                return await fn(ctx, args)
            except DesktopError as exc:
                return ToolResult.error(str(exc))
            except Exception as exc:  # noqa: BLE001 - pyautogui FailSafeException 等
                if type(exc).__name__ == "FailSafeException":
                    return ToolResult.error("已紧急中止：鼠标被移到了屏幕角落（pyautogui 安全机制）", denied=True)
                return ToolResult.error(f"桌面操作失败: {type(exc).__name__}: {str(exc)[:300]}")

        return handler

    async def _find_window(target: str) -> backends.Window:
        windows = await asyncio.to_thread(backends.list_windows)
        text = target.strip()
        exact = [window for window in windows if window.handle == text]
        if exact:
            return exact[0]
        matches = [window for window in windows if text.lower() in f"{window.title} {window.process}".lower()]
        if not matches:
            raise DesktopError(f"没有找到窗口: {target}（用 desktop_windows 查看）")
        return matches[0]

    # ---- 观察
    async def desktop_screenshot(ctx: Any, args: ShotIn) -> ToolResult:
        if not backends.has_display():
            return ToolResult.error("这台机器没有图形界面，无法截图")
        shot, summary = await _take(ctx)
        return ToolResult(content=summary, details={"images": [shot.path], "path": shot.path})

    async def desktop_windows(ctx: Any, args: WindowsIn) -> ToolResult:
        windows = await asyncio.to_thread(backends.list_windows)
        state = _state(ctx)
        lines = []
        for window in windows[:60]:
            left, top = _to_image(state, window.rect[0], window.rect[1])
            right, bottom = _to_image(state, window.rect[2], window.rect[3])
            mark = "👉" if window.active else "  "
            lines.append(
                f"{mark} id={window.handle} {window.title!r}"
                + (f" [{window.process}]" if window.process else "")
                + f" @({left},{top})-({right},{bottom})"
            )
        return ToolResult(content="\n".join(lines) or "（没有可见窗口）")

    async def desktop_focus(ctx: Any, args: FocusIn) -> ToolResult:
        window = await _find_window(args.target)
        reason = app_denial(settings, window.title, window.process)
        if reason:
            return ToolResult.error(reason, denied=True)
        await asyncio.to_thread(backends.focus_window, window.handle)
        return await _after(ctx, f"已切换到窗口: {window.title}", True)

    async def desktop_inspect(ctx: Any, args: InspectIn) -> ToolResult:
        state = _state(ctx)
        window = await _find_window(args.window) if args.window.strip() else await asyncio.to_thread(backends.active_window)
        if window is None:
            return ToolResult.error("没有前台窗口")
        reason = app_denial(settings, window.title, window.process)
        if reason:
            return ToolResult.error(reason, denied=True)
        controls = await asyncio.to_thread(
            backends.inspect_controls, window.handle, args.limit, not args.all_controls
        )
        state.controls = {control.ref: control for control in controls}
        lines = [f"窗口: {window.title}（{len(controls)} 个控件；坐标为截图像素，可直接用 ref 点击）"]
        for control in controls:
            cx, cy = _to_image(state, *control.center)
            parts = [f"[{control.ref}] {control.kind}"]
            if control.name:
                parts.append(repr(control.name))
            if control.value:
                parts.append(f"值={control.value!r}")
            if not control.enabled:
                parts.append("不可用")
            parts.append(f"@({cx},{cy})")
            lines.append(" ".join(parts))
        return ToolResult(content="\n".join(lines))

    # ---- 动作
    async def desktop_click(ctx: Any, args: ClickIn) -> ToolResult:
        state = _state(ctx)
        if args.ref:
            control = state.controls.get(args.ref.strip())
            if control is None:
                return ToolResult.error(f"没有控件 {args.ref}（先运行 desktop_inspect；界面变化后需重新读取）")
            sx, sy = control.center
            label = f"{control.kind} {control.name!r}"
        elif args.x is not None and args.y is not None:
            if state.shot is not None and not (0 <= args.x <= state.shot.width and 0 <= args.y <= state.shot.height):
                return ToolResult.error(f"坐标 ({args.x}, {args.y}) 超出截图范围 {state.shot.width}x{state.shot.height}")
            sx, sy = _to_screen(state, args.x, args.y)
            label = f"({args.x}, {args.y})"
        else:
            return ToolResult.error("需要 x、y 坐标或控件 ref")
        action = f"{'双击' if args.double else '点击'}{'' if args.button == 'left' else '（' + args.button + '键）'} {label}"
        denied = await _guard(ctx, action)
        if denied:
            return denied
        await asyncio.to_thread(backends.click, sx, sy, args.button, 2 if args.double else 1)
        return await _after(ctx, f"已{action}", args.screenshot)

    async def desktop_drag(ctx: Any, args: DragIn) -> ToolResult:
        state = _state(ctx)
        x1, y1 = _to_screen(state, args.x1, args.y1)
        x2, y2 = _to_screen(state, args.x2, args.y2)
        action = f"从 ({args.x1}, {args.y1}) 拖到 ({args.x2}, {args.y2})"
        denied = await _guard(ctx, action)
        if denied:
            return denied
        await asyncio.to_thread(backends.drag, x1, y1, x2, y2)
        return await _after(ctx, f"已{action}", args.screenshot)

    async def desktop_type(ctx: Any, args: TypeIn) -> ToolResult:
        value = args.text
        masked = False
        if args.secret:
            if args.secret not in settings.secrets:
                return ToolResult.error(f"密钥 {args.secret} 没有在 [tools.desktop] secrets 中登记", denied=True)
            try:
                value = ctx.secret(args.secret)
            except KeyError:
                return ToolResult.error(f"未配置密钥: {args.secret}")
            masked = True
        if not value:
            return ToolResult.error("没有要输入的内容")
        shown = f"密钥 {args.secret}" if masked else repr(value[:80] + ("…" if len(value) > 80 else ""))
        action = f"输入 {shown}" + ("，然后回车" if args.enter else "")
        denied = await _guard(ctx, action)
        if denied:
            return denied
        await asyncio.to_thread(backends.type_text, value)
        if args.enter:
            await asyncio.to_thread(backends.press_keys, ["enter"])
        return await _after(ctx, f"已{action}", args.screenshot)

    async def desktop_key(ctx: Any, args: KeyIn) -> ToolResult:
        keys = parse_keys(args.keys)
        if not keys:
            return ToolResult.error("没有指定按键")
        action = f"按键 {'+'.join(keys)}" + (f" ×{args.repeat}" if args.repeat > 1 else "")
        denied = await _guard(ctx, action)
        if denied:
            return denied
        for _ in range(args.repeat):
            await asyncio.to_thread(backends.press_keys, keys)
            await asyncio.sleep(0.05)
        return await _after(ctx, f"已{action}", args.screenshot)

    async def desktop_scroll(ctx: Any, args: ScrollIn) -> ToolResult:
        state = _state(ctx)
        point = _to_screen(state, args.x, args.y) if args.x is not None and args.y is not None else (None, None)
        action = f"滚动 {args.amount:+d} 格"
        denied = await _guard(ctx, action)
        if denied:
            return denied
        await asyncio.to_thread(backends.scroll, args.amount, point[0], point[1])
        return await _after(ctx, f"已{action}", args.screenshot)

    async def desktop_launch(ctx: Any, args: LaunchIn) -> ToolResult:
        reason = app_denial(settings, "", Path(args.program).name)
        if reason:
            return ToolResult.error(reason, denied=True)
        if not backends.has_display():
            return ToolResult.error("这台机器没有图形界面，无法启动桌面程序")
        try:
            pid = await asyncio.to_thread(backends.launch, args.program, list(args.args))
        except (OSError, ValueError) as exc:
            return ToolResult.error(f"启动失败: {exc}")
        _state(ctx).approved = _state(ctx).approved or settings.confirm == "none"
        return await _after(ctx, f"已启动 {args.program}" + (f"（pid {pid}）" if pid else ""), True, args.wait_s)

    async def desktop_wait(ctx: Any, args: WaitIn) -> ToolResult:
        return await _after(ctx, f"已等待 {args.seconds:g} 秒", True, args.seconds)

    async def _on_session_end(*_args: Any, **kwargs: Any) -> None:
        session = kwargs.get("session")
        if session is not None:
            states.pop(str(getattr(session, "id", "")), None)

    api.on(EventName.SESSION_END, _on_session_end)

    caps = frozenset({"desktop:*"})
    specs: list[tuple[str, str, Any, Any, frozenset[str]]] = [
        ("desktop_screenshot", "截取屏幕（图片附给模型），返回尺寸、前台窗口与鼠标位置", ShotIn, desktop_screenshot, caps),
        ("desktop_windows", "列出可见窗口（id、标题、进程、位置）", WindowsIn, desktop_windows, caps),
        ("desktop_focus", "把指定窗口切到前台", FocusIn, desktop_focus, caps),
        ("desktop_inspect", "读取窗口的控件树（Windows），得到可直接点击的控件编号", InspectIn, desktop_inspect, caps),
        ("desktop_click", "按截图坐标或控件编号点击；动作后自动截图", ClickIn, desktop_click, caps),
        ("desktop_drag", "按住左键从一点拖到另一点", DragIn, desktop_drag, caps),
        ("desktop_type", "在当前焦点处输入文本（支持中文）；密码用 secret", TypeIn, desktop_type, caps),
        ("desktop_key", "按键或组合键（enter、esc、ctrl+s、alt+tab…）", KeyIn, desktop_key, caps),
        ("desktop_scroll", "滚动鼠标滚轮", ScrollIn, desktop_scroll, caps),
        ("desktop_launch", "启动桌面程序（按执行命令的权限规则确认）", LaunchIn, desktop_launch, caps | {"exec:*"}),
        ("desktop_wait", "等待界面变化后截图", WaitIn, desktop_wait, caps),
    ]
    for name, description, params, fn, tool_caps in specs:
        api.register_tool(
            Tool(
                name=name,
                description=description,
                params=params,
                handler=_wrap(fn),
                capabilities=tool_caps,
                timeout_s=120.0,
                concurrency_safe=False,
            )
        )


__all__ = ["DesktopSettings", "app_denial", "parse_keys", "parse_settings", "setup"]
