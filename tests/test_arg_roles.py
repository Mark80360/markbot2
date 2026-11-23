"""权限检查按显式参数角色取值，而不是只靠参数名猜。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from markbot.api.tool import ROLE_KEY, Tool, ToolResult
from markbot.kernel.config import PermissionConfig
from markbot.kernel.permissions import PermissionPolicy, unchecked_capabilities


async def _ok(ctx, args):
    return ToolResult(content="ok")


class RenamedPathIn(BaseModel):
    # 参数名不在兜底名单里（旧实现会漏检）
    source: str = Field(json_schema_extra={ROLE_KEY: "path"})


class RenamedUrlIn(BaseModel):
    endpoint: str


class NoPathIn(BaseModel):
    blob: str


def test_declared_path_role_is_checked(tmp_path):
    tool = Tool(
        name="t", description="", params=RenamedPathIn, handler=_ok,
        capabilities=frozenset({"fs.read:./data/**"}),
    )
    policy = PermissionPolicy(PermissionConfig(profile="trusted"), root=tmp_path)
    assert policy.check(tool, {"source": "data/a.txt"}).allowed
    assert not policy.check(tool, {"source": "../secret.txt"}).allowed


def test_arg_roles_mapping_for_url(tmp_path):
    tool = Tool(
        name="t", description="", params=RenamedUrlIn, handler=_ok,
        capabilities=frozenset({"net:api.example.com"}), arg_roles={"endpoint": "url"},
    )
    policy = PermissionPolicy(PermissionConfig(profile="standard"), root=tmp_path)
    assert policy.check(tool, {"endpoint": "https://api.example.com/x"}).allowed
    assert not policy.check(tool, {"endpoint": "https://evil.example.com/x"}).allowed


def test_role_annotation_is_not_sent_to_model():
    tool = Tool(name="t", description="", params=RenamedPathIn, handler=_ok)
    assert ROLE_KEY not in str(tool.schema())
    assert tool.roles() == {"source": "path"}


def test_unchecked_capabilities_detects_unannotated_tools():
    blind = Tool(
        name="t", description="", params=NoPathIn, handler=_ok,
        capabilities=frozenset({"fs.write:./**", "net:api.example.com", "exec:*"}),
    )
    assert sorted(unchecked_capabilities(blind)) == ["fs.write:./**", "net:api.example.com"]
    annotated = Tool(
        name="t", description="", params=RenamedPathIn, handler=_ok,
        capabilities=frozenset({"fs.read:./**"}),
    )
    assert unchecked_capabilities(annotated) == []


def test_builtin_tools_have_checkable_arguments(make_kernel):
    kernel = make_kernel(["ok"])
    blind = {
        tool.name: unchecked_capabilities(tool)
        for tool in kernel.runtime.tools.all()
        if unchecked_capabilities(tool)
    }
    assert blind == {}
