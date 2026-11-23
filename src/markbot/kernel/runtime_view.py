"""运行时视图：把四条车道的贡献合成为 Kernel 唯一认识的形态。"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from markbot.api.extension import Command
from markbot.api.tool import Tool
from markbot.kernel.tools import CommandRegistry, ToolRegistry
from markbot.schema.hashing import tree_hash

DEFAULT_SYSTEM_PROMPT = """你是 MarkBot，一个边界清晰、可审计的个人助手。

工作方式：
- 先确认目标与约束，再动手；信息不足时说明缺口，不要编造。
- 优先使用已注册工具完成可验证的操作，不要臆测文件内容或命令输出。
- 技能只提供索引，需要时用 read_skill 读取正文，再按正文执行。
- 交付物要落盘，并在回复中给出路径与关键结论。
"""

DEFAULT_GUARDRAILS = """- 不得绕过权限策略；越权请求应直接报告。
- 写文件、执行命令、网络访问前遵守确认策略。
- 密钥只在工具边界使用，绝不写入回复、日志或文件。
- 需要密钥时只引用名称，交给工具在执行时注入（run_command 的 secrets 参数）；
  不要读取配置文件、日志或环境去获取密钥值，也不要还原、拆分或编码被遮蔽的 [REDACTED]。
- 上面两条针对的是 MarkBot 配置里的密钥。用户在对话里主动提供、并明确要求你使用的凭证
  （网站 Cookie、一次性口令等）属于用户授权：可以在本次任务里按要求使用（如注入浏览器或请求头，
  临时文件放在会话临时目录、用完删除），只是不要在回复里复述、不要写进长期记忆；
  被遮蔽成 [REDACTED] 的部分请用户换一种方式提供，不要猜。
- 自己能做什么以工具列表和 <env> 为准（例如「对话渠道」说明了能否发图片/文件），
  不要臆测「没有接入」「不支持」；确实受限时说明是哪条规则或哪个配置项。
- MarkBot 数据目录受保护：技能与长期记忆只能用 memory_* 工具写入，不要用文件或命令工具
  直接修改；临时脚本与数据写到工作区或 MarkBot 临时目录（默认 ~/.markbot/workspace）。
- 工具失败时如实报告错误，并尝试可替代的路径。"""


SKILL_NAME_MAX = 64
SKILL_DESCRIPTION_MAX = 1024
SKILL_ASSET_MAX_CHARS = 40_000

# 与 Agent Skills 约定一致：小写、数字、单个短横线，且不以短横线收尾。
_SKILL_NAME = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path
    source: str
    allowed_tools: tuple[str, ...] = ()
    auto_invocable: bool = True
    priority: int = 0
    requires_bins: tuple[str, ...] = ()
    requires_env: tuple[str, ...] = ()


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


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _requirements(data: dict[str, Any]) -> dict[str, Any]:
    """读取 ``requires``；兼容 ``metadata.markbot`` / ``metadata.nanobot``。"""
    requires = data.get("requires")
    if isinstance(requires, dict):
        return requires
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        for key in ("markbot", "nanobot"):
            payload = metadata.get(key)
            if isinstance(payload, dict) and isinstance(payload.get("requires"), dict):
                return payload["requires"]
    return {}


def validate_skill_metadata(data: Any, directory: str) -> str | None:
    """检查技能 frontmatter 的身份契约；合规返回 None，否则返回原因。"""
    if not isinstance(data, dict):
        return "frontmatter 不是映射"
    name = data.get("name")
    if not isinstance(name, str) or not name:
        return "缺少 name"
    if name != directory:
        return f"name 必须与目录名一致（期望 {directory}）"
    if len(name) > SKILL_NAME_MAX or _SKILL_NAME.fullmatch(name) is None:
        return f"name 必须是小写短横线格式且不超过 {SKILL_NAME_MAX} 字符"
    description = data.get("description")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= SKILL_DESCRIPTION_MAX:
        return f"description 必须是 1~{SKILL_DESCRIPTION_MAX} 字符的字符串"
    allowed = data.get("allowed-tools", data.get("allowed_tools"))
    if allowed is not None and not isinstance(allowed, (str, list)):
        return "allowed-tools 必须是字符串或列表"
    requires = data.get("requires")
    if requires is not None and not isinstance(requires, dict):
        return "requires 必须是映射"
    return None


def parse_skill(
    path: Path, origin: str = "builtin", priority: int = 0
) -> tuple[SkillMeta | None, str]:
    """解析 SKILL.md，返回 ``(元数据, 错误原因)``。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"读取失败: {exc}"
    head, _ = _split_frontmatter(text)
    try:
        data = yaml.safe_load(head) if head.strip() else {}
    except yaml.YAMLError as exc:
        return None, f"frontmatter YAML 不合法: {exc}"
    error = validate_skill_metadata(data, path.parent.name)
    if error is not None or not isinstance(data, dict):
        return None, error or "frontmatter 不是映射"
    requires = _requirements(data)
    return (
        SkillMeta(
            name=str(data["name"]).strip(),
            description=str(data["description"]).strip(),
            path=path,
            source=origin,
            allowed_tools=tuple(_string_list(data.get("allowed-tools", data.get("allowed_tools")))),
            auto_invocable=not bool(data.get("disable-model-invocation", False)),
            priority=priority,
            requires_bins=tuple(_string_list(requires.get("bins"))),
            requires_env=tuple(_string_list(requires.get("env"))),
        ),
        "",
    )


@dataclass(frozen=True)
class _SkillRoot:
    path: Path
    origin: str
    priority: int


def _skill_files(root: Path) -> list[Path]:
    files: list[Path] = []
    direct = root / "SKILL.md"
    if direct.is_file():
        files.append(direct)
    try:
        children = sorted(root.iterdir())
    except OSError:
        return files
    for child in children:
        candidate = child / "SKILL.md"
        if child.is_dir() and candidate.is_file():
            files.append(candidate)
    return files


def _clip_lines(header: str, lines: list[str], max_chars: int) -> str:
    marker = f"\n[… 技能索引已截断，共 {len(lines)} 个，用 list_skills 查看全部]"
    limit = max(max_chars - len(marker), len(header))
    kept: list[str] = []
    total = len(header)
    for line in lines:
        if total + 1 + len(line) > limit:
            break
        kept.append(line)
        total += 1 + len(line)
    body = header + ("\n" + "\n".join(kept) if kept else "")
    return body + marker


class SkillIndex:
    """技能索引：启动时只加载元数据，正文与附带资源按需读取。

    每次访问都会比对目录指纹，因此学习层在运行期写入的
    ``skills/<name>/SKILL.md`` 无需重启或手动 reload 就能进入索引。
    """

    def __init__(self) -> None:
        self._skills: dict[str, SkillMeta] = {}
        self._roots: list[_SkillRoot] = []
        self._fingerprint: tuple[tuple[str, int | None, int], ...] | None = None
        self._lock = threading.RLock()
        self.warnings: list[str] = []

    def add_root(self, root: Path, *, origin: str, priority: int = 0) -> None:
        # 目录可以暂不存在：学习层与项目技能会在运行期创建。
        with self._lock:
            self._roots.append(_SkillRoot(Path(root), origin, priority))
            self._rebuild()
            self._fingerprint = self._scan_fingerprint()

    def _rebuild(self) -> None:
        skills: dict[str, SkillMeta] = {}
        warnings: list[str] = []
        for root in self._roots:
            for skill_file in _skill_files(root.path):
                meta, error = parse_skill(
                    skill_file, origin=root.origin, priority=root.priority
                )
                if meta is None:
                    warnings.append(f"技能已跳过（{error}）: {skill_file}")
                    continue
                existing = skills.get(meta.name)
                if existing and existing.priority >= meta.priority:
                    warnings.append(
                        f"技能重名，保留 {existing.source}: {meta.name}（忽略 {root.origin}）"
                    )
                    continue
                skills[meta.name] = meta
        self._skills = skills
        self.warnings = warnings

    def _scan_fingerprint(self) -> tuple[tuple[str, int | None, int], ...]:
        parts: list[tuple[str, int | None, int]] = []
        for root in self._roots:
            if not root.path.exists():
                parts.append((root.path.as_posix(), None, 0))
                continue
            for skill_file in _skill_files(root.path):
                try:
                    stat = skill_file.stat()
                except OSError:
                    continue
                parts.append((skill_file.as_posix(), stat.st_mtime_ns, stat.st_size))
        return tuple(parts)

    def _ensure_fresh(self) -> None:
        if not self._roots:
            return
        current = self._scan_fingerprint()
        if self._fingerprint is None:
            self._fingerprint = current
            return
        if current == self._fingerprint:
            return
        with self._lock:
            current = self._scan_fingerprint()
            if current == self._fingerprint:
                return
            self._rebuild()
            self._fingerprint = current

    def get(self, name: str) -> SkillMeta | None:
        self._ensure_fresh()
        return self._skills.get(name)

    def names(self) -> list[str]:
        self._ensure_fresh()
        return sorted(self._skills)

    def all(self) -> list[SkillMeta]:
        self._ensure_fresh()
        return [self._skills[name] for name in sorted(self._skills)]

    def __len__(self) -> int:
        self._ensure_fresh()
        return len(self._skills)

    @staticmethod
    def missing_requirements(meta: SkillMeta) -> list[str]:
        missing = [
            f"CLI: {binary}" for binary in meta.requires_bins if shutil.which(binary) is None
        ]
        missing.extend(
            f"ENV: {name}" for name in meta.requires_env if not os.environ.get(name)
        )
        return missing

    def index_prompt(self, max_chars: int = 8000) -> str:
        self._ensure_fresh()
        entries = [meta for meta in self.all() if meta.auto_invocable]
        if not entries:
            return ""

        available: list[SkillMeta] = []
        unavailable: list[SkillMeta] = []
        for meta in entries:
            (unavailable if self.missing_requirements(meta) else available).append(meta)

        def line(meta: SkillMeta, *, short: bool = False) -> str:
            desc = meta.description.splitlines()[0].strip()
            if short and len(desc) > 60:
                desc = desc[:60] + "…"
            return f"- {meta.name}: {desc}（来源：{meta.source}）"

        header = "可用技能（正文用 read_skill，技能内文件用 read_skill_file）："
        if available:
            base = header + "\n" + "\n".join(line(meta) for meta in available)
            if len(base) > max_chars:
                base = header + "\n" + "\n".join(line(meta, short=True) for meta in available)
            if len(base) > max_chars:
                base = _clip_lines(
                    header, [line(meta, short=True) for meta in available], max_chars
                )
        else:
            base = "可用技能：暂无（其余技能缺少依赖，见 list_skills）"

        if unavailable:
            note = "\n不可用技能（缺少依赖）：" + "、".join(meta.name for meta in unavailable)
            if len(base) + len(note) <= max_chars:
                base += note
        return base

    def read_body(self, name: str) -> tuple[SkillMeta, str]:
        meta = self.get(name)
        if meta is None:
            raise KeyError(name)
        text = meta.path.read_text(encoding="utf-8", errors="replace")
        _, body = _split_frontmatter(text)
        # 兼容外部技能里的 {baseDir} 占位符，统一换成本技能所在目录。
        return meta, body.strip().replace("{baseDir}", str(meta.path.parent.resolve()))

    def read_asset(
        self,
        name: str,
        rel_path: str,
        *,
        max_chars: int = SKILL_ASSET_MAX_CHARS,
    ) -> tuple[SkillMeta, Path, str]:
        """读取技能目录内的附带文件（references/assets 等）。

        资源读取限定在该技能的目录内，越界直接拒绝，避免绕过工作区边界。
        """
        meta = self.get(name)
        if meta is None:
            raise KeyError(name)
        base = meta.path.parent.resolve()
        target = (base / rel_path).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise PermissionError(f"技能资源越界: {rel_path}") from exc
        if not target.is_file():
            raise FileNotFoundError(rel_path)
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…（已截断）"
        return meta, target, text


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
