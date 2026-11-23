"""网络读取工具。"""

from __future__ import annotations

import re

import httpx
from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_SPACES = re.compile(r"\n{3,}")
_SCHEME = re.compile(r"^https?://", re.IGNORECASE)


class FetchIn(BaseModel):
    url: str = Field(
        description="http/https 地址；省略协议头时按 http:// 处理",
        json_schema_extra={"x-markbot-role": "url"},
    )
    max_chars: int = Field(default=20000, ge=100, le=200000)
    timeout_s: float = Field(default=30.0, ge=1, le=120)
    verify_tls: bool = Field(
        default=True,
        description="是否校验 HTTPS 证书；访问局域网自签证书服务时可设为 false",
    )


def _normalize_url(raw: str) -> str | None:
    url = raw.strip()
    if _SCHEME.match(url):
        return url
    if "://" in url:
        return None
    return f"http://{url}"


def _to_text(body: str) -> str:
    body = _SCRIPT.sub("", body)
    text = _TAG.sub(" ", body)
    text = re.sub(r"[ \t]+", " ", text)
    return _SPACES.sub("\n\n", text).strip()


def setup(api: ExtensionAPI) -> None:
    async def fetch_url(ctx, args: FetchIn) -> ToolResult:
        url = _normalize_url(args.url)
        if url is None:
            return ToolResult.error("只支持 http/https 地址")
        try:
            async with httpx.AsyncClient(
                timeout=args.timeout_s,
                follow_redirects=True,
                verify=args.verify_tls,
            ) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            hint = (
                "；若目标为局域网自签证书，可设置 verify_tls=false"
                if "CERTIFICATE_VERIFY_FAILED" in str(exc)
                else ""
            )
            return ToolResult.error(f"请求失败: {exc}{hint}")
        if resp.status_code >= 400:
            return ToolResult.error(f"HTTP {resp.status_code}: {url}")
        content_type = resp.headers.get("content-type", "")
        text = resp.text if "html" in content_type or "text" in content_type else resp.text
        cleaned = _to_text(text)
        return ToolResult(
            content=cleaned[: args.max_chars],
            details={"url": url, "status": resp.status_code, "chars": len(cleaned)},
        )

    api.register_tool(
        Tool(
            name="fetch_url",
            description="抓取网页并转换为纯文本，支持省略协议头与局域网自签证书",
            params=FetchIn,
            handler=fetch_url,
            capabilities=frozenset({"net:*"}),
            timeout_s=90.0,
        )
    )


__all__ = ["setup"]
