"""飞书渠道公共部分：常量、日志、MarkBot home、本机令牌与配置小工具。

从 channel.py 拆出（ADR 0005）；入口仍是 channel.py。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


# 日志直接复用主 agent 的 gateway_log，不另起一套
try:
    from markbot.kernel import gateway_log
except ImportError:  # 未 `pip install -e .` 时回退到仓库源码
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from markbot.kernel import gateway_log



def _register_channel_secrets(*values: str) -> None:
    """渠道是独立进程，要自己登记密钥，gateway_log 的脱敏才能按值遮蔽。"""
    try:
        from markbot.api.redaction import register_secrets
    except ImportError:
        return
    register_secrets(values)


FEISHU_BASE = "https://open.feishu.cn"
LARK_BASE = "https://open.larksuite.com"
TEXT_LIMIT = 3500
# 单张卡片内容过长时先拆成多条，避免超出飞书卡片体积上限。
CARD_TEXT_LIMIT = 12000
CONFIRM_TTL_S = 180.0

# 出站媒体按扩展名决定飞书消息类型（与 nanobot 对齐）。
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}
AUDIO_EXTS = {".opus", ".ogg", ".oga"}
VIDEO_EXTS = {".mp4", ".mov", ".avi"}
FILE_TYPE_MAP = {
    ".opus": "opus",
    ".mp4": "mp4",
    ".pdf": "pdf",
    ".doc": "doc",
    ".docx": "doc",
    ".xls": "xls",
    ".xlsx": "xls",
    ".ppt": "ppt",
    ".pptx": "ppt",
}

# CardKit 流式卡片：元素 id 固定，按增量覆盖内容。
STREAM_ELEMENT_ID = "streaming_md"
STREAM_EDIT_INTERVAL = 0.5


def log_path() -> Path:
    """日志文件与主 agent 相同：~/.markbot/gateway/YYYY-MM-DD.log。"""
    return gateway_log.gateway_log_path(markbot_home())


def _stdout_is_file(path: Path) -> bool:
    """stdout 是否已经指向 path（被外部重定向），用于避免重复落盘。"""
    try:
        out = os.fstat(sys.stdout.fileno())
        target = os.stat(path)
    except (OSError, ValueError, AttributeError):
        return False
    return (out.st_dev, out.st_ino) == (target.st_dev, target.st_ino)


def log(message: str) -> None:
    """经 gateway_log 落盘，同时输出到控制台。"""
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [feishu] {message}"
    if not _stdout_is_file(log_path()):
        gateway_log.write(markbot_home(), f"[feishu] {message}")
    print(line, flush=True)


def _preview(text: str, limit: int = 60) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…(共{len(flat)}字)"


def markbot_home() -> Path:
    return Path(os.environ.get("MARKBOT_HOME") or Path.home() / ".markbot")


def default_config_path() -> Path:
    return markbot_home() / "config.toml"


AUTH_HEADER = "X-MarkBot-Token"


def _auth_token() -> str:
    """与 markbot serve 共用的本机令牌（<home>/gateway/auth.token）。"""
    from markbot.kernel import local_auth

    return local_auth.load_or_create_token(markbot_home())


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _secret_ref(value: Any) -> str:
    """``env:NAME`` 从环境变量读取，其余原样返回。"""
    text = "" if value is None else str(value)
    if text.startswith("env:"):
        return os.environ.get(text[4:].strip(), "")
    return text


def _string_list(value: Any) -> tuple[str, ...]:
    """allow_from 既支持 TOML 数组，也支持逗号分隔字符串。"""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        return ()
    return tuple(item.strip() for item in items if item.strip())
