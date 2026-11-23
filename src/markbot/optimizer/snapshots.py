"""学习层快照、晋升与回滚。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from markbot.schema.hashing import hash_bytes, short_hash, tree_hash
from markbot.schema.locking import learned_write_lock
from markbot.schema.models import LearnedManifest, SnapshotEntry


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_tree(root: Path) -> dict[str, str]:
    root = Path(root)
    tree: dict[str, str] = {}
    if not root.exists():
        return tree
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel.split("/"):
            continue
        tree[rel] = path.read_text(encoding="utf-8", errors="replace")
    return tree


def write_tree(root: Path, tree: dict[str, str], *, clear: bool = True) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if clear:
        for child in list(root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    for rel, text in tree.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def create_snapshot(learned_dir: Path, snapshots_root: Path) -> str:
    learned_dir = Path(learned_dir)
    tree = read_tree(learned_dir)
    digest = tree_hash(learned_dir)
    name = short_hash(digest)
    target = Path(snapshots_root) / name
    meta_path = target / "meta.json"
    if meta_path.exists():
        # 快照按内容哈希命名：同一内容已存在就不再重复复制整棵树
        try:
            if json.loads(meta_path.read_text(encoding="utf-8")).get("hash") == digest:
                return digest
        except ValueError:
            pass
    target.mkdir(parents=True, exist_ok=True)
    (target / "tree.json").write_text(
        json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for rel, text in tree.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (target / "meta.json").write_text(
        json.dumps({"hash": digest, "created_at": _now()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return digest


def list_snapshots(snapshots_root: Path) -> list[dict[str, Any]]:
    root = Path(snapshots_root)
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        meta: dict[str, Any] = {}
        meta_path = child / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        items.append(
            {
                "name": child.name,
                "hash": meta.get("hash", ""),
                "created_at": meta.get("created_at", ""),
                "path": str(child),
            }
        )
    return sorted(items, key=lambda item: item.get("created_at", ""))


def _find_snapshot(snapshots_root: Path, target: str | None) -> Path:
    items = list_snapshots(snapshots_root)
    if not items:
        raise FileNotFoundError("没有可回滚的快照")
    if not target:
        return Path(items[-1]["path"])
    for item in items:
        if item["name"] == target or item["hash"] == target or item["hash"].endswith(target):
            return Path(item["path"])
    raise FileNotFoundError(f"找不到快照: {target}")


def restore_snapshot(learned_dir: Path, snapshots_root: Path, target: str | None = None) -> str:
    snapshot = _find_snapshot(snapshots_root, target)
    data = json.loads((snapshot / "tree.json").read_text(encoding="utf-8"))
    with learned_write_lock(Path(learned_dir)):
        write_tree(Path(learned_dir), {str(k): str(v) for k, v in data.items()})
    meta_path = snapshot / "meta.json"
    if meta_path.exists():
        try:
            return str(json.loads(meta_path.read_text(encoding="utf-8")).get("hash", snapshot.name))
        except ValueError:
            pass
    return snapshot.name


class LearnedConflict(RuntimeError):
    """候选基于的学习层版本已过期（评估期间有其它写入方修改了 learned/）。"""


def _current_hash(learned_dir: Path) -> str:
    return tree_hash(learned_dir) if Path(learned_dir).exists() else ""


def promote(
    candidate_tree: dict[str, str],
    learned_dir: Path,
    snapshots_root: Path,
    *,
    origin_task: str | None = None,
    expected_hash: str | None = None,
) -> tuple[str, str]:
    """晋升前先快照当前学习层；返回 (parent_hash, new_hash)。

    ``expected_hash`` 是候选生成时读到的学习层哈希。晋升会整体替换 ``learned/``，
    若这期间记忆工具/整理器写入过（哈希变化），直接覆盖会丢掉这些写入，因此拒绝晋升。
    """
    learned_dir = Path(learned_dir)
    with learned_write_lock(learned_dir):
        if expected_hash is not None and _current_hash(learned_dir) != expected_hash:
            raise LearnedConflict("learned/ 在评估期间被修改，放弃本次晋升")
        return _promote_locked(candidate_tree, learned_dir, snapshots_root, origin_task)


def _promote_locked(
    candidate_tree: dict[str, str],
    learned_dir: Path,
    snapshots_root: Path,
    origin_task: str | None,
) -> tuple[str, str]:
    parent = create_snapshot(learned_dir, snapshots_root)
    payload = {k: v for k, v in candidate_tree.items() if k != "manifest.json"}
    write_tree(learned_dir, payload)
    new_hash = tree_hash(learned_dir)
    entries = [
        SnapshotEntry(
            path=rel,
            source="learned",
            origin_task=origin_task,
            sha256=hash_bytes(text.encode("utf-8")),
        )
        for rel, text in sorted(payload.items())
        if rel != "manifest.json"
    ]
    manifest = LearnedManifest(
        snapshot=new_hash,
        parent=parent,
        created_at=_now(),
        entries=entries,
    )
    (learned_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return parent, new_hash


def read_manifest(learned_dir: Path) -> LearnedManifest | None:
    path = Path(learned_dir) / "manifest.json"
    if not path.exists():
        return None
    try:
        return LearnedManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


__all__ = [
    "LearnedConflict",
    "create_snapshot",
    "list_snapshots",
    "promote",
    "read_manifest",
    "read_tree",
    "restore_snapshot",
    "write_tree",
]
