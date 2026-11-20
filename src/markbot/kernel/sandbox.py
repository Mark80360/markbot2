"""Kernel 内部使用的沙箱执行；实现位于稳定契约 ``markbot.api.sandbox``。"""

from markbot.api.sandbox import (
    ENV_ALLOWLIST,
    ProcessResult,
    build_env,
    run_sandboxed,
    run_shell_sandboxed,
)

__all__ = [
    "ENV_ALLOWLIST",
    "ProcessResult",
    "build_env",
    "run_sandboxed",
    "run_shell_sandboxed",
]
