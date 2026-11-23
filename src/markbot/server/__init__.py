"""应用服务层：HTTP 入口等长驻服务。依赖 kernel / cron / memory，不依赖 cli。"""

from markbot.server.http import run_server

__all__ = ["run_server"]
