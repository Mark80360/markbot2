"""扩展间共享、跨热重载存活的进程内状态（加载器跳过 ``_`` 开头的文件；ADR 0012）。

运行时热重载会重新导入扩展模块；后台进程表若放在扩展模块里，重载后句柄就找不到了。
这里通过普通的绝对导入只存在一份。
"""

from __future__ import annotations

import itertools
import threading
from typing import Any

PROCS: dict[str, Any] = {}
PROCS_LOCK = threading.Lock()
PROC_COUNTER = itertools.count(1)

__all__ = ["PROCS", "PROCS_LOCK", "PROC_COUNTER"]
