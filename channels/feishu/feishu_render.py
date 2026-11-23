"""回复渲染：Markdown -> 飞书富文本 / 卡片元素（纯函数）。

从 channel.py 拆出（ADR 0005）；入口仍是 channel.py。
"""

from __future__ import annotations

import json
import re
from typing import Any

from feishu_parse import (
    _extract_interactive_content,
)


# ---------------------------------------------------------------- Markdown → 飞书
_TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~")
_COMPLEX_MD_RE = re.compile(
    r"```|^\|.+\|.*\n\s*\|[-:\s|]+\||^#{1,6}\s+", re.MULTILINE
)
_SIMPLE_MD_RE = re.compile(
    r"\*\*.+?\*\*|__.+?__|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|~~.+?~~", re.DOTALL
)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
_OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)


def _strip_md_formatting(text: str) -> str:
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    return text


def _parse_md_table(table_text: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
    if len(lines) < 3:
        return None

    def split(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    headers = [_strip_md_formatting(header) for header in split(lines[0])]
    rows = [[_strip_md_formatting(cell) for cell in split(line)] for line in lines[2:]]
    columns = [
        {"tag": "column", "name": f"c{i}", "display_name": header, "width": "auto"}
        for i, header in enumerate(headers)
    ]
    return {
        "tag": "table",
        "page_size": len(rows) + 1,
        "columns": columns,
        "rows": [
            {f"c{i}": row[i] if i < len(row) else "" for i in range(len(headers))}
            for row in rows
        ],
    }


def _split_headings(content: str) -> list[dict[str, Any]]:
    protected = content
    code_blocks: list[str] = []
    for match in _CODE_BLOCK_RE.finditer(content):
        code_blocks.append(match.group(1))
        protected = protected.replace(match.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

    elements: list[dict[str, Any]] = []
    last_end = 0
    for match in _HEADING_RE.finditer(protected):
        before = protected[last_end : match.start()].strip()
        if before:
            elements.append({"tag": "markdown", "content": before})
        text = _strip_md_formatting(match.group(2).strip())
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{text}**" if text else ""},
            }
        )
        last_end = match.end()
    remaining = protected[last_end:].strip()
    if remaining:
        elements.append({"tag": "markdown", "content": remaining})

    for index, block in enumerate(code_blocks):
        for element in elements:
            if element.get("tag") == "markdown":
                element["content"] = element["content"].replace(f"\x00CODE{index}\x00", block)
    return elements or [{"tag": "markdown", "content": content}]


def _build_card_elements(content: str) -> list[dict[str, Any]]:
    protected = content
    code_blocks: list[str] = []
    for match in _CODE_BLOCK_RE.finditer(content):
        code_blocks.append(match.group(1))
        protected = protected.replace(match.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

    elements: list[dict[str, Any]] = []
    last_end = 0
    for match in _TABLE_RE.finditer(protected):
        before = protected[last_end : match.start()]
        if before.strip():
            elements.extend(_split_headings(before))
        elements.append(
            _parse_md_table(match.group(1)) or {"tag": "markdown", "content": match.group(1)}
        )
        last_end = match.end()
    remaining = protected[last_end:]
    if remaining.strip():
        elements.extend(_split_headings(remaining))

    for index, block in enumerate(code_blocks):
        for element in elements:
            if element.get("tag") == "markdown":
                element["content"] = element["content"].replace(f"\x00CODE{index}\x00", block)
    return elements or [{"tag": "markdown", "content": content}]


def _split_elements_by_table_limit(
    elements: list[dict[str, Any]], max_tables: int = 1
) -> list[list[dict[str, Any]]]:
    """飞书卡片每张最多一个表格（错误码 11310），超出就拆成多条。"""
    if not elements:
        return [[]]
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    table_count = 0
    for element in elements:
        if element.get("tag") == "table":
            if table_count >= max_tables:
                if current:
                    groups.append(current)
                current = []
                table_count = 0
            current.append(element)
            table_count += 1
        else:
            current.append(element)
    if current:
        groups.append(current)
    return groups or [[]]


def _detect_msg_format(content: str) -> str:
    """text：短纯文本；post：含链接的中等长度；interactive：Markdown 卡片。"""
    stripped = content.strip()
    if _COMPLEX_MD_RE.search(stripped):
        return "interactive"
    if len(stripped) > 2000:
        return "interactive"
    if _SIMPLE_MD_RE.search(stripped):
        return "interactive"
    if _LIST_RE.search(stripped) or _OLIST_RE.search(stripped):
        return "interactive"
    if _MD_LINK_RE.search(stripped):
        return "post"
    if len(stripped) <= 200:
        return "text"
    return "post"


def _markdown_to_post(content: str) -> str:
    paragraphs: list[list[dict[str, Any]]] = []
    for line in content.strip().split("\n"):
        elements: list[dict[str, Any]] = []
        last_end = 0
        for match in _MD_LINK_RE.finditer(line):
            before = line[last_end : match.start()]
            if before:
                elements.append({"tag": "text", "text": before})
            elements.append({"tag": "a", "text": match.group(1), "href": match.group(2)})
            last_end = match.end()
        remaining = line[last_end:]
        if remaining:
            elements.append({"tag": "text", "text": remaining})
        if not elements:
            elements.append({"tag": "text", "text": ""})
        paragraphs.append(elements)
    return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)


def _card_to_text(content: str) -> str:
    """卡片发送失败时的兜底：复用入站卡片解析，把元素还原成纯文本。"""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return content
    parts = _extract_interactive_content(payload)
    return "\n\n".join(part for part in parts if part.strip()) or content
