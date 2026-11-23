"""密钥注入：模型只写名称，值在执行时以环境变量注入，任何时候都不进入上下文。"""

from __future__ import annotations

import asyncio
import json

import pytest

from markbot.api.ui import NullUI
from markbot.extensions import shell
from markbot.kernel import session as session_mod
from markbot.kernel.config import PermissionConfig, load_config
from markbot.kernel.execution import LocalExecution
from markbot.kernel.permissions import PermissionPolicy, command_programs
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import SourceSet
from markbot.kernel.session import REDACTED, Session
from markbot.kernel.tools import KernelToolContext

# 测试用的假密钥值：不符合任何密钥形状规则，只能靠「按登记值遮蔽」拦住
SECRET = "-".join(["mb", "inject", "value", "7f3a9c2e81d4"])


@pytest.fixture(autouse=True)
def _clean_registry():
    session_mod.clear_registered_secrets()
    yield
    session_mod.clear_registered_secrets()


# ------------------------------------------------------------ 命令解析


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('curl -H "A: $T" "http://x/?a=1&b=2"', ["curl"]),
        ("curl a | jq .", ["curl", "jq"]),
        ("curl a; bash evil.sh", ["curl", "bash"]),
        ("curl a\nbash evil.sh", ["curl", "bash"]),
        ("curl a && echo ok || cat /etc/passwd", ["curl", "echo", "cat"]),
        ("curl a > out.json 2>&1", ["curl"]),
        ("TOKEN=1 curl a", ["curl"]),
        ("(curl a)", ["curl"]),
        ("/usr/bin/curl a & sleep 1", ["curl", "sleep"]),
    ],
)
def test_command_programs(command, expected):
    assert command_programs(command) == expected


@pytest.mark.parametrize("command", ["curl $(cat f)", "curl `id`", 'curl "open', "cat <(ls)"])
def test_command_programs_rejects_unparseable(command):
    assert command_programs(command) is None


# ------------------------------------------------------------ 策略


def _policy(profile: str = "trusted", **secret_env) -> PermissionPolicy:
    return PermissionPolicy(PermissionConfig(profile=profile, secret_env=secret_env))


def test_injection_allowed_for_declared_program():
    decision = _policy(CRAWL_TOKEN=["curl"]).check_secret_injection(
        ["CRAWL_TOKEN"], 'curl -H "X-Token: $CRAWL_TOKEN" http://127.0.0.1:11235/health'
    )
    assert decision.allowed


@pytest.mark.parametrize(
    "command",
    [
        "curl x | base64",
        "curl x; echo $CRAWL_TOKEN",
        "curl x\nprintenv",
        "curl $(cat f)",
    ],
)
def test_injection_denied_when_other_programs_present(command):
    decision = _policy(CRAWL_TOKEN=["curl"]).check_secret_injection(["CRAWL_TOKEN"], command)
    assert not decision.allowed


def test_injection_denied_for_unregistered_secret():
    decision = _policy(CRAWL_TOKEN=["curl"]).check_secret_injection(["OTHER_KEY"], "curl x")
    assert not decision.allowed
    assert "secret_env" in decision.reason


def test_injection_denied_for_reserved_names_and_strict():
    assert not _policy(PATH=["*"]).check_secret_injection(["PATH"], "ls").allowed
    assert not _policy("strict", A_KEY=["*"]).check_secret_injection(["A_KEY"], "ls").allowed


def test_wildcard_and_list_shorthand():
    assert _policy(A_KEY=["*"]).check_secret_injection(["A_KEY"], "python3 x.py | jq .").allowed
    policy = PermissionPolicy(PermissionConfig(secret_env=["A_KEY"]))  # type: ignore[arg-type]
    assert policy.injectable_secrets() == ["A_KEY"]


def test_secret_env_loaded_from_toml(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[secrets]\nCRAWL_TOKEN = "x"\n\n[permissions.secret_env]\nCRAWL_TOKEN = ["curl"]\n',
        encoding="utf-8",
    )
    cfg = load_config(home=tmp_path, project_root=tmp_path)
    assert cfg.permissions.secret_env == {"CRAWL_TOKEN": ["curl"]}


# ------------------------------------------------------------ 工具


def _run_command_tool(config=None):
    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI(), config=config or {})
    shell.setup(api)
    api.commit()
    return {tool.name: tool for tool in source.tools}["run_command"]


def _call(tool, ctx, **kwargs):
    async def invoke():
        return await tool.handler(ctx, tool.params(**kwargs))

    return asyncio.run(invoke())


def _ctx(tmp_path, *, secrets=None, **secret_env):
    policy = PermissionPolicy(
        PermissionConfig(profile="trusted", secret_env=secret_env), root=tmp_path
    )
    return KernelToolContext(
        session=Session("测试"), policy=policy, secrets=secrets or {}, env=LocalExecution(tmp_path)
    )


def test_run_command_injects_secret_as_env(tmp_path):
    tool = _run_command_tool()
    ctx = _ctx(tmp_path, secrets={"MB_TOKEN": SECRET}, MB_TOKEN=["printf"])
    result = _call(tool, ctx, command='printf "%s" "$MB_TOKEN"', secrets=["MB_TOKEN"])
    assert not result.is_error
    assert result.content == SECRET  # 工具内部拿到真实值；遮蔽发生在写回会话时
    assert result.details["secrets"] == ["MB_TOKEN"]


def test_run_command_without_secrets_does_not_see_env(tmp_path):
    tool = _run_command_tool()
    ctx = _ctx(tmp_path, secrets={"MB_TOKEN": SECRET}, MB_TOKEN=["printf"])
    result = _call(tool, ctx, command='printf "[%s]" "$MB_TOKEN"')
    assert result.content == "[]"


def test_run_command_denies_unlisted_program(tmp_path):
    tool = _run_command_tool()
    ctx = _ctx(tmp_path, secrets={"MB_TOKEN": SECRET}, MB_TOKEN=["curl"])
    result = _call(tool, ctx, command='printf "%s" "$MB_TOKEN"', secrets=["MB_TOKEN"])
    assert result.is_error
    assert result.details.get("denied") is True
    assert SECRET not in result.content


def test_run_command_reports_missing_secret_value(tmp_path):
    tool = _run_command_tool()
    ctx = _ctx(tmp_path, secrets={}, MB_TOKEN=["printf"])
    result = _call(tool, ctx, command="printf ok", secrets=["MB_TOKEN"])
    assert result.is_error
    assert "[secrets]" in result.content


def test_description_lists_only_names():
    tool = _run_command_tool(
        {"permissions": {"secret_env": {"CRAWL4AI_API_TOKEN": ["curl"]}}}
    )
    assert "CRAWL4AI_API_TOKEN" in tool.description
    assert "secrets" in tool.description
    assert "当前没有可注入的密钥" in _run_command_tool().description


# ------------------------------------------------------------ 端到端


def test_end_to_end_secret_never_reaches_model_or_archive(tmp_path, home, project, make_kernel):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        "[permissions]\n"
        'profile = "trusted"\n'
        'allow_exec = ["printf"]\n\n'
        "[permissions.secret_env]\n"
        'MB_TOKEN = ["printf"]\n\n'
        "[secrets]\n"
        f'MB_TOKEN = "{SECRET}"\n',
        encoding="utf-8",
    )
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="run_command",
                        args={"command": 'printf "got:%s" "$MB_TOKEN"', "secrets": ["MB_TOKEN"]},
                    )
                ]
            ),
            "完成",
        ]
    )
    kernel = make_kernel(provider=provider, config=load_config(home=home, project_root=project))
    result = asyncio.run(kernel.run("用密钥调接口"))
    assert result.stopped_reason == "completed"

    tool_messages = [m for m in kernel.session.messages if m.role == "tool"]
    assert tool_messages[0].content.startswith(f"got:{REDACTED}")
    assert all(SECRET not in json.dumps(m.model_dump(), ensure_ascii=False)
               for m in kernel.session.messages)

    archive = home / "sessions" / result.session_id
    for path in archive.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_text(encoding="utf-8", errors="ignore"), path
    for path in (home / "logs").rglob("*.md"):
        assert SECRET not in path.read_text(encoding="utf-8", errors="ignore"), path
