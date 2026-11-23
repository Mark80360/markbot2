"""网络搜索：``web_search``（ADR 0014）。

后端（``[tools.search] backend``）：

| backend | 需要 | 说明 |
|---|---|---|
| ``searxng`` | ``searxng_url`` | 自建 SearXNG（需在实例里开启 json 格式），推荐 |
| ``tavily`` | 密钥 ``TAVILY_API_KEY`` | 面向 Agent 的搜索 API |
| ``brave`` | 密钥 ``BRAVE_API_KEY`` | Brave Search API |
| ``bing`` | 密钥 ``BING_API_KEY`` | Bing Web Search API |
| ``bing_html`` | 无 | 解析 cn.bing.com 结果页（国内网络可用，页面改版可能失效） |
| ``duckduckgo`` | 无 | 解析 html.duckduckgo.com 结果页（国内通常需要代理） |
| ``auto`` | — | 默认：已配置的 searxng / 有密钥的 API 优先，再依次尝试 bing_html、duckduckgo |

密钥放在 ``[secrets]``，名字可用 ``<backend>_api_key_secret`` 改；工具只在请求时取值，不进入模型上下文。
代理沿用 ``[tools.search] proxy`` 或 ``[tools.web] proxy``。结果缓存 10 分钟。
"""

from __future__ import annotations

import html as htmllib
import re
import time
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import BaseModel, Field

from markbot.api import Tool, ToolResult

BACKENDS = ("searxng", "tavily", "brave", "bing", "bing_html", "duckduckgo")
KEY_DEFAULTS = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY", "bing": "BING_API_KEY"}
CACHE_TTL_S = 600.0
SNIPPET_CHARS = 300
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TAG = re.compile(r"<[^>]+>")


class SearchIn(BaseModel):
    query: str = Field(description="搜索关键词（简短、具体）", min_length=1, max_length=400)
    max_results: int = Field(default=8, ge=1, le=20)
    recency: Literal["", "day", "week", "month", "year"] = Field(
        default="", description="只看最近一天/周/月/年的结果（新闻、动态类问题建议设置）"
    )
    site: str = Field(default="", description="只搜索这个网站，例如 github.com")


def _text(fragment: str) -> str:
    return " ".join(htmllib.unescape(_TAG.sub(" ", fragment or "")).split())


def _clip(text: str, limit: int = SNIPPET_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _result(title: Any, url: Any, snippet: Any = "", date: Any = "") -> dict[str, str]:
    return {
        "title": _clip(_text(str(title or "")), 200),
        "url": str(url or "").strip(),
        "snippet": _clip(_text(str(snippet or ""))),
        "date": str(date or "")[:20],
    }


def _valid(results: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in results:
        url = item["url"]
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        out.append(item)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ 解析（纯函数）
def parse_searxng(data: dict[str, Any]) -> list[dict[str, str]]:
    return [
        _result(item.get("title"), item.get("url"), item.get("content"), item.get("publishedDate"))
        for item in data.get("results") or []
        if isinstance(item, dict)
    ]


def parse_tavily(data: dict[str, Any]) -> list[dict[str, str]]:
    return [
        _result(item.get("title"), item.get("url"), item.get("content"), item.get("published_date"))
        for item in data.get("results") or []
        if isinstance(item, dict)
    ]


def parse_brave(data: dict[str, Any]) -> list[dict[str, str]]:
    web = data.get("web") or {}
    return [
        _result(item.get("title"), item.get("url"), item.get("description"), item.get("age") or item.get("page_age"))
        for item in web.get("results") or []
        if isinstance(item, dict)
    ]


def parse_bing(data: dict[str, Any]) -> list[dict[str, str]]:
    pages = data.get("webPages") or {}
    return [
        _result(item.get("name"), item.get("url"), item.get("snippet"), item.get("dateLastCrawled"))
        for item in pages.get("value") or []
        if isinstance(item, dict)
    ]


_DDG_LINK = re.compile(r"<a\b([^>]*class=\"[^\"]*result__a[^\"]*\"[^>]*)>(.*?)</a>", re.I | re.S)
_DDG_SNIPPET = re.compile(r"<(?:a|div)\b[^>]*class=\"[^\"]*result__snippet[^\"]*\"[^>]*>(.*?)</(?:a|div)>", re.I | re.S)
_HREF = re.compile(r"href=\"([^\"]+)\"", re.I)


def _ddg_url(href: str) -> str:
    href = htmllib.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    if "uddg=" in href:
        target = parse_qs(urlsplit(href).query).get("uddg")
        if target:
            return target[0]
    return href


def parse_duckduckgo(page: str) -> list[dict[str, str]]:
    links = _DDG_LINK.findall(page or "")
    snippets = _DDG_SNIPPET.findall(page or "")
    out: list[dict[str, str]] = []
    for index, (attrs, title) in enumerate(links):
        href = _HREF.search(attrs)
        if not href:
            continue
        url = _ddg_url(href.group(1))
        if "duckduckgo.com/y.js" in url:  # 广告
            continue
        out.append(_result(title, url, snippets[index] if index < len(snippets) else ""))
    return out


_BING_ITEM = re.compile(r"<li\b[^>]*class=\"[^\"]*\bb_algo\b[^\"]*\"[^>]*>(.*?)</li>", re.I | re.S)
_BING_TITLE = re.compile(r"<h2\b[^>]*>.*?<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.I | re.S)
_BING_SNIPPET = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)


def parse_bing_html(page: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for block in _BING_ITEM.findall(page or ""):
        title = _BING_TITLE.search(block)
        if not title:
            continue
        snippet = _BING_SNIPPET.search(block)
        out.append(_result(title.group(2), htmllib.unescape(title.group(1)), snippet.group(1) if snippet else ""))
    return out


# ------------------------------------------------------------------ 后端
_RECENCY = {
    "searxng": {"day": "day", "week": "week", "month": "month", "year": "year"},
    "tavily": {"day": "day", "week": "week", "month": "month", "year": "year"},
    "brave": {"day": "pd", "week": "pw", "month": "pm", "year": "py"},
    "bing": {"day": "Day", "week": "Week", "month": "Month"},
    "duckduckgo": {"day": "d", "week": "w", "month": "m", "year": "y"},
    "bing_html": {"day": 'ex1:"ez1"', "week": 'ex1:"ez2"', "month": 'ex1:"ez3"'},
}


def _settings(config: Any) -> dict[str, Any]:
    tools = (config or {}).get("tools") if isinstance(config, dict) else None
    section = (tools or {}).get("search") if isinstance(tools, dict) else None
    return section if isinstance(section, dict) else {}


def _proxy(config: Any) -> str:
    tools = (config or {}).get("tools") if isinstance(config, dict) else None
    web = (tools or {}).get("web") if isinstance(tools, dict) else None
    return str(_settings(config).get("proxy") or (web or {}).get("proxy") or "").strip()


def _key_name(settings: dict[str, Any], backend: str) -> str:
    return str(settings.get(f"{backend}_api_key_secret") or settings.get("api_key_secret") or KEY_DEFAULTS.get(backend, ""))


def _secret(ctx: Any, name: str) -> str:
    getter = getattr(ctx, "secret", None)
    if not name or not callable(getter):
        return ""
    try:
        return str(getter(name) or "")
    except KeyError:
        return ""


def backend_chain(settings: dict[str, Any], has_secret: Any) -> list[str]:
    backend = str(settings.get("backend") or "auto").strip().lower()
    if backend in BACKENDS:
        return [backend]
    chain: list[str] = []
    if settings.get("searxng_url"):
        chain.append("searxng")
    for name in ("tavily", "brave", "bing"):
        if has_secret(_key_name(settings, name)):
            chain.append(name)
    return [*chain, "bing_html", "duckduckgo"]


async def _search(
    client: httpx.AsyncClient, backend: str, query: str, limit: int, recency: str,
    settings: dict[str, Any], key: str,
) -> list[dict[str, str]]:
    window = _RECENCY.get(backend, {}).get(recency, "") if recency else ""
    if backend == "searxng":
        base = str(settings.get("searxng_url") or "").rstrip("/")
        params = {"q": query, "format": "json", "language": str(settings.get("language") or "zh-CN")}
        if window:
            params["time_range"] = window
        resp = await client.get(f"{base}/search", params=params)
        resp.raise_for_status()
        return parse_searxng(resp.json())
    if backend == "tavily":
        body: dict[str, Any] = {"query": query, "max_results": limit}
        if window:
            body["time_range"] = window
        resp = await client.post(
            "https://api.tavily.com/search", json=body, headers={"Authorization": f"Bearer {key}"}
        )
        resp.raise_for_status()
        return parse_tavily(resp.json())
    if backend == "brave":
        params = {"q": query, "count": min(limit, 20)}
        if window:
            params["freshness"] = window
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params=params,
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        resp.raise_for_status()
        return parse_brave(resp.json())
    if backend == "bing":
        params = {"q": query, "count": min(limit, 50), "mkt": str(settings.get("market") or "zh-CN")}
        if window:
            params["freshness"] = window
        resp = await client.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params=params,
            headers={"Ocp-Apim-Subscription-Key": key},
        )
        resp.raise_for_status()
        return parse_bing(resp.json())
    if backend == "bing_html":
        params = {"q": query, "count": min(limit, 30)}
        if window:
            params["filters"] = window
        resp = await client.get("https://cn.bing.com/search", params=params)
        resp.raise_for_status()
        return parse_bing_html(resp.text)
    if backend == "duckduckgo":
        params = {"q": query, "kl": str(settings.get("region") or "cn-zh")}
        if window:
            params["df"] = window
        resp = await client.get("https://html.duckduckgo.com/html/", params=params)
        resp.raise_for_status()
        return parse_duckduckgo(resp.text)
    raise ValueError(f"未知搜索后端: {backend}")


def render(results: list[dict[str, str]], backend: str, query: str) -> str:
    lines = [f"搜索「{query}」（{backend}，{len(results)} 条）："]
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. {item['title'] or '(无标题)'}")
        lines.append(f"   {item['url']}")
        meta = " · ".join(part for part in (item.get("date"), item.get("snippet")) if part)
        if meta:
            lines.append(f"   {meta}")
    lines.append("需要细节时用 fetch_url 打开最相关的链接；引用事实时附上来源链接。")
    return "\n".join(lines)


def register_search(api: Any) -> None:
    config = getattr(api, "config", None)
    settings = _settings(config)
    if settings.get("enabled") is False:
        return
    cache: dict[tuple[str, ...], tuple[float, list[dict[str, str]], str]] = {}

    async def web_search(ctx, args: SearchIn) -> ToolResult:
        query = args.query.strip()
        if args.site.strip():
            query = f"site:{args.site.strip()} {query}"
        chain = backend_chain(settings, lambda name: bool(_secret(ctx, name)))
        key = (query, str(args.max_results), args.recency, ",".join(chain))
        hit = cache.get(key)
        if hit and hit[0] > time.monotonic():
            return ToolResult(content=render(hit[1], hit[2], query), details={"backend": hit[2], "results": hit[1], "cached": True})
        client_kwargs: dict[str, Any] = {
            "timeout": float(settings.get("timeout_s") or 20),
            "follow_redirects": True,
            "headers": {"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        }
        proxy = _proxy(config)
        if proxy:
            client_kwargs["proxy"] = proxy
        errors: list[str] = []
        async with httpx.AsyncClient(**client_kwargs) as client:
            for backend in chain:
                secret_value = _secret(ctx, _key_name(settings, backend)) if backend in KEY_DEFAULTS else ""
                if backend in KEY_DEFAULTS and not secret_value:
                    errors.append(f"{backend}: 未配置密钥 {_key_name(settings, backend)}")
                    continue
                try:
                    found = await _search(
                        client, backend, query, args.max_results, args.recency, settings, secret_value
                    )
                except Exception as exc:  # noqa: BLE001 - 换下一个后端
                    detail = str(exc).strip()
                    errors.append(f"{backend}: {type(exc).__name__}{(': ' + detail[:150]) if detail else ''}")
                    continue
                results = _valid(found, args.max_results)
                if results:
                    if len(cache) > 128:
                        cache.clear()
                    cache[key] = (time.monotonic() + CACHE_TTL_S, results, backend)
                    return ToolResult(
                        content=render(results, backend, query),
                        details={"backend": backend, "results": results, "errors": errors},
                    )
                errors.append(f"{backend}: 没有结果")
        return ToolResult.error(
            "搜索失败：" + "；".join(errors)
            + "。可以在 config.toml 的 [tools.search] 配置 SearXNG 或搜索 API 密钥，或配置 [tools.web] proxy",
            details={"errors": errors},
        )

    api.register_tool(
        Tool(
            name="web_search",
            description=(
                "搜索网络，返回标题、链接、摘要与日期。查找最新信息、资料出处时使用；"
                "新闻类问题设置 recency；找到相关结果后用 fetch_url 打开阅读"
            ),
            params=SearchIn,
            handler=web_search,
            capabilities=frozenset({"net:*"}),
            timeout_s=60.0,
        )
    )


__all__ = [
    "BACKENDS",
    "backend_chain",
    "parse_bing",
    "parse_bing_html",
    "parse_brave",
    "parse_duckduckgo",
    "parse_searxng",
    "parse_tavily",
    "register_search",
    "render",
]
