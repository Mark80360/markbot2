"""网关日志：进程与线程的日志、警告、未捕获异常统一落到 ``~/.markbot/gateway/``。

文件按日期命名（``YYYY-MM-DD.log``），与 ``markbot gateway`` 自身的日志共用，
便于排查“程序没反应”这类没有输出到控制台的问题。
"""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

_LOCK = threading.Lock()
_INSTALLED: set[Path] = set()


def gateway_dir(home: Path) -> Path:
    return Path(home) / "gateway"


def gateway_log_path(home: Path) -> Path:
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    return gateway_dir(home) / f"{day}.log"


def write(home: Path, message: str) -> None:
    """线程安全地追加一行带时间戳的日志；写失败不影响主流程。

    所有写入口都经过一次脱敏：``logging`` 打出来的 URL、渠道日志里的消息正文
    都可能夹带密钥，不能直接落盘。
    """
    path = gateway_log_path(home)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    # 延迟导入：gateway_log 在包初始化早期就会被加载，避开循环导入
    from markbot.kernel.session import redact_secrets

    line = f"[{stamp}] {redact_secrets(message)}\n"
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass


class _GatewayFileHandler(logging.Handler):
    """每条日志都按当天日期落盘，避免跨天后继续写旧文件。"""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self.home = Path(home)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            write(self.home, self.format(record))
        except Exception:  # noqa: BLE001 - 日志失败不能拖垮业务
            self.handleError(record)


def install(home: Path) -> Path:
    """安装进程级日志钩子：logging、warnings、主线程与子线程未捕获异常。

    重复调用是安全的（同一进程只安装一次）。
    """
    path = gateway_log_path(home)
    if path.parent in _INSTALLED:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = _GatewayFileHandler(home)
    handler.setFormatter(
        logging.Formatter(
            "[%(threadName)s] %(levelname)s %(name)s: %(message)s"
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.captureWarnings(True)

    def _excepthook(exc_type, exc, tb) -> None:
        logging.getLogger("markbot").critical(
            "未捕获异常", exc_info=(exc_type, exc, tb)
        )

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        name = getattr(args.thread, "name", "?") if args.thread else "?"
        logging.getLogger("markbot").critical(
            "线程 %s 未捕获异常",
            name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_hook
    _INSTALLED.add(path.parent)
    return path


__all__ = ["gateway_dir", "gateway_log_path", "install", "write"]
