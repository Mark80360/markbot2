"""长期记忆存储：原子写、容量上限、写后校验、路径白名单。

只允许两类落盘目标（相对 ``learned/``）：
- ``strategy/memory.md``：常驻记忆，每轮进入系统提示；
- ``memory/wiki/<slug>.md`` + ``memory/wiki/index.json``：按需检索的知识页。

每次写入都经过 ``schema.learned.allowed_path`` 白名单与容量检查，写后重新读盘
校验；校验失败会回滚到写入前的文本。日志只记录动作、路径、字符数与校验结果，
不记录记忆正文。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from markbot.api.redaction import redact_secrets
from markbot.memory.lifespan import annotate, review_days, split_meta
from markbot.schema.learned import allowed_path
from markbot.schema.locking import learned_write_lock

_logger = logging.getLogger("markbot.memory")

RESIDENT_REL = "strategy/memory.md"
WIKI_REL = "memory/wiki"
INDEX_REL = "memory/wiki/index.json"
RESIDENT_LIMIT = 6_000
WIKI_LIMIT = 8_000
INDEX_LIMIT = 2_000
SNIPPET_CHARS = 200

_LOCK_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


@contextmanager
def _lock_for(path: Path):
    """进程内单文件锁：配合 ``os.replace`` 的原子性，避免并发写互相覆盖。"""
    key = str(path)
    with _LOCK_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


def _learned_locked(method):
    """MemoryStore 的写入口：跨进程互斥（与优化器、整理器、回滚共用一把学习层写锁）。"""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with learned_write_lock(self.root):
            return method(self, *args, **kwargs)

    return wrapper


def _clean_line(line: str) -> str:
    """去掉 bullet 前缀与行尾时效注释，得到可比较的正文。"""
    text = line.strip()
    while text[:2] in {"- ", "* ", "+ "}:
        text = text[2:].strip()
    return split_meta(text)[0]


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def _is_bullet(line: str) -> bool:
    return line.lstrip().startswith(("- ", "* ", "+ "))


def _slug(topic: str) -> str:
    """把页名收敛为白名单字符；纯中文等无法转写的页名用稳定哈希兜底。"""
    raw = _normalize(topic).lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-_")
    if not slug or not slug[0].isalnum():
        slug = "topic-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return slug[:60]


def _skill_slug(topic: str) -> str:
    """技能名遵循 Agent Skills 约定：只保留小写字母、数字与单个短横线。"""
    slug = _slug(topic).replace("_", "-")
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:64]


def _short(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _dates(lifespan: str) -> tuple[str, str]:
    """返回 ``(更新日期, 复核期限)``；permanent 的复核期限为空串。"""
    now = datetime.now().astimezone().date()
    days = review_days(lifespan)
    review = "" if days is None else (now + timedelta(days=days)).isoformat()
    return now.isoformat(), review


def _new_page(slug: str, title: str, lifespan: str) -> list[str]:
    updated, review = _dates(lifespan)
    lifespan = lifespan if lifespan in {"permanent", "durable", "ephemeral"} else "durable"
    lines = ["---", f"topic: {slug}", f"lifespan: {lifespan}", f"updated_at: {updated}"]
    if review:
        lines.append(f"review_after: {review}")
    lines.extend(["---", f"# {title}", ""])
    return lines


def index_lines(entries: dict, *, today: str | None = None) -> list[str]:
    """wiki 索引注入上下文时的行格式；写入容量检查与渲染共用，保证两边口径一致。"""
    today = today or datetime.now().astimezone().date().isoformat()
    lines: list[str] = []
    for slug in sorted(entries):
        entry = entries.get(slug) or {}
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or slug).strip()
        summary = str(entry.get("summary") or "").strip()
        review = str(entry.get("review_after") or "")
        mark = "（待复核）" if review and review <= today else ""
        lines.append(f"- {slug}{mark}: {summary or title}")
    return lines


def render_index(entries: dict, *, today: str | None = None) -> str:
    return "\n".join(index_lines(entries, today=today))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时名带上线程 id：同一进程多个写者不会撞到同一个临时文件
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _merge_lines(
    lines: list[str],
    fact: str,
    replaces: str,
    lifespan: str = "durable",
    *,
    key: str = "",
    verified_by: str = "",
    sources: tuple[str, ...] | list[str] = (),
    project_rev: str = "",
) -> tuple[list[str], list[str], str, str]:
    """把一条事实并入行列表。

    返回 ``(新行, 被替换的旧行, needle, state)``，state 为
    ``appended`` / ``replaced`` / ``same`` / ``missing``。
    ``replaces`` 按正文片段定位；``key`` 按元数据冲突键定位（新键直接追加）。
    """
    needle = _normalize(replaces)
    entry = f"- {annotate(fact, lifespan, key=key, verified_by=verified_by,
                          sources=sources, project_rev=project_rev)}"
    if needle:
        hits = [
            index
            for index, line in enumerate(lines)
            if _is_bullet(line) and needle in _clean_line(line)
        ]
        if not hits:
            return lines, [], needle, "missing"
    elif key:
        hits = [
            index
            for index, line in enumerate(lines)
            if _is_bullet(line) and split_meta(line.strip())[1].get("key") == key
        ]
    else:
        hits = []
    if hits:
        removed = [_clean_line(lines[index]) for index in hits]
        dropped = set(hits)
        kept = [line for index, line in enumerate(lines) if index not in dropped]
        kept.insert(min(hits[0], len(kept)), entry)
        return kept, removed, needle, "replaced"
    if any(_clean_line(line) == fact for line in lines):
        return lines, [], "", "same"
    return [*lines, entry], [], "", "appended"


@dataclass
class Receipt:
    action: str
    ok: bool = False
    changed: bool = False
    path: str = ""
    written: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    verified: bool = False
    chars: int = 0
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "ok": self.ok,
            "changed": self.changed,
            "path": self.path,
            "written": list(self.written),
            "removed": list(self.removed),
            "verified": self.verified,
            "chars": self.chars,
            "message": self.message,
        }


class MemoryStore:
    def __init__(self, learned_dir: Path) -> None:
        self.root = Path(learned_dir)

    # ------------------------------------------------------------------ paths
    @property
    def resident_path(self) -> Path:
        return self.root / RESIDENT_REL

    @property
    def wiki_dir(self) -> Path:
        return self.root / WIKI_REL

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_REL

    # ------------------------------------------------------------------ write
    @_learned_locked
    def remember(
        self,
        text: str,
        *,
        target: str = "memory",
        topic: str = "",
        replaces: str = "",
        lifespan: str = "durable",
        key: str = "",
        verified_by: str = "user",
        sources: tuple[str, ...] | list[str] = (),
        project_rev: str = "",
        session_id: str = "",
    ) -> Receipt:
        fact = _normalize(text)
        if not fact:
            return self._refuse("remember", "内容为空", "", session_id)
        if redact_secrets(fact) != fact:
            return self._refuse("remember", "疑似包含密钥，拒绝写入", "", session_id)
        origin = {
            "lifespan": lifespan,
            "key": _normalize(key),
            "verified_by": _normalize(verified_by),
            "sources": tuple(str(item) for item in sources),
            "project_rev": _normalize(project_rev),
        }
        if target == "wiki":
            if not _normalize(topic):
                return self._refuse("remember", "target=wiki 需要 topic", "", session_id)
            return self._remember_wiki(fact, topic, replaces, origin, session_id)
        return self._remember_resident(fact, replaces, origin, session_id)

    def _remember_resident(
        self, fact: str, replaces: str, origin: dict, session_id: str
    ) -> Receipt:
        path = self.resident_path
        if not allowed_path(RESIDENT_REL):
            return self._refuse("remember", f"目标越界: {RESIDENT_REL}", RESIDENT_REL, session_id)
        with _lock_for(path):
            try:
                old = self._read(path)
            except OSError as exc:
                return self._refuse("remember", f"读取失败: {exc}", RESIDENT_REL, session_id)

            lines, removed, needle, state = _merge_lines(
                old.splitlines(), fact, replaces, **origin
            )
            if state == "missing":
                return self._refuse(
                    "remember", "未找到要替换的旧内容", RESIDENT_REL, session_id
                )
            if state == "same":
                _logger.info(
                    "记忆无变化 path=%s session=%s", RESIDENT_REL, session_id or "-"
                )
                return Receipt(
                    "remember", ok=True, path=RESIDENT_REL, verified=True, chars=len(old),
                    message="无变化",
                )

            new_text = "\n".join(lines).rstrip("\n") + "\n"
            return self._commit(
                path,
                RESIDENT_REL,
                old,
                new_text,
                fact=fact,
                needle=needle,
                key=origin["key"],
                limit=RESIDENT_LIMIT,
                removed=removed,
                session_id=session_id,
            )

    def _remember_wiki(
        self, fact: str, topic: str, replaces: str, origin: dict, session_id: str
    ) -> Receipt:
        lifespan = origin["lifespan"]
        slug = _slug(topic)
        rel = f"{WIKI_REL}/{slug}.md"
        if not allowed_path(rel):
            return self._refuse("remember", f"目标越界: {rel}", rel, session_id)
        title = _normalize(topic)
        path = self.root / rel

        with _lock_for(path):
            try:
                old = self._read(path)
            except OSError as exc:
                return self._refuse("remember", f"读取失败: {exc}", rel, session_id)

            lines = old.splitlines() if old.strip() else _new_page(slug, title, lifespan)
            merged, removed, needle, state = _merge_lines(lines, fact, replaces, **origin)
            if state == "missing":
                return self._refuse(
                    "remember", "未找到要替换的旧内容", rel, session_id
                )
            if state == "same":
                return Receipt(
                    "remember", ok=True, path=rel, verified=True, chars=len(old),
                    message="无变化",
                )

            new_text = "\n".join(merged).rstrip("\n") + "\n"
            if len(new_text) > WIKI_LIMIT:
                return self._refuse(
                    "remember", f"页面超出容量（{len(new_text)}/{WIKI_LIMIT} 字符）", rel, session_id
                )

            updated, review = _dates(lifespan)
            # 页锁 -> 索引锁：所有 wiki 写路径保持同一顺序，避免死锁
            with _lock_for(self.index_path):
                index = self._read_index()
                previous = dict(index)
                index[slug] = {
                    "title": title,
                    "summary": _short(fact),
                    "updated_at": updated,
                    "review_after": review,
                    "lifespan": lifespan if lifespan in {"permanent", "durable", "ephemeral"} else "durable",
                    "verified_by": origin["verified_by"],
                    "source_sessions": list(origin["sources"])[:5],
                    "chars": len(new_text),
                }
                index_text = self._dump_index(index)
                # 容量按「注入上下文的渲染结果」计算，而不是 index.json 的字节数：
                # JSON 带缩进与 title/日期/来源等元数据，旧口径下 4~5 页就把 2000 字符占满了
                rendered = len(render_index(index))
                if rendered > INDEX_LIMIT:
                    topics = "、".join(sorted(previous)) or "（无）"
                    return self._refuse(
                        "remember",
                        f"记忆索引已满（注入 {rendered}/{INDEX_LIMIT} 字符，{len(previous)} 页）。"
                        f"不要原样重试：把内容并入已有主题（topic 取其一：{topics}），"
                        "或先用 memory_forget 删除过期页面",
                        rel,
                        session_id,
                    )

                try:
                    _atomic_write(path, new_text)
                    if not self._page_ok(path, fact, needle, origin["key"]):
                        self._restore(path, old)
                        return self._refuse("remember", "写入后校验失败，已回滚", rel, session_id)
                    _atomic_write(self.index_path, index_text)
                except OSError as exc:
                    self._restore(path, old)
                    return self._refuse("remember", f"写入失败: {exc}", rel, session_id)

                verified = self._page_ok(path, fact, needle, origin["key"]) and slug in self._read_index()
                if not verified:
                    self._restore(path, old)
                    try:
                        _atomic_write(self.index_path, self._dump_index(previous))
                    except OSError as exc:
                        _logger.error("记忆索引回滚失败 path=%s error=%s", INDEX_REL, exc)
                    return self._refuse("remember", "索引校验失败，已回滚", rel, session_id)

            self._archive(removed, fact)
            _logger.info(
                "记忆写入 path=%s chars=%d verified=%s session=%s",
                rel, len(new_text), verified, session_id or "-",
            )
            return Receipt(
                "remember",
                ok=True,
                changed=True,
                path=rel,
                written=[_short(f"- {fact}")],
                removed=[_short(item) for item in removed],
                verified=verified,
                chars=len(new_text),
            )

    def _commit(
        self,
        path: Path,
        rel: str,
        old: str,
        new_text: str,
        *,
        fact: str,
        needle: str,
        key: str = "",
        limit: int,
        removed: list[str],
        session_id: str,
    ) -> Receipt:
        if len(new_text) > limit:
            return self._refuse(
                "remember", f"超出容量（{len(new_text)}/{limit} 字符）", rel, session_id
            )
        try:
            _atomic_write(path, new_text)
        except OSError as exc:
            return self._refuse("remember", f"写入失败: {exc}", rel, session_id)

        verified = self._page_ok(path, fact, needle, key)
        if not verified:
            self._restore(path, old)
            return self._refuse("remember", "写入后校验失败，已回滚", rel, session_id)

        self._archive(removed, fact)
        _logger.info(
            "记忆写入 path=%s chars=%d verified=%s session=%s",
            rel, len(new_text), verified, session_id or "-",
        )
        return Receipt(
            "remember",
            ok=True,
            changed=True,
            path=rel,
            written=[_short(f"- {fact}")],
            removed=[_short(item) for item in removed],
            verified=True,
            chars=len(new_text),
        )

    @_learned_locked
    def forget(self, *, text: str = "", topic: str = "", session_id: str = "") -> Receipt:
        if _normalize(topic):
            return self._forget_wiki(topic, session_id)
        needle = _normalize(text)
        if not needle:
            return self._refuse("forget", "需要提供 text 或 topic", "", session_id)

        path = self.resident_path
        with _lock_for(path):
            try:
                old = self._read(path)
            except OSError as exc:
                return self._refuse("forget", f"读取失败: {exc}", RESIDENT_REL, session_id)

            kept: list[str] = []
            removed: list[str] = []
            for line in old.splitlines():
                if _is_bullet(line) and needle in _clean_line(line):
                    removed.append(_clean_line(line))
                else:
                    kept.append(line)
            if not removed:
                return Receipt(
                    "forget", ok=True, path=RESIDENT_REL, verified=True, chars=len(old),
                    message="未找到匹配条目，未修改",
                )

            new_text = "\n".join(kept).rstrip("\n")
            new_text = new_text + "\n" if new_text else ""
            try:
                _atomic_write(path, new_text)
                remaining = [
                    line
                    for line in self._read(path).splitlines()
                    if _is_bullet(line) and needle in _clean_line(line)
                ]
            except OSError as exc:
                self._restore(path, old)
                return self._refuse("forget", f"写入失败: {exc}", RESIDENT_REL, session_id)

            if remaining:
                self._restore(path, old)
                return self._refuse("forget", "写入后校验失败，已回滚", RESIDENT_REL, session_id)

            self._archive(removed, "forget")
            _logger.info(
                "记忆删除 path=%s removed=%d session=%s",
                RESIDENT_REL, len(removed), session_id or "-",
            )
            return Receipt(
                "forget",
                ok=True,
                changed=True,
                path=RESIDENT_REL,
                removed=[_short(item) for item in removed],
                verified=True,
                chars=len(new_text),
            )

    def _forget_wiki(self, topic: str, session_id: str) -> Receipt:
        slug = _slug(topic)
        rel = f"{WIKI_REL}/{slug}.md"
        path = self.root / rel
        with _lock_for(path):
            if not path.exists():
                return Receipt(
                    "forget", ok=True, path=rel, verified=True, message="页面不存在，未修改"
                )
            with _lock_for(self.index_path):
                old_index = self._read_index()
                try:
                    path.unlink()
                    index = dict(old_index)
                    index.pop(slug, None)
                    _atomic_write(self.index_path, self._dump_index(index))
                except OSError as exc:
                    return self._refuse("forget", f"删除失败: {exc}", rel, session_id)

                verified = not path.exists() and slug not in self._read_index()
            if not verified:
                return self._refuse("forget", "删除后校验失败", rel, session_id)
            _logger.info("记忆删除 path=%s session=%s", rel, session_id or "-")
            return Receipt(
                "forget", ok=True, changed=True, path=rel, verified=True, message="页面已删除"
            )

    # ------------------------------------------------------------------- read
    def search(self, query: str, *, limit: int = 5, scope: str = "all") -> list[dict]:
        """按 ``scope`` 检索常驻记忆与 wiki；sessions 范围由 ``journal`` 负责。"""
        terms = [term.lower() for term in _normalize(query).split() if term]
        if not terms:
            return []
        hits: list[dict] = []
        resident = self._read_or_empty(self.resident_path) if scope in {"all", "memory"} else ""
        for number, line in enumerate(resident.splitlines(), start=1):
            text = _clean_line(line)
            if not text:
                continue
            if any(term in text.lower() for term in terms):
                hits.append(
                    {
                        "source": "memory",
                        "path": RESIDENT_REL,
                        "line": number,
                        "text": _short(text, SNIPPET_CHARS),
                    }
                )
                if len(hits) >= limit:
                    return hits

        scored: list[tuple[int, dict]] = []
        if scope in {"all", "wiki"} and self.wiki_dir.exists():
            for page in sorted(self.wiki_dir.glob("*.md")):
                body = self._read_or_empty(page)
                low = body.lower()
                score = sum(low.count(term) for term in terms)
                if score <= 0:
                    continue
                snippet = ""
                for line in body.splitlines():
                    text = _clean_line(line)
                    if text and any(term in text.lower() for term in terms):
                        snippet = _short(text, SNIPPET_CHARS)
                        break
                scored.append(
                    (
                        score,
                        {
                            "source": "wiki",
                            "path": f"{WIKI_REL}/{page.name}",
                            "line": 0,
                            "text": snippet or page.stem,
                        },
                    )
                )
        scored.sort(key=lambda item: (-item[0], item[1]["path"]))
        for _, hit in scored:
            hits.append(hit)
            if len(hits) >= limit:
                break
        return hits

    def skill_path(self, name: str) -> Path:
        return self.root / "skills" / _skill_slug(name) / "SKILL.md"

    @_learned_locked
    def remember_skill(
        self,
        name: str,
        description: str,
        steps: list[str] | tuple[str, ...],
        *,
        session_id: str = "",
    ) -> Receipt:
        """写入可复用流程 ``skills/<name>/SKILL.md``（含 description frontmatter）。"""
        slug = _skill_slug(name)
        rel = f"skills/{slug}/SKILL.md"
        if not allowed_path(rel):
            return self._refuse("skill", f"目标越界: {rel}", rel, session_id)
        desc = _normalize(description)[:1024]
        clean_steps = [step for step in (_normalize(item) for item in steps) if step]
        if not desc:
            return self._refuse("skill", "缺少技能描述", rel, session_id)
        if not clean_steps:
            return self._refuse("skill", "缺少可执行步骤", rel, session_id)

        title = _normalize(name) or slug
        lines = ["---", f"name: {slug}", f"description: {desc}", "---", "", f"# {title}", ""]
        lines.extend(f"{index}. {step}" for index, step in enumerate(clean_steps, 1))
        text = "\n".join(lines) + "\n"
        if len(text) > WIKI_LIMIT:
            return self._refuse("skill", f"技能超出容量（{len(text)}/{WIKI_LIMIT}）", rel, session_id)

        path = self.root / rel
        with _lock_for(path):
            try:
                old = self._read(path)
            except OSError as exc:
                return self._refuse("skill", f"读取失败: {exc}", rel, session_id)
            if old == text:
                return Receipt("skill", ok=True, path=rel, verified=True, chars=len(text),
                               message="无变化")
            try:
                _atomic_write(path, text)
                restored = self._read(path)
            except OSError as exc:
                self._restore(path, old)
                return self._refuse("skill", f"写入失败: {exc}", rel, session_id)
            if desc not in restored or clean_steps[-1] not in restored:
                self._restore(path, old)
                return self._refuse("skill", "写入后校验失败，已回滚", rel, session_id)

        _logger.info(
            "技能写入 path=%s steps=%d session=%s", rel, len(clean_steps), session_id or "-"
        )
        return Receipt(
            "skill",
            ok=True,
            changed=True,
            path=rel,
            written=[f"{len(clean_steps)} 步"],
            removed=[_short(old)] if old.strip() else [],
            verified=True,
            chars=len(text),
        )

    def status(self) -> dict:
        resident = self._read_or_empty(self.resident_path)
        pages = sorted(self.wiki_dir.glob("*.md")) if self.wiki_dir.exists() else []
        index = self._read_index()
        return {
            "resident": {
                "path": RESIDENT_REL,
                "chars": len(resident),
                "limit": RESIDENT_LIMIT,
            },
            "wiki": {
                "path": WIKI_REL,
                "pages": len(pages),
                "topics": [page.stem for page in pages],
            },
            "index": {"path": INDEX_REL, "entries": len(index), "limit": INDEX_LIMIT},
        }

    # ---------------------------------------------------------------- helpers
    def _refuse(self, action: str, reason: str, rel: str, session_id: str) -> Receipt:
        _logger.warning(
            "记忆操作被拒绝 action=%s reason=%s path=%s session=%s",
            action, reason, rel or "-", session_id or "-",
        )
        return Receipt(action, ok=False, path=rel, message=reason)

    @staticmethod
    def _read(path: Path) -> str:
        """读文件；不存在视为空文件，其他 IO 错误向上抛出。"""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def _read_or_empty(self, path: Path) -> str:
        try:
            return self._read(path)
        except OSError:
            return ""

    @staticmethod
    def _restore(path: Path, old: str) -> None:
        try:
            if old:
                _atomic_write(path, old)
            elif path.exists():
                path.unlink()
        except OSError as exc:
            _logger.error("记忆回滚失败 path=%s error=%s", path.name, exc)

    def _archive(self, removed: list[str], superseded_by: str) -> None:
        """把被替换/删除的旧事实留存到 ``memory/wiki/archive/``，不参与注入。

        归档失败只告警，不影响主写入；旧事实仍可从日志与快照追溯。
        """
        bodies = [body for body in removed if body.strip()]
        if not bodies:
            return
        month = datetime.now().astimezone().strftime("%Y-%m")
        rel = f"{WIKI_REL}/archive/superseded-{month}.md"
        if not allowed_path(rel):
            _logger.warning("记忆归档被拒绝 path=%s reason=gate", rel)
            return
        path = self.root / rel
        stamp = datetime.now().astimezone().date().isoformat()
        lines = [
            f"- {body} <!-- superseded_by: {_short(superseded_by, 80)};at={stamp} -->"
            for body in bodies
        ]
        with _lock_for(path):
            try:
                old = self._read_or_empty(path)
                head = "" if old.strip() else f"# 已替换记忆（{month}）\n"
                _atomic_write(path, head + old + "\n".join(lines) + "\n")
            except OSError as exc:
                _logger.warning("记忆归档失败 path=%s error=%s", rel, exc)
                return
        _logger.info("记忆归档 path=%s removed=%d", rel, len(bodies))

    @staticmethod
    def _page_ok(path: Path, fact: str, needle: str = "", key: str = "") -> bool:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        bodies = [_clean_line(line) for line in lines if _is_bullet(line)]
        if fact not in bodies:
            return False
        if needle:
            return all(body == fact for body in bodies if needle in body)
        if key:
            keyed = [
                body
                for line, body in ((line, _clean_line(line)) for line in lines if _is_bullet(line))
                if split_meta(line)[1].get("key") == key
            ]
            return keyed == [fact]
        return True

    def _read_index(self) -> dict:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            _logger.warning("记忆索引不可读，按空索引处理 path=%s error=%s", INDEX_REL, exc)
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        return entries if isinstance(entries, dict) else {}

    @staticmethod
    def _dump_index(entries: dict) -> str:
        return json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2) + "\n"

    @staticmethod
    def _today() -> str:
        return datetime.now().astimezone().strftime("%Y-%m-%d")


__all__ = [
    "INDEX_LIMIT",
    "index_lines",
    "render_index",
    "INDEX_REL",
    "MemoryStore",
    "RESIDENT_LIMIT",
    "RESIDENT_REL",
    "Receipt",
    "WIKI_REL",
]
