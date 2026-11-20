"""把 MarkBot2 的会话归档映射成 Harness² 的 task（控制面适配器）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from markbot.optimizer.gate import illegal_paths
from markbot.schema.models import SessionMeta


@dataclass(frozen=True)
class BenchSpec:
    name: str
    runs_root: Path
    install_mode: str = "workspace_agents_md"


class ReplayRunner(Protocol):
    async def __call__(self, task_id: str, harness_dir: Path, run_id: str) -> Path: ...


class SessionStore:
    def __init__(self, home: Path) -> None:
        self.root = Path(home) / "sessions"

    def _meta_path(self, task_id: str) -> Path:
        return self.root / task_id / "meta.json"

    def meta(self, task_id: str) -> dict[str, Any]:
        path = self._meta_path(task_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def read(self, task_id: str, rel: str) -> str:
        return self.path(task_id, rel).read_text(encoding="utf-8", errors="replace")

    def path(self, task_id: str, rel: str) -> Path:
        return self.root / task_id / rel

    def completed(self, *, before: str | None = None, limit: int | None = None) -> list[SessionMeta]:
        if not self.root.exists():
            return []
        rows: list[SessionMeta] = []
        for directory in sorted(self.root.iterdir()):
            meta_path = directory / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = SessionMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.status != "completed":
                continue
            if before and meta.ended_at and meta.ended_at >= before:
                continue
            rows.append(meta)
        rows.sort(key=lambda m: m.ended_at or m.started_at)
        return rows[:limit] if limit else rows

    def trajectory(self, task_id: str) -> list[dict[str, Any]]:
        path = self.path(task_id, "trajectory.jsonl")
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows


class AssistantAdapter:
    """只依赖注入的 runner，不在优化器里积攒 Kernel 逻辑。"""

    def __init__(
        self,
        *,
        store: SessionStore,
        run_started_at: str,
        learned_dir: Path,
        runner: ReplayRunner | None = None,
    ) -> None:
        self.store = store
        self.run_started_at = run_started_at
        self.learned_dir = Path(learned_dir)
        self.runner = runner
        self.spec = BenchSpec(
            name="markbot",
            runs_root=Path(store.root).parent / "optimizer" / "runs",
            install_mode="workspace_agents_md",
        )

    def tasks(self, split: str = "all") -> list[str]:
        return [meta.id for meta in self.store.completed(before=self.run_started_at)]

    def domain_of(self, task_id: str) -> str:
        return str(self.store.meta(task_id).get("domain", "general"))

    def task_prompt(self, task_id: str) -> str:
        return self.store.read(task_id, "request.md")

    def task_files(self, task_id: str) -> Path | None:
        path = self.store.path(task_id, "files")
        return path if path.exists() else None

    async def execute(self, task_id: str, harness_dir: Path, run_id: str) -> Path:
        if self.runner is None:
            raise RuntimeError("未注入 ReplayRunner，无法重放会话")
        return await self.runner(task_id, Path(harness_dir), run_id)

    def base_harness(self) -> Path:
        return self.learned_dir

    async def score(self, run_dir: Path, task_id: str) -> None:
        # 个人场景没有 rubric；由门禁在回归集上统一判定
        return None

    def delivered(self, run_dir: Path) -> bool:
        return (Path(run_dir) / "deliverable" / "final.md").exists()

    def deliverable_text(self, run_dir: Path) -> str:
        path = Path(run_dir) / "deliverable" / "final.md"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def validate_tree(self, tree: dict[str, str]) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel, text in tree.items():
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            bad = illegal_paths(root)
        if bad:
            raise ValueError("学习层包含越界路径: " + ", ".join(bad))


__all__ = ["AssistantAdapter", "BenchSpec", "ReplayRunner", "SessionStore"]
