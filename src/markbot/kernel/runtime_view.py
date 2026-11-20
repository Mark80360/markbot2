"""运行时视图：把四条车道的贡献合成为 Kernel 唯一认识的形态。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from markbot.api.extension import Command
from markbot.api.tool import Tool
from markbot.kernel.tools import CommandRegistry, ToolRegistry
from markbot.schema.hashing import tree_hash

DEFAULT_SYSTEM_PROMPT = """你是 MarkBot2，一个边界清晰、可审计的个人助手。

工作方式：
- 先确认目标与约束，再动手；信息不足时说明缺口，不要编造。
- 优先使用已注册工具完成可验证的操作，不要臆测文件内容或命令输出。
- 技能只提供索引，需要时用 read_skill 读取正文，再按正文执行。
- 交付物要落盘，并在回复中给出路径与关键结论。
"""

DEFAULT_GUARDRAILS = """- 不得绕过权限策略；越权请求应直接报告。
- 写文件、执行命令、网络访问前遵守确认策略。
- 密钥只在工具边界使用，绝不写入回复、日志或文件。
- 工具失败时如实报告错误，并尝试可替代的路径。"""


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path
    source: str
    allowed_tools: tuple[str, ...] = ()
    auto_invocable: bool = True
    priority: int = 0


@dataclass
class Strategy:
    system_prompt: str = ""
    guardrails: str = ""
    memory: str = ""
    tool_notes: str = ""
    prompts: list[str] = field(default_factory=list)
    origins: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        parts = [
            self.system_prompt,
            self.guardrails,
            self.memory,
            self.tool_notes,
        ]
        parts.extend(self.prompts)
        return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _split_frontmatter(text: str) -> tuple[str, str]:
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return "", stripped
    lines = stripped.splitlines()
    if len(lines) < 2:
        return "", stripped
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            head = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            return head, body
    return "", stripped


def parse_skill(path: Path, origin: str = "builtin", priority: int = 0) -> SkillMeta | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head, _ = _split_frontmatter(text)
    try:
        data = yaml.safe_load(head) if head.strip() else {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    description = str(data.get("description") or "").strip()
    if not description:
        return None
    name = str(data.get("name") or path.parent.name).strip() or path.parent.name
    allowed = data.get("allowed-tools") or data.get("allowed_tools") or []
    if isinstance(allowed, str):
        allowed = [part.strip() for part in allowed.split(",") if part.strip()]
    allowed_tools = tuple(str(item) for item in allowed if item)
    auto = not bool(data.get("disable-model-invocation", False))
    return SkillMeta(
        name=name,
        description=description,
        path=path,
        source=origin,
        allowed_tools=allowed_tools,
        auto_invocable=auto,
        priority=priority,
    )


class SkillIndex:
    """启动时只加载索引；正文按需读取。"""

    def __init__(self) -> None:
        self._skills: dict[str, SkillMeta] = {}
        self.warnings: list[str] = []

    def add_root(self, root: Path, *, origin: str, priority: int = 0) -> None:
        root = Path(root)
        if not root.exists():
            return
        candidates: list[Path] = []
        if (root / "SKILL.md").exists():
            candidates.append(root / "SKILL.md")
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "SKILL.md").exists():
                candidates.append(child / "SKILL.md")
        for skill_file in candidates:
            meta = parse_skill(skill_file, origin=origin, priority=priority)
            if meta is None:
                self.warnings.append(f"技能 frontmatter 不合法，已跳过: {skill_file}")
                continue
            existing = self._skills.get(meta.name)
            if existing and existing.priority >= meta.priority:
                self.warnings.append(
                    f"技能重名，保留 {existing.source}: {meta.name}（忽略 {origin}）"
                )
                continue
            self._skills[meta.name] = meta

    def get(self, name: str) -> SkillMeta | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def all(self) -> list[SkillMeta]:
        return [self._skills[n] for n in self.names()]

    def __len__(self) -> int:
        return len(self._skills)

    def index_prompt(self, max_chars: int = 8000) -> str:
        if not self._skills:
            return ""
        entries = [m for m in self.all() if m.auto_invocable]
        if not entries:
            return ""

        def line(meta: SkillMeta, short: bool) -> str:
            desc = meta.description.splitlines()[0].strip()
            if short:
                desc = desc[:60]
            return f"- {meta.name}: {desc}（路径：{meta.path.as_posix()}）"

        full = "可用技能：\n" + "\n".join(line(m, False) for m in entries)
        if len(full) <= max_chars:
            return full
        short = "可用技能：\n" + "\n".join(line(m, True) for m in entries)
        return short[:max_chars]

    def read_body(self, name: str) -> tuple[SkillMeta, str]:
        meta = self._skills.get(name)
        if meta is None:
            raise KeyError(name)
        text = meta.path.read_text(encoding="utf-8", errors="replace")
        _, body = _split_frontmatter(text)
        return meta, body.strip()


def load_strategy_dir(directory: Path) -> Strategy | None:
    directory = Path(directory)
    if not directory.exists():
        return None
    strategy = Strategy()
    files = {
        "system_prompt": directory / "system_prompt.md",
        "guardrails": directory / "guardrails.md",
        "memory": directory / "memory.md",
    }
    for attr, path in files.items():
        if path.exists():
            setattr(strategy, attr, path.read_text(encoding="utf-8", errors="replace"))
    notes_dir = directory / "tool_notes"
    if notes_dir.exists():
        notes = [
            p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(notes_dir.glob("*.md"))
        ]
        strategy.tool_notes = "\n\n".join(n for n in notes if n.strip())
    return strategy


@dataclass
class SourceSet:
    name: str
    origin: str
    priority: int = 0
    tools: list[Tool] = field(default_factory=list)
    skill_roots: list[Path] = field(default_factory=list)
    strategy: Strategy | None = None
    sub_agents: dict[str, str] = field(default_factory=dict)
    commands: list[Command] = field(default_factory=list)
    providers: list[Any] = field(default_factory=list)
    subscriptions: list[tuple[str, str, Any]] = field(default_factory=list)
    entries: dict[str, list[Any]] = field(default_factory=dict)
    packages: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RuntimeView:
    tools: ToolRegistry
    skills: SkillIndex
    strategy: Strategy
    sub_agents: dict[str, str]
    commands: CommandRegistry
    providers: list[Any]
    subscriptions: list[tuple[str, str, Any]]
    entries: dict[str, list[Any]]
    sources: dict[str, str]
    warnings: list[str]
    conflicts: list[Any]
    packages: list[dict[str, Any]]
    harness_snapshot: str | None = None
    # learned/ 是记忆与技能的落盘根；运行期记忆工具据此定位写入位置
    learned_dir: Path | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "tools": len(self.tools),
            "skills": len(self.skills),
            "commands": len(self.commands.names()),
            "sub_agents": len(self.sub_agents),
            "packages": len(self.packages),
            "sources": dict(sorted(self.sources.items())),
            "harness_snapshot": self.harness_snapshot,
        }

    def save_snapshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            **self.summary(),
            "packages": self.packages,
            "warnings": self.warnings,
            "conflicts": [
                {"name": c.tool, "winner": c.winner, "loser": c.loser} for c in self.conflicts
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def compose(sources: list[SourceSet]) -> RuntimeView:
    """按优先级合并：project > packages > learned > builtin。"""
    ordered = sorted(sources, key=lambda s: (s.priority, s.name))
    tools = ToolRegistry()
    skills = SkillIndex()
    commands = CommandRegistry()
    strategy = Strategy()
    sub_agents: dict[str, str] = {}
    providers: list[Any] = []
    subscriptions: list[tuple[str, str, Any]] = []
    entries: dict[str, list[Any]] = {}
    source_map: dict[str, str] = {}
    warnings: list[str] = []
    packages: list[dict[str, Any]] = []

    for source in ordered:
        for tool in source.tools:
            tools.register(tool, origin=source.origin, priority=source.priority)
        for root in source.skill_roots:
            skills.add_root(root, origin=source.origin, priority=source.priority)
        for command in source.commands:
            commands.register(command, origin=source.origin, priority=source.priority)
        providers.extend(source.providers)
        subscriptions.extend(source.subscriptions)
        for key, values in source.entries.items():
            entries.setdefault(key, []).extend(values)
        sub_agents.update(source.sub_agents)
        packages.extend(source.packages)
        warnings.extend(source.warnings)

        if source.strategy is not None:
            if source.strategy.system_prompt:
                strategy.system_prompt = source.strategy.system_prompt
            if source.strategy.guardrails:
                strategy.guardrails = (
                    f"{strategy.guardrails}\n\n{source.strategy.guardrails}".strip()
                    if strategy.guardrails
                    else source.strategy.guardrails
                )
            if source.strategy.memory:
                strategy.memory = source.strategy.memory
            if source.strategy.tool_notes:
                strategy.tool_notes = (
                    f"{strategy.tool_notes}\n\n{source.strategy.tool_notes}".strip()
                    if strategy.tool_notes
                    else source.strategy.tool_notes
                )
            strategy.prompts.extend(source.strategy.prompts)

    for tool in tools.all():
        source_map[tool.name] = tools.origin_of(tool.name)
    for meta in skills.all():
        source_map[f"skill:{meta.name}"] = meta.source

    warnings.extend(skills.warnings)
    return RuntimeView(
        tools=tools,
        skills=skills,
        strategy=strategy,
        sub_agents=sub_agents,
        commands=commands,
        providers=providers,
        subscriptions=subscriptions,
        entries=entries,
        sources=source_map,
        warnings=warnings,
        conflicts=tools.conflicts + commands.conflicts,
        packages=packages,
    )


def build_runtime_view(sources: list[SourceSet], learned_dir: Path | None = None) -> RuntimeView:
    view = compose(sources)
    if learned_dir is not None:
        view.learned_dir = Path(learned_dir)
        if view.learned_dir.exists():
            view.harness_snapshot = tree_hash(view.learned_dir)
    return view


__all__ = [
    "DEFAULT_GUARDRAILS",
    "DEFAULT_SYSTEM_PROMPT",
    "RuntimeView",
    "SkillIndex",
    "SkillMeta",
    "SourceSet",
    "Strategy",
    "build_runtime_view",
    "compose",
    "load_strategy_dir",
    "parse_skill",
]
