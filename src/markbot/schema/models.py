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
    # 附带的图片路径（ADR 0015）：只记路径，发给模型时 provider 才读取编码
    images: list[str] = Field(default_factory=list)


class SessionEvent(_Model):
    """会话事件：append-only 日志的唯一记录单元。

    只记录会话状态变化（用户消息、助手消息、工具结果、压缩、遮蔽），不记录 provider
    原始 payload；``seq`` 在同一会话内严格递增，投影时按它排序。

    ``redaction``（schema 1.1 新增）：把投影中指定下标的消息替换成占位文本并清空
    工具参数，用于移除触发上游内容审核的上下文。下标相对于该事件之前的投影结果。
    """

    schema_version: int = 1
    seq: int
    ts: str
    type: Literal[
        "user_message", "assistant_message", "tool_result", "compaction", "redaction"
    ]
    data: dict[str, Any] = Field(default_factory=dict)


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
    "SessionEvent",
    "SessionMeta",
    "SnapshotEntry",
    "ToolCallRecord",
]
