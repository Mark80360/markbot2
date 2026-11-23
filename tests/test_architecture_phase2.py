"""第二阶段：分层（上下文提供者）、日志保留、学习层写锁与快照去重。"""

from __future__ import annotations

import threading
import time
from datetime import date
from pathlib import Path

import pytest

from markbot.kernel.context import CONTEXT_PROVIDERS, ContextBuilder, providers_from_runtime
from markbot.kernel.retention import prune_logs
from markbot.optimizer.snapshots import LearnedConflict, create_snapshot, promote, read_tree
from markbot.schema.hashing import tree_hash
from markbot.schema.locking import learned_write_lock, lock_path

# ------------------------------------------------------------ 上下文提供者


def test_memory_is_provided_through_extension_entry(make_kernel, tmp_path):
    learned = tmp_path / "learned"
    (learned / "strategy").mkdir(parents=True)
    (learned / "strategy" / "memory.md").write_text("- 用户叫小马\n", encoding="utf-8")
    kernel = make_kernel(["ok"], learned_dir=learned)
    factories = kernel.runtime.entries.get(CONTEXT_PROVIDERS) or []
    assert factories, "内置 memory 扩展应注册常驻记忆提供者"
    assert "小马" in kernel.loop._context._compose_system(_FakeSession())


def test_context_builder_without_providers_has_no_memory(tmp_path):
    class Runtime:
        learned_dir = tmp_path
        entries: dict = {}

    assert providers_from_runtime(Runtime()) == []


def test_broken_provider_factory_is_skipped(tmp_path):
    def boom(_learned):
        raise RuntimeError("boom")

    class Ok:
        def render(self) -> str:
            return "ok"

    class Runtime:
        learned_dir = tmp_path
        entries = {CONTEXT_PROVIDERS: [boom, lambda _l: Ok()]}

    providers = providers_from_runtime(Runtime())
    assert [p.render() for p in providers] == ["ok"]


class _FakeSession:
    metadata: dict = {}


def test_context_builder_accepts_explicit_provider(tmp_path):
    class Fixed:
        def render(self) -> str:
            return "fixed-memory"

    class Runtime:
        learned_dir = tmp_path
        entries = {CONTEXT_PROVIDERS: [lambda _l: None]}

    fixed = Fixed()
    assert ContextBuilder(Runtime(), memory=fixed).memory is fixed
    assert ContextBuilder(Runtime()).memory is None


# ------------------------------------------------------------ 日志保留


def _touch(path: Path, size: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_prune_logs_by_age(tmp_path):
    home = tmp_path
    today = date(2026, 11, 22)
    _touch(home / "logs/interaction/2026-11-10.md")
    _touch(home / "logs/interaction/2026-11-20.md")
    _touch(home / "logs/runtime/2026-10-01.md")
    _touch(home / "logs/runtime/2026-11-10.md")
    _touch(home / "gateway/2026-10-01.log")
    _touch(home / "gateway/state.json")
    _touch(home / "logs/optimizer.jsonl")

    report = prune_logs(home, retention_days=30, interaction_retention_days=7, today=today)
    assert sorted(report.deleted) == [
        "gateway/2026-10-01.log",
        "logs/interaction/2026-11-10.md",
        "logs/runtime/2026-10-01.md",
    ]
    # 非按日文件、未过期文件都保留
    assert (home / "gateway/state.json").exists()
    assert (home / "logs/optimizer.jsonl").exists()
    assert (home / "logs/interaction/2026-11-20.md").exists()
    assert (home / "logs/runtime/2026-11-10.md").exists()


def test_prune_logs_by_total_size_prefers_interaction_and_keeps_today(tmp_path):
    home = tmp_path
    today = date(2026, 11, 22)
    mb = 1024 * 1024
    _touch(home / "logs/interaction/2026-11-20.md", mb)
    _touch(home / "logs/interaction/2026-11-22.md", mb)
    _touch(home / "logs/runtime/2026-11-19.md", mb)
    report = prune_logs(
        home, retention_days=30, interaction_retention_days=30, max_total_mb=2, today=today
    )
    assert report.deleted == ["logs/interaction/2026-11-20.md"]
    assert (home / "logs/interaction/2026-11-22.md").exists()
    assert (home / "logs/runtime/2026-11-19.md").exists()


# ------------------------------------------------------------ 学习层写锁


def test_learned_lock_is_reentrant_in_same_thread(tmp_path):
    learned = tmp_path / "learned"
    with learned_write_lock(learned):
        with learned_write_lock(learned):
            pass
    assert lock_path(learned).parent == tmp_path.resolve()


def test_learned_lock_excludes_other_threads(tmp_path):
    learned = tmp_path / "learned"
    order: list[str] = []
    entered = threading.Event()

    def holder() -> None:
        with learned_write_lock(learned):
            entered.set()
            time.sleep(0.2)
            order.append("holder-done")

    def waiter() -> None:
        entered.wait(5)
        with learned_write_lock(learned):
            order.append("waiter")

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert order == ["holder-done", "waiter"]


def test_memory_store_writes_take_learned_lock(tmp_path, monkeypatch):
    from markbot.memory import store as store_module

    calls: list[Path] = []
    real = store_module.learned_write_lock

    def spy(path, **kwargs):
        calls.append(Path(path))
        return real(path, **kwargs)

    monkeypatch.setattr(store_module, "learned_write_lock", spy)
    store = store_module.MemoryStore(tmp_path / "learned")
    store.remember("用户偏好中文回复")
    assert calls == [tmp_path / "learned"]


# ------------------------------------------------------------ 快照去重 / 晋升冲突


def test_optimizer_snapshot_is_content_addressed(tmp_path):
    learned = tmp_path / "learned"
    (learned / "strategy").mkdir(parents=True)
    (learned / "strategy" / "system_prompt.md").write_text("base\n", encoding="utf-8")
    snapshots = tmp_path / "snapshots"
    first = create_snapshot(learned, snapshots)
    meta = next(snapshots.iterdir()) / "meta.json"
    stamp = meta.read_text(encoding="utf-8")
    assert create_snapshot(learned, snapshots) == first
    assert meta.read_text(encoding="utf-8") == stamp  # 没有重复复制
    assert len(list(snapshots.iterdir())) == 1


def test_promote_refuses_when_learned_changed_meanwhile(tmp_path):
    learned = tmp_path / "learned"
    (learned / "strategy").mkdir(parents=True)
    (learned / "strategy" / "system_prompt.md").write_text("base\n", encoding="utf-8")
    base_hash = tree_hash(learned)
    # 评估期间记忆工具写入了新内容
    (learned / "strategy" / "memory.md").write_text("- 新记忆\n", encoding="utf-8")
    with pytest.raises(LearnedConflict):
        promote(
            {"strategy/system_prompt.md": "better\n"},
            learned,
            tmp_path / "snapshots",
            expected_hash=base_hash,
        )
    tree = read_tree(learned)
    assert tree["strategy/memory.md"] == "- 新记忆\n"
    assert tree["strategy/system_prompt.md"] == "base\n"


def test_consolidator_snapshot_dedup(tmp_path):
    from markbot.memory.consolidator import Consolidator

    home = tmp_path / "home"
    learned = home / "learned"
    (learned / "strategy").mkdir(parents=True)
    (learned / "strategy" / "memory.md").write_text("- a\n", encoding="utf-8")
    consolidator = Consolidator(learned, home=home)
    consolidator._snapshot()
    consolidator._snapshot()
    root = home / "memory" / "snapshots"
    assert len([p for p in root.iterdir() if p.is_dir()]) == 1
    (learned / "strategy" / "memory.md").write_text("- b\n", encoding="utf-8")
    time.sleep(1.1)  # 快照目录名精确到秒
    consolidator._snapshot()
    assert len([p for p in root.iterdir() if p.is_dir()]) == 2
