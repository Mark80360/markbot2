"""任务规划：``todo_write`` / ``todo_read``（ADR 0012）。

长任务（多文件修改、调研）先列计划、逐项推进。清单挂在会话上，并且：

- 每轮由上下文组装注入系统提示（``todos`` 分节），压缩后也不会丢；
- 进程重启后从会话事件里最后一次 ``todo_write`` 调用的参数恢复（不新增事件类型）；
- ``/status`` 可以看到进度。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult

MAX_ITEMS = 50
_MARK = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}


class TodoItem(BaseModel):
    content: str = Field(description="这一步要做什么（祈使句，简短具体）", min_length=1, max_length=300)
    status: Literal["pending", "in_progress", "completed", "cancelled"] = Field(default="pending")


class WriteIn(BaseModel):
    items: list[TodoItem] = Field(
        description="完整的任务清单（整体替换旧清单）；同一时间最多一项 in_progress",
        max_length=MAX_ITEMS,
    )


class EmptyIn(BaseModel):
    pass


def _restore(session: Any) -> list[dict[str, str]]:
    """从会话事件里找最后一次 todo_write 的参数（重启后恢复）。"""
    for event in reversed(list(getattr(session, "events", None) or [])):
        if getattr(event, "type", "") != "assistant_message":
            continue
        for call in reversed((getattr(event, "data", None) or {}).get("tool_calls") or []):
            if isinstance(call, dict) and call.get("name") == "todo_write":
                items = (call.get("arguments") or {}).get("items") or []
                return [
                    {"content": str(item.get("content", "")), "status": str(item.get("status", "pending"))}
                    for item in items
                    if isinstance(item, dict) and item.get("content")
                ]
    return []


def current(session: Any) -> list[dict[str, str]]:
    todos = getattr(session, "todos", None)
    if todos is None:
        todos = _restore(session)
        try:
            session.todos = todos
        except AttributeError:
            pass
    return todos


def render(todos: list[dict[str, str]]) -> str:
    if not todos:
        return "（没有任务清单）"
    done = sum(1 for item in todos if item["status"] == "completed")
    lines = [f"任务清单（完成 {done}/{len(todos)}）:"]
    for index, item in enumerate(todos, 1):
        lines.append(f"{index}. {_MARK.get(item['status'], '[ ]')} {item['content']}")
    return "\n".join(lines)


def setup(api: ExtensionAPI) -> None:
    async def todo_write(ctx, args: WriteIn) -> ToolResult:
        items = [{"content": item.content.strip(), "status": item.status} for item in args.items]
        active = [item for item in items if item["status"] == "in_progress"]
        if len(active) > 1:
            return ToolResult.error("同一时间只能有一项 in_progress；请把其它项改回 pending 或 completed")
        try:
            ctx.session.todos = items
        except AttributeError:
            return ToolResult.error("当前会话不支持任务清单")
        return ToolResult(content=render(items), details={"count": len(items)})

    async def todo_read(ctx, args: EmptyIn) -> ToolResult:
        return ToolResult(content=render(current(ctx.session)))

    api.register_tool(
        Tool(
            name="todo_write",
            description=(
                "写入/更新本次任务的步骤清单（整体替换）。多步骤任务开始时先列计划，"
                "每开始一项标 in_progress，完成后立即标 completed"
            ),
            params=WriteIn,
            handler=todo_write,
        )
    )
    api.register_tool(
        Tool(
            name="todo_read",
            description="查看当前任务清单与进度",
            params=EmptyIn,
            handler=todo_read,
        )
    )


__all__ = ["current", "render", "setup"]
