"""稳定扩展契约。扩展只允许依赖本包与 ``markbot.schema``。"""

from markbot.api.events import ALL_EVENTS, BLOCKABLE, EventName
from markbot.api.extension import Command, CommandHandler, ExtensionAPI
from markbot.api.tool import Tool, ToolContext, ToolHandler, ToolResult
from markbot.api.ui import UI, ConsoleUI, NullUI
from markbot.schema.version import API_VERSION

__all__ = [
    "ALL_EVENTS",
    "API_VERSION",
    "BLOCKABLE",
    "Command",
    "CommandHandler",
    "ConsoleUI",
    "EventName",
    "ExtensionAPI",
    "NullUI",
    "Tool",
    "ToolContext",
    "ToolHandler",
    "ToolResult",
    "UI",
]
