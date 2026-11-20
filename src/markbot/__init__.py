"""MarkBot2：极薄 Kernel + 四条扩展车道。

Kernel 只认识 ``markbot.api`` / ``markbot.schema``，不 import 任何具体扩展。
"""

from markbot.schema.version import API_VERSION, SCHEMA_VERSION

__version__ = "0.1.0"

__all__ = ["API_VERSION", "SCHEMA_VERSION", "__version__"]
