"""上下文组装：每次请求重新合成，不做增量拼接。

按分节预算组装：``system`` / ``guardrails`` 是硬约束，永不裁剪；其余分节按
优先级整块保留或整块丢弃，只有列表型内容（技能索引）允许带标记截断。任何
省略都会在上下文里留下可见标记，禁止静默消失。
"""

from __future__ import annotations

import logging
import platform
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from markbot.api.media import DEFAULT_MAX_IMAGES, prepare_images
from markbot.kernel.runtime_view import RuntimeView

_logger = logging.getLogger("markbot.context")

SKILL_INDEX_MAX = 8_000

# 扩展通过 ``api.append_entry(CONTEXT_PROVIDERS, factory)`` 注册常驻上下文来源；
# ``factory(learned_dir)`` 返回带 ``render()`` 的对象或 ``None``。Kernel 不直接依赖
# 记忆实现（markbot.memory），记忆只是扩展车道上的一个提供者。
CONTEXT_PROVIDERS = "context_providers"


@runtime_checkable
class ContextProvider(Protocol):
    def render(self) -> str: ...


ContextProviderFactory = Callable[[Path], "ContextProvider | None"]


def providers_from_runtime(runtime: Any) -> list[ContextProvider]:
    learned = getattr(runtime, "learned_dir", None)
    if learned is None:
        return []
    out: list[ContextProvider] = []
    for factory in (getattr(runtime, "entries", None) or {}).get(CONTEXT_PROVIDERS, []):
        try:
            provider = factory(Path(learned))
        except Exception as exc:  # noqa: BLE001 - 单个提供者失败不影响组装
            _logger.warning("上下文提供者初始化失败: %s", exc)
            continue
        if provider is not None:
            out.append(provider)
    return out


@dataclass
class Section:
    name: str
    text: str
    priority: int
    hard: bool = False
    cap: int | None = None
    allow_partial: bool = False


_SHELL_NOTES = {
    "bash": "命令按 bash 语法书写。",
    "sh": "命令由 POSIX sh 执行（可能是 dash）：不要用 bash 专有语法（[[ ]]、${var//x/y}、数组、<(...)）。",
    "pwsh": "命令由 PowerShell 7 执行：用 PowerShell 语法（Get-ChildItem、Select-String、$env:NAME），"
    "不要假设有 ls/grep/sed 等 Unix 工具。",
    "powershell": "命令由 Windows PowerShell 5 执行：用 PowerShell 语法，注意 curl 是 Invoke-WebRequest 的别名，"
    "需要原生 curl 时写 curl.exe。",
    "cmd": "命令由 cmd.exe 执行：用 cmd 语法（dir、type、findstr、%NAME%）。",
}


_TOOLS_CACHE: list[str] | None = None
_PROBE_TOOLS = ("git", "rg", "python", "python3", "node", "npm", "pnpm", "go", "cargo", "docker", "uv", "gh")


def available_tools() -> list[str]:
    """PATH 上可用的常用开发工具（进程内缓存，只探测一次）。"""
    global _TOOLS_CACHE
    if _TOOLS_CACHE is None:
        _TOOLS_CACHE = [name for name in _PROBE_TOOLS if shutil.which(name)]
    return _TOOLS_CACHE


def git_branch(root: Path) -> str:
    """读取 .git/HEAD 得到当前分支（不调用 git）；不在仓库里返回空串。"""
    for directory in (root, *root.parents):
        head = directory / ".git"
        if head.is_file():  # worktree：.git 是指向真实目录的文件
            try:
                target = head.read_text(encoding="utf-8").split(":", 1)[1].strip()
                head = (directory / target).resolve()
            except (OSError, IndexError):
                return ""
        head_file = head / "HEAD"
        if head_file.is_file():
            try:
                text = head_file.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
            if text.startswith("ref:"):
                return text.split("/", 2)[-1] if text.count("/") >= 2 else text[4:].strip()
            return f"(detached {text[:8]})"
    return ""


def scratch_dir(env: Any, session: Any) -> Path | None:
    home = getattr(getattr(env, "guard", None), "home", None)
    session_id = getattr(session, "id", None)
    if home is None or not session_id:
        return None
    return Path(home) / "sessions" / str(session_id) / "scratch"


# 对话渠道能力说明：模型不知道自己在哪个渠道时，会臆测「不能发图片」（2026-11-23 日志）
CHANNEL_NOTES = {
    "feishu": (
        "飞书。你的回复会发到飞书聊天；回复里写 ![说明](本地绝对路径) 或 [文件名](本地绝对路径)，"
        "渠道会把该图片/文件作为飞书消息一并发给用户（截图、二维码、图表、报告都可以直接发）。"
        "后台任务完成后的结果同样会带上这些图片/文件"
    ),
}


def channel_line(session: Any) -> str:
    metadata = getattr(session, "metadata", None) or {}
    channel = str(metadata.get("channel") or "").strip().lower() if isinstance(metadata, dict) else ""
    if not channel:
        return ""
    return f"对话渠道: {CHANNEL_NOTES.get(channel, channel)}"


def environment_block(env: Any, session: Any = None) -> str:
    """运行环境说明（时间、时区、系统、shell、工作目录、git 分支、可用工具），每轮重新生成。

    让模型不必调用工具查时间，也不会按错误的 shell 语法写命令（ADR 0009 / 0012）。
    """
    now = datetime.now().astimezone()
    shell = str(getattr(env, "shell", "") or "sh")
    root = getattr(env, "root", None)
    lines = [
        "<env>",
        # 精确到分钟：秒级变化会让每一步的 system 前缀都不同，不利于 provider 侧缓存
        f"当前时间: {now.strftime('%Y-%m-%d %H:%M')} {now.strftime('%A')} (UTC{now.strftime('%z')})",
        f"操作系统: {platform.system()} {platform.release()} ({platform.machine()})",
        f"命令 shell: {shell}。{_SHELL_NOTES.get(shell, '')}".rstrip(),
    ]
    if root is not None:
        lines.append(f"工作目录: {root}")
        branch = git_branch(Path(root))
        if branch:
            lines.append(f"git 分支: {branch}")
    tools = available_tools()
    if tools:
        lines.append(f"可用命令行工具: {', '.join(tools)}")
    scratch = scratch_dir(env, session)
    if scratch is not None:
        lines.append(f"本会话临时目录（可读写，放临时脚本与中间数据）: {scratch}")
    writable = getattr(getattr(env, "guard", None), "writable", ()) or ()
    if writable:
        lines.append(f"共享工作目录（可读写，跨会话保留）: {writable[0]}")
    channel = channel_line(session)
    if channel:
        lines.append(channel)
    lines.append("</env>")
    return "\n".join(lines)


# ------------------------------------------------------------------ 项目说明（ADR 0013）
_AGENT_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "CONTRIBUTING.md")
_STACK_MARKERS = (
    ("pyproject.toml", "Python（pyproject）"),
    ("requirements.txt", "Python（requirements）"),
    ("package.json", "Node.js"),
    ("tsconfig.json", "TypeScript"),
    ("go.mod", "Go"),
    ("Cargo.toml", "Rust"),
    ("pom.xml", "Java（Maven）"),
    ("build.gradle", "Java/Kotlin（Gradle）"),
    ("Dockerfile", "Docker"),
)
AGENT_FILE_CHARS = 3000
README_CHARS = 1200
_BRIEF_CACHE: dict[str, tuple[tuple[int, ...], str]] = {}


def project_brief(root: Path) -> str:
    """绑定项目时注入的说明：技术栈 + 协作约定文件（AGENTS.md 等）+ README 开头。按 mtime 缓存。"""
    root = Path(root)
    candidates = [root / name for name in (*_AGENT_FILES, "README.md")]
    stamps: list[int] = []
    for path in candidates:
        try:
            stamps.append(path.stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    key = str(root)
    cached = _BRIEF_CACHE.get(key)
    if cached and cached[0] == tuple(stamps):
        return cached[1]
    stack = [label for marker, label in _STACK_MARKERS if (root / marker).exists()]
    lines = [f"当前项目：{root.name}（{root}）"]
    if stack:
        lines.append("技术栈：" + "、".join(stack))
    for path in candidates[:-1]:
        text = _read_head(path, AGENT_FILE_CHARS)
        if text:
            lines.append(f"项目约定（{path.name}，修改代码时必须遵守）：\n{text}")
            break
    readme = _read_head(candidates[-1], README_CHARS)
    if readme:
        lines.append(f"README 开头：\n{readme}")
    brief = "\n\n".join(lines)
    _BRIEF_CACHE[key] = (tuple(stamps), brief)
    return brief


def _read_head(path: Path, limit: int) -> str:
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if len(text) > limit:
        text = text[:limit] + f"\n[… 已截断，完整内容见 {path.name}]"
    return text


# ------------------------------------------------------------------ 重复读取去重（ADR 0013）
_STALE_READ = "[同一文件的同一范围在后面被重新读取过，此处旧内容已省略，以后面的读取结果为准]"
_DEDUPE_MIN_CHARS = 500


def dedupe_reads(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一文件同一范围被 read_file 读过多次时，只在发给模型的上下文里保留最后一次的内容。

    只改本次请求的副本，不修改会话（会话事实不变，归档与审计看到的仍是原文）。
    """
    keys: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            if call.get("name") != "read_file":
                continue
            args = call.get("arguments") or {}
            path = str(args.get("path") or "")
            if path:
                keys[str(call.get("id"))] = (
                    f"{path}|{args.get('start_line', 1)}|{args.get('max_lines', 400)}"
                )
    if len(keys) < 2:
        return messages
    last: dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.get("role") == "tool" and str(message.get("tool_call_id")) in keys:
            last[keys[str(message.get("tool_call_id"))]] = index
    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        key = keys.get(str(message.get("tool_call_id"))) if message.get("role") == "tool" else None
        if (
            key is not None
            and last.get(key, index) != index
            and not message.get("is_error")
            and len(str(message.get("content") or "")) >= _DEDUPE_MIN_CHARS
        ):
            message = {**message, "content": _STALE_READ}
        out.append(message)
    return out


def compose_sections(sections: list[Section], budget: int) -> str:
    fixed = [section for section in sections if section.hard]
    rest = sorted(
        (section for section in sections if not section.hard),
        key=lambda section: section.priority,
        reverse=True,
    )
    kept = [section.text.strip() for section in fixed if section.text.strip()]
    remaining = budget - sum(len(text) for text in kept)
    dropped: list[str] = []
    for section in rest:
        text = section.text.strip()
        if not text:
            continue
        if section.cap is not None:
            text = text[: section.cap]
        if len(text) <= remaining:
            kept.append(text)
            remaining -= len(text)
        elif section.allow_partial and remaining >= 50:
            kept.append(text[:remaining] + f"\n[… 已省略，完整内容见 {section.name}]")
            remaining = 0
        else:
            dropped.append(section.name)
    if dropped:
        kept.append(f"[本轮省略：{'、'.join(dropped)}]")
    return "\n\n".join(kept)


class ContextBuilder:
    def __init__(
        self,
        runtime: RuntimeView,
        *,
        budget_chars: int = 60_000,
        memory: ContextProvider | None = None,
        environment: Callable[..., str] | None = None,
        project: Callable[..., str] | None = None,
        vision: bool = False,
        max_images: int = DEFAULT_MAX_IMAGES,
    ) -> None:
        self.runtime = runtime
        # 多模态（ADR 0015）：模型能否看图、每次请求最多带几张
        self.vision = vision
        self.max_images = max_images
        self.budget_chars = budget_chars
        # ``strategy.memory`` 只是启动快照；常驻块必须每轮从 learned/ 重新渲染，
        # 否则本回合写入、下回合读不到。未显式传入时取扩展注册的第一个提供者。
        if memory is None:
            found = providers_from_runtime(runtime)
            memory = found[0] if found else None
        self.memory = memory
        self.environment = environment
        self.project = project

    async def build(self, session: Any, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "system": self._compose_system(session),
            "messages": prepare_images(
                dedupe_reads(session.messages_for_model()),
                vision=self.vision,
                max_images=self.max_images,
            ),
            "tools": tool_schemas,
        }

    def _active_skills(self, session: Any) -> str:
        """渲染本轮 ``$skill`` 显式指定的技能正文，调用结束后不留在会话里。"""
        blocks: list[str] = []
        for name in session.metadata.get("_active_skills") or []:
            try:
                meta, body = self.runtime.skills.read_body(str(name))
            except KeyError:
                continue
            blocks.append(f"### {meta.name}\n{body}")
        if not blocks:
            return ""
        text = "用户显式指定的技能（按正文执行）：\n\n" + "\n\n".join(blocks)
        if len(text) > 20_000:
            text = text[:20_000] + "\n[… 技能正文过长，已截断]"
        return text

    def _environment(self, session: Any = None) -> str:
        if self.environment is None:
            return ""
        try:
            try:
                return self.environment(session)
            except TypeError:
                return self.environment()
        except Exception as exc:  # noqa: BLE001 - 环境说明失败不影响组装
            _logger.warning("运行环境说明生成失败: %s", exc)
            return ""

    def _project(self, session: Any) -> str:
        if self.project is None:
            return ""
        try:
            return self.project(session)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("项目说明生成失败: %s", exc)
            return ""

    def _compose_system(self, session: Any) -> str:
        strategy = self.runtime.strategy
        resident = self.memory.render() if self.memory is not None else strategy.memory
        sections = [
            Section("system_prompt", strategy.system_prompt, priority=100, hard=True),
            Section("guardrails", strategy.guardrails, priority=95, hard=True),
            Section("environment", self._environment(session), priority=90),
            Section("project", self._project(session), priority=89),
            Section("todos", _todos_section(session), priority=88),
            Section("work_rules", _work_rules(session), priority=87),
            Section(
                "active_skills",
                self._active_skills(session),
                priority=85,
            ),
            Section("memory", resident, priority=70),
            Section("tool_notes", strategy.tool_notes, priority=65),
            Section("prompts", "\n\n".join(strategy.prompts), priority=60),
            Section(
                "skills",
                self.runtime.skills.index_prompt(max_chars=SKILL_INDEX_MAX),
                priority=50,
                cap=SKILL_INDEX_MAX,
                allow_partial=True,
            ),
        ]
        text = compose_sections(sections, self.budget_chars)
        _logger.debug(
            "上下文组装 chars=%d budget=%d sections=%s",
            len(text),
            self.budget_chars,
            ",".join(str(len(section.text)) for section in sections),
        )
        return text


_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]", "cancelled": "[-]"}

CODING_RULES = """编码任务的工作方式：
- 先了解再动手：用 grep_search / file_outline / read_file 找到相关代码，确认现状与约定（README、AGENTS.md）。
- 小步修改：已有文件用 edit_file（唯一的 old_string）或 apply_patch，不要整文件重写；保持原有风格。
- 多步骤任务先用 todo_write 列计划，逐项推进并更新状态。
- 改完必须验证：用 run_tests / run_checks 运行测试与检查；失败就定位修复后重跑，最多几轮，仍失败如实说明。
- 改用户仓库前先 git_status：有无关的未提交改动时不要混进来，较大的改动在新分支或 git_worktree 里做；
  提交用 git_commit 并只列出本次改动的文件。
- 可以用 spawn_agent 把独立子任务（探索多处代码、评审改动、补测试）交给子代理并行完成。
- 交付时用 write_report 生成报告，并说明改了哪些文件、为什么改、验证结果与未解决的风险。"""

RESEARCH_RULES = """调研任务的工作方式：
- 优先使用可靠的一手来源；关键事实、数据与结论附上来源链接。
- fetch_url 被拦截（blocked）或需要登录、执行 JavaScript 时，改用浏览器工具。
- 区分事实与推测；信息不足时说明缺口，不要编造。"""


def _todos_section(session: Any) -> str:
    todos = getattr(session, "todos", None) or []
    if not todos:
        return ""
    done = sum(1 for item in todos if item.get("status") == "completed")
    lines = [f"当前任务清单（完成 {done}/{len(todos)}；进展后用 todo_write 更新）:"]
    for index, item in enumerate(todos, 1):
        lines.append(f"{index}. {_MARKS.get(item.get('status', ''), '[ ]')} {item.get('content', '')}")
    return "\n".join(lines)


def _work_rules(session: Any) -> str:
    domain = str(getattr(session, "domain", "") or "")
    if domain == "code" or getattr(session, "file_changes", None):
        return CODING_RULES
    if domain == "web":
        return RESEARCH_RULES
    return ""


__all__ = [
    "CODING_RULES",
    "CONTEXT_PROVIDERS",
    "ContextBuilder",
    "ContextProvider",
    "ContextProviderFactory",
    "dedupe_reads",
    "environment_block",
    "project_brief",
    "providers_from_runtime",
]
