"""飞书消息解析：文本/富文本/卡片/分享卡片内容提取与 @ 提及处理（纯函数）。

从 channel.py 拆出（ADR 0005）；入口仍是 channel.py。
"""

from __future__ import annotations

import json
import re
from typing import Any


# ---------------------------------------------------------------- 消息解析
def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _as_list(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _extract_share_card_content(content_json: dict[str, Any], msg_type: str) -> str:
    parts: list[str] = []
    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")
    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: str | dict[str, Any]) -> list[str]:
    parts: list[str] = []
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []
    if not isinstance(content, dict):
        return parts

    # user_dsl 是卡片原始定义，信息最全。
    user_dsl = content.get("user_dsl")
    if isinstance(user_dsl, str) and user_dsl.strip():
        try:
            dsl = _as_dict(json.loads(user_dsl))
        except (json.JSONDecodeError, TypeError):
            dsl = None
        if dsl is not None:
            parts.extend(_extract_interactive_content(dsl))
            if parts:
                return parts

    title = content.get("title")
    if isinstance(title, dict):
        title_content = title.get("content") or title.get("text") or ""
        if title_content:
            parts.append(f"title: {title_content}")
    elif isinstance(title, str) and title:
        parts.append(f"title: {title}")

    elements = _as_list(content.get("elements"))
    if elements is not None:
        if elements and isinstance(elements[0], list):
            for row in elements:
                for element in _as_list(row) or []:
                    parts.extend(_extract_element_content(element))
        else:
            for element in elements:
                parts.extend(_extract_element_content(element))

    body = _as_dict(content.get("body"))
    if body is not None:
        for element in _as_list(body.get("elements")) or []:
            parts.extend(_extract_element_content(element))

    card = _as_dict(content.get("card"))
    if card:
        parts.extend(_extract_interactive_content(card))

    header = _as_dict(content.get("header"))
    if header is not None:
        header_title = _as_dict(header.get("title"))
        if header_title is not None:
            header_text = header_title.get("content") or header_title.get("text") or ""
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: Any) -> list[str]:
    parts: list[str] = []
    element = _as_dict(element)
    if element is None:
        return parts
    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)
    elif tag == "text":
        text = element.get("text", "")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in _as_list(element.get("fields")) or []:
            field_object = _as_dict(field)
            if field_object is None:
                continue
            field_text = _as_dict(field_object.get("text", {}))
            if field_text is not None and field_text.get("content"):
                parts.append(str(field_text["content"]))
    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)
    elif tag == "button":
        text = _as_dict(element.get("text", {}))
        if text is not None and text.get("content"):
            parts.append(str(text["content"]))
        multi_url = _as_dict(element.get("multi_url")) or {}
        url = element.get("url", "") or multi_url.get("url", "")
        if url:
            parts.append(f"link: {url}")
    elif tag == "img":
        alt = _as_dict(element.get("alt", {}))
        parts.append(alt.get("content", "[image]") if alt is not None else "[image]")
    elif tag == "note":
        for item in _as_list(element.get("elements")) or []:
            parts.extend(_extract_element_content(item))
    elif tag == "column_set":
        for column in _as_list(element.get("columns")) or []:
            column_object = _as_dict(column)
            if column_object is None:
                continue
            for item in _as_list(column_object.get("elements")) or []:
                parts.extend(_extract_element_content(item))
    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)
    elif tag == "table":
        columns: list[tuple[str, str]] = []
        for column in _as_list(element.get("columns")) or []:
            column_object = _as_dict(column)
            if column_object is None:
                continue
            name = column_object.get("name")
            if isinstance(name, str) and name:
                columns.append((name, str(column_object.get("display_name") or name)))
        if columns:
            parts.append(" | ".join(header for _, header in columns))
        for row in _as_list(element.get("rows")) or []:
            row_object = _as_dict(row)
            if row_object is None:
                continue
            values: list[str] = []
            for name, _ in columns:
                value = row_object.get(name)
                if isinstance(value, list):
                    value = " ".join(str(item).strip() for item in value if item is not None)
                values.append("" if value is None else str(value).strip())
            row_text = " | ".join(values).strip()
            if row_text:
                parts.append(row_text)
    else:
        for item in _as_list(element.get("elements")) or []:
            parts.extend(_extract_element_content(item))

    return parts


def _extract_post_content(content_json: dict[str, Any]) -> tuple[str, list[str]]:
    def _parse_block(block: dict[str, Any]) -> tuple[str | None, list[str]]:
        content = _as_list(block.get("content"))
        if content is None:
            return None, []
        texts: list[str] = []
        images: list[str] = []
        title = block.get("title")
        if isinstance(title, str) and title:
            texts.append(title)
        for row in content:
            for el in _as_list(row) or []:
                element = _as_dict(el)
                if element is None:
                    continue
                tag = element.get("tag")
                if tag in ("text", "a"):
                    text = element.get("text", "")
                    if isinstance(text, str):
                        texts.append(text)
                elif tag == "at":
                    user = element.get("user_name", "user")
                    texts.append(f"@{user if isinstance(user, str) and user else 'user'}")
                elif tag == "code_block":
                    lang = element.get("language", "")
                    code_text = element.get("text", "")
                    if not isinstance(lang, str):
                        lang = ""
                    if not isinstance(code_text, str):
                        code_text = ""
                    texts.append(f"\n```{lang}\n{code_text}\n```\n")
                elif tag == "img" and isinstance(element.get("image_key"), str):
                    images.append(str(element["image_key"]))
        return (" ".join(texts).strip() or None), images

    root = content_json
    post = _as_dict(root.get("post"))
    if post is not None:
        root = post

    if "content" in root:
        text, images = _parse_block(root)
        if text or images:
            return text or "", images

    for key in ("zh_cn", "en_us", "ja_jp"):
        block = _as_dict(root.get(key))
        if block is None:
            continue
        text, images = _parse_block(block)
        if text or images:
            return text or "", images
    for value in root.values():
        block = _as_dict(value)
        if block is not None:
            text, images = _parse_block(block)
            if text or images:
                return text or "", images
    return "", []


def _extract_post_attachments(
    content_json: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """富文本里的 files 附件；文件夹只回传名字，提示用户压缩后再发。"""

    def _files_in(block: Any) -> tuple[list[dict[str, Any]], list[str]] | None:
        obj = _as_dict(block)
        if obj is None or "files" not in obj:
            return None
        files: list[dict[str, Any]] = []
        folders: list[str] = []
        for item in _as_list(obj.get("files")) or []:
            entry = _as_dict(item)
            if entry is None:
                continue
            if entry.get("is_folder") is True:
                name = entry.get("file_name")
                folders.append(name if isinstance(name, str) and name else "folder")
            elif isinstance(entry.get("file_key"), str) and entry["file_key"]:
                files.append(entry)
        return files, folders

    root = content_json
    post = _as_dict(root.get("post"))
    if post is not None:
        root = post

    found = _files_in(root)
    if found is not None:
        return found

    for key in ("zh_cn", "en_us", "ja_jp"):
        found = _files_in(root.get(key))
        if found is not None:
            return found
    for value in root.values():
        found = _files_in(value)
        if found is not None:
            return found
    return [], []


def _content_text(msg_type: str, payload: dict[str, Any]) -> str:
    if msg_type == "text":
        text = payload.get("text", "")
        return text if isinstance(text, str) else ""
    if msg_type == "post":
        return _extract_post_content(payload)[0]
    return ""


def _mention_open_id(mention: dict[str, Any]) -> str:
    return str((_as_dict(mention.get("id")) or {}).get("open_id") or "")


def _strip_mention_keys(text: str, mentions: list[dict[str, Any]]) -> str:
    for mention in mentions:
        key = str(mention.get("key") or "")
        if key:
            text = text.replace(key, "")
    return re.sub(r"\s+", " ", text).strip()


def _strip_leading_bot_mention(
    text: str, mentions: list[dict[str, Any]], bot_open_id: str
) -> str:
    """去掉开头的 @机器人，避免它进入提示词。"""
    if not mentions or not text or not bot_open_id:
        return text
    candidate = text.lstrip()
    for mention in mentions:
        key = str(mention.get("key") or "")
        if not key or not re.match(rf"{re.escape(key)}(?![A-Za-z0-9_])", candidate):
            continue
        if _mention_open_id(mention) != bot_open_id:
            continue
        return candidate[len(key):].strip() or text
    return text


def _resolve_mentions(text: str, mentions: list[dict[str, Any]]) -> str:
    """把 @_user_n 占位符换成 @姓名 (open_id)，让模型知道被 @ 的是谁。"""
    if not mentions or not text:
        return text
    for mention in mentions:
        key = str(mention.get("key") or "")
        if not key:
            continue
        pattern = rf"{re.escape(key)}(?![A-Za-z0-9_])"
        if not re.search(pattern, text):
            continue
        mention_id = _as_dict(mention.get("id")) or {}
        open_id = str(mention_id.get("open_id") or "")
        user_id = str(mention_id.get("user_id") or "")
        name = str(mention.get("name") or key)
        if open_id and user_id:
            replacement = f"@{name} ({open_id}, user id: {user_id})"
        elif open_id:
            replacement = f"@{name} ({open_id})"
        else:
            replacement = f"@{name}"
        text = re.sub(pattern, replacement, text)
    return text


def _is_bot_mentioned(
    raw_content: str, mentions: list[dict[str, Any]], bot_open_id: str
) -> bool:
    if "@_all" in raw_content:
        return True
    if not mentions:
        return False
    if bot_open_id:
        return any(_mention_open_id(item) == bot_open_id for item in mentions)
    return True
