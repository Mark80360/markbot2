"""网络读取工具。

安全（ADR 0009）：
- 默认禁止访问非公网地址（私网、回环、链路本地、保留地址等），防止模型被网页内容诱导去
  探测本机/局域网服务（SSRF）。确需访问的内网服务写在 ``[tools.web] allow_private``。
- 每一跳重定向都重新检查目标地址，避免「公网地址 302 到内网」绕过。
- ``verify_tls=false`` 只对 ``allow_private`` 白名单里的主机生效（局域网自签证书）；
  公网地址一律校验证书。

内容（ADR 0014）：HTML 默认提取正文并转成 Markdown（标题、列表、带绝对地址的链接，去掉导航/页脚/
脚本）；PDF 在安装了 pypdf 时提取文本。读超时与 502/503/504 自动重试一次；成功结果缓存 5 分钟。
同目录的 ``search.py`` 提供 ``web_search``。
"""

from __future__ import annotations

import asyncio
import html as htmllib
import io
import ipaddress
import re
import socket
import time
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.extensions.web.search import register_search

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_SPACES = re.compile(r"\n{3,}")
_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
MAX_REDIRECTS = 5
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 MarkBot/0.1"
)
# 反爬/验证码页面特征：命中时提示改用浏览器
_BLOCK_MARKERS = (
    "captcha", "验证码", "人机验证", "安全验证", "访问验证", "are you a robot",
    "cf-browser-verification", "challenge-platform", "access denied",
)
_TEXT_TYPES = ("text/", "json", "xml", "javascript", "x-www-form-urlencoded", "csv", "yaml")
RETRY_STATUS = frozenset({502, 503, 504})
RETRY_DELAY_S = 0.5
CACHE_TTL_S = 300.0
PDF_MAX_PAGES = 50


class FetchIn(BaseModel):
    url: str = Field(
        description="http/https 地址；省略协议头时先试 https://，失败再试 http://",
        json_schema_extra={"x-markbot-role": "url"},
    )
    max_chars: int = Field(default=20000, ge=100, le=200000)
    timeout_s: float = Field(default=30.0, ge=1, le=120)
    verify_tls: bool = Field(
        default=True,
        description="是否校验 HTTPS 证书；只对 [tools.web] allow_private 里的内网主机可以设为 false",
    )
    format: Literal["markdown", "text", "html"] = Field(
        default="markdown",
        description="markdown：提取正文并保留标题、列表与链接（默认）；text：全部可见文本；html：原始 HTML",
    )


def _describe(exc: BaseException) -> str:
    """异常描述：``httpx.ReadTimeout`` 等的 ``str()`` 为空，必须带上类型名。"""
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _normalize_url(raw: str) -> str | None:
    url = raw.strip()
    if _SCHEME.match(url):
        return url
    if "://" in url:
        return None
    return f"https://{url}"


def _to_text(body: str) -> str:
    body = _SCRIPT.sub("", body)
    text = htmllib.unescape(_TAG.sub(" ", body))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return _SPACES.sub("\n\n", text).strip()


# ------------------------------------------------------------------ 正文提取（ADR 0014）
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_DROP = re.compile(
    r"<(script|style|noscript|svg|template|iframe|canvas|head|title)\b[^>]*>.*?</\1\s*>", re.I | re.S
)
_CHROME = re.compile(r"<(nav|header|footer|aside|form|menu|dialog)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.I | re.S)
_LINK = re.compile(
    r"""<a\b[^>]*?href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))[^>]*>(.*?)</a\s*>""", re.I | re.S
)
_BLOCK_OPEN = re.compile(r"<(p|div|section|article|main|tr|blockquote|pre|table|ul|ol|dl|dt|dd|figure)\b[^>]*>", re.I)
_BLOCK_CLOSE = re.compile(r"</(p|div|section|article|main|tr|blockquote|pre|table|ul|ol|dl|dt|dd|figure)\s*>", re.I)
MAIN_MIN_CHARS = 200


def _inline(fragment: str) -> str:
    return " ".join(htmllib.unescape(_TAG.sub(" ", fragment)).split())


def _main_html(body: str) -> str:
    """优先取正文最长的 <article> / <main>；没有时去掉导航、页眉页脚、侧栏与表单后用整页。"""
    for tag in ("article", "main"):
        blocks = re.findall(rf"<{tag}\b[^>]*>(.*?)</{tag}\s*>", body, re.I | re.S)
        if blocks:
            best = max(blocks, key=lambda block: len(_inline(block)))
            if len(_inline(best)) >= MAIN_MIN_CHARS:
                return best
    return _CHROME.sub(" ", body)


def html_to_markdown(raw: str, base_url: str = "") -> str:
    """把网页转成便于模型阅读的 Markdown：正文、标题层级、列表与绝对地址的链接。"""
    text = _DROP.sub(" ", _COMMENT.sub("", raw or ""))
    text = _main_html(text)

    def heading(match: re.Match[str]) -> str:
        inner = _inline(match.group(2))
        return f"\n\n{'#' * int(match.group(1))} {inner}\n\n" if inner else "\n\n"

    def link(match: re.Match[str]) -> str:
        href = htmllib.unescape(match.group(1) or match.group(2) or match.group(3) or "").strip()
        label = _inline(match.group(4))
        if not label:
            return " "
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            return label
        return f"[{label}]({urljoin(base_url, href) if base_url else href})"

    text = _HEADING.sub(heading, text)
    text = _LINK.sub(link, text)
    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</t[dh]\s*>", " ", text, flags=re.I)
    text = _BLOCK_OPEN.sub("\n", text)
    text = _BLOCK_CLOSE.sub("\n\n", text)
    text = htmllib.unescape(_TAG.sub("", text))
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in text.splitlines()]
    lines = [line for line in lines if line not in ("-",)]
    return _SPACES.sub("\n\n", "\n".join(lines)).strip()


def _pdf_text(data: bytes) -> tuple[str, int] | None:
    """有 pypdf 时提取 PDF 文本；没有安装或解析失败返回 None。"""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages[:PDF_MAX_PAGES]
        text = "\n\n".join((page.extract_text() or "").strip() for page in pages)
    except Exception:  # noqa: BLE001 - 损坏的 PDF 按二进制处理
        return None
    return (text.strip(), len(reader.pages)) if text.strip() else None


def _title(body: str) -> str:
    match = _TITLE.search(body or "")
    return re.sub(r"\s+", " ", match.group(1)).strip()[:200] if match else ""


def _looks_blocked(status: int, body: str) -> bool:
    if status in (401, 403, 429, 503):
        return True
    head = (body or "")[:5000].lower()
    return any(marker in head for marker in _BLOCK_MARKERS)


# ------------------------------------------------------------------ SSRF 防护
def _settings(config: Any) -> dict[str, Any]:
    tools = (config or {}).get("tools") if isinstance(config, dict) else None
    web = (tools or {}).get("web") if isinstance(tools, dict) else None
    return web if isinstance(web, dict) else {}


def _allow_entries(config: Any) -> list[str]:
    raw = _settings(config).get("allow_private") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip().lower() for item in raw if str(item).strip()]


def _host_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    return host, port


def _entry_matches(entry: str, host: str, port: int, ips: list[ipaddress._BaseAddress]) -> bool:
    """白名单条目：``host``、``host:port``、``[v6]:port``、CIDR（``192.168.71.0/24``）。"""
    want_port: int | None = None
    name = entry
    if entry.startswith("["):
        name, _, rest = entry[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            want_port = int(rest[1:])
    elif entry.count(":") == 1:
        name, _, port_text = entry.partition(":")
        if port_text.isdigit():
            want_port = int(port_text)
        else:
            name = entry
    if want_port is not None and want_port != port:
        return False
    if "/" in name:
        try:
            network = ipaddress.ip_network(name, strict=False)
        except ValueError:
            return False
        return any(ip in network for ip in ips)
    return name == host


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


async def _resolve_host(host: str, port: int) -> list[ipaddress._BaseAddress]:
    """解析主机名得到全部地址；解析失败返回空列表（随后由 HTTP 请求自行报错）。"""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=10
        )
    except (OSError, asyncio.TimeoutError, UnicodeError):
        return []
    out: list[ipaddress._BaseAddress] = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
        except ValueError:
            continue
    return out


async def check_target(url: str, config: Any) -> tuple[str | None, bool]:
    """返回 ``(拒绝原因, 是否命中内网白名单)``；允许访问时拒绝原因为 ``None``。"""
    host, port = _host_port(url)
    if not host:
        return "URL 缺少主机名", False
    ips = await _resolve_host(host, port)
    allowed = any(_entry_matches(entry, host, port, ips) for entry in _allow_entries(config))
    if allowed:
        return None, True
    private = [str(ip) for ip in ips if not _is_public(ip)]
    if private or host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        shown = ", ".join(private[:3]) or host
        return (
            f"禁止访问非公网地址 {host}（{shown}）。确需访问的内网服务请在 config.toml 的 "
            f'[tools.web] allow_private 中登记，例如 allow_private = ["{host}:{port}"]'
        ), False
    return None, False


# ------------------------------------------------------------------ 工具
def _client_kwargs(config: Any, timeout_s: float, verify: bool) -> dict[str, Any]:
    settings = _settings(config)
    headers = {
        "User-Agent": str(settings.get("user_agent") or DEFAULT_USER_AGENT),
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    kwargs: dict[str, Any] = {
        "timeout": timeout_s,
        # 重定向由工具逐跳处理，每一跳都要过 SSRF 检查
        "follow_redirects": False,
        "verify": verify,
        "headers": headers,
    }
    proxy = str(settings.get("proxy") or "").strip()
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def _render(resp: Any, url: str, max_chars: int, fmt: str = "markdown") -> ToolResult:
    content_type = str((getattr(resp, "headers", {}) or {}).get("content-type", "")).lower()
    status = int(getattr(resp, "status_code", 0) or 0)
    details: dict[str, Any] = {"url": url, "status": status, "content_type": content_type}
    is_text = not content_type or any(kind in content_type for kind in _TEXT_TYPES)
    if not is_text:
        body = getattr(resp, "content", b"") or b""
        size = len(body) if isinstance(body, (bytes, bytearray)) else 0
        if "pdf" in content_type and isinstance(body, (bytes, bytearray)):
            extracted = _pdf_text(bytes(body))
            if extracted is not None:
                text, pages = extracted
                details.update(pdf_pages=pages, chars=len(text))
                return ToolResult(content=text[:max_chars], details=details)
        details["bytes"] = size
        details["binary"] = True
        return ToolResult(
            content=(
                f"[二进制内容未解码：{content_type or '未知类型'}，{size} 字节] {url}\n"
                "需要其中的文字时请下载后用专门的工具处理（如 PDF 文本提取）。"
            ),
            details=details,
        )
    raw = str(getattr(resp, "text", "") or "")
    is_html = "html" in content_type or raw.lstrip()[:15].lower().startswith(("<!doctype", "<html"))
    if not is_html or fmt == "html":
        cleaned = raw.strip()
    elif fmt == "text":
        cleaned = _to_text(raw)
    else:
        cleaned = html_to_markdown(raw, url)
    if is_html:
        title = _title(raw)
        if title:
            details["title"] = title
    details["chars"] = len(cleaned)
    if _looks_blocked(status, raw):
        details["blocked"] = True
    return ToolResult(content=cleaned[:max_chars], details=details)


def setup(api: ExtensionAPI) -> None:
    config = getattr(api, "config", None)
    # 成功响应的短期缓存：同一回合里反复抓同一页面（或子代理重复抓取）不重复请求
    cache: dict[tuple[str, bool], tuple[float, Any, str]] = {}
    ttl = float(_settings(config).get("cache_ttl_s", CACHE_TTL_S) or 0)

    async def _fetch(url: str, args: FetchIn) -> tuple[Any, str] | ToolResult:
        """带重试与缓存的 _get：读超时、连接中断、502/503/504 重试一次。"""
        key = (url, bool(args.verify_tls))
        hit = cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1], hit[2]
        outcome: tuple[Any, str] | ToolResult | None = None
        for attempt in range(2):
            try:
                outcome = await _get(url, args)
            except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError):
                if attempt == 0:
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue
                raise
            if not isinstance(outcome, ToolResult):
                status = int(getattr(outcome[0], "status_code", 0) or 0)
                if status in RETRY_STATUS and attempt == 0:
                    await asyncio.sleep(RETRY_DELAY_S)
                    continue
                if 200 <= status < 300 and ttl > 0:
                    if len(cache) > 256:
                        cache.clear()
                    cache[key] = (time.monotonic() + ttl, outcome[0], outcome[1])
            break
        assert outcome is not None
        return outcome

    async def _get(url: str, args: FetchIn) -> tuple[Any, str] | ToolResult:
        """逐跳请求并检查目标地址；返回 ``(响应, 最终 URL)`` 或错误结果。"""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            reason, whitelisted = await check_target(current, config)
            if reason:
                return ToolResult.error(reason, denied=True)
            verify = args.verify_tls or not whitelisted
            async with httpx.AsyncClient(**_client_kwargs(config, args.timeout_s, verify)) as client:
                resp = await client.get(current)
            status = int(getattr(resp, "status_code", 0) or 0)
            location = (getattr(resp, "headers", {}) or {}).get("location")
            if 300 <= status < 400 and location:
                nxt = urljoin(current, str(location))
                if not _SCHEME.match(nxt):
                    return ToolResult.error(f"重定向到不支持的地址: {nxt}")
                current = nxt
                continue
            return resp, current
        return ToolResult.error(f"重定向超过 {MAX_REDIRECTS} 次: {url}")

    async def fetch_url(ctx, args: FetchIn) -> ToolResult:
        url = _normalize_url(args.url)
        if url is None:
            return ToolResult.error("只支持 http/https 地址")
        guessed = not _SCHEME.match(args.url.strip())
        notes: list[str] = []
        if not args.verify_tls:
            _, whitelisted = await check_target(url, config)
            if not whitelisted:
                notes.append("（提示：verify_tls=false 只对 [tools.web] allow_private 中的内网主机生效，已按校验证书处理）")
        try:
            outcome = await _fetch(url, args)
        except httpx.HTTPError as exc:
            if guessed and url.startswith("https://"):
                # 省略协议头时先试 https，连接失败再退回 http
                fallback = "http://" + url[len("https://"):]
                try:
                    outcome = await _fetch(fallback, args)
                    url = fallback
                except httpx.HTTPError as exc2:
                    return ToolResult.error(f"请求失败: {_describe(exc2)}（https 也失败: {_describe(exc)}）")
            else:
                hint = (
                    "；若目标为局域网自签证书，请先把主机加入 [tools.web] allow_private，再设置 verify_tls=false"
                    if "CERTIFICATE_VERIFY_FAILED" in str(exc)
                    else ""
                )
                return ToolResult.error(f"请求失败: {_describe(exc)}{hint}")
        if isinstance(outcome, ToolResult):
            return outcome
        resp, final_url = outcome
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            body = str(getattr(resp, "text", "") or "")
            blocked = _looks_blocked(status, body)
            hint = "；该站点可能有反爬/登录限制，可改用浏览器工具访问" if blocked else ""
            return ToolResult(
                content=f"HTTP {status}: {final_url}{hint}",
                is_error=True,
                details={"url": final_url, "status": status, "blocked": blocked},
            )
        result = _render(resp, final_url, args.max_chars, args.format)
        if result.details.get("blocked"):
            notes.append("（提示：页面像是验证码/反爬拦截页，内容可能不是目标页面；可改用浏览器工具）")
        if notes:
            result = ToolResult(
                content=result.content + "\n\n" + "\n".join(notes),
                is_error=result.is_error,
                details=result.details,
            )
        return result

    api.register_tool(
        Tool(
            name="fetch_url",
            description=(
                "抓取网页并提取正文（Markdown，保留标题、列表与链接；不执行 JavaScript），PDF 会提取文本。"
                "默认禁止访问内网地址；遇到需要登录、JS 渲染或反爬拦截的页面请改用浏览器工具"
            ),
            params=FetchIn,
            handler=fetch_url,
            capabilities=frozenset({"net:*"}),
            timeout_s=90.0,
        )
    )
    register_search(api)


__all__ = ["check_target", "html_to_markdown", "setup"]
