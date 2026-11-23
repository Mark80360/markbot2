"""Phase 0 加固（ADR 0009）的回归测试。

覆盖：子进程环境与 shell、绝对路径 glob、明文密钥拦截、确认预览、同步工具超时、
fetch_url（错误信息 / 内容类型 / https 优先 / SSRF / 重定向）、记忆索引容量整理与去重、
仓库内置包的 git 信任、飞书空事件处理器、MCP 能力与确认、运行环境说明。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from markbot.api import Tool, ToolResult
from markbot.api.sandbox import (
    ENV_ALLOWLIST,
    PROXY_ENV,
    build_env,
    resolve_shell,
    run_sandboxed,
    run_shell_sandboxed,
    shell_name,
)
from markbot.kernel.config import (
    ALLOW_PLAINTEXT_ENV,
    PlaintextSecretsError,
    enforce_secret_policy,
    load_config,
)
from markbot.kernel.context import environment_block
from markbot.kernel.execution import LocalExecution
from markbot.kernel.loop import confirm_preview
from markbot.kernel.paths import PathGuard
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.trust import TrustStore
from markbot.memory.store import INDEX_LIMIT, MemoryStore, fit_index, render_index
from markbot.schema.session_log import trajectory_text

POSIX = os.name == "posix"


def _tools(module, *names):
    from markbot.api.ui import NullUI
    from markbot.kernel.registry import StagedExtensionAPI
    from markbot.kernel.runtime_view import SourceSet

    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI())
    module.setup(api)
    api.commit()
    registry = {tool.name: tool for tool in source.tools}
    return [registry[name] for name in names]


def _ctx(root: Path):
    from markbot.kernel.config import PermissionConfig
    from markbot.kernel.permissions import PermissionPolicy
    from markbot.kernel.session import Session
    from markbot.kernel.tools import KernelToolContext

    policy = PermissionPolicy(PermissionConfig(profile="trusted"), root=root)
    return KernelToolContext(session=Session("测试"), policy=policy)


def _call(tool, ctx, **kwargs):
    async def invoke():
        outcome = tool.handler(ctx, tool.params(**kwargs))
        if hasattr(outcome, "__await__"):
            return await outcome
        return outcome

    return asyncio.run(invoke())


# ===================================================================== 子进程环境


def test_proxy_variables_pass_through(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:7891")
    monkeypatch.setenv("NO_PROXY", "localhost")
    env = build_env()
    assert env.get("HTTPS_PROXY") == "http://proxy.local:7891"
    assert env.get("NO_PROXY") == "localhost"
    assert {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"} <= PROXY_ENV <= ENV_ALLOWLIST


def test_windows_runtime_variables_are_allowlisted():
    for name in ("APPDATA", "LOCALAPPDATA", "PATHEXT", "COMSPEC", "SYSTEMROOT"):
        assert name in ENV_ALLOWLIST


def test_passthrough_env_is_opt_in(monkeypatch):
    monkeypatch.setenv("MARKBOT_CUSTOM_VAR", "v1")
    assert "MARKBOT_CUSTOM_VAR" not in build_env()
    assert build_env(passthrough=["MARKBOT_CUSTOM_VAR"])["MARKBOT_CUSTOM_VAR"] == "v1"


def test_resolve_shell_returns_command_prefix():
    prefix = resolve_shell("auto")
    assert prefix and Path(prefix[0]).name
    assert prefix[-1] in {"-c", "-Command", "/c"}
    expected = {"bash", "sh"} if POSIX else {"pwsh", "powershell", "cmd"}
    assert shell_name(prefix) in expected


def test_resolve_shell_unknown_value_falls_back():
    assert resolve_shell("definitely-not-a-shell-xyz") == resolve_shell("auto")


@pytest.mark.skipif(not POSIX or shutil.which("bash") is None, reason="需要 bash")
def test_bash_syntax_runs_under_configured_shell(tmp_path):
    # dash 下会报 Bad substitution；配置成 bash 后可以执行
    env = LocalExecution(tmp_path, shell="bash")
    assert env.shell == "bash"
    result = asyncio.run(env.exec('x=hello; echo "${x//l/L}"; [[ 1 == 1 ]] && echo ok'))
    assert result.ok, result.combined()
    assert result.stdout.split() == ["heLLo", "ok"]


def test_shell_defaults_to_legacy_when_not_configured(tmp_path):
    env = LocalExecution(tmp_path)
    assert env.shell_prefix is None
    assert env.shell in {"sh", "cmd"}


def test_run_shell_with_explicit_prefix(tmp_path):
    prefix = resolve_shell("auto")
    command = "echo shell-ok"
    result = asyncio.run(run_shell_sandboxed(command, cwd=tmp_path, shell=prefix))
    assert result.ok, result.combined()
    assert "shell-ok" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows 进程树")
def test_windows_timeout_kills_process_tree(tmp_path):
    marker = tmp_path / "child-alive.txt"
    child = (
        "import time,pathlib; time.sleep(3); "
        f"pathlib.Path(r'{marker}').write_text('alive')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
    )
    started = time.monotonic()
    result = asyncio.run(run_sandboxed([sys.executable, "-c", parent], cwd=tmp_path, timeout_s=1))
    assert result.timed_out
    assert time.monotonic() - started < 15
    time.sleep(4)
    assert not marker.exists(), "超时后子进程仍在运行"


# ======================================================================= glob


def _guarded(tmp_path: Path) -> tuple[LocalExecution, Path, Path]:
    base = tmp_path.resolve()
    root = base / "project"
    home = base / "home"
    root.mkdir()
    (home / "workspace" / "job").mkdir(parents=True)
    (home / "learned").mkdir()
    return LocalExecution(root, guard=PathGuard.build(root, home=home)), root, home


def test_glob_absolute_pattern_in_writable_carve_out(tmp_path):
    env, _, home = _guarded(tmp_path)
    (home / "workspace" / "job" / "a.py").write_text("x", encoding="utf-8")
    (home / "workspace" / "job" / "b.txt").write_text("x", encoding="utf-8")
    matches = env.glob(f"{(home / 'workspace').as_posix()}/**/*.py")
    assert matches == [(home / "workspace" / "job" / "a.py").resolve().as_posix()]


def test_glob_absolute_pattern_inside_root_is_relative(tmp_path):
    env, root, _ = _guarded(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "m.py").write_text("x", encoding="utf-8")
    assert env.glob(f"{root.as_posix()}/src/*.py") == ["src/m.py"]


def test_glob_absolute_pattern_into_protected_dir_is_denied(tmp_path):
    env, _, home = _guarded(tmp_path)
    (home / "learned" / "secret.md").write_text("x", encoding="utf-8")
    with pytest.raises(PermissionError):
        env.glob(f"{(home / 'learned').as_posix()}/**/*.md")


def test_glob_relative_skips_protected_results(tmp_path):
    env, root, _ = _guarded(tmp_path)
    (root / ".markbot").mkdir()
    (root / ".markbot" / "x.md").write_text("x", encoding="utf-8")
    (root / "y.md").write_text("y", encoding="utf-8")
    assert env.glob("**/*.md") == ["y.md"]


def test_glob_tool_accepts_absolute_pattern(tmp_path):
    from markbot.extensions import fs
    env, _, home = _guarded(tmp_path)
    (home / "workspace" / "job" / "a.py").write_text("x", encoding="utf-8")
    (glob_files,) = _tools(fs, "glob_files")

    class Ctx:
        pass

    ctx = Ctx()
    ctx.env = env
    result = _call(glob_files, ctx, pattern=f"{(home / 'workspace').as_posix()}/**/*.py")
    assert not result.is_error
    assert "a.py" in result.content


# =================================================================== 明文密钥


def _home_with_plaintext(tmp_path: Path, extra: str = "") -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        f'[agent]\napi_key = "sk-plain-should-not-leak"\n{extra}', encoding="utf-8"
    )
    return home


def test_plaintext_secrets_refuse_to_start(tmp_path, monkeypatch):
    monkeypatch.delenv(ALLOW_PLAINTEXT_ENV, raising=False)
    cfg = load_config(home=_home_with_plaintext(tmp_path), project_root=tmp_path)
    with pytest.raises(PlaintextSecretsError) as info:
        enforce_secret_policy(cfg)
    message = str(info.value)
    assert "agent.api_key" in message
    assert "markbot secrets migrate" in message
    assert "sk-plain-should-not-leak" not in message


def test_plaintext_secrets_can_be_allowed(tmp_path, monkeypatch):
    monkeypatch.delenv(ALLOW_PLAINTEXT_ENV, raising=False)
    cfg = load_config(home=_home_with_plaintext(tmp_path), project_root=tmp_path)
    enforce_secret_policy(cfg, allow=True)
    monkeypatch.setenv(ALLOW_PLAINTEXT_ENV, "1")
    enforce_secret_policy(cfg)


def test_plaintext_secrets_allowed_by_config(tmp_path, monkeypatch):
    monkeypatch.delenv(ALLOW_PLAINTEXT_ENV, raising=False)
    home = _home_with_plaintext(tmp_path, "[security]\nallow_plaintext_secrets = true\n")
    enforce_secret_policy(load_config(home=home, project_root=tmp_path))


def test_no_plaintext_secrets_starts(tmp_path, monkeypatch):
    monkeypatch.delenv(ALLOW_PLAINTEXT_ENV, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[agent]\napi_key = "env:SOME_KEY"\n', encoding="utf-8")
    enforce_secret_policy(load_config(home=home, project_root=tmp_path))


def test_cli_chat_refuses_plaintext_secrets(tmp_path, monkeypatch, capsys):
    from markbot.cli.main import main

    monkeypatch.delenv(ALLOW_PLAINTEXT_ENV, raising=False)
    home = _home_with_plaintext(tmp_path)
    (home / "config.toml").write_text(
        '[agent]\nprovider = "echo"\napi_key = "sk-plain-should-not-leak"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MARKBOT_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    assert main(["你好"]) == 2
    err = capsys.readouterr().err
    assert "已拒绝启动" in err
    assert "sk-plain-should-not-leak" not in err


def test_shell_and_web_sections_are_parsed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[shell]\nprogram = "bash"\npassthrough_env = ["FOO"]\n'
        '[tools.web]\nallow_private = ["192.168.71.90:11235"]\n',
        encoding="utf-8",
    )
    cfg = load_config(home=home, project_root=tmp_path)
    assert cfg.shell.program == "bash"
    assert cfg.shell.passthrough_env == ["FOO"]
    assert cfg.web.allow_private == ["192.168.71.90:11235"]


# ================================================================== 循环


def test_confirm_preview_shows_full_command():
    command = "echo safe " + "x" * 2000 + " ; rm -rf ~"
    preview = confirm_preview({"command": command, "timeout_s": 30})
    assert "rm -rf ~" in preview
    assert command in preview


def test_confirm_preview_truncates_other_args_with_total():
    preview = confirm_preview({"content": "y" * 5000, "path": "a.txt"})
    assert "中间省略" in preview
    assert "字符" in preview
    assert len(preview) < 1200


def test_sync_handler_timeout_is_enforced(make_kernel):
    class Empty(BaseModel):
        pass

    def slow(ctx, args):
        time.sleep(3)
        return ToolResult(content="late")

    provider = ScriptedProvider(
        [ModelReply(tool_calls=[ToolCall(id="c1", name="slow_sync", args={})]), "结束"]
    )
    kernel = make_kernel(provider=provider)
    kernel.runtime.tools.register(
        Tool(name="slow_sync", description="慢", params=Empty, handler=slow, timeout_s=0.3)
    )
    started = time.monotonic()
    result = asyncio.run(kernel.run("跑慢工具"))
    # 超时后回合继续，且不会在回合结束时等待那个仍在 sleep 的线程
    assert time.monotonic() - started < 2.5
    assert result.text == "结束"
    trajectory = trajectory_text(kernel.home / "sessions" / result.session_id)
    assert "tool timeout: slow_sync" in trajectory


def test_sync_handler_still_works(make_kernel):
    class Empty(BaseModel):
        pass

    provider = ScriptedProvider(
        [ModelReply(tool_calls=[ToolCall(id="c1", name="quick_sync", args={})]), "好了"]
    )
    kernel = make_kernel(provider=provider)
    kernel.runtime.tools.register(
        Tool(
            name="quick_sync",
            description="快",
            params=Empty,
            handler=lambda ctx, args: ToolResult(content="quick-ok"),
        )
    )
    result = asyncio.run(kernel.run("跑快工具"))
    assert result.text == "好了"
    assert "quick-ok" in trajectory_text(kernel.home / "sessions" / result.session_id)


def test_environment_block_mentions_time_os_and_shell(tmp_path):
    env = LocalExecution(tmp_path, shell="auto")
    block = environment_block(env)
    assert block.startswith("<env>") and block.endswith("</env>")
    assert "当前时间" in block
    assert "操作系统" in block
    assert f"命令 shell: {env.shell}" in block


def test_kernel_system_prompt_includes_environment(make_kernel):
    kernel = make_kernel(["ok"])
    seen: dict = {}

    async def capture(payload, on_delta=None):
        seen["system"] = payload["system"]
        return ModelReply(text="ok")

    kernel.provider.complete = capture  # type: ignore[method-assign]
    asyncio.run(kernel.run("现在几点", archive=False))
    assert "<env>" in seen["system"]
    assert "当前时间" in seen["system"]


# ================================================================== fetch_url


class _Resp:
    def __init__(self, status=200, ctype="text/html", text="", headers=None, content=b""):
        self.status_code = status
        self.headers = {"content-type": ctype, **(headers or {})}
        self.text = text
        self.content = content


def _client(responses: dict[str, _Resp], seen: list[str] | None = None, error=None):
    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            if seen is not None:
                seen.append(url)
            if error is not None and url.startswith("https://"):
                raise error
            return responses[url]

    return Client


@pytest.fixture
def web_tool(monkeypatch):
    from markbot.extensions import web
    async def public(host, port):
        import ipaddress

        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        table = {
            "internal.example": "10.0.0.5",
            "lan.example": "192.168.71.90",
        }
        return [ipaddress.ip_address(table.get(host, "93.184.216.34"))]

    monkeypatch.setattr(web, "_resolve_host", public)

    def build(config=None):
        from markbot.api.ui import NullUI
        from markbot.kernel.registry import StagedExtensionAPI
        from markbot.kernel.runtime_view import SourceSet

        if config is None:
            (tool,) = _tools(web, "fetch_url")
            return tool, _ctx(Path.cwd())
        source = SourceSet(name="builtin", origin="builtin")
        api = StagedExtensionAPI("test", source=source, ui=NullUI(), config=config)
        web.setup(api)
        api.commit()
        return source.tools[0], _ctx(Path.cwd())

    return web, build


def test_fetch_timeout_error_is_not_empty(web_tool, monkeypatch):
    web, build = web_tool
    monkeypatch.setattr(
        web.httpx, "AsyncClient", _client({}, error=httpx.ReadTimeout(""))
    )
    tool, ctx = build()
    result = _call(tool, ctx, url="https://example.com")
    assert result.is_error
    assert "ReadTimeout" in result.content
    assert result.content.strip() != "请求失败:"


def test_fetch_defaults_to_https_and_falls_back_to_http(web_tool, monkeypatch):
    web, build = web_tool
    seen: list[str] = []
    monkeypatch.setattr(
        web.httpx,
        "AsyncClient",
        _client(
            {"http://example.com": _Resp(text="<p>plain http</p>")},
            seen,
            error=httpx.ConnectError("refused"),
        ),
    )
    tool, ctx = build()
    result = _call(tool, ctx, url="example.com")
    assert not result.is_error, result.content
    assert seen == ["https://example.com", "http://example.com"]
    assert result.details["url"] == "http://example.com"


def test_fetch_binary_content_is_not_decoded(web_tool, monkeypatch):
    web, build = web_tool
    resp = _Resp(ctype="application/pdf", text="%PDF garbage", content=b"%PDF" + b"\x00" * 100)
    monkeypatch.setattr(web.httpx, "AsyncClient", _client({"https://example.com/a.pdf": resp}))
    tool, ctx = build()
    result = _call(tool, ctx, url="https://example.com/a.pdf")
    assert not result.is_error
    assert result.details["binary"] is True
    assert "application/pdf" in result.content
    assert "garbage" not in result.content


def test_fetch_blocks_private_addresses(web_tool, monkeypatch):
    web, build = web_tool
    seen: list[str] = []
    monkeypatch.setattr(web.httpx, "AsyncClient", _client({}, seen))
    tool, ctx = build()
    for url in ("http://127.0.0.1:8787/chat", "http://internal.example/", "http://localhost/"):
        result = _call(tool, ctx, url=url)
        assert result.is_error, url
        assert result.details.get("denied") is True
        assert "allow_private" in result.content
    assert seen == []


def test_fetch_allows_whitelisted_private_host(web_tool, monkeypatch):
    web, build = web_tool
    url = "http://lan.example:11235/health"
    monkeypatch.setattr(
        web.httpx, "AsyncClient", _client({url: _Resp(ctype="application/json", text='{"ok":1}')})
    )
    tool, ctx = build({"tools": {"web": {"allow_private": ["lan.example:11235"]}}})
    result = _call(tool, ctx, url=url)
    assert not result.is_error, result.content
    assert '{"ok":1}' in result.content
    # 端口不匹配仍然拒绝
    other = _call(tool, ctx, url="http://lan.example:22/")
    assert other.is_error


def test_fetch_allow_private_accepts_cidr(web_tool, monkeypatch):
    web, build = web_tool
    url = "http://192.168.71.90:7891/"
    monkeypatch.setattr(web.httpx, "AsyncClient", _client({url: _Resp(text="<p>ok</p>")}))
    tool, ctx = build({"tools": {"web": {"allow_private": ["192.168.71.0/24"]}}})
    assert not _call(tool, ctx, url=url).is_error


def test_fetch_redirect_to_private_is_blocked(web_tool, monkeypatch):
    web, build = web_tool
    responses = {
        "https://example.com/go": _Resp(status=302, headers={"location": "http://127.0.0.1:8787/"}),
    }
    monkeypatch.setattr(web.httpx, "AsyncClient", _client(responses))
    tool, ctx = build()
    result = _call(tool, ctx, url="https://example.com/go")
    assert result.is_error
    assert "非公网地址" in result.content


def test_fetch_follows_public_redirect(web_tool, monkeypatch):
    web, build = web_tool
    responses = {
        "https://example.com/old": _Resp(status=301, headers={"location": "/new"}),
        "https://example.com/new": _Resp(text="<title>New</title><p>moved</p>"),
    }
    monkeypatch.setattr(web.httpx, "AsyncClient", _client(responses))
    tool, ctx = build()
    result = _call(tool, ctx, url="https://example.com/old")
    assert not result.is_error
    assert result.details["url"] == "https://example.com/new"
    assert result.details["title"] == "New"


def test_fetch_verify_tls_false_ignored_for_public_host(web_tool, monkeypatch):
    web, build = web_tool
    created: list[dict] = []

    class Client(_client({"https://example.com": _Resp(text="<p>x</p>")})):
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(web.httpx, "AsyncClient", Client)
    tool, ctx = build()
    result = _call(tool, ctx, url="https://example.com", verify_tls=False)
    assert not result.is_error
    assert created and all(kwargs["verify"] is True for kwargs in created)
    assert "只对" in result.content


def test_fetch_marks_blocked_pages(web_tool, monkeypatch):
    web, build = web_tool
    responses = {
        "https://example.com/a": _Resp(status=403, text="Access Denied"),
        "https://example.com/b": _Resp(text="<title>验证码中间页</title><p>请完成验证码</p>"),
    }
    monkeypatch.setattr(web.httpx, "AsyncClient", _client(responses))
    tool, ctx = build()
    denied = _call(tool, ctx, url="https://example.com/a")
    assert denied.is_error and denied.details["blocked"] is True
    assert "浏览器" in denied.content
    captcha = _call(tool, ctx, url="https://example.com/b")
    assert captcha.details["blocked"] is True


# ================================================================== 记忆


def test_index_full_is_fitted_instead_of_refused(tmp_path):
    store = MemoryStore(tmp_path / "learned")
    long = "非常长的摘要" * 30
    for index in range(60):
        receipt = store.remember(f"{long}{index}", target="wiki", topic=f"t{index:02d}")
        assert receipt.ok, receipt.message
    index = store._read_index()
    assert len(render_index(index)) <= INDEX_LIMIT
    # 所有页面正文都还在，可以检索
    assert len(list(store.wiki_dir.glob("*.md"))) == 60
    assert store.search(f"{long}3")


def test_fit_index_prefers_old_ephemeral_and_keeps_permanent():
    entries = {
        "old-eph": {"summary": "e" * 120, "lifespan": "ephemeral", "updated_at": "2026-01-01"},
        "perm": {"summary": "p" * 120, "lifespan": "permanent", "updated_at": "2025-01-01"},
        "new": {"summary": "n" * 120, "lifespan": "durable", "updated_at": "2026-11-23"},
    }
    notes = fit_index(entries, keep="new", limit=200)
    assert entries["new"]["summary"] == "n" * 120
    assert entries["perm"].get("indexed") is not False
    assert entries["old-eph"].get("indexed") is False
    assert notes


def test_status_reports_capacity(tmp_path):
    store = MemoryStore(tmp_path / "learned")
    store.remember("用户偏好：回复用中文")
    info = store.status()
    assert info["resident"]["usage"].endswith("%")
    assert "rendered_chars" in info["index"]
    assert info["index"]["usage"].endswith("%")


def test_remember_dedupes_contained_facts(tmp_path):
    store = MemoryStore(tmp_path / "learned")
    store.remember("用户关注药物研发与基因相关领域")
    richer = store.remember("用户关注药物研发与基因相关领域，重点在靶点发现方法论")
    assert richer.ok and richer.removed
    subset = store.remember("用户关注药物研发与基因相关领域")
    assert subset.ok and not subset.changed
    text = store.resident_path.read_text(encoding="utf-8")
    assert text.count("药物研发") == 1
    assert "靶点发现" in text


def test_remember_keeps_distinct_short_facts(tmp_path):
    store = MemoryStore(tmp_path / "learned")
    store.remember("用户偏好：回复用中文")
    store.remember("用户住在杭州")
    text = store.resident_path.read_text(encoding="utf-8")
    assert "回复用中文" in text and "杭州" in text


# ================================================================== 包信任


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="需要 git")
def test_git_clean_trust(tmp_path):
    repo = tmp_path / "repo"
    pkg = repo / "capabilities" / "demo"
    (pkg / "skills" / "demo").mkdir(parents=True)
    (pkg / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    _git("init", "-q", cwd=repo)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".", cwd=repo)
    assert TrustStore.git_clean(pkg) is False  # 只暂存、未提交也算有改动
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=repo)
    assert TrustStore.git_clean(pkg) is True
    (pkg / "skills" / "demo" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert TrustStore.git_clean(pkg) is False
    _git("checkout", "--", ".", cwd=repo)
    (pkg / "new.py").write_text("x", encoding="utf-8")
    assert TrustStore.git_clean(pkg) is False


def test_git_clean_outside_repo_is_false(tmp_path):
    (tmp_path / "pkg").mkdir()
    assert TrustStore.git_clean(tmp_path / "pkg") is False


def test_skipped_package_notice_once(make_kernel):
    kernel = make_kernel(["a", "b"])
    kernel.runtime.packages = [
        {"id": "office", "scope": "repo", "trusted": False, "loaded": False, "error": ""}
    ]
    first = asyncio.run(kernel.run("一", archive=False))
    second = asyncio.run(kernel.run("二", archive=False))
    assert "markbot trust office" in first.text
    assert "markbot trust office" not in second.text


# ================================================================== 飞书 / MCP


def test_feishu_registers_ignored_events(monkeypatch):
    import importlib

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "channels" / "feishu"))
    channel = importlib.import_module("channel")

    calls: list[str] = []

    class Builder:
        def __getattr__(self, name):
            def register(handler):
                calls.append(name)
                return self

            return register

    channel.register_ignored_events(Builder())
    assert "register_p2_im_message_reaction_created_v1" in calls
    assert "register_p2_im_message_reaction_deleted_v1" in calls

    class Old:
        pass

    assert isinstance(channel.register_ignored_events(Old()), Old)


def test_mcp_server_capabilities_and_confirm_tools_are_parsed():
    from markbot.extensions import mcp

    servers = mcp._parse_servers(
        {
            "tools": {
                "mcp_servers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["@playwright/mcp@latest"],
                        "capabilities": ["net:*", "ui", "bogus"],
                        "confirm_tools": ["browser_click", "browser_type*"],
                    }
                }
            }
        }
    )
    spec = servers["playwright"]
    assert spec.capabilities == ["net:*", "ui"]
    assert mcp._needs_confirm(spec, "browser_type")
    assert mcp._needs_confirm(spec, "browser_click")
    assert not mcp._needs_confirm(spec, "browser_snapshot")


def test_mcp_confirm_denied_without_ui():
    from markbot.extensions import mcp

    class Ctx:
        async def confirm(self, title, message):
            return False

    assert asyncio.run(mcp._confirm_call(Ctx(), "pw", "browser_click", {"ref": "e1"})) is False
    assert asyncio.run(mcp._confirm_call(object(), "pw", "browser_click", {})) is False
