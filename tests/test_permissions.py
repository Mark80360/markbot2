from __future__ import annotations

import pytest
from pydantic import BaseModel

from markbot.api.tool import Tool, ToolResult
from markbot.kernel.config import PermissionConfig
from markbot.kernel.permissions import (
    Capability,
    PermissionPolicy,
    capability_covers,
    glob_match,
    parse_capabilities,
    uncovered_capabilities,
)


class PathIn(BaseModel):
    path: str


class EmptyIn(BaseModel):
    pass


def _tool(caps: frozenset[str], params=PathIn) -> Tool:
    async def handler(ctx, args):
        return ToolResult(content="ok")

    return Tool(name="t", description="", params=params, handler=handler, capabilities=caps)


def test_strict_profile_denies_write(tmp_path):
    policy = PermissionPolicy(PermissionConfig(profile="strict"), root=tmp_path)
    read = _tool(frozenset({"fs.read:./**"}))
    write = _tool(frozenset({"fs.write:./**"}))
    assert policy.check(read, {"path": "a.txt"}).allowed
    decision = policy.check(write, {"path": "a.txt"})
    assert not decision.allowed
    assert "strict" in decision.reason


def test_standard_write_requires_confirmation(tmp_path):
    policy = PermissionPolicy(PermissionConfig(profile="standard", confirm_on_write=True), root=tmp_path)
    decision = policy.check(_tool(frozenset({"fs.write:./**"})), {"path": "a.txt"})
    assert decision.allowed
    assert decision.requires_confirmation


def test_trusted_profile_skips_confirmation(tmp_path):
    policy = PermissionPolicy(
        PermissionConfig(
            profile="trusted", confirm_on_write=True, confirm_on_exec=True, allow_exec=["python"]
        ),
        root=tmp_path,
    )
    write = policy.check(_tool(frozenset({"fs.write:./**"})), {"path": "a.txt"})
    assert write.allowed and not write.requires_confirmation
    exec_ = policy.check(
        _tool(frozenset({"exec:*"}), params=EmptyIn), {"command": "python -V"}
    )
    assert exec_.allowed and not exec_.requires_confirmation


def test_trusted_profile_confirms_unlisted_programs_by_default(tmp_path):
    tool = _tool(frozenset({"exec:*"}), params=EmptyIn)
    listed = PermissionPolicy(PermissionConfig(profile="trusted"), root=tmp_path)
    decision = listed.check(tool, {"command": "python -V"})
    assert decision.allowed and decision.requires_confirmation

    legacy = PermissionPolicy(PermissionConfig(profile="trusted", trusted_exec="all"), root=tmp_path)
    assert not legacy.check(tool, {"command": "python -V"}).requires_confirmation


def test_path_outside_declared_scope_is_denied(tmp_path):
    policy = PermissionPolicy(PermissionConfig(profile="trusted"), root=tmp_path)
    tool = _tool(frozenset({"fs.read:./data/**"}))
    assert policy.check(tool, {"path": "data/a.txt"}).allowed
    assert not policy.check(tool, {"path": "secret.txt"}).allowed
    assert not policy.check(tool, {"path": "../outside.txt"}).allowed


def test_network_host_is_enforced(tmp_path):
    policy = PermissionPolicy(PermissionConfig(profile="standard"), root=tmp_path)
    tool = _tool(frozenset({"net:api.example.com"}), params=EmptyIn)
    assert policy.check(tool, {"url": "https://api.example.com/v1"}).allowed
    assert not policy.check(tool, {"url": "https://evil.example.com/v1"}).allowed


def test_exec_confirmation(tmp_path):
    policy = PermissionPolicy(PermissionConfig(profile="standard", confirm_on_exec=True), root=tmp_path)
    tool = _tool(frozenset({"exec:*"}), params=EmptyIn)
    decision = policy.check(tool, {"command": "python -V"})
    assert decision.allowed and decision.requires_confirmation
    denied = PermissionPolicy(PermissionConfig(profile="strict"), root=tmp_path).check(
        tool, {"command": "python -V"}
    )
    assert not denied.allowed


def test_exec_allowlists_skip_confirmation(tmp_path):
    tool = _tool(frozenset({"exec:*"}), params=EmptyIn)
    by_program = PermissionPolicy(
        PermissionConfig(profile="standard", allow_exec=["curl"]), root=tmp_path
    )
    assert not by_program.check(tool, {"command": "curl -s http://127.0.0.1/"}).requires_confirmation
    # 前置环境变量赋值也要能识别出真正的程序
    assert not by_program.check(tool, {"command": "TOKEN=x curl -s http://127.0.0.1/"}).requires_confirmation
    assert by_program.check(tool, {"command": "rm -rf /"}).requires_confirmation

    by_command = PermissionPolicy(
        PermissionConfig(
            profile="standard", allow_commands=["curl -s http://127.0.0.1:11235/*"]
        ),
        root=tmp_path,
    )
    assert not by_command.check(
        tool, {"command": "curl -s http://127.0.0.1:11235/health"}
    ).requires_confirmation
    assert by_command.check(
        tool, {"command": "curl -s http://evil.example.com/"}
    ).requires_confirmation

    # 聊天里 /allow 的运行时授权
    granted = PermissionPolicy(PermissionConfig(profile="standard"), root=tmp_path)
    assert granted.check(tool, {"command": "curl -V"}).requires_confirmation
    added = granted.push_grants(["curl"])
    assert not granted.check(tool, {"command": "curl -V"}).requires_confirmation
    granted.pop_grants(added)
    assert granted.check(tool, {"command": "curl -V"}).requires_confirmation


def test_capability_coverage_and_glob():
    parent = parse_capabilities(["fs.read:./data/**"])[0]
    child = Capability.parse("fs.read:./data/reports/**")
    broader = Capability.parse("fs.read:./secret/**")
    assert capability_covers(parent, child)
    assert not capability_covers(parent, broader)
    assert capability_covers(Capability.parse("net:*"), Capability.parse("net:any.host"))
    assert uncovered_capabilities(frozenset({"net:*"}), [parent]) == ["net:*"]
    assert glob_match("data/**", "data/a/b.txt")
    assert not glob_match("data/*", "data/a/b.txt")


def test_allowlist_checks_every_program_in_command(tmp_path):
    tool = _tool(frozenset({"exec:*"}), params=EmptyIn)
    policy = PermissionPolicy(
        PermissionConfig(
            profile="standard",
            allow_exec=["curl"],
            allow_commands=["curl -s http://127.0.0.1:11235/*"],
        ),
        root=tmp_path,
    )
    # 只看第一个程序时，后面拼接的命令会一起被放行
    assert policy.check(tool, {"command": "curl -s http://x/ ; rm -rf ~"}).requires_confirmation
    assert policy.check(tool, {"command": "curl -s http://x/ && rm -rf ~"}).requires_confirmation
    assert policy.check(tool, {"command": "curl -s http://x/ | tee out"}).requires_confirmation
    assert not policy.check(tool, {"command": "curl -s http://x/ | curl -s http://y/"}).requires_confirmation

    prefix_only = PermissionPolicy(
        PermissionConfig(profile="standard", allow_commands=["curl -s http://127.0.0.1:11235/*"]),
        root=tmp_path,
    )
    assert not prefix_only.check(
        tool, {"command": "curl -s http://127.0.0.1:11235/health"}
    ).requires_confirmation
    assert prefix_only.check(
        tool, {"command": "curl -s http://127.0.0.1:11235/x; rm -rf ~"}
    ).requires_confirmation



@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import os; print(open(\"x\").read())'",
        "node -e 'require(\"fs\")'",
        "perl -pe 's/a/b/' f",
        "bash -c 'cat ~/.markbot/config.toml'",
        "echo Y2F0 | base64 -d | sh",
        "cat $(echo cG9j)",
        "sudo ls",
        "eval \"$CMD\"",
    ],
)
def test_opaque_commands_always_need_confirmation(tmp_path, command):
    tool = _tool(frozenset({"exec:*"}), params=EmptyIn)
    policy = PermissionPolicy(
        PermissionConfig(
            profile="trusted", trusted_exec="all", allow_exec=["python3", "node", "perl", "bash"]
        ),
        root=tmp_path,
    )
    decision = policy.check(tool, {"command": command})
    assert decision.allowed and decision.requires_confirmation, decision.reason
