"""离线整理器（nanobot Dream 的等价物）。

数据流：``sessions/*/trajectory.jsonl`` + ``compacted.jsonl`` -> 模型产出候选事实
（纯 JSON）-> ``MemoryStore`` 写入。模型拿不到任何工具，也不接触文件系统，
所有落盘都经过 store 的白名单、容量检查与写后校验，因此不可能越界写
``sessions/`` 或 ``config.toml``。

失败语义：
- 任一批次写入失败 -> 用快照回滚，游标不推进，下一轮重试；
- 失败与新增都会写运行日志（只记数量、路径与游标状态，不记正文）。
"""

from __future__ import annotations

import json
import logging
import shutil
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from markbot.memory.journal import Journal, SessionBatch, load_cursor, save_cursor
from markbot.memory.lifespan import LIFESPANS, split_meta, status
from markbot.memory.store import INDEX_REL, RESIDENT_REL, MemoryStore, _atomic_write

_logger = logging.getLogger("markbot.memory")

MAX_CANDIDATES = 20
SNAPSHOT_KEEP = 10
PROMPT_CHARS = 12_000

CONSOLIDATOR_SYSTEM = """你是长期记忆整理器。只输出 JSON 数组，不要解释、不要 Markdown 代码块。
每个元素字段：{"text": "...", "target": "memory|wiki|skill", "topic": "...",
"lifespan": "permanent|durable|ephemeral", "replaces": "...", "key": "...",
"steps": ["..."]}

规则：
- 只保留跨会话稳定、以后仍有用的用户偏好、项目事实、决策与理由；
- 进度播报、未确认的猜测、随时能查到的资料一律不要；
- 同一事实只留一条；纠正旧事实时用 replaces 填旧表述，或给稳定 key 让同键旧条目被替换；
- 项目/领域细节写 target=wiki 并给 topic（小写英文短横线）；
- 同一流程出现两次以上时写 target=skill，topic 为技能名，steps 为可执行步骤；
- 没有可沉淀的新事实时输出 []。"""


@dataclass
class ConsolidateReport:
    sessions: int = 0
    written: int = 0
    skipped: int = 0
    failed: int = 0
    no_input: bool = False
    incomplete: str = ""
    cursor_advanced: bool = False
    changed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "written": self.written,
            "skipped": self.skipped,
            "failed": self.failed,
            "no_input": self.no_input,
            "incomplete": self.incomplete,
            "cursor_advanced": self.cursor_advanced,
            "changed": list(self.changed),
        }


class Consolidator:
    def __init__(
        self,
        learned_dir: Path,
        *,
        home: Path | None = None,
        provider: Any = None,
        max_sessions: int = 20,
        excerpt_chars: int = PROMPT_CHARS,
        apply_lock: Any = None,
    ) -> None:
        self.store = MemoryStore(learned_dir)
        self.home = Path(home) if home else self.store.root.parent
        self.journal = Journal(self.home)
        self.provider = provider
        self.max_sessions = max(1, int(max_sessions))
        self.excerpt_chars = max(1000, int(excerpt_chars))
        self.apply_lock = apply_lock
        self.staging = self.home / "memory" / "staging"

    async def run(self) -> ConsolidateReport:
        if self.provider is None:
            _logger.warning("记忆整理跳过 reason=no_provider")
            return ConsolidateReport(incomplete="未配置模型，无法整理记忆")

        cursor = load_cursor(self.home)
        batches = self.journal.scan_since(cursor, max_sessions=self.max_sessions)
        if not batches:
            _logger.info("记忆整理跳过 reason=no_archived_sessions")
            return ConsolidateReport(no_input=True)

        try:
            prompt = self._prompt(batches)
            reply = await self.provider.complete(
                {"system": CONSOLIDATOR_SYSTEM, "messages": [{"role": "user", "content": prompt}]}
            )
            candidates = _parse_candidates(getattr(reply, "text", "") or "")
        except Exception as exc:  # noqa: BLE001 - 模型失败按"未完成"处理
            _logger.warning(
                "记忆整理失败 error=%s sessions=%d", exc, len(batches)
            )
            return ConsolidateReport(sessions=len(batches), incomplete=f"模型调用失败: {exc}")

        if candidates is None:
            # 解析失败不等于"模型确认没有新事实"：保留游标，下一轮重试
            _logger.warning(
                "记忆整理输出不可解析，游标未推进 sessions=%d", len(batches)
            )
            return ConsolidateReport(
                sessions=len(batches), incomplete="模型输出不是合法 JSON，游标未推进"
            )

        if not candidates:
            cursor.advance(batches)
            cursor.last_run = _now()
            save_cursor(self.home, cursor)
            _logger.info("记忆整理完成 written=0 sessions=%d", len(batches))
            return ConsolidateReport(sessions=len(batches), cursor_advanced=True)

        # 模型调用不占锁；只有落盘/回滚阶段与聊天串行
        with self._apply_guard():
            snapshot = self._snapshot()
            self._write_plan(batches, candidates)
            report = ConsolidateReport(sessions=len(batches))
            sources = tuple(batch.session_id for batch in batches)
            for candidate in candidates:
                if candidate["target"] == "skill":
                    receipt = self.store.remember_skill(
                        candidate["topic"],
                        candidate["text"],
                        candidate["steps"],
                    )
                else:
                    receipt = self.store.remember(
                        candidate["text"],
                        target=candidate["target"],
                        topic=candidate.get("topic", ""),
                        replaces=candidate.get("replaces", ""),
                        lifespan=candidate["lifespan"],
                        key=candidate.get("key", ""),
                        verified_by="dream",
                        sources=sources,
                    )
                if not receipt.ok:
                    report.failed += 1
                    _logger.warning(
                        "记忆整理条目被拒绝 path=%s reason=%s", receipt.path or "-", receipt.message
                    )
                elif receipt.changed:
                    report.written += 1
                    report.changed.append(receipt.path)
                else:
                    report.skipped += 1

            if report.failed:
                self._restore(snapshot)
                report.written = 0
                report.changed.clear()
                report.incomplete = f"{report.failed} 条写入失败，已回滚"
                _logger.warning(
                    "记忆整理未完成 sessions=%d written=%d failed=%d",
                    report.sessions, report.written, report.failed,
                )
                return report

            cursor.advance(batches)
            cursor.last_run = _now()
            save_cursor(self.home, cursor)
            report.cursor_advanced = True
            _logger.info(
                "记忆整理完成 sessions=%d written=%d skipped=%d",
                report.sessions, report.written, report.skipped,
            )
            return report

    def _apply_guard(self):
        return self.apply_lock if self.apply_lock is not None else nullcontext()

    # ------------------------------------------------------------------ input
    def _prompt(self, batches: list[SessionBatch]) -> str:
        blocks: list[str] = []
        budget = self.excerpt_chars
        for batch in batches:
            excerpt = self.journal.excerpt(
                batch, chars=max(500, budget // max(1, len(batches)))
            )
            if not excerpt:
                continue
            blocks.append(f"### 会话 {batch.session_id}\n{excerpt}")
        body = "\n\n".join(blocks)[: self.excerpt_chars]
        stale = self._stale_items()
        stale_block = ""
        if stale:
            stale_block = (
                "### 待复核条目（仍有效则用 replaces 填原文并给出相同 text 以刷新时效；"
                "失效或已变化则给出更正事实）\n"
                + "\n".join(f"- {line}" for line in stale)
                + "\n\n"
            )
        return f"以下是已归档会话的摘录，请提炼可长期复用的记忆候选：\n\n{stale_block}{body}"

    def _stale_items(self) -> list[str]:
        try:
            text = self.store.resident_path.read_text(encoding="utf-8")
        except OSError:
            return []
        items: list[str] = []
        for line in text.splitlines():
            if not line.lstrip().startswith("- "):
                continue
            body, meta = split_meta(line)
            if status(meta) != "stale":
                continue
            items.append(body.lstrip("- ").strip())
        return items[:50]

    def _write_plan(self, batches: list[SessionBatch], candidates: list[dict]) -> None:
        """暂存区留一份候选计划，便于审计；不进入 learned/。"""
        try:
            self.staging.mkdir(parents=True, exist_ok=True)
            payload = {
                "sessions": [batch.session_id for batch in batches],
                "candidates": candidates,
            }
            (self.staging / f"plan-{_stamp()}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            plans = sorted(self.staging.glob("plan-*.json"))
            for stale in plans[:-SNAPSHOT_KEEP]:
                stale.unlink(missing_ok=True)
        except OSError as exc:
            _logger.warning("记忆整理暂存写入失败 error=%s", exc)

    # --------------------------------------------------------------- snapshot
    def _memory_rels(self) -> list[str]:
        return _learned_rels(self.store.root)

    def _snapshot(self) -> dict[str, str | None]:
        files: dict[str, str | None] = {}
        base = self.home / "memory" / "snapshots" / _stamp() / "learned"
        for rel in self._memory_rels():
            path = self.store.root / rel
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                files[rel] = None
                continue
            except OSError as exc:
                _logger.warning("记忆快照读取失败 path=%s error=%s", rel, exc)
                continue
            files[rel] = text
            dest = base / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        _prune(self.home / "memory" / "snapshots", SNAPSHOT_KEEP)
        return files

    def _restore(self, files: dict[str, str | None]) -> None:
        for rel, text in files.items():
            path = self.store.root / rel
            try:
                if text is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, text)
            except OSError as exc:
                _logger.error("记忆回滚失败 path=%s error=%s", rel, exc)
        # 本轮新建的 wiki 页、技能与归档不在快照里，回滚时要一并删除
        for rel in _learned_rels(self.store.root):
            if rel in files:
                continue
            try:
                (self.store.root / rel).unlink(missing_ok=True)
            except OSError as exc:
                _logger.error("记忆回滚失败 path=%s error=%s", rel, exc)


# ------------------------------------------------------------------ public cli
def list_snapshots(home: Path) -> list[str]:
    root = Path(home) / "memory" / "snapshots"
    if not root.exists():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def restore_snapshot(
    home: Path, learned_dir: Path, name: str | None = None, *, dry_run: bool = False
) -> list[str]:
    """用快照覆盖记忆文件；``dry_run`` 只返回将被改动的相对路径。"""
    root = Path(home) / "memory" / "snapshots"
    names = list_snapshots(home)
    if not names:
        raise FileNotFoundError("没有可用的记忆快照")
    chosen = name or names[-1]
    if chosen not in names:
        raise FileNotFoundError(f"找不到快照: {chosen}")
    base = root / chosen / "learned"
    if not base.exists():
        raise FileNotFoundError(f"快照不完整: {chosen}")

    learned = Path(learned_dir)
    changed: list[str] = []
    keep: set[str] = set()
    for src in sorted(base.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(base).as_posix()
        keep.add(rel)
        dest = learned / rel
        text = src.read_text(encoding="utf-8")
        try:
            current = dest.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current == text:
            continue
        if not dry_run:
            _atomic_write(dest, text)
        changed.append(rel)

    wiki = learned / "memory" / "wiki"
    if wiki.exists():
        for page in sorted(wiki.glob("*.md")):
            rel = page.relative_to(learned).as_posix()
            if rel not in keep:
                if not dry_run:
                    page.unlink(missing_ok=True)
                changed.append(rel)
    # 快照之外新建的技能与归档也一并清理，保证"恢复"语义完整
    for rel in _learned_rels(learned):
        if rel in keep or rel in {RESIDENT_REL, INDEX_REL}:
            continue
        if not dry_run:
            (learned / rel).unlink(missing_ok=True)
        changed.append(rel)
    if not dry_run:
        _logger.info("记忆快照已恢复 name=%s changed=%d", chosen, len(changed))
    return changed


def _learned_rels(learned: Path) -> list[str]:
    """``learned/`` 下受记忆快照管理的相对路径（不含运行时目录）。"""
    rels = [RESIDENT_REL, INDEX_REL]
    wiki = learned / "memory" / "wiki"
    if wiki.exists():
        rels.extend(f"memory/wiki/{page.name}" for page in sorted(wiki.glob("*.md")))
        archive = wiki / "archive"
        if archive.exists():
            rels.extend(
                f"memory/wiki/archive/{page.name}" for page in sorted(archive.glob("*.md"))
            )
    for skill in sorted((learned / "skills").glob("*/SKILL.md")):
        rels.append(f"skills/{skill.parent.name}/SKILL.md")
    return rels


def _parse_candidates(text: str) -> list[dict] | None:
    """解析模型输出；无法解析时返回 ``None``，与"确实没有候选"的 ``[]`` 区分开。"""
    raw = text.strip()
    if raw.startswith("```"):
        lines = [line for line in raw.splitlines() if not line.strip().startswith("```")]
        raw = "\n".join(lines)
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        _logger.warning("记忆整理输出不是合法 JSON，忽略本轮候选")
        return None
    if not isinstance(data, list):
        return None

    out: list[dict] = []
    for item in data[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        text_value = " ".join(str(item.get("text") or "").split())
        target = str(item.get("target") or "memory").strip().lower()
        lifespan = str(item.get("lifespan") or "durable").strip().lower()
        if not text_value or target not in {"memory", "wiki", "skill"}:
            continue
        if lifespan not in LIFESPANS:
            lifespan = "durable"
        topic = str(item.get("topic") or "").strip()
        if target in {"wiki", "skill"} and not topic:
            target = "memory"
        steps: list[str] = []
        raw_steps = item.get("steps")
        if isinstance(raw_steps, list):
            steps = [
                " ".join(str(step).split()) for step in raw_steps if str(step).strip()
            ]
        if target == "skill" and not steps:
            target = "wiki"
        out.append(
            {
                "text": text_value,
                "target": target,
                "topic": topic,
                "lifespan": lifespan,
                "replaces": " ".join(str(item.get("replaces") or "").split()),
                "key": " ".join(str(item.get("key") or "").split()),
                "steps": steps,
            }
        )
    return out


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _prune(root: Path, keep: int) -> None:
    if not root.exists():
        return
    dirs = sorted(path for path in root.iterdir() if path.is_dir())
    for stale in dirs[:-keep]:
        if stale.name == "staging":
            continue
        shutil.rmtree(stale, ignore_errors=True)


__all__ = [
    "ConsolidateReport",
    "Consolidator",
    "list_snapshots",
    "restore_snapshot",
]
