"""Phase 3 第一批（ADR 0014）：网页正文提取、fetch 重试与缓存、web_search、浏览器扩展与能力类型。

浏览器部分只测纯函数与注册逻辑；真实 Playwright 的冒烟测试需要本机装好浏览器，
用 ``MARKBOT_BROWSER_SMOKE=1`` 打开。
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
from pathlib import Path

import httpx
import pytest

from markbot.api.ui import NullUI
from markbot.extensions import browser, web
from markbot.extensions.web import search
from markbot.kernel.permissions import (
    BROWSER,
    DESKTOP,
    Capability,
    capability_covers,
    parse_capabilities,
)
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import SourceSet


def _ctx(root: Path, profile: str = "trusted", secrets: dict[str, str] | None = None):
    from markbot.kernel.config import PermissionConfig
    from markbot.kernel.permissions import PermissionPolicy
    from markbot.kernel.session import Session
    from markbot.kernel.tools import KernelToolContext

    policy = PermissionPolicy(PermissionConfig(profile=profile), root=root)
    return KernelToolContext(session=Session("测试"), policy=policy, secrets=secrets)


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


class _Resp:
    def __init__(self, status=200, ctype="text/html", text="", headers=None, content=b"", data=None):
        self.status_code = status
        self.headers = {"content-type": ctype, **(headers or {})}
        self.text = text
        self.content = content
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=httpx.Request("GET", "https://x"), response=None
            )


def _client(handler):
    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            return handler("GET", url, kwargs)

        async def post(self, url, **kwargs):
            return handler("POST", url, kwargs)

    return Client


@pytest.fixture
def public_dns(monkeypatch):
    async def resolve(host, port):
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            return [ipaddress.ip_address("10.0.0.5" if host.endswith(".internal") else "93.184.216.34")]

    monkeypatch.setattr(web, "_resolve_host", resolve)
    monkeypatch.setattr(web, "RETRY_DELAY_S", 0)


# ================================================================== 正文提取

ARTICLE = """
<html><head><title>T</title><style>.a{}</style></head><body>
<nav><a href="/home">首页</a><a href="/about">关于</a></nav>
<header>站点抬头</header>
<article>
  <h1>标题一</h1>
  <p>第一段 &amp; 正文，<a href="/doc/1">相对链接</a>与<a href="https://other.example/x">绝对链接</a>。</p>
  <h2>小节</h2>
  <ul><li>条目 A</li><li>条目 B</li></ul>
  <p>""" + "足够长的正文。" * 40 + """</p>
  <script>evil()</script>
</article>
<footer>版权所有</footer>
</body></html>
"""


def test_markdown_keeps_structure_and_absolute_links():
    md = web.html_to_markdown(ARTICLE, "https://site.example/news/a.html")
    assert "# 标题一" in md and "## 小节" in md
    assert "- 条目 A" in md and "- 条目 B" in md
    assert "[相对链接](https://site.example/doc/1)" in md
    assert "[绝对链接](https://other.example/x)" in md
    assert "第一段 & 正文" in md
    for noise in ("首页", "站点抬头", "版权所有", "evil()", ".a{}"):
        assert noise not in md


def test_markdown_without_article_drops_page_chrome():
    page = "<body><nav>菜单</nav><div><p>正文内容</p></div><footer>页脚</footer></body>"
    md = web.html_to_markdown(page)
    assert "正文内容" in md and "菜单" not in md and "页脚" not in md


def test_fetch_defaults_to_markdown_and_supports_text(public_dns, monkeypatch):
    responses = {"https://site.example/a": _Resp(text=ARTICLE)}
    monkeypatch.setattr(web.httpx, "AsyncClient", _client(lambda m, u, k: responses[u]))
    tool = _setup(web)["fetch_url"]
    ctx = _ctx(Path.cwd())
    md = _call(tool, ctx, url="https://site.example/a")
    assert "# 标题一" in md.content and "首页" not in md.content
    plain = _call(tool, ctx, url="https://site.example/a", format="text")
    assert "首页" in plain.content and "# 标题一" not in plain.content


def test_fetch_retries_transient_status_once(public_dns, monkeypatch):
    calls: list[str] = []

    def handler(method, url, kwargs):
        calls.append(url)
        return _Resp(status=503, text="busy") if len(calls) == 1 else _Resp(text="<p>ok</p>")

    monkeypatch.setattr(web.httpx, "AsyncClient", _client(handler))
    result = _call(_setup(web)["fetch_url"], _ctx(Path.cwd()), url="https://site.example/r")
    assert not result.is_error and "ok" in result.content
    assert len(calls) == 2


def test_fetch_retries_read_timeout_once(public_dns, monkeypatch):
    calls: list[str] = []

    def handler(method, url, kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return _Resp(text="<p>second</p>")

    monkeypatch.setattr(web.httpx, "AsyncClient", _client(handler))
    result = _call(_setup(web)["fetch_url"], _ctx(Path.cwd()), url="https://site.example/t")
    assert not result.is_error and "second" in result.content


def test_fetch_caches_successful_responses(public_dns, monkeypatch):
    calls: list[str] = []

    def handler(method, url, kwargs):
        calls.append(url)
        return _Resp(text="<p>cached</p>")

    monkeypatch.setattr(web.httpx, "AsyncClient", _client(handler))
    tool = _setup(web)["fetch_url"]
    ctx = _ctx(Path.cwd())
    _call(tool, ctx, url="https://site.example/c")
    _call(tool, ctx, url="https://site.example/c")
    assert len(calls) == 1
    tool = _setup(web, {"tools": {"web": {"cache_ttl_s": 0}}})["fetch_url"]
    _call(tool, ctx, url="https://site.example/c")
    _call(tool, ctx, url="https://site.example/c")
    assert len(calls) == 3


def test_fetch_extracts_pdf_text_when_pypdf_available(public_dns, monkeypatch):
    monkeypatch.setattr(web, "_pdf_text", lambda data: ("PDF 正文", 3))
    resp = _Resp(ctype="application/pdf", content=b"%PDF-1.4 ...")
    monkeypatch.setattr(web.httpx, "AsyncClient", _client(lambda m, u, k: resp))
    result = _call(_setup(web)["fetch_url"], _ctx(Path.cwd()), url="https://site.example/a.pdf")
    assert result.content == "PDF 正文"
    assert result.details["pdf_pages"] == 3


def test_pdf_text_returns_none_for_garbage():
    assert web._pdf_text(b"not a pdf") is None


# ================================================================== web_search


def test_search_parsers():
    assert search.parse_searxng({"results": [{"title": "A", "url": "https://a", "content": "x"}]})[0]["url"] == "https://a"
    assert search.parse_tavily({"results": [{"title": "T", "url": "https://t", "content": "c"}]})[0]["snippet"] == "c"
    brave = {"web": {"results": [{"title": "<b>B</b>", "url": "https://b", "description": "d", "age": "1d"}]}}
    assert search.parse_brave(brave)[0] == {"title": "B", "url": "https://b", "snippet": "d", "date": "1d"}
    bing = {"webPages": {"value": [{"name": "N", "url": "https://n", "snippet": "s"}]}}
    assert search.parse_bing(bing)[0]["title"] == "N"


def test_parse_duckduckgo_html_unwraps_redirects():
    page = (
        '<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org%2F&rut=x">'
        "Python <b>官网</b></a>"
        '<a class="result__snippet" href="#">The official &amp; home</a></div>'
        '<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">广告</a></div>'
    )
    results = search.parse_duckduckgo(page)
    assert results[0]["url"] == "https://python.org/"
    assert results[0]["title"] == "Python 官网"
    assert results[0]["snippet"] == "The official & home"
    assert len(results) == 1


def test_parse_bing_html():
    page = (
        '<ol><li class="b_algo"><h2><a href="https://docs.python.org/3/" h="ID">Python 文档</a></h2>'
        '<div class="b_caption"><p>官方 <strong>文档</strong></p></div></li>'
        '<li class="b_ad"><h2><a href="https://ad.example">广告</a></h2></li></ol>'
    )
    results = search.parse_bing_html(page)
    assert results == [{"title": "Python 文档", "url": "https://docs.python.org/3/", "snippet": "官方 文档", "date": ""}]


def test_backend_chain_auto_and_explicit():
    assert search.backend_chain({}, lambda name: False) == ["bing_html", "duckduckgo"]
    chain = search.backend_chain({"searxng_url": "http://s"}, lambda name: name == "TAVILY_API_KEY")
    assert chain == ["searxng", "tavily", "bing_html", "duckduckgo"]
    assert search.backend_chain({"backend": "brave"}, lambda name: False) == ["brave"]


def test_web_search_falls_back_and_formats(monkeypatch):
    seen: list[str] = []

    def handler(method, url, kwargs):
        seen.append(url)
        if "searxng" in url:
            return _Resp(status=502)
        if "bing.com" in url:
            assert kwargs["params"]["q"] == "site:python.org asyncio"
            return _Resp(
                text='<li class="b_algo"><h2><a href="https://docs.python.org/a">asyncio</a></h2><p>事件循环</p></li>'
            )
        raise AssertionError(url)

    monkeypatch.setattr(search.httpx, "AsyncClient", _client(handler))
    tools = _setup(web, {"tools": {"search": {"searxng_url": "http://searxng.local"}}})
    result = _call(tools["web_search"], _ctx(Path.cwd()), query="asyncio", site="python.org")
    assert not result.is_error, result.content
    assert result.details["backend"] == "bing_html"
    assert "https://docs.python.org/a" in result.content and "事件循环" in result.content
    assert any("searxng" in error for error in result.details["errors"])
    # 第二次命中缓存
    again = _call(tools["web_search"], _ctx(Path.cwd()), query="asyncio", site="python.org")
    assert again.details["cached"] is True
    assert len(seen) == 2


def test_web_search_uses_api_key_from_secrets(monkeypatch):
    captured: dict = {}

    def handler(method, url, kwargs):
        captured.update(method=method, url=url, headers=kwargs.get("headers"))
        return _Resp(ctype="application/json", data={"results": [{"title": "R", "url": "https://r.example", "content": "c"}]})

    monkeypatch.setattr(search.httpx, "AsyncClient", _client(handler))
    tools = _setup(web, {"tools": {"search": {"backend": "tavily"}}})
    token = "tv" + "-123"
    ctx = _ctx(Path.cwd(), secrets={"TAVILY_API_KEY": token})
    result = _call(tools["web_search"], ctx, query="q", recency="week")
    assert not result.is_error
    assert captured["method"] == "POST" and captured["headers"]["Authorization"] == "Bearer " + token
    assert token not in result.content


def test_web_search_reports_all_failures(monkeypatch):
    monkeypatch.setattr(search.httpx, "AsyncClient", _client(lambda m, u, k: _Resp(text="<html></html>")))
    result = _call(_setup(web)["web_search"], _ctx(Path.cwd()), query="nothing")
    assert result.is_error
    assert "bing_html" in result.content and "duckduckgo" in result.content


def test_web_search_can_be_disabled():
    assert "web_search" not in _setup(web, {"tools": {"search": {"enabled": False}}})


# ================================================================== 能力类型


def test_browser_and_desktop_capabilities_parse_and_cover():
    caps = parse_capabilities(["browser:*", "desktop:notepad"])
    assert {cap.kind for cap in caps} == {BROWSER, DESKTOP}
    assert capability_covers(Capability(BROWSER, "example.com"), Capability(BROWSER, "a.example.com"))
    assert not capability_covers(Capability(BROWSER, "example.com"), Capability(BROWSER, "evil.com"))
    assert capability_covers(Capability(DESKTOP, "Notepad"), Capability(DESKTOP, "notepad"))


def test_strict_profile_denies_browser_tools(tmp_path):
    tool = _setup(browser, {"tools": {"browser": {"enabled": True}}})["browser_open"]
    strict = _ctx(tmp_path, profile="strict")
    decision = strict.policy.check(tool, {"url": "https://example.com"})
    assert not decision.allowed and "浏览器" in decision.reason
    trusted = _ctx(tmp_path)
    assert trusted.policy.check(tool, {"url": "https://example.com"}).allowed


# ================================================================== 浏览器扩展


def test_browser_registration_follows_enabled_flag(monkeypatch):
    assert _setup(browser, {"tools": {"browser": {"enabled": False}}}) == {}
    tools = _setup(browser, {"tools": {"browser": {"enabled": True}}})
    assert {"browser_open", "browser_snapshot", "browser_click", "browser_type", "browser_handoff"} <= set(tools)
    assert all("browser:*" in tool.capabilities for tool in tools.values())
    assert "fs.read:./**" in tools["browser_upload"].capabilities
    monkeypatch.setattr(browser.importlib.util, "find_spec", lambda name: None)
    assert _setup(browser, {"tools": {"browser": {"enabled": "auto"}}}) == {}


def test_parse_settings_defaults_and_proxy():
    settings = browser.parse_settings(
        {"tools": {"web": {"proxy": "http://p:1"}, "browser": {"confirm": "bogus", "blocked_domains": "Bad.com"}}}
    )
    assert settings.confirm == "risky"
    assert settings.proxy == "http://p:1"
    assert settings.blocked_domains == ["bad.com"]
    assert settings.headless is True


def test_navigation_denial(public_dns):
    settings = browser.BrowserSettings(blocked_domains=["bad.com"])
    run = asyncio.run
    assert run(browser.navigation_denial("https://example.com/", settings, {})) is None
    assert "blocked_domains" in run(browser.navigation_denial("https://x.bad.com/", settings, {}))
    assert run(browser.navigation_denial("file:///etc/passwd", settings, {}))
    assert run(browser.navigation_denial("http://127.0.0.1:8787/", settings, {}))
    assert run(browser.navigation_denial("http://db.internal/", settings, {}))
    assert run(browser.navigation_denial("about:blank", settings, {})) is None
    only = browser.BrowserSettings(allowed_domains=["example.com"])
    assert run(browser.navigation_denial("https://docs.example.com/", only, {})) is None
    assert "allowed_domains" in run(browser.navigation_denial("https://other.org/", only, {}))
    allowed = {"tools": {"web": {"allow_private": ["192.168.71.90"]}}}
    assert run(browser.navigation_denial("http://192.168.71.90:3000/", settings, allowed)) is None


def test_risky_reason():
    assert browser.risky_reason({"tag": "button", "type": "submit", "text": "保存"})
    assert browser.risky_reason({"tag": "button", "type": "", "text": "下一步", "inForm": True})
    assert browser.risky_reason({"tag": "a", "text": "立即购买"})
    assert browser.risky_reason({"tag": "div", "text": "Delete repository"})
    assert browser.risky_reason({"tag": "input", "type": "file", "text": ""})
    assert browser.risky_reason({"tag": "a", "text": "下一页", "href": "/p/2"}) is None
    assert browser.risky_reason({"tag": "button", "type": "button", "text": "展开"}) is None


def test_render_snapshot():
    data = {
        "title": "登录",
        "url": "https://example.com/login",
        "items": [
            {"ref": "e1", "role": "text", "name": "用户名", "value": "mark", "inView": True},
            {"ref": "e2", "role": "password", "name": "密码", "value": "***", "inView": True},
            {"ref": "e3", "role": "link", "name": "忘记密码", "href": "/forgot", "inView": False},
        ],
        "total": 3,
        "text": "欢迎登录",
        "scrollY": 0,
        "height": 2000,
        "viewport": 900,
    }
    text = browser.render_snapshot(data)
    assert '[e1]* text "用户名" 值="mark"' in text
    assert "[e3]  link" in text and "-> /forgot" in text
    assert "滚动位置: 0/1100" in text
    assert "欢迎登录" in text
    assert "e3" not in browser.render_snapshot(data, only_in_view=True)


def test_browser_type_rejects_unregistered_secret(tmp_path):
    tools = _setup(browser, {"tools": {"browser": {"enabled": True, "secrets": ["SITE_PASSWORD"]}}})
    ctx = _ctx(tmp_path, secrets={"OTHER": "x"})
    result = _call(tools["browser_type"], ctx, ref="e1", secret="OTHER")
    assert result.is_error and "没有在 [tools.browser] secrets 中登记" in result.content


def test_browser_open_denies_before_launching(tmp_path, public_dns):
    tools = _setup(browser, {"tools": {"browser": {"enabled": True}}})
    result = _call(tools["browser_open"], _ctx(tmp_path), url="http://127.0.0.1:8787/")
    assert result.is_error and result.details.get("denied")


@pytest.mark.skipif(os.environ.get("MARKBOT_BROWSER_SMOKE") != "1", reason="需要本机浏览器：MARKBOT_BROWSER_SMOKE=1")
def test_browser_smoke(tmp_path):
    pytest.importorskip("playwright")
    tools = _setup(
        browser,
        {"tools": {"browser": {"enabled": True, "user_data_dir": str(tmp_path / "profile"), "confirm": "none"}}},
    )
    ctx = _ctx(tmp_path)
    opened = _call(tools["browser_open"], ctx, url="about:blank")
    assert not opened.is_error, opened.content
    html = "<h1>表单</h1><input placeholder='名字'><a href='#next'>下一页</a>"
    loaded = _call(tools["browser_eval"], ctx, script=f"() => {{ document.body.innerHTML = {html!r}; return 1 }}")
    assert not loaded.is_error, loaded.content
    snap = _call(tools["browser_snapshot"], ctx)
    assert "名字" in snap.content
    _call(tools["browser_close"], ctx)
