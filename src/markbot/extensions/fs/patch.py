"""unified diff 解析与应用（``apply_patch`` 工具的纯函数部分，ADR 0012）。

支持：``diff --git`` / ``---`` / ``+++`` 头、多个文件、多个 hunk、新建（``--- /dev/null``）、
删除（``+++ /dev/null``）、``\\ No newline at end of file``。应用时：

- 按 hunk 头给出的行号就近查找上下文（行号偏移可以容忍）；
- 精确匹配失败时再按「忽略行尾空白」匹配一次；
- 仍匹配不到就报错，指出是哪个文件的哪个 hunk，**不做任何修改**（由调用方保证多文件原子性）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
NULL = "/dev/null"


class PatchError(ValueError):
    pass


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[tuple[str, str]] = field(default_factory=list)
    header: str = ""
    # 「\ No newline at end of file」出现在新文件内容之后
    new_no_eol: bool = False
    old_no_eol: bool = False

    @property
    def old_block(self) -> list[str]:
        return [text for tag, text in self.lines if tag in " -"]

    @property
    def new_block(self) -> list[str]:
        return [text for tag, text in self.lines if tag in " +"]


@dataclass
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def is_new(self) -> bool:
        return self.old_path == NULL

    @property
    def is_delete(self) -> bool:
        return self.new_path == NULL

    @property
    def target(self) -> str:
        return self.old_path if self.is_delete else self.new_path

    def counts(self) -> tuple[int, int]:
        added = sum(1 for hunk in self.hunks for tag, _ in hunk.lines if tag == "+")
        removed = sum(1 for hunk in self.hunks for tag, _ in hunk.lines if tag == "-")
        return added, removed


def _strip_prefix(raw: str) -> str:
    path = raw.strip().split("\t", 1)[0].strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path == NULL:
        return NULL
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_patch(text: str) -> list[FilePatch]:
    lines = text.replace("\r\n", "\n").split("\n")
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            current = FilePatch(_strip_prefix(line[4:]), _strip_prefix(lines[index + 1][4:]))
            files.append(current)
            hunk = None
            index += 2
            continue
        match = _HUNK.match(line)
        if match:
            if current is None:
                raise PatchError(f"第 {index + 1} 行：hunk 前缺少 ---/+++ 文件头")
            hunk = Hunk(
                old_start=int(match.group(1)),
                old_len=int(match.group(2) if match.group(2) is not None else 1),
                new_start=int(match.group(3)),
                new_len=int(match.group(4) if match.group(4) is not None else 1),
                header=line,
            )
            current.hunks.append(hunk)
            index += 1
            continue
        if hunk is not None and line[:1] in (" ", "+", "-") and not line.startswith(("--- ", "+++ ")):
            hunk.lines.append((line[0], line[1:]))
        elif hunk is not None and line == "" and _expects_more(hunk):
            # 有些编辑器会把空的上下文行（单个空格）裁成空行
            hunk.lines.append((" ", ""))
        elif hunk is not None and line.startswith("\\"):
            last = hunk.lines[-1][0] if hunk.lines else " "
            if last in " +":
                hunk.new_no_eol = True
            if last in " -":
                hunk.old_no_eol = True
        index += 1
    files = [item for item in files if item.hunks or item.is_new or item.is_delete]
    if not files:
        raise PatchError("没有解析到任何文件改动：需要 unified diff 格式（--- / +++ / @@ ... @@）")
    for item in files:
        for hunk in item.hunks:
            olds = len(hunk.old_block)
            news = len(hunk.new_block)
            if olds != hunk.old_len or news != hunk.new_len:
                # 行数不符多半是模型手写 hunk 头时数错了：以实际内容为准，不拒绝
                hunk.old_len, hunk.new_len = olds, news
    return files


def _expects_more(hunk: Hunk) -> bool:
    return len(hunk.old_block) < hunk.old_len or len(hunk.new_block) < hunk.new_len


def _find(lines: list[str], block: list[str], expected: int, start: int, *, loose: bool) -> int | None:
    if not block:
        return max(start, min(expected, len(lines)))

    def same(a: str, b: str) -> bool:
        return a.rstrip() == b.rstrip() if loose else a == b

    candidates = range(start, len(lines) - len(block) + 1)
    best: int | None = None
    for pos in candidates:
        if all(same(lines[pos + offset], block[offset]) for offset in range(len(block))):
            if best is None or abs(pos - expected) < abs(best - expected):
                best = pos
            if pos >= expected:
                break
    return best


def apply_hunks(text: str, patch: FilePatch) -> str:
    """把 ``patch`` 应用到 ``text``（已统一为 ``\\n`` 换行），返回新内容（同样是 ``\\n``）。"""
    eol = text.endswith("\n") or text == ""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out: list[str] = []
    cursor = 0
    delta = 0
    for number, hunk in enumerate(patch.hunks, 1):
        expected = (hunk.old_start if hunk.old_len == 0 else hunk.old_start - 1) + delta
        pos = _find(lines, hunk.old_block, expected, cursor, loose=False)
        if pos is None:
            pos = _find(lines, hunk.old_block, expected, cursor, loose=True)
        if pos is None:
            first = next((text for text in hunk.old_block if text.strip()), "")
            raise PatchError(
                f"{patch.target}: 第 {number} 个 hunk（{hunk.header}）的上下文在文件中找不到"
                + (f"，起始内容: {first.strip()[:80]!r}" if first else "")
                + "。请重新读取文件，按当前内容生成补丁"
            )
        out.extend(lines[cursor:pos])
        # 上下文行用文件里的原文：宽松匹配（忽略行尾空白）时，补丁里的上下文行可能丢了行尾空格
        offset = 0
        for tag, line in hunk.lines:
            if tag == " ":
                out.append(lines[pos + offset])
                offset += 1
            elif tag == "-":
                offset += 1
            else:
                out.append(line)
        cursor = pos + len(hunk.old_block)
        delta += len(hunk.new_block) - len(hunk.old_block)
        if cursor >= len(lines):
            if hunk.new_no_eol:
                eol = False
            elif hunk.old_no_eol:
                eol = True
    out.extend(lines[cursor:])
    if not out:
        return ""
    return "\n".join(out) + ("\n" if eol else "")


def new_file_text(patch: FilePatch) -> str:
    lines = [text for hunk in patch.hunks for text in hunk.new_block]
    if not lines:
        return ""
    no_eol = bool(patch.hunks and patch.hunks[-1].new_no_eol)
    return "\n".join(lines) + ("" if no_eol else "\n")


__all__ = ["FilePatch", "Hunk", "NULL", "PatchError", "apply_hunks", "new_file_text", "parse_patch"]
