"""cron 能力包：注册 cron 工具。

调度服务由 ``markbot serve`` / ``markbot gateway`` 进程创建并注入，工具通过
``get_service`` 懒查找，避免扩展直接依赖 Kernel。
"""

from __future__ import annotations

from markbot.api import ExtensionAPI
from markbot.cron.service import get_service
from markbot.cron.tool import build_cron_tool


def setup(api: ExtensionAPI) -> None:
    api.register_tool(build_cron_tool(get_service))
