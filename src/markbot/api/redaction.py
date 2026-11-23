"""密钥脱敏：按已登记的密钥值精确遮蔽 + 按密钥形状兜底（稳定契约的一部分）。

放在 ``markbot.api`` 而不是 ``kernel.session``：日志、会话归档、记忆、定时任务、渠道
都要用它；放在 kernel 内部会让 ``memory`` 等模块反向依赖 kernel 并形成导入环。
已登记的密钥是进程级的：Kernel 启动时登记配置里的密钥，独立进程（渠道）各自登记。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

# 归档前对密钥形状的字符串做脱敏；宁可多遮，不写入明文。
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(sk-ant-[a-z0-9_\-]{16,})"),
    re.compile(r"(?i)\b(sk-[a-z0-9_\-]{16,})"),
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})"),
    re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    # 前缀允许下划线等字符：``CRAWL4AI_API_TOKEN=`` 这类环境变量名里 ``TOKEN`` 前
    # 没有词边界，旧写法 ``\btoken\b`` 会漏过，导致密钥以明文落盘。
    # 值里排除反斜杠：脱敏对象常常是 JSON 文本，``token=xxx\"`` 的 ``\`` 是转义符，
    # 一旦被吞掉就会留下裸引号，把整段 JSON 破坏成非法格式（归档时 json.loads 崩溃）。
    # 等号/冒号两侧允许 ``\"`` / ``"``：日志把 ``SECRET = "xxx"`` 序列化成 JSON 后变成
    # ``SECRET = \"xxx\"``，旧写法的值从反斜杠开始、长度为 0，密钥因此原样落盘；
    # ``"api_key": "xxx"`` 这种 JSON 键值同理。
    re.compile(
        r"(?i)[A-Za-z0-9_.\-]*(?:api[_-]?key|access[_-]?token|token|secret|password|passwd)"
        r"\b(?:\\?['\"])?\s*[:=]\s*(?:\\?['\"])?([^\s'\"`,;\\]{8,})"
    ),
    re.compile(r"(?i)\bbearer\s+([a-z0-9._\-]{16,})"),
)


# 已登记的密钥原值（config 的 api_key、[secrets] 等）。按值精确替换，不依赖形状猜测：
# 形状正则只能兜底，任何不像密钥的密钥（飞书 app secret、自建服务 token）都会漏。
_KNOWN_SECRETS: tuple[str, ...] = ()
MIN_KNOWN_SECRET_CHARS = 8


def register_secrets(values: Iterable[Any]) -> int:
    """登记已知密钥原值，返回当前登记数量；可重复调用，结果取并集。

    同时登记 JSON 转义形态：日志会把 payload 序列化成 JSON，含引号、反斜杠或
    非 ASCII 字符的密钥在文本里不再是原样。少于 8 个字符的值不登记，避免误伤正文。
    """
    global _KNOWN_SECRETS
    found = set(_KNOWN_SECRETS)
    for value in values or ():
        text = str(value or "").strip()
        if len(text) < MIN_KNOWN_SECRET_CHARS:
            continue
        found.add(text)
        found.add(json.dumps(text, ensure_ascii=False)[1:-1])
        found.add(json.dumps(text, ensure_ascii=True)[1:-1])
    # 长值先替换，避免短值先命中把长值切碎后漏掉剩余部分
    _KNOWN_SECRETS = tuple(sorted(found, key=len, reverse=True))
    return len(_KNOWN_SECRETS)


def clear_registered_secrets() -> None:
    """清空已登记的密钥（测试用）。"""
    global _KNOWN_SECRETS
    _KNOWN_SECRETS = ()


def redact_known(text: str) -> str:
    """只按已登记的密钥值替换，不做形状猜测。

    用于送回模型的工具结果：形状正则会误伤正常代码/配置，而已登记的值一定是密钥。
    """
    if not text or not _KNOWN_SECRETS:
        return text
    out = text
    for secret in _KNOWN_SECRETS:
        if secret in out:
            out = out.replace(secret, REDACTED)
    return out


def redact_secrets(text: str) -> str:
    out = redact_known(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(
            lambda match: match.group(0).replace(match.group(1), REDACTED), out
        )
    return out


__all__ = [
    "MIN_KNOWN_SECRET_CHARS",
    "REDACTED",
    "clear_registered_secrets",
    "redact_known",
    "redact_secrets",
    "register_secrets",
]
