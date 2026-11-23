"""图片输入（多模态，ADR 0015）。

会话里只记录图片**路径**（``Message.images``），发给模型时 provider 才读取文件并编码，
因此会话日志、调试日志与上下文预算都不受图片体积影响。

- ``prepare_images``：组装上下文时决定哪些图片真正发给模型：只保留最近 ``max_images`` 张，
  更早的换成文字占位；模型不支持看图（``[agent] vision = false``）时全部换成占位，
  并提示可以用 ``view_image`` 交给视觉模型描述。
- ``image_block``：provider 读取图片（限制大小与格式），返回 ``(media_type, base64)``。
- ``user_images``：从用户消息里识别渠道已下载的图片（飞书 ``[image: 路径]``）。
- ``vision_enabled``：``[agent] vision = auto`` 时按 provider / 模型名判断。
"""

from __future__ import annotations

import base64
import logging
import re
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("markbot.media")

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_IMAGES = 3
VISION_HINTS = (
    "claude", "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-5", "gemini", "vl", "vision",
    "4v", "glm-4.5v", "qwen2.5-omni", "omni", "pixtral", "llava", "minicpm-v", "internvl",
    "doubao-seed", "kimi-latest", "step-1o", "sensenova-v",
)
USER_IMAGE = re.compile(r"\[image:\s*([^\]\n]+?)\s*\]")


def media_type(path: str | Path) -> str | None:
    return IMAGE_TYPES.get(Path(str(path)).suffix.lower())


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_block(path: str | Path, max_bytes: int = MAX_IMAGE_BYTES) -> tuple[str, str] | None:
    """读取图片并 base64 编码；文件缺失、过大或不是图片时返回 None（调用方改用文字说明）。"""
    target = Path(str(path))
    try:
        size = target.stat().st_size
        if size <= 0 or size > max_bytes:
            logger.warning("图片过大或为空，未发送: %s (%d 字节)", target, size)
            return None
        data = target.read_bytes()
    except OSError as exc:
        logger.warning("读取图片失败: %s (%s)", target, exc)
        return None
    kind = _sniff(data)
    if kind is None:
        return None
    return kind, base64.b64encode(data).decode("ascii")


def is_image_file(path: str | Path) -> bool:
    target = Path(str(path))
    return media_type(target) is not None and target.is_file()


def vision_enabled(setting: Any, provider_kind: str, model: str) -> bool:
    value = str(setting if setting is not None else "auto").strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    if provider_kind == "anthropic":
        return True
    name = (model or "").lower()
    return any(hint in name for hint in VISION_HINTS)


def user_images(text: str, roots: Iterable[Path]) -> list[str]:
    """用户消息里 ``[image: 路径]`` 指向的图片；只接受 ``roots`` 下真实存在的图片文件。"""
    allowed = [Path(root).resolve() for root in roots]
    found: list[str] = []
    for match in USER_IMAGE.finditer(text or ""):
        raw = match.group(1).strip()
        try:
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if not any(path == root or root in path.parents for root in allowed):
            continue
        if is_image_file(path) and str(path) not in found:
            found.append(str(path))
    return found


def _note(paths: list[str], reason: str) -> str:
    return "\n".join(f"[图片 {path}：{reason}]" for path in paths)


def prepare_images(
    messages: list[dict[str, Any]], *, vision: bool, max_images: int = DEFAULT_MAX_IMAGES
) -> list[dict[str, Any]]:
    """决定本次请求实际发送哪些图片（返回副本，不修改会话）。"""
    total = sum(len(message.get("images") or []) for message in messages)
    if total == 0:
        return messages
    keep = max(0, int(max_images)) if vision else 0
    out: list[dict[str, Any]] = []
    remaining = keep
    # 从后往前：最近的图片优先保留
    for message in reversed(messages):
        images = [str(item) for item in message.get("images") or []]
        if not images:
            out.append(message)
            continue
        sent: list[str] = []
        dropped: list[str] = []
        for path in reversed(images):
            if remaining > 0:
                sent.insert(0, path)
                remaining -= 1
            else:
                dropped.insert(0, path)
        note = ""
        if dropped:
            reason = (
                "较早的图片已省略，需要时重新查看" if vision
                else "当前模型不能直接看图；需要时用 view_image 让视觉模型描述"
            )
            note = _note(dropped, reason)
        content = str(message.get("content") or "")
        if note:
            content = (content + "\n" + note).strip()
        out.append({**message, "content": content, "images": sent})
    out.reverse()
    return out


__all__ = [
    "DEFAULT_MAX_IMAGES",
    "IMAGE_TYPES",
    "MAX_IMAGE_BYTES",
    "image_block",
    "is_image_file",
    "media_type",
    "prepare_images",
    "user_images",
    "vision_enabled",
]
