"""确定性目录哈希：信任表、快照与优化器门禁共用。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

EMPTY_TREE = "sha256:" + hashlib.sha256(b"").hexdigest()


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iter_files(root: Path) -> Iterator[tuple[str, bytes]]:
    """按相对路径排序产出文件；忽略隐藏缓存与符号链接。"""
    if not root.exists():
        return
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in {"__pycache__", ".pytest_cache", ".ruff_cache", ".git"} for part in rel.split("/")):
            continue
        files.append(path)
    for path in sorted(files, key=lambda p: p.relative_to(root).as_posix()):
        yield path.relative_to(root).as_posix(), path.read_bytes()


def tree_hash(root: Path) -> str:
    """对目录内容做稳定哈希，返回 ``sha256:<hex>``。"""
    digest = hashlib.sha256()
    for rel, data in iter_files(root):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def short_hash(value: str) -> str:
    return value.split(":", 1)[-1][:12]


__all__ = ["EMPTY_TREE", "hash_bytes", "iter_files", "short_hash", "tree_hash"]
