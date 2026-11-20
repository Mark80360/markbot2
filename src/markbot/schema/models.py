"""纯数据模型：无内部依赖，供所有包共享。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ToolCallRecord(_Model):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(_Model):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False


class SessionMeta(_Model):
    id: str
    started_at: str
    ended_at: str | None = None
    domain: str = "general"
    status: str = "running"
    harness_snapshot: str | None = None
    steps: int = 0
    tool_calls: int = 0
    compactions: int = 0
    request: str = ""


class SnapshotEntry(_Model):
    path: str
    source: str = "learned"
    origin_task: str | None = None
    sha256: str | None = None


class LearnedManifest(_Model):
    snapshot: str = ""
    parent: str | None = None
    created_at: str
    entries: list[SnapshotEntry] = Field(default_factory=list)


class PackagePermissions(_Model):
    capabilities: list[str] = Field(default_factory=list)


class PackageResources(_Model):
    extensions: list[str] = Field(default_factory=lambda: ["./extensions"])
    skills: list[str] = Field(default_factory=lambda: ["./skills"])
    prompts: list[str] = Field(default_factory=lambda: ["./prompts/*.md"])


class PackageManifest(_Model):
    name: str
    version: str = "0.0.0"
    description: str = ""
    priority: int = 0
    license: str | None = None
    permissions: PackagePermissions = Field(default_factory=PackagePermissions)
    resources: PackageResources = Field(default_factory=PackageResources)


__all__ = [
    "LearnedManifest",
    "Message",
    "PackageManifest",
    "PackagePermissions",
    "PackageResources",
    "SessionMeta",
    "SnapshotEntry",
    "ToolCallRecord",
]
