from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from markbot.api.ui import NullUI
from markbot.extensions import fs, shell, skills, web
from markbot.kernel.config import PermissionConfig
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import SkillIndex, SourceSet
from markbot.kernel.session import Session
from markbot.kernel.tools import KernelToolContext


def _tools(module, *names):
    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI())
    module.setup(api)
    api.commit()
    registry = {tool.name: tool for tool in source.tools}
    return [registry[name] for name in names]


def _ctx(root: Path, **session_kwargs) -> KernelToolContext:
    session = Session("测试", **session_kwargs)
    policy = PermissionPolicy(PermissionConfig(profile="trusted"), root=root)
    return KernelToolContext(session=session, policy=policy)


def _call(tool, ctx, **kwargs):
    return asyncio.run(_invoke(tool, ctx, **kwargs))


async def _invoke(tool, ctx, **kwargs):
    outcome = tool.handler(ctx, tool.params(**kwargs))
    if hasattr(outcome, "__await__"):
        return await outcome
    return outcome


# --------------------------------------------------------------------- fs


def test_fs_write_read_list_glob(tmp_path):
    write_file, read_file, list_dir, glob_files = _tools(
        fs, "write_file", "read_file", "list_dir", "glob_files"
    )
    ctx = _ctx(tmp_path)

    result = _call(write_file, ctx, path="notes/a.txt", content="line1\nline2\nline3")
    assert not result.is_error
    assert (tmp_path / "notes" / "a.txt").read_text(encoding="utf-8") == "line1\nline2\nline3"

    result = _call(read_file, ctx, path="notes/a.txt", start_line=2, max_lines=1)
    assert result.content == "line2"
    assert result.details["lines"] == 1
    assert result.details["total_lines"] == 3

    result = _call(list_dir, ctx, path="notes")
    assert "file\ta.txt" in result.content
    assert "dir" not in result.content

    result = _call(glob_files, ctx, pattern="**/*.txt")
    assert result.content == "notes/a.txt"


def test_fs_rejects_path_outside_root(tmp_path):
    write_file = _tools(fs, "write_file")[0]
    ctx = _ctx(tmp_path / "workspace")
    ctx.policy.root.mkdir()

    with pytest.raises(PermissionError):
        _call(write_file, ctx, path="../escape.txt", content="x")


def test_fs_read_errors_and_truncation(tmp_path):
    read_file, list_dir = _tools(fs, "read_file", "list_dir")
    ctx = _ctx(tmp_path)

    assert _call(read_file, ctx, path="missing.txt").is_error
    assert _call(list_dir, ctx, path="missing").is_error

    (tmp_path / "big.txt").write_text("x" * 40_100, encoding="utf-8")
    result = _call(read_file, ctx, path="big.txt")
    assert result.content.endswith("…（已截断）")
    assert len(result.content) < 40_100


# ------------------------------------------------------------------- shell


def test_shell_run_command_success_and_failure(tmp_path):
    run_command = _tools(shell, "run_command")[0]
    ctx = _ctx(tmp_path)

    ok = _call(run_command, ctx, command="printf hello")
    assert not ok.is_error
    assert ok.content == "hello"
    assert ok.details["returncode"] == 0

    failed = _call(run_command, ctx, command="exit 3")
    assert failed.is_error
    assert failed.content == "命令失败，退出码 3"
    assert failed.details["returncode"] == 3


# --------------------------------------------------------------------- web


def test_web_url_normalization_and_text_extraction():
    assert web._normalize_url("example.com/a") == "http://example.com/a"
    assert web._normalize_url("https://example.com") == "https://example.com"
    assert web._normalize_url("ftp://example.com") is None

    text = web._to_text(
        "<html><style>.x{}</style><script>bad()</script><p>Hello   world</p></html>"
    )
    assert "bad()" not in text
    assert ".x{}" not in text
    assert "Hello world" in text


def test_web_fetch_success_cleans_html(monkeypatch):
    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><script>bad()</script><body>Hello <b>web</b></body></html>"

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return Response()

    monkeypatch.setattr(web.httpx, "AsyncClient", Client)
    fetch_url = _tools(web, "fetch_url")[0]

    result = _call(fetch_url, _ctx(Path.cwd()), url="example.com", max_chars=100)
    assert not result.is_error
    assert "Hello web" in result.content
    assert "bad()" not in result.content
    assert result.details["url"] == "http://example.com"


def test_web_fetch_reports_http_and_network_errors(monkeypatch):
    class Response:
        status_code = 404
        headers = {"content-type": "text/plain"}
        text = "not found"

    class NotFoundClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return Response()

    monkeypatch.setattr(web.httpx, "AsyncClient", NotFoundClient)
    fetch_url = _tools(web, "fetch_url")[0]
    result = _call(fetch_url, _ctx(Path.cwd()), url="example.com")
    assert result.is_error
    assert "HTTP 404" in result.content

    class BrokenClient(NotFoundClient):
        async def get(self, url):
            raise httpx.ConnectError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(web.httpx, "AsyncClient", BrokenClient)
    result = _call(fetch_url, _ctx(Path.cwd()), url="https://self-signed.local")
    assert result.is_error
    assert "verify_tls=false" in result.content

    result = _call(fetch_url, _ctx(Path.cwd()), url="ftp://example.com")
    assert result.is_error
    assert "只支持 http/https" in result.content


# ------------------------------------------------------------------ skills

SKILL = """---
name: demo
description: Demo skill
allowed-tools: [run_script]
---
STEP-BODY
"""


def _skill_index(tmp_path: Path) -> SkillIndex:
    root = tmp_path / "skills"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    index = SkillIndex()
    index.add_root(root, origin="learned")
    return index


def test_skills_list_and_read(tmp_path):
    index = _skill_index(tmp_path)
    list_skills, read_skill = _tools(skills, "list_skills", "read_skill")
    ctx = _ctx(tmp_path, skills=index)

    listed = _call(list_skills, ctx)
    assert "demo: Demo skill" in listed.content
    assert "STEP-BODY" not in listed.content

    read = _call(read_skill, ctx, name="demo")
    assert read.content.startswith("# demo")
    assert "STEP-BODY" in read.content
    assert read.details["source"] == "learned"

    assert _call(read_skill, ctx, name="nope").is_error


def test_skills_read_attached_file(tmp_path):
    index = _skill_index(tmp_path)
    read_file = _tools(skills, "read_skill_file")[0]
    ctx = _ctx(tmp_path, skills=index)
    skill_dir = index.get("demo").path.parent
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "api.md").write_text("# API", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")

    ok = _call(read_file, ctx, skill="demo", path="references/api.md")
    assert not ok.is_error
    assert "# API" in ok.content
    assert ok.details["skill"] == "demo"

    assert _call(read_file, ctx, skill="nope", path="references/api.md").is_error
    missing = _call(read_file, ctx, skill="demo", path="references/missing.md")
    assert missing.is_error and "不存在" in missing.content

    outside = _call(read_file, ctx, skill="demo", path="../../outside.txt")
    assert outside.is_error and "越界" in outside.content


def test_skills_script_guards_and_execution(tmp_path):
    index = _skill_index(tmp_path)
    run_script = _tools(skills, "run_skill_script")[0]
    ctx = _ctx(tmp_path, skills=index)
    skill_dir = index.get("demo").path.parent

    assert _call(run_script, ctx, skill="nope", script="x.py").is_error
    assert _call(run_script, ctx, skill="demo", script="../escape.py").is_error
    assert _call(run_script, ctx, skill="demo", script="missing.py").is_error

    (skill_dir / "run.txt").write_text("x", encoding="utf-8")
    unsupported = _call(run_script, ctx, skill="demo", script="run.txt")
    assert unsupported.is_error
    assert "不支持的脚本类型" in unsupported.content

    (skill_dir / "run.py").write_text(
        "import sys\nprint('args=' + ','.join(sys.argv[1:]))\n", encoding="utf-8"
    )
    result = _call(
        run_script,
        ctx,
        skill="demo",
        script="run.py",
        args=["a", "b"],
        timeout_s=30,
    )
    assert not result.is_error
    assert result.content.strip() == "args=a,b"


def test_skills_tools_require_index(tmp_path):
    list_skills = _tools(skills, "list_skills")[0]
    ctx = _ctx(tmp_path)
    with pytest.raises(RuntimeError):
        _call(list_skills, ctx)
