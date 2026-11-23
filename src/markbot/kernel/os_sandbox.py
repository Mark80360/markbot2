"""OS 级命令隔离（可选）。

``PathGuard.scan_command`` 只能识别命令里的字面路径，看不穿 ``python -c``、变量拼接、
``base64`` 解码这类写法。开启 ``[permissions] os_sandbox = "macos"`` 后，命令改由
``sandbox-exec`` 执行：受保护目录（MarkBot home、``<root>/.markbot``、额外受保护目录）
在内核层面禁止读写，只有例外目录（``<home>/workspace`` 读写、``<home>/media`` 与
``sessions/<id>/files`` 只读）重新开放。

这是「防误用 + 防越界」的纵深防御，不是完整沙箱：网络、进程、工作区内的文件不受限制。
不可信代码仍应放进容器或低权限用户运行。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from markbot.kernel.paths import PathGuard

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
MODES = frozenset({"off", "macos"})


def available(mode: str) -> bool:
    return mode == "macos" and sys.platform == "darwin" and SANDBOX_EXEC.exists()


def _quote(path: Path) -> str:
    # SBPL 字符串：反斜杠与双引号需要转义
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _variants(path: Path) -> list[Path]:
    """macOS 上 /var、/tmp 是 /private 下的符号链接：两种写法都要覆盖。"""
    out = [path]
    text = str(path)
    if text.startswith("/private/"):
        out.append(Path(text[len("/private"):]))
    elif text.startswith(("/var/", "/tmp/", "/etc/")):
        out.append(Path("/private" + text))
    return out


def build_profile(guard: PathGuard, *, extra_readable: tuple[Path, ...] = ()) -> str:
    """生成 SBPL：默认放行，再拒绝受保护目录，最后重新开放例外目录（后写的规则优先）。

    ``extra_readable``：本次执行额外只读开放的目录（例如学习层里某个技能自己的目录，
    技能脚本要能读到自身文件与 references/）。
    """
    lines = ["(version 1)", "(allow default)"]
    for base in guard.protected:
        for path in _variants(base):
            lines.append(f"(deny file-read* file-write* (subpath {_quote(path)}))")
    for base in guard.writable:
        for path in _variants(base):
            lines.append(f"(allow file-read* file-write* (subpath {_quote(path)}))")
    for base in guard.readable:
        for path in _variants(base):
            lines.append(f"(allow file-read* (subpath {_quote(path)}))")
    for base in extra_readable:
        for path in _variants(Path(base).resolve()):
            lines.append(f"(allow file-read* (subpath {_quote(path)}))")
    if guard.home is not None:
        # 超限工具结果的落盘目录：sessions/<id>/files/** 只读
        for home in _variants(guard.home):
            prefix = re.escape(str(home / "sessions"))
            lines.append(f'(allow file-read* (regex #"^{prefix}/[^/]+/files(/|$)"))')
    return "\n".join(lines) + "\n"


def wrap(command: str, guard: PathGuard) -> list[str]:
    """返回在 sandbox-exec 下用 /bin/sh 执行 ``command`` 的 argv。"""
    return [str(SANDBOX_EXEC), "-p", build_profile(guard), "/bin/sh", "-c", command]


def wrap_argv(
    argv: list[str], guard: PathGuard, *, extra_readable: tuple[Path, ...] = ()
) -> list[str]:
    """不经 shell 的 argv 版本。"""
    return [
        str(SANDBOX_EXEC), "-p", build_profile(guard, extra_readable=extra_readable), *argv
    ]


__all__ = ["MODES", "SANDBOX_EXEC", "available", "build_profile", "wrap", "wrap_argv"]
