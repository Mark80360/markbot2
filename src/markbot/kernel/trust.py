"""信任表：首次加载第三方包必须显式批准。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from markbot.schema.hashing import tree_hash


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class TrustStore:
    def __init__(self, home: Path, project_root: Path | None = None) -> None:
        self.home = Path(home)
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.user_path = self.home / "trust.json"
        self.project_path = self.project_root / ".markbot" / "trust.json"
        self.warnings: list[str] = []

    def _read(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {"packages": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.warnings.append(f"信任表损坏，已忽略: {path} -> {exc}")
            return {"packages": {}}
        if not isinstance(data, dict):
            return {"packages": {}}
        data.setdefault("packages", {})
        return data

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def entries(self) -> dict[str, dict[str, Any]]:
        merged = dict(self._read(self.user_path).get("packages", {}))
        merged.update(self._read(self.project_path).get("packages", {}))
        return merged

    def hash_of(self, path: Path) -> str:
        return tree_hash(Path(path))

    def is_trusted(self, package_id: str, path: Path | None = None) -> bool:
        entry = self.entries().get(package_id)
        if not entry:
            return False
        if path is None:
            return True
        approved = entry.get("hash")
        return bool(approved) and approved == self.hash_of(path)

    @staticmethod
    def git_clean(path: Path, *, timeout_s: float = 5.0) -> bool:
        """``path`` 下的文件都已被 git 跟踪、且没有未提交改动（含未跟踪的新文件）。

        用于仓库内置包的开发模式信任；没有 git、不在仓库里、超时都返回 False。
        """
        git = shutil.which("git")
        directory = Path(path)
        if git is None or not directory.is_dir():
            return False
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

        def run(*args: str) -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(
                    [git, "-C", str(directory), *args],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_s,
                    creationflags=flags,
                )
            except (OSError, subprocess.SubprocessError):
                return None

        tracked = run("ls-files", "--", ".")
        if tracked is None or tracked.returncode != 0 or not tracked.stdout.strip():
            return False
        status = run("status", "--porcelain", "--untracked-files=all", "--", ".")
        if status is None or status.returncode != 0:
            return False
        return not status.stdout.strip()

    def approve(self, package_id: str, path: Path, *, scope: str = "user") -> str:
        digest = self.hash_of(path)
        target = self.project_path if scope == "project" else self.user_path
        data = self._read(target)
        data["packages"][package_id] = {"hash": digest, "approved_at": _now()}
        self._write(target, data)
        return digest

    def revoke(self, package_id: str) -> bool:
        changed = False
        for path in (self.user_path, self.project_path):
            data = self._read(path)
            if package_id in data.get("packages", {}):
                del data["packages"][package_id]
                self._write(path, data)
                changed = True
        return changed


__all__ = ["TrustStore"]
