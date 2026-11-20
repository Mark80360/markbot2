"""时效与复核：纯函数，不做 I/O。

常驻记忆的每条 bullet 在行尾附带元数据注释：

    - 用户偏好回复用中文 <!-- m:durable;u=2026-11-20;r=2027-02-18 -->

``u`` 是写入日期，``r`` 是复核期限。到期不删除，只降级为 ``stale``：
注入时带"待复核"标注，整理器逐条处置。过期事实比没有记忆更危险，所以
没有任何路径会静默丢弃条目。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

LIFESPANS = ("permanent", "durable", "ephemeral")
DEFAULT_DAYS = {"permanent": None, "durable": 90, "ephemeral": 14}
_ATTRS = {
    "u": "updated_at",
    "r": "review_after",
    "v": "verified_by",
    "s": "source_sessions",
    "k": "key",
    "p": "project_rev",
}
_META_RE = re.compile(r"<!--\s*m:([^>]*?)\s*-->\s*$")
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.:,\-]+")


def today() -> date:
    return datetime.now().astimezone().date()


def parse_date(text: str | None) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def split_meta(line: str) -> tuple[str, dict]:
    """把一行拆成 ``(去掉注释的正文, 元数据)``；无注释时元数据为空。"""
    match = _META_RE.search(line)
    if not match:
        return line.rstrip(), {}
    body = line[: match.start()].rstrip()
    fields = [part.strip() for part in match.group(1).split(";") if part.strip()]
    meta: dict = {}
    if fields:
        meta["lifespan"] = fields[0] if fields[0] in LIFESPANS else "durable"
    for field in fields[1:]:
        if "=" not in field:
            continue
        key, value = field.split("=", 1)
        name = _ATTRS.get(key.strip())
        if name:
            meta[name] = value.strip()
    return body, meta


def _safe(value: str, limit: int = 120) -> str:
    return _SAFE_RE.sub("-", str(value or "")).strip("-")[:limit]


def review_days(lifespan: str, *, durable_days: int = 90, ephemeral_days: int = 14) -> int | None:
    if lifespan == "permanent":
        return None
    if lifespan == "ephemeral":
        return ephemeral_days
    return durable_days


def annotate(
    text: str,
    lifespan: str = "durable",
    *,
    now: date | None = None,
    durable_days: int = 90,
    ephemeral_days: int = 14,
    key: str = "",
    verified_by: str = "",
    sources: tuple[str, ...] | list[str] = (),
    project_rev: str = "",
) -> str:
    """给正文追加元数据注释；未知 lifespan 按 durable 处理。

    元数据承载来源与冲突键，供注入过滤、整理器复核与审计使用：
    ``m`` 时效、``u`` 写入日期、``r`` 复核期限、``v`` 验证来源、
    ``s`` 来源会话、``k`` 冲突键、``p`` 项目版本。
    """
    clean, _ = split_meta(str(text or "").strip())
    if lifespan not in LIFESPANS:
        lifespan = "durable"
    stamp = (now or today()).isoformat()
    days = review_days(lifespan, durable_days=durable_days, ephemeral_days=ephemeral_days)
    review = "" if days is None else (now or today()) + timedelta(days=days)
    fields = [f"m:{lifespan}", f"u={stamp}"]
    if days is not None:
        fields.append(f"r={review.isoformat()}")
    if verified_by:
        fields.append(f"v={_safe(verified_by, 20)}")
    joined = ",".join(_safe(item, 40) for item in list(sources)[:5] if item)
    if joined:
        fields.append(f"s={joined}")
    if key:
        fields.append(f"k={_safe(key, 80)}")
    if project_rev:
        fields.append(f"p={_safe(project_rev, 40)}")
    return f"{clean} <!-- {';'.join(fields)} -->"


def status(meta: dict, *, now: date | None = None) -> str:
    """``fresh`` / ``stale`` / ``unknown``（旧条目没有元数据）。"""
    if not meta:
        return "unknown"
    review = parse_date(meta.get("review_after"))
    if review is None:
        return "fresh"
    return "stale" if (now or today()) >= review else "fresh"


def inject_line(line: str, *, now: date | None = None, show_stale: bool = True) -> str | None:
    """渲染用：去掉注释；缺少来源的新式条目与过期条目有明确处置。

    - 带元数据但没有 ``v``（verified_by）的条目视为无来源，不注入；
    - 完全没有元数据的旧条目按迁移兼容处理，照常注入；
    - 过期条目带"待复核"标注，``show_stale=false`` 时跳过。
    """
    body, meta = split_meta(line)
    if meta and not meta.get("verified_by"):
        return None
    state = status(meta, now=now)
    if state != "stale":
        return body
    if not show_stale:
        return None
    return f"[待复核：{meta.get('review_after')} 起未验证] {body}"


__all__ = [
    "DEFAULT_DAYS",
    "LIFESPANS",
    "annotate",
    "inject_line",
    "parse_date",
    "review_days",
    "split_meta",
    "status",
    "today",
]
