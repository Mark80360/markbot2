"""浏览器自动化（Playwright，ADR 0014）。

内置浏览器扩展，补上 ``fetch_url`` 做不到的事：执行 JavaScript 的页面、登录态、点击/输入/表单、
下载、截图，以及遇到验证码时请用户在浏览器窗口里接手。

启用：``[tools.browser] enabled = "auto"``（默认：安装了 playwright 才注册工具）/ ``true`` / ``false``。
安装：``pip install -e ".[browser]"`` 后执行 ``playwright install chromium``；也可以设置
``channel = "msedge"`` / ``"chrome"`` 使用本机已安装的浏览器，不用下载。

设计：
- 浏览器跑在扩展私有的事件循环线程里（与 MCP 扩展相同的做法）：每个回合各自 ``asyncio.run``，
  Playwright 对象不能跨事件循环使用。持久化用户目录（默认 ``<home>/browser-profile``）保存登录态，
  空闲 ``idle_timeout_s`` 后关闭浏览器（登录态仍保留在用户目录里）。
- 每个会话有自己的标签页；同一会话的操作串行执行。
- 页面快照：可交互元素编号成 ``e1``、``e2``……（写进 ``data-markbot-ref`` 属性），模型用编号点击/输入，
  不用猜 CSS 选择器；再附上页面可见文本的开头。
- 安全：
  - 每一次页面导航（包括点击链接、重定向、iframe）都经过地址检查：只允许 http(s)，
    ``blocked_domains`` / ``allowed_domains``，以及与 ``fetch_url`` 相同的内网地址限制（``[tools.web] allow_private``）；
  - ``confirm = "risky"``（默认）：看起来会产生后果的点击（提交、发送、购买、支付、删除、发布……）、
    输入后回车提交、执行脚本、上传文件要用户确认；``"all"`` 所有点击/输入都确认；``"none"`` 不确认；
    无人值守的定时任务没有确认对象，这些动作会被拒绝；
  - 输入密码等敏感值用 ``secret``：只能取 ``[tools.browser] secrets`` 里登记的密钥名，值不会出现在模型上下文与日志；
  - 能力声明 ``browser:*``：strict 档位下整体禁用。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from markbot.api import EventName, ExtensionAPI, Tool, ToolResult
from markbot.extensions.web import check_target, html_to_markdown

logger = logging.getLogger("markbot.browser")

_REF = re.compile(r"^e\d{1,5}$")
RISKY_WORDS = (
    "提交", "发送", "购买", "支付", "付款", "下单", "删除", "移除", "注销", "转账", "发布", "确认支付",
    "立即购买", "确认订单", "退出登录", "submit", "send", "buy", "pay", "purchase", "order", "checkout",
    "delete", "remove", "publish", "post", "transfer", "sign out", "log out", "logout", "unsubscribe",
)
SNAPSHOT_JS = r"""
(maxItems) => {
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],' +
    '[role=radio],[role=tab],[role=menuitem],[role=option],[role=switch],[role=textbox],[role=combobox],' +
    '[role=searchbox],[contenteditable=""],[contenteditable=true],[onclick]';
  document.querySelectorAll('[data-markbot-ref]').forEach(e => e.removeAttribute('data-markbot-ref'));
  const items = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (r.width < 1 || r.height < 1 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (el.type === 'hidden') continue;
    n += 1; const ref = 'e' + n; el.setAttribute('data-markbot-ref', ref);
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'input' ? (el.type || 'text') : tag);
    const label = el.labels && el.labels.length ? el.labels[0].innerText : '';
    let name = (el.getAttribute('aria-label') || label || el.innerText || el.getAttribute('placeholder') ||
      el.title || el.alt || el.getAttribute('name') || (tag === 'input' ? el.value : '') || '').trim().replace(/\s+/g, ' ');
    const editable = tag === 'input' || tag === 'textarea' || tag === 'select';
    let value = editable ? String(el.value || '') : '';
    if (el.type === 'password' && value) value = '***';
    items.push({ref, role, tag, name: name.slice(0, 80), value: value.slice(0, 60),
      href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 120) : '',
      checked: el.checked === true, disabled: el.disabled === true,
      inView: r.bottom > 0 && r.top < innerHeight});
    if (items.length >= maxItems) break;
  }
  const text = document.body ? document.body.innerText : '';
  return {title: document.title, url: location.href, items, total: n, text: text.slice(0, 20000),
    scrollY: Math.round(scrollY), height: document.documentElement.scrollHeight, viewport: innerHeight};
}
"""
ELEMENT_INFO_JS = r"""
(el) => ({tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '').toLowerCase(),
  text: (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').trim().slice(0, 80),
  inForm: !!(el.form || el.closest('form')), href: el.getAttribute('href') || '',
  role: el.getAttribute('role') || ''})
"""


# ------------------------------------------------------------------ 配置
@dataclass
class BrowserSettings:
    enabled: Any = "auto"
    headless: bool = True
    channel: str = ""
    executable_path: str = ""
    user_data_dir: str = ""
    allowed_domains: list[str] = field(default_factory=list)
    blocked_domains: list[str] = field(default_factory=list)
    confirm: str = "risky"
    secrets: list[str] = field(default_factory=list)
    idle_timeout_s: float = 900.0
    navigation_timeout_s: float = 30.0
    proxy: str = ""
    viewport_width: int = 1280
    viewport_height: int = 900
    # 有界面模式用哪个 X 显示（Linux 服务器上常见的是 Xvfb 虚拟屏 ":99"）；空 = 沿用进程的 DISPLAY
    display: str = ""


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(item).strip().lower() for item in value or [] if str(item).strip()]


def parse_settings(config: Any) -> BrowserSettings:
    tools = (config or {}).get("tools") if isinstance(config, dict) else None
    raw = (tools or {}).get("browser") if isinstance(tools, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    web = (tools or {}).get("web") if isinstance(tools, dict) else None
    settings = BrowserSettings()
    for key in ("enabled", "headless", "channel", "executable_path", "user_data_dir", "proxy", "display"):
        if key in raw:
            setattr(settings, key, raw[key])
    for key in ("idle_timeout_s", "navigation_timeout_s"):
        if key in raw:
            setattr(settings, key, float(raw[key]))
    for key in ("viewport_width", "viewport_height"):
        if key in raw:
            setattr(settings, key, int(raw[key]))
    settings.allowed_domains = _list(raw.get("allowed_domains"))
    settings.blocked_domains = _list(raw.get("blocked_domains"))
    settings.secrets = [str(item).strip() for item in raw.get("secrets") or [] if str(item).strip()]
    confirm = str(raw.get("confirm") or "risky").strip().lower()
    settings.confirm = confirm if confirm in {"risky", "all", "none"} else "risky"
    if not settings.proxy and isinstance(web, dict):
        settings.proxy = str(web.get("proxy") or "")
    return settings


def is_enabled(settings: BrowserSettings) -> bool:
    value = settings.enabled
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"true", "1", "yes", "on"}:
            return True
        if value in {"false", "0", "no", "off"}:
            return False
        return importlib.util.find_spec("playwright") is not None
    return bool(value)


def _domain_match(host: str, pattern: str) -> bool:
    pattern = pattern.lstrip("*.").lower()
    host = host.lower()
    return bool(pattern) and (host == pattern or host.endswith("." + pattern))


async def navigation_denial(url: str, settings: BrowserSettings, config: Any) -> str | None:
    """页面导航的地址检查；允许时返回 None。"""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme in {"about", "data", "blob"}:
        return None
    if scheme not in {"http", "https"}:
        return f"浏览器不允许访问 {scheme}: 地址"
    host = (parts.hostname or "").lower()
    if any(_domain_match(host, pattern) for pattern in settings.blocked_domains):
        return f"{host} 在 [tools.browser] blocked_domains 中，禁止访问"
    if settings.allowed_domains and not any(_domain_match(host, p) for p in settings.allowed_domains):
        return f"{host} 不在 [tools.browser] allowed_domains 中，禁止访问"
    reason, _ = await check_target(url, config)
    return reason


def risky_reason(info: dict[str, Any]) -> str | None:
    """点击这个元素是否可能产生后果（提交表单、付款、删除……）。"""
    text = str(info.get("text") or "").lower()
    kind = str(info.get("type") or "").lower()
    tag = str(info.get("tag") or "").lower()
    if kind == "submit" or (tag == "button" and not kind and info.get("inForm")):
        return f"提交表单（{info.get('text') or tag}）"
    if tag == "input" and kind in {"image"} and info.get("inForm"):
        return "提交表单"
    for word in RISKY_WORDS:
        if word in text:
            return f"可能产生后果的操作「{info.get('text')}」"
    if kind == "file":
        return "上传文件"
    return None


def render_snapshot(data: dict[str, Any], *, max_items: int = 150, max_chars: int = 4000, only_in_view: bool = False) -> str:
    lines = [f"页面: {data.get('title') or '(无标题)'}", f"URL: {data.get('url')}"]
    height = int(data.get("height") or 0)
    viewport = int(data.get("viewport") or 0)
    if height and viewport and height > viewport:
        lines.append(f"滚动位置: {data.get('scrollY', 0)}/{max(0, height - viewport)}（可 browser_scroll）")
    items = [item for item in data.get("items") or [] if item.get("inView") or not only_in_view]
    if items:
        lines.append("可交互元素（用 ref 点击/输入；* 表示在当前视口内）:")
        for item in items[:max_items]:
            parts = [f"[{item['ref']}]{'*' if item.get('inView') else ' '} {item.get('role')}"]
            if item.get("name"):
                parts.append(json.dumps(item["name"], ensure_ascii=False))
            if item.get("value"):
                parts.append(f"值={json.dumps(item['value'], ensure_ascii=False)}")
            if item.get("checked"):
                parts.append("已选中")
            if item.get("disabled"):
                parts.append("不可用")
            if item.get("href"):
                parts.append(f"-> {item['href']}")
            lines.append(" ".join(parts))
        total = int(data.get("total") or len(items))
        if total > len(items[:max_items]):
            lines.append(f"…共 {total} 个元素，只列出前 {len(items[:max_items])} 个")
    text = str(data.get("text") or "").strip()
    if text and max_chars > 0:
        clipped = text[:max_chars] + ("…" if len(text) > max_chars else "")
        lines.append(f"页面文本（前 {min(len(text), max_chars)} 字）:\n{clipped}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 运行时
class BrowserError(RuntimeError):
    pass


class _Runtime:
    def __init__(self, settings: BrowserSettings, config: Any) -> None:
        self.settings = settings
        self.config = config
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self.pw: Any = None
        self.context: Any = None
        self.pages: dict[str, list[Any]] = {}
        self.active: dict[str, int] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.downloads: dict[str, list[str]] = {}
        self.blocked: dict[str, str] = {}
        self.dirs: dict[str, Path] = {}
        self.last_used = time.monotonic()
        self._idle_task: asyncio.Task[Any] | None = None

    # ---- 线程与事件循环
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self.loop is not None and self.thread is not None and self.thread.is_alive():
                return self.loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            self.thread = threading.Thread(target=run, daemon=True, name="markbot-browser")
            self.thread.start()
            ready.wait(5)
            self.loop = loop
            return loop

    async def call(self, coro: Any, timeout: float) -> Any:
        """在浏览器线程里执行协程，从调用方的事件循环等待结果。"""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except asyncio.TimeoutError as exc:
            future.cancel()
            raise BrowserError(f"浏览器操作超时（{int(timeout)}s）") from exc

    # ---- 浏览器
    async def _ensure_context(self, home: Path | None) -> Any:
        if self.context is not None:
            return self.context
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserError(
                '未安装 playwright：pip install -e ".[browser]"，然后运行 playwright install chromium'
                '（或在 [tools.browser] 设置 channel = "msedge" / "chrome" 使用本机浏览器）'
            ) from exc
        profile = Path(self.settings.user_data_dir).expanduser() if self.settings.user_data_dir else (
            (home or Path.home() / ".markbot") / "browser-profile"
        )
        profile.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "headless": bool(self.settings.headless),
            "accept_downloads": True,
            "viewport": {"width": self.settings.viewport_width, "height": self.settings.viewport_height},
            "locale": "zh-CN",
        }
        if self.settings.channel:
            kwargs["channel"] = self.settings.channel
        if self.settings.executable_path:
            kwargs["executable_path"] = self.settings.executable_path
        if self.settings.proxy:
            kwargs["proxy"] = {"server": self.settings.proxy}
        if not self.settings.headless and self.settings.display:
            kwargs["env"] = {**os.environ, "DISPLAY": str(self.settings.display)}
        self.pw = await async_playwright().start()
        try:
            self.context = await self.pw.chromium.launch_persistent_context(str(profile), **kwargs)
        except Exception as exc:
            await self.pw.stop()
            self.pw = None
            raise BrowserError(
                f"浏览器启动失败: {exc}。如果提示找不到浏览器，运行 playwright install chromium，"
                '或在 [tools.browser] 设置 channel = "msedge" / "chrome"；'
                "同一个 user_data_dir 同时只能被一个浏览器使用"
            ) from exc
        self.context.on("close", lambda *_: self._on_closed())
        await self.context.route("**/*", self._route)
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.ensure_future(self._idle_watch())
        logger.info("浏览器已启动 profile=%s headless=%s", profile, self.settings.headless)
        return self.context

    def _on_closed(self) -> None:
        self.context = None
        self.pages.clear()
        self.active.clear()

    async def _route(self, route: Any) -> None:
        request = route.request
        try:
            navigation = request.is_navigation_request()
        except Exception:  # noqa: BLE001
            navigation = request.resource_type == "document"
        if navigation:
            reason = await navigation_denial(request.url, self.settings, self.config)
            if reason:
                self._record_block(request, reason)
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    def _record_block(self, request: Any, reason: str) -> None:
        try:
            page = request.frame.page
        except Exception:  # noqa: BLE001
            page = None
        for sid, pages in self.pages.items():
            if page in pages:
                self.blocked[sid] = reason
        logger.warning("浏览器导航被拦截 url=%s reason=%s", str(request.url)[:120], reason)

    async def _idle_watch(self) -> None:
        while True:
            await asyncio.sleep(30)
            idle = time.monotonic() - self.last_used
            if self.context is not None and self.settings.idle_timeout_s > 0 and idle > self.settings.idle_timeout_s:
                logger.info("浏览器空闲 %ds，关闭（登录态保留在用户目录）", int(idle))
                await self.close_all()

    async def close_all(self) -> None:
        context, pw = self.context, self.pw
        self.context = None
        self.pw = None
        self.pages.clear()
        self.active.clear()
        try:
            if context is not None:
                await context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:  # noqa: BLE001
            pass

    async def close_session(self, sid: str) -> int:
        pages = self.pages.pop(sid, [])
        self.active.pop(sid, None)
        for page in pages:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
        return len(pages)

    # ---- 标签页
    def _wire(self, sid: str, page: Any) -> None:
        page.set_default_timeout(self.settings.navigation_timeout_s * 1000)

        def on_download(download: Any) -> None:
            asyncio.ensure_future(self._save_download(sid, download))

        def on_popup(popup: Any) -> None:
            self.pages.setdefault(sid, []).append(popup)
            self.active[sid] = len(self.pages[sid]) - 1
            self._wire(sid, popup)

        page.on("download", on_download)
        page.on("popup", on_popup)

    async def _save_download(self, sid: str, download: Any) -> None:
        directory = self.dirs.get(sid)
        if directory is None:
            return
        target_dir = directory / "downloads"
        target_dir.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[\\/:*?\"<>|]+", "_", download.suggested_filename or "download")[:120]
        target = target_dir / name
        try:
            await download.save_as(str(target))
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载保存失败: %s", exc)
            return
        self.downloads.setdefault(sid, []).append(str(target))

    async def page(self, sid: str, *, create: bool = True, home: Path | None = None) -> Any:
        pages = [page for page in self.pages.get(sid, []) if not page.is_closed()]
        self.pages[sid] = pages
        if not pages:
            if not create:
                return None
            context = await self._ensure_context(home)
            page = await context.new_page()
            self._wire(sid, page)
            pages.append(page)
            self.active[sid] = 0
        index = min(max(0, self.active.get(sid, 0)), len(pages) - 1)
        self.active[sid] = index
        return pages[index]

    async def run(self, sid: str, home: Path | None, fn: Any) -> Any:
        self.last_used = time.monotonic()
        lock = self.locks.setdefault(sid, asyncio.Lock())
        async with lock:
            page = await self.page(sid, home=home)
            self.blocked.pop(sid, None)
            try:
                return await fn(page)
            finally:
                self.last_used = time.monotonic()

    async def snapshot(self, page: Any, max_items: int) -> dict[str, Any]:
        return await page.evaluate(SNAPSHOT_JS, max_items)

    @staticmethod
    async def settle(page: Any) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001 - 页面没有导航时不报错
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:  # noqa: BLE001
            pass


_RUNTIMES: dict[int, _Runtime] = {}


# ------------------------------------------------------------------ 参数
class OpenIn(BaseModel):
    url: str = Field(description="要打开的地址（http/https）", json_schema_extra={"x-markbot-role": "url"})
    new_tab: bool = Field(default=False, description="在新标签页打开")


class SnapshotIn(BaseModel):
    max_items: int = Field(default=150, ge=10, le=500)
    max_chars: int = Field(default=4000, ge=0, le=20000, description="附带的页面文本字数")
    only_in_view: bool = Field(default=False, description="只列出当前视口内的元素")


class RefIn(BaseModel):
    ref: str = Field(description="browser_snapshot 给出的元素编号，如 e12")


class ClickIn(RefIn):
    double: bool = False


class TypeIn(RefIn):
    text: str = Field(default="", description="要输入的文本")
    secret: str = Field(default="", description="改为输入这个密钥（[tools.browser] secrets 里登记的名字），值不会进入上下文")
    submit: bool = Field(default=False, description="输入后按回车提交")
    clear: bool = Field(default=True, description="先清空原有内容")


class SelectIn(RefIn):
    values: list[str] = Field(description="要选中的选项（值或可见文本）", min_length=1)


class PressIn(BaseModel):
    key: str = Field(description="按键，如 Enter、Escape、Tab、PageDown、Control+A")


class ScrollIn(BaseModel):
    direction: Literal["down", "up", "top", "bottom"] = "down"
    pages: float = Field(default=1.0, ge=0.1, le=20)


class WaitIn(BaseModel):
    text: str = Field(default="", description="等待页面出现这段文字")
    seconds: float = Field(default=2.0, ge=0.1, le=60)


class ScreenshotIn(BaseModel):
    full_page: bool = False
    ref: str = Field(default="", description="只截这个元素")


class ExtractIn(BaseModel):
    max_chars: int = Field(default=12000, ge=500, le=100000)


class TabsIn(BaseModel):
    action: Literal["list", "switch", "close"] = "list"
    index: int = Field(default=0, ge=0)


class EvalIn(BaseModel):
    script: str = Field(description="在页面里执行的 JavaScript 表达式或函数，如 () => document.title")


class HandoffIn(BaseModel):
    reason: str = Field(description="需要用户做什么，如：完成登录页的滑块验证")


class UploadIn(RefIn):
    paths: list[str] = Field(description="要上传的文件路径", min_length=1, json_schema_extra={"x-markbot-role": "path"})


class EmptyIn(BaseModel):
    pass


# ------------------------------------------------------------------ 工具
def setup(api: ExtensionAPI) -> None:
    config = getattr(api, "config", None)
    settings = parse_settings(config)
    if not is_enabled(settings):
        return
    runtime = _Runtime(settings, config)
    _RUNTIMES[id(runtime)] = runtime
    op_timeout = settings.navigation_timeout_s + 30

    def _sid(ctx: Any) -> str:
        return str(getattr(getattr(ctx, "session", None), "id", "") or "default")

    def _home(ctx: Any) -> Path | None:
        home = getattr(getattr(getattr(ctx, "env", None), "guard", None), "home", None)
        return Path(home) if home else None

    def _scratch(ctx: Any) -> Path | None:
        home = _home(ctx)
        return home / "sessions" / _sid(ctx) / "scratch" if home else None

    async def _run(ctx: Any, fn: Any, timeout: float | None = None) -> Any:
        scratch = _scratch(ctx)
        if scratch is not None:
            runtime.dirs[_sid(ctx)] = scratch
        return await runtime.call(runtime.run(_sid(ctx), _home(ctx), fn), timeout or op_timeout)

    async def _confirm(ctx: Any, title: str, message: str) -> bool:
        confirm = getattr(ctx, "confirm", None)
        return bool(confirm and await confirm(title, message))

    def _denied(action: str) -> ToolResult:
        return ToolResult.error(
            f"permission denied: 用户未确认{action}（无人值守的任务无法确认；"
            "可在 [tools.browser] confirm 调整确认策略）",
            denied=True,
        )

    async def _snapshot_text(page: Any, sid: str, *, max_items: int = 80, max_chars: int = 2500) -> str:
        await runtime.settle(page)
        data = await runtime.snapshot(page, max_items)
        text = render_snapshot(data, max_items=max_items, max_chars=max_chars)
        notes = []
        if runtime.blocked.get(sid):
            notes.append(f"⚠️ 有导航被拦截：{runtime.blocked[sid]}")
        downloads = runtime.downloads.pop(sid, [])
        if downloads:
            notes.append("已下载：" + "、".join(downloads))
        return "\n".join([text, *notes])

    async def _locate(page: Any, ref: str) -> Any:
        if not _REF.match(ref or ""):
            raise BrowserError(f"无效的元素编号: {ref!r}（应为 browser_snapshot 给出的 e1、e2…）")
        locator = page.locator(f'[data-markbot-ref="{ref}"]')
        if await locator.count() == 0:
            raise BrowserError(f"元素 {ref} 已失效（页面变化了），请重新 browser_snapshot")
        return locator.first

    def _wrap(fn: Any) -> Any:
        async def handler(ctx: Any, args: Any) -> ToolResult:
            try:
                return await fn(ctx, args)
            except BrowserError as exc:
                return ToolResult.error(str(exc))
            except Exception as exc:  # noqa: BLE001 - Playwright 的各种错误统一转成可读结果
                message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
                if "ERR_BLOCKED_BY_CLIENT" in str(exc):
                    reason = runtime.blocked.get(_sid(ctx)) or "地址不允许访问"
                    return ToolResult.error(f"导航被拦截：{reason}", denied=True)
                return ToolResult.error(f"浏览器操作失败: {type(exc).__name__}: {message[:300]}")

        return handler

    # ---- 导航与读取
    async def browser_open(ctx: Any, args: OpenIn) -> ToolResult:
        url = args.url.strip()
        if "://" not in url and not url.startswith("about:"):
            url = "https://" + url
        reason = await navigation_denial(url, settings, config)
        if reason:
            return ToolResult.error(reason, denied=True)
        sid = _sid(ctx)

        async def go(page: Any) -> str:
            if args.new_tab:
                page = await page.context.new_page()
                runtime._wire(sid, page)
                runtime.pages.setdefault(sid, []).append(page)
                runtime.active[sid] = len(runtime.pages[sid]) - 1
            response = await page.goto(url, wait_until="domcontentloaded")
            status = getattr(response, "status", None)
            text = await _snapshot_text(page, sid)
            return (f"HTTP {status}\n" if status and status >= 400 else "") + text

        return ToolResult(content=await _run(ctx, go))

    async def browser_snapshot(ctx: Any, args: SnapshotIn) -> ToolResult:
        async def snap(page: Any) -> str:
            data = await runtime.snapshot(page, args.max_items)
            return render_snapshot(
                data, max_items=args.max_items, max_chars=args.max_chars, only_in_view=args.only_in_view
            )

        return ToolResult(content=await _run(ctx, snap))

    async def browser_extract(ctx: Any, args: ExtractIn) -> ToolResult:
        async def extract(page: Any) -> tuple[str, str, str]:
            return await page.content(), page.url, await page.title()

        html, url, title = await _run(ctx, extract)
        text = html_to_markdown(html, url)
        clipped = text[: args.max_chars] + ("\n…（已截断）" if len(text) > args.max_chars else "")
        return ToolResult(content=f"# {title}\n{url}\n\n{clipped}", details={"url": url, "chars": len(text)})

    # ---- 交互
    async def browser_click(ctx: Any, args: ClickIn) -> ToolResult:
        sid = _sid(ctx)

        async def inspect(page: Any) -> dict[str, Any]:
            locator = await _locate(page, args.ref)
            return await locator.evaluate(ELEMENT_INFO_JS)

        info = await _run(ctx, inspect)
        risk = risky_reason(info)
        if settings.confirm == "all" or (settings.confirm == "risky" and risk):
            detail = risk or f"点击「{info.get('text') or args.ref}」"
            if not await _confirm(ctx, "确认浏览器操作", f"{detail}\n元素: {args.ref} {info.get('tag')} {info.get('text')}"):
                return _denied(detail)

        async def click(page: Any) -> str:
            locator = await _locate(page, args.ref)
            if args.double:
                await locator.dblclick()
            else:
                await locator.click()
            return await _snapshot_text(page, sid)

        return ToolResult(content=await _run(ctx, click))

    async def browser_type(ctx: Any, args: TypeIn) -> ToolResult:
        sid = _sid(ctx)
        value = args.text
        masked = False
        if args.secret:
            if args.secret not in settings.secrets:
                return ToolResult.error(
                    f"密钥 {args.secret} 没有在 [tools.browser] secrets 中登记，不能输入到网页", denied=True
                )
            try:
                value = ctx.secret(args.secret)
            except KeyError:
                return ToolResult.error(f"未配置密钥: {args.secret}")
            masked = True
        if settings.confirm == "all" or (settings.confirm == "risky" and args.submit):
            shown = f"密钥 {args.secret}" if masked else json.dumps(value[:200], ensure_ascii=False)
            if not await _confirm(ctx, "确认浏览器输入", f"在 {args.ref} 输入 {shown}" + ("，然后回车提交" if args.submit else "")):
                return _denied("输入并提交" if args.submit else "输入")

        async def type_(page: Any) -> str:
            locator = await _locate(page, args.ref)
            if args.clear:
                await locator.fill(value)
            else:
                await locator.press_sequentially(value)
            if args.submit:
                await locator.press("Enter")
            return await _snapshot_text(page, sid)

        result = await _run(ctx, type_)
        return ToolResult(content=f"已输入{'（密钥，已隐藏）' if masked else ''}\n{result}")

    async def browser_select(ctx: Any, args: SelectIn) -> ToolResult:
        sid = _sid(ctx)
        if settings.confirm == "all" and not await _confirm(ctx, "确认浏览器操作", f"在 {args.ref} 选择 {args.values}"):
            return _denied("选择")

        async def select(page: Any) -> str:
            locator = await _locate(page, args.ref)
            try:
                await locator.select_option(args.values)
            except Exception:  # noqa: BLE001 - 按可见文本再试一次
                await locator.select_option(label=args.values)
            return await _snapshot_text(page, sid)

        return ToolResult(content=await _run(ctx, select))

    async def browser_press(ctx: Any, args: PressIn) -> ToolResult:
        sid = _sid(ctx)
        key = args.key.strip()
        if not re.match(r"^[A-Za-z0-9+_ ]{1,40}$", key):
            return ToolResult.error(f"无效的按键: {key!r}")
        if settings.confirm == "all" or (settings.confirm == "risky" and key.lower().endswith("enter")):
            if not await _confirm(ctx, "确认浏览器操作", f"按下 {key}（可能提交表单）"):
                return _denied(f"按 {key}")

        async def press(page: Any) -> str:
            await page.keyboard.press(key)
            return await _snapshot_text(page, sid)

        return ToolResult(content=await _run(ctx, press))

    async def browser_scroll(ctx: Any, args: ScrollIn) -> ToolResult:
        sid = _sid(ctx)

        async def scroll(page: Any) -> str:
            if args.direction == "top":
                await page.evaluate("() => window.scrollTo(0, 0)")
            elif args.direction == "bottom":
                await page.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
            else:
                sign = 1 if args.direction == "down" else -1
                await page.evaluate("(d) => window.scrollBy(0, d * window.innerHeight)", sign * args.pages)
            await page.wait_for_timeout(400)
            data = await runtime.snapshot(page, 150)
            return render_snapshot(data, max_items=80, max_chars=2500, only_in_view=True) + (
                f"\n⚠️ {runtime.blocked[sid]}" if runtime.blocked.get(sid) else ""
            )

        return ToolResult(content=await _run(ctx, scroll))

    async def browser_wait(ctx: Any, args: WaitIn) -> ToolResult:
        sid = _sid(ctx)

        async def wait(page: Any) -> str:
            if args.text:
                try:
                    await page.get_by_text(args.text).first.wait_for(timeout=args.seconds * 1000)
                    found = "已出现"
                except Exception:  # noqa: BLE001
                    found = "等待超时，仍未出现"
                return f"文字「{args.text}」{found}\n" + await _snapshot_text(page, sid)
            await page.wait_for_timeout(args.seconds * 1000)
            return await _snapshot_text(page, sid)

        return ToolResult(content=await _run(ctx, wait, timeout=args.seconds + op_timeout))

    async def browser_back(ctx: Any, args: EmptyIn) -> ToolResult:
        sid = _sid(ctx)

        async def back(page: Any) -> str:
            await page.go_back(wait_until="domcontentloaded")
            return await _snapshot_text(page, sid)

        return ToolResult(content=await _run(ctx, back))

    async def browser_upload(ctx: Any, args: UploadIn) -> ToolResult:
        sid = _sid(ctx)
        files = []
        for raw in args.paths:
            path = ctx.env.resolve(raw)
            if not path.is_file():
                return ToolResult.error(f"文件不存在: {raw}")
            files.append(str(path))
        if settings.confirm != "none" and not await _confirm(
            ctx, "确认上传文件", f"把 {len(files)} 个文件上传到当前网页：\n" + "\n".join(files)
        ):
            return _denied("上传文件")

        async def upload(page: Any) -> str:
            locator = await _locate(page, args.ref)
            await locator.set_input_files(files)
            return await _snapshot_text(page, sid)

        return ToolResult(content=await _run(ctx, upload))

    async def browser_screenshot(ctx: Any, args: ScreenshotIn) -> ToolResult:
        scratch = _scratch(ctx)
        if scratch is None:
            return ToolResult.error("当前环境没有会话临时目录，无法保存截图")
        target = scratch / "screenshots" / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"

        async def shot(page: Any) -> str:
            target.parent.mkdir(parents=True, exist_ok=True)
            if args.ref:
                await (await _locate(page, args.ref)).screenshot(path=str(target))
            else:
                await page.screenshot(path=str(target), full_page=args.full_page)
            return page.url

        url = await _run(ctx, shot)
        return ToolResult(
            content=f"截图已保存并附上：{target}（{url}）。需要发给用户时在回复里写 ![截图]({target.as_posix()})",
            details={"path": str(target), "url": url, "images": [str(target)]},
        )

    async def browser_tabs(ctx: Any, args: TabsIn) -> ToolResult:
        sid = _sid(ctx)

        async def tabs(page: Any) -> str:
            pages = runtime.pages.get(sid, [])
            if args.action in {"switch", "close"}:
                if args.index >= len(pages):
                    raise BrowserError(f"没有第 {args.index} 个标签页")
                if args.action == "switch":
                    runtime.active[sid] = args.index
                    await pages[args.index].bring_to_front()
                else:
                    await pages[args.index].close()
                    pages.pop(args.index)
                    runtime.active[sid] = max(0, min(runtime.active.get(sid, 0), len(pages) - 1))
            lines = []
            for index, item in enumerate(runtime.pages.get(sid, [])):
                mark = "👉" if index == runtime.active.get(sid, 0) else "  "
                lines.append(f"{mark} [{index}] {await item.title()} {item.url}")
            return "\n".join(lines) or "（没有打开的标签页）"

        return ToolResult(content=await _run(ctx, tabs))

    async def browser_eval(ctx: Any, args: EvalIn) -> ToolResult:
        if settings.confirm != "none" and not await _confirm(
            ctx, "确认在网页中执行脚本", args.script[:1500]
        ):
            return _denied("执行脚本")

        async def run_js(page: Any) -> Any:
            return await page.evaluate(args.script)

        value = await _run(ctx, run_js)
        text = json.dumps(value, ensure_ascii=False, default=str, indent=1)
        return ToolResult(content=text[:8000] + ("\n…（已截断）" if len(text) > 8000 else ""))

    async def browser_handoff(ctx: Any, args: HandoffIn) -> ToolResult:
        if settings.headless:
            return ToolResult.error(
                "当前是无界面模式（[tools.browser] headless = true），用户看不到浏览器窗口，无法接手。"
                "需要人工处理验证码/扫码时把 headless 设为 false"
            )
        shot = await browser_screenshot(ctx, ScreenshotIn())
        sid = _sid(ctx)

        async def front(page: Any) -> str:
            await page.bring_to_front()
            return page.url

        url = await _run(ctx, front)
        message = (
            f"{args.reason}\n请在运行 MarkBot 的电脑上的浏览器窗口里完成（{url}），完成后回复 1 继续，回复 2 放弃。"
            + (f"\n当前截图：{shot.details.get('path')}" if not shot.is_error else "")
        )
        if not await _confirm(ctx, "需要你在浏览器里操作", message):
            return ToolResult.error("用户没有完成浏览器里的操作（或无人值守），已停止", denied=True)

        async def after(page: Any) -> str:
            return await _snapshot_text(page, sid)

        return ToolResult(content="用户已完成操作。\n" + await _run(ctx, after))

    async def browser_close(ctx: Any, args: EmptyIn) -> ToolResult:
        count = await runtime.call(runtime.close_session(_sid(ctx)), 30)
        return ToolResult(content=f"已关闭本会话的 {count} 个标签页（浏览器登录态保留）")

    async def _on_session_end(*_args: Any, **kwargs: Any) -> None:
        session = kwargs.get("session")
        if session is not None and runtime.loop is not None:
            try:
                await runtime.call(runtime.close_session(str(getattr(session, "id", ""))), 30)
            except Exception:  # noqa: BLE001
                pass

    async def _on_shutdown(*_args: Any, **_kwargs: Any) -> None:
        if runtime.loop is not None:
            try:
                await runtime.call(runtime.close_all(), 30)
            except Exception:  # noqa: BLE001
                pass

    api.on(EventName.SESSION_END, _on_session_end)
    api.on(EventName.SHUTDOWN, _on_shutdown)

    caps = frozenset({"browser:*"})
    tools: list[tuple[str, str, Any, Any]] = [
        ("browser_open", "用浏览器打开网页（执行 JavaScript、保留登录态），返回可交互元素编号与页面文本", OpenIn, browser_open),
        ("browser_snapshot", "查看当前页面：可交互元素（e1、e2… 编号）与页面文本", SnapshotIn, browser_snapshot),
        ("browser_extract", "提取当前页面正文为 Markdown（适合阅读文章、表格、搜索结果）", ExtractIn, browser_extract),
        ("browser_click", "点击元素（用 snapshot 的编号）；提交、付款、删除等操作需要用户确认", ClickIn, browser_click),
        ("browser_type", "在输入框输入文本（可选回车提交）；密码等用 secret 参数", TypeIn, browser_type),
        ("browser_select", "在下拉框中选择选项", SelectIn, browser_select),
        ("browser_press", "按键（Enter、Escape、Tab、PageDown 等）", PressIn, browser_press),
        ("browser_scroll", "滚动页面并返回视口内的元素", ScrollIn, browser_scroll),
        ("browser_wait", "等待页面出现某段文字，或等待几秒", WaitIn, browser_wait),
        ("browser_back", "返回上一页", EmptyIn, browser_back),
        ("browser_upload", "向文件输入框上传本地文件（需要用户确认）", UploadIn, browser_upload),
        ("browser_screenshot", "截取当前页面或某个元素（图片会附给模型查看，也可以发给用户）", ScreenshotIn, browser_screenshot),
        ("browser_tabs", "列出、切换或关闭本会话的标签页", TabsIn, browser_tabs),
        ("browser_eval", "在页面里执行 JavaScript 并返回结果（需要用户确认）", EvalIn, browser_eval),
        ("browser_handoff", "遇到验证码、扫码登录等需要人工处理时，请用户在浏览器窗口里完成", HandoffIn, browser_handoff),
        ("browser_close", "关闭本会话打开的标签页", EmptyIn, browser_close),
    ]
    mode = "无头模式" if settings.headless else (
        f"有界面模式（DISPLAY={settings.display}）" if settings.display else "有界面模式"
    )
    for name, description, params, fn in tools:
        if name == "browser_open":
            description = (
                f"{description}。当前为{mode}，由 [tools.browser] headless / display 配置决定；"
                "无头模式被网站拦截时请用户修改配置并重启"
            )
        api.register_tool(
            Tool(
                name=name,
                description=description,
                params=params,
                handler=_wrap(fn),
                capabilities=caps | {"fs.read:./**"} if name == "browser_upload" else caps,
                timeout_s=op_timeout + 600 if name in {"browser_handoff", "browser_wait"} else op_timeout + 10,
                concurrency_safe=False,
            )
        )


__all__ = [
    "BrowserSettings",
    "is_enabled",
    "navigation_denial",
    "parse_settings",
    "render_snapshot",
    "risky_reason",
    "setup",
]
