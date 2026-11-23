"""版本号：``markbot.schema`` 是唯一兼容性判断依据。"""

from __future__ import annotations

SCHEMA_VERSION = "1.1.0"  # 1.1：会话事件新增 redaction
API_VERSION = "1.0"
MIN_PYTHON = (3, 12)

__all__ = ["SCHEMA_VERSION", "API_VERSION", "MIN_PYTHON"]
