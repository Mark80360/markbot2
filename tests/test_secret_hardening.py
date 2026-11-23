"""密钥脱敏与子进程回收的回归测试。

覆盖 2026-11-20 服务器日志里的两类泄露和超时后残留进程：
- 日志把 ``SECRET = "xxx"`` 序列化成 JSON 后，形状正则漏掉了密钥；
- ``cat config.toml`` 的工具输出原样进入模型上下文；
- 命令超时/回合取消后，``cmd &`` 起的后台子进程残留，并阻塞输出读取。
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

from markbot.api.sandbox import run_shell_sandboxed
from markbot.kernel import session as session_mod
from markbot.kernel.config import load_config, secret_values
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.session import REDACTED, redact_known, redact_secrets, register_secrets


@pytest.fixture(autouse=True)
def _clean_registry():
    session_mod.clear_registered_secrets()
    yield
    session_mod.clear_registered_secrets()


# ------------------------------------------------------------ 形状正则


def test_pattern_redacts_json_escaped_assignment():
    raw = 'FEISHU_APP_SECRET = "51Lb5K1TlVNmKjXvUxTEST"'
    logged = json.dumps({"content": raw}, ensure_ascii=False)
    out = redact_secrets(logged)
    assert "51Lb5K1TlVNmKjXvUxTEST" not in out
    assert json.loads(out)["content"].startswith("FEISHU_APP_SECRET")


def test_pattern_redacts_json_key_value():
    out = redact_secrets('{"api_key": "abcdEFGH12345678"}')
    assert "abcdEFGH12345678" not in out
    assert json.loads(out) == {"api_key": REDACTED}


def test_pattern_keeps_plain_unquoted_behaviour():
    out = redact_secrets("CRAWL4AI_API_TOKEN=3a6ba6a4aa1422ffee")
    assert "3a6ba6a4aa1422ffee" not in out


# ------------------------------------------------------------ 按值脱敏


def test_known_secret_redacted_regardless_of_shape():
    register_secrets(["plainvalue-without-shape-9f3k"])
    text = "result: plainvalue-without-shape-9f3k done"
    assert "plainvalue-without-shape-9f3k" not in redact_known(text)
    assert "plainvalue-without-shape-9f3k" not in redact_secrets(text)


def test_known_secret_json_escaped_form():
    secret = 'ab"cd\\ef-12345678'
    register_secrets([secret])
    logged = json.dumps({"x": secret})
    out = redact_secrets(logged)
    assert "12345678" not in out
    json.loads(out)


def test_short_or_empty_values_not_registered():
    register_secrets(["abc", "", None])
    assert redact_known("abc and more") == "abc and more"


def test_longer_secret_replaced_before_its_prefix():
    register_secrets(["prefix-secret", "prefix-secret-longer-part"])
    out = redact_known("v=prefix-secret-longer-part")
    assert out == f"v={REDACTED}"


def test_secret_values_collects_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_TEST_MCP_TOKEN", "mcp-token-value-123456")
    (tmp_path / "config.toml").write_text(
        """
[agent]
api_key = "sk-agent-key-000000"
max_tokens = 100

[secrets]
FEISHU_APP_SECRET = "feishu-secret-000000"

[channels.feishu]
app_id = "cli_not_a_secret"
app_secret = "channel-secret-000000"

[tools.mcp_servers.remote]
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ${MB_TEST_MCP_TOKEN}" }
""",
        encoding="utf-8",
    )
    cfg = load_config(home=tmp_path, project_root=tmp_path)
    values = set(secret_values(cfg))
    assert {
        "sk-agent-key-000000",
        "feishu-secret-000000",
        "channel-secret-000000",
        "mcp-token-value-123456",
    } <= values
    assert "cli_not_a_secret" not in values
    assert not any("${" in value for value in values)


# ------------------------------------------------------------ 工具结果


def test_tool_result_masks_registered_secret(make_kernel):
    register_secrets(["tool-output-secret-123456"])
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall(id="c1", name="echo", args={"text": "v=tool-output-secret-123456"})
                ]
            ),
            "ok",
        ]
    )
    kernel = make_kernel(provider=provider)
    asyncio.run(kernel.run("echo", archive=False))
    tool_messages = [m for m in kernel.session.messages if m.role == "tool"]
    assert len(tool_messages) == 1
    assert "tool-output-secret-123456" not in tool_messages[0].content
    assert REDACTED in tool_messages[0].content


def test_tool_result_without_secret_is_unchanged(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[ToolCall(id="c1", name="echo", args={"text": "hi"})]),
            "ok",
        ]
    )
    kernel = make_kernel(provider=provider)
    asyncio.run(kernel.run("echo", archive=False))
    tool_messages = [m for m in kernel.session.messages if m.role == "tool"]
    assert tool_messages[0].content == "hi"


# ------------------------------------------------------------ 子进程回收

posix_only = pytest.mark.skipif(os.name != "posix", reason="进程组语义仅 POSIX")
_BACKGROUND = "sleep 30 & echo $! > child.pid; sleep 30"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _child_pid(tmp_path) -> int:
    deadline = time.monotonic() + 3
    path = tmp_path / "child.pid"
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text().strip())
        time.sleep(0.05)
    raise AssertionError("后台子进程 pid 未写出")


@posix_only
def test_timeout_kills_background_children_and_returns_quickly(tmp_path):
    started = time.monotonic()
    result = asyncio.run(run_shell_sandboxed(_BACKGROUND, cwd=tmp_path, timeout_s=0.5))
    elapsed = time.monotonic() - started
    assert result.timed_out
    # 旧实现只杀 shell，后台 sleep 占着管道，这里会等满 30 秒
    assert elapsed < 10
    assert _wait_dead(_child_pid(tmp_path))


@posix_only
def test_outer_cancellation_kills_process_group(tmp_path):
    async def main() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                run_shell_sandboxed(_BACKGROUND, cwd=tmp_path, timeout_s=60),
                timeout=0.5,
            )

    asyncio.run(main())
    assert _wait_dead(_child_pid(tmp_path))
