"""本机进程间鉴权：HTTP 入口 <-> 渠道 <-> 定时任务投递共用的共享令牌。

背景：``markbot serve`` 的 ``/chat`` 会接收 ``grants`` 与回调地址；没有鉴权时，本机任何
进程（或监听 0.0.0.0 时同网段的任何人）都能自带授权、把「确认」指向自己控制的地址，
权限确认形同虚设。渠道侧的 ``/notify`` 同理可被用来冒充机器人发消息。

约定：

- 令牌保存在 ``<home>/gateway/auth.token``，首次使用时生成，文件权限 0600。
- 所有内部 HTTP 调用都带 ``X-MarkBot-Token`` 头；服务端用常量时间比较校验。
- 回调地址（确认、通知）只接受回环地址上的 http(s) URL，防止把请求转发到外部。

本模块不依赖其它层，渠道（独立进程）也可以直接 import。
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

HEADER = "X-MarkBot-Token"
TOKEN_FILE = Path("gateway") / "auth.token"
_MIN_TOKEN_CHARS = 32


def token_path(home: Path) -> Path:
    return Path(home).expanduser() / TOKEN_FILE


def load_or_create_token(home: Path) -> str:
    """读取共享令牌；不存在时以 0600 权限原子创建。"""
    path = token_path(home)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        text = ""
    if len(text) >= _MIN_TOKEN_CHARS:
        _tighten(path)
        return text
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        # 另一个进程（服务端 / 渠道）抢先创建：以它为准
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= _MIN_TOKEN_CHARS:
            return existing
        path.unlink(missing_ok=True)
        fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def _tighten(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
    except OSError:
        pass


def verify(presented: str | None, expected: str) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.strip().encode(), expected.strip().encode())


def is_loopback_host(host: str) -> bool:
    text = (host or "").strip().strip("[]").lower()
    if text in {"localhost", ""}:
        return text == "localhost"
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


@lru_cache(maxsize=64)
def is_local_address(host: str) -> bool:
    """``host`` 是否为本机网卡上的 IP 字面量（能在本机 bind 即为本机地址）。

    只接受 IP 字面量，不做 DNS 解析：避免把回调交给一个会被解析到别处的域名。
    """
    text = (host or "").strip().strip("[]")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if ip.is_unspecified or ip.is_multicast:
        return False
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((str(ip), 0))
        return True
    except OSError:
        return False


def _callback_url_ok(url: str, *, allow_local: bool) -> bool:
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        return False
    host = parts.hostname or ""
    if is_loopback_host(host):
        return True
    return allow_local and is_local_address(host)


def is_loopback_url(url: str) -> bool:
    """只允许 ``http(s)://<回环地址>[:port]/...``；不允许内嵌凭据。"""
    return _callback_url_ok(url, allow_local=False)


def is_local_url(url: str) -> bool:
    """回环地址，或本机网卡 IP（渠道 webhook 模式绑定在局域网 IP 上时）。

    回调目标仍然只能是本机进程：外部地址、域名、内嵌凭据一律拒绝。
    """
    return _callback_url_ok(url, allow_local=True)


__all__ = [
    "HEADER",
    "TOKEN_FILE",
    "is_local_address",
    "is_local_url",
    "is_loopback_host",
    "is_loopback_url",
    "load_or_create_token",
    "token_path",
    "verify",
]
