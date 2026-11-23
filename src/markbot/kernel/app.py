"""Kernel 门面：把配置、信任、运行时视图、循环组装成一个可运行对象。"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from markbot.api.events import EventName
from markbot.api.media import image_block, user_images, vision_enabled
from markbot.api.ui import UI, NullUI
from markbot.kernel.config import Config, load_config, normalize_list, secret_values
from markbot.kernel.context import ContextBuilder, environment_block, project_brief
from markbot.kernel.daily_log import DailyLog
from markbot.kernel.events import EventBus
from markbot.kernel.execution import LocalExecution
from markbot.kernel.loop import AgentLoop, LoopConfig, TurnResult, add_turn_usage
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers import Provider, ProviderError, build_provider, build_role_providers
from markbot.kernel.registry import RuntimeAssembler
from markbot.kernel.retention import prune_logs
from markbot.kernel.runtime_view import RuntimeView
from markbot.kernel.session import (
    COMPACTION_PREFIX,
    COMPACTION_SUFFIX,
    Session,
    new_session_id,
    now_iso,
    register_secrets,
)
from markbot.kernel.tools import ToolRegistry
from markbot.kernel.trust import TrustStore
from markbot.kernel.turn import TurnContext, bind_turn, turn_workspace

_DOMAIN_HINTS = {
    "office": ("xlsx", "excel", "ppt", "word", "docx", "表格", "文档", "周报"),
    "code": ("code", "git", "repo", "代码", "仓库", "测试"),
    "web": ("http", "url", "网页", "抓取", "搜索"),
    "pdf": ("pdf", "扫描件", "提取"),
}

# 渠道里的显式技能调用：``$demo 后面的内容`` 只匹配索引中存在的技能。
_EXPLICIT_SKILL = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9-]*)")

_MISSING = object()

# 常驻内存的会话上限。渠道/定时任务的会话由 session_id 寻址、可随时从归档恢复，
# 超出上限时按最近活动时间淘汰（淘汰前补一次快照），避免长驻进程内存只增不减。
MAX_RESIDENT_SESSIONS = 32


_SUMMARY_ITEMS = 20
_SUMMARY_ITEM_CHARS = 200


def _moderation_safe_summary(messages: list[Any]) -> str:
    """确定性摘要：最近的用户/助手正文（截断），跳过工具结果、占位与本轮请求。"""
    items = [
        message
        for message in messages
        if message.role in ("user", "assistant") and (message.content or "").strip()
    ]
    last_user = max(
        (index for index, message in enumerate(items) if message.role == "user"), default=None
    )
    lines: list[str] = []
    for index, message in enumerate(items):
        if index == last_user:
            continue
        text = " ".join(message.content.split())
        if text.startswith("[该条内容触发上游内容安全策略"):
            continue
        if text.startswith(" ".join(COMPACTION_PREFIX.split())):
            lines.append(f"- 更早的摘要：{text[:1500]}")
            continue
        who = "用户" if message.role == "user" else "助手"
        clipped = text if len(text) <= _SUMMARY_ITEM_CHARS else text[:_SUMMARY_ITEM_CHARS] + "…"
        lines.append(f"- {who}：{clipped}")
    lines = lines[-_SUMMARY_ITEMS:]
    head = "上一个会话因内容审核无法继续，以下是此前对话的要点（工具输出已省略）："
    return head + "\n" + ("\n".join(lines) if lines else "- （无可保留的内容）")


def guess_domain(text: str) -> str:
    low = text.lower()
    for domain, hints in _DOMAIN_HINTS.items():
        if any(hint in low for hint in hints):
            return domain
    return "general"


class Kernel:
    def __init__(
        self,
        *,
        config: Config,
        runtime: RuntimeView,
        provider: Provider,
        trust: TrustStore,
        ui: UI,
        warnings: list[str] | None = None,
        workspace: Path | None = None,
        gateway_logs: bool = False,
        role_providers: dict[str, Provider] | None = None,
    ) -> None:
        self.config = config
        # 按用途路由的模型（fast 用于压缩与记忆整理；ADR 0011）；没有配置时回退主模型
        self.role_providers: dict[str, Provider] = dict(role_providers or {})
        self.home = Path(config.home)
        self.project_root = Path(config.project_root)
        self.workspace = Path(workspace or self.project_root).resolve()
        self.runtime = runtime
        self.provider = provider
        self.trust = trust
        self.ui = ui
        self.secrets = dict(config.secrets)
        # 登记密钥原值：之后日志、归档、工具结果都按值遮蔽，不再只靠形状正则
        register_secrets(secret_values(config))
        self.events = EventBus()
        self.warnings = list(warnings or [])
        self.logs = DailyLog(
            self.home,
            enabled=self.config.logging.enabled,
            runtime=self.config.logging.runtime,
            interaction=self.config.logging.interaction,
            mirror_gateway=gateway_logs,
        )
        for warning in self.warnings:
            self.logs.runtime_log("WARN", warning)
        self.prune_logs()
        self.session: Session | None = None
        self._sessions: dict[str, Session] = {}
        # 空闲归档用的时间戳：最近活动、最近一次成功归档（均为 time.time()）
        self._activity: dict[str, float] = {}
        self._archived_at: dict[str, float] = {}
        # 并发回合：会话表的增删查由 _state_lock 保护；_running 记录在途回合，
        # 淘汰与空闲归档不能碰正在运行的会话
        self._state_lock = threading.RLock()
        self._running: dict[str, int] = {}
        self._unsubscribers: list[Callable[[], None]] = []
        # 已在聊天里提示过的「未信任被跳过」的包：每个进程每个包只提示一次
        self._skip_notified: set[str] = set()
        # 在途回合的事件循环与任务（每个回合跑在各自线程的 asyncio.run 里），供 cancel() 跨线程取消
        self._turn_tasks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task[Any]]] = {}
        # 绑定项目时按工作区缓存 (policy, env)（ADR 0013）
        self._scopes: dict[str, tuple[PermissionPolicy, LocalExecution]] = {}
        self._sub_counter = 0
        self._turn_started: dict[str, float] = {}
        self._cancelled_tasks: set[int] = set()
        self._wire(runtime)
        self._announce_extensions(runtime)
        self.policy = self._policy()
        self.loop = self._loop()

    def prune_logs(self) -> None:
        """按 [logging] 的保留策略清理按日日志；失败只记警告。"""
        if not self.config.logging.enabled:
            return
        try:
            report = prune_logs(
                self.home,
                retention_days=self.config.logging.retention_days,
                interaction_retention_days=self.config.logging.interaction_retention_days,
                max_total_mb=self.config.logging.max_total_mb,
            )
        except OSError as exc:
            self.logs.runtime_log("WARN", "日志清理失败", error=str(exc))
            return
        if report.deleted:
            self.logs.runtime_log(
                "INFO", "日志清理", deleted=len(report.deleted), freed_bytes=report.freed_bytes
            )

    def set_ui(self, ui: UI) -> UI:
        """临时替换 UI（例如把确认请求转发到聊天），返回原 UI 以便恢复。"""
        previous = self.ui
        self.ui = ui
        self.loop._ui = ui
        return previous

    # ------------------------------------------------------------------ build
    @classmethod
    def build(
        cls,
        *,
        home: Path | None = None,
        project_root: Path | None = None,
        config: Config | None = None,
        provider: Provider | None = None,
        ui: UI | None = None,
        include_packages: bool = True,
        learned_dir: Path | None = None,
        inject_failure: str | None = None,
        workspace: Path | None = None,
        gateway_logs: bool = False,
    ) -> "Kernel":
        cfg = config or load_config(home=home, project_root=project_root)
        ui = ui or NullUI()
        trust = TrustStore(cfg.home, cfg.project_root)
        assembler = RuntimeAssembler(
            cfg,
            trust=trust,
            ui=ui,
            include_packages=include_packages,
            learned_dir=learned_dir,
            inject_failure=inject_failure,
        )
        runtime = assembler.build()
        runtime.save_snapshot(Path(cfg.home) / "cache" / "runtime_view.json")
        prov = provider or build_provider(cfg, cfg.secrets)
        # 只有 provider 由配置构建时才构建角色模型：测试/评测传入的脚本 provider 不能被
        # 配置里的真实模型悄悄替换掉部分调用
        roles: dict[str, Provider] = {}
        if provider is None:
            try:
                roles = build_role_providers(cfg, cfg.secrets)
            except ProviderError:
                roles = {}
        return cls(
            config=cfg,
            runtime=runtime,
            provider=prov,
            trust=trust,
            ui=ui,
            warnings=[*getattr(cfg, "warnings", []), *assembler.warnings],
            workspace=workspace,
            gateway_logs=gateway_logs,
            role_providers=roles,
        )

    def role_provider(self, role: str) -> Provider:
        """按用途取模型；没有配置该角色时返回主模型。"""
        return self.role_providers.get(role) or self.provider

    # ---------------------------------------------------------------- helpers
    def _policy(self) -> PermissionPolicy:
        return PermissionPolicy(self.config.permissions, root=self.workspace, home=self.home)

    def _make_env(self, policy: PermissionPolicy) -> LocalExecution:
        return LocalExecution(
            policy.root,
            guard=policy.guard,
            os_sandbox_mode=getattr(policy, "os_sandbox", "off"),
            shell=self.config.shell.program or "auto",
            passthrough_env=normalize_list(self.config.shell.passthrough_env),
        )

    def _scope_for_turn(self) -> tuple[PermissionPolicy, LocalExecution]:
        """本回合的 (policy, env)：绑定了项目时用项目目录作为工作区（ADR 0013）。"""
        root = turn_workspace()
        if root is None:
            return self.policy, self.env
        key = str(root)
        with self._state_lock:
            scope = self._scopes.get(key)
            if scope is None:
                policy = PermissionPolicy(self.config.permissions, root=Path(root), home=self.home)
                scope = (policy, self._make_env(policy))
                self._scopes[key] = scope
        return scope

    def _project_section(self, session: Any = None) -> str:
        root = turn_workspace()
        return project_brief(Path(root)) if root is not None else ""

    def projects(self) -> dict[str, str]:
        return dict(self.config.projects)

    def _loop(self) -> AgentLoop:
        # 常驻记忆由内置 memory 扩展注册（runtime.entries["context_providers"]），
        # Kernel 不依赖 markbot.memory 的实现
        env = self._make_env(self.policy)
        self.env = env
        self._scopes = {}
        token_budget = int(getattr(self.config.agent, "context_budget_tokens", 0) or 0)
        budget_chars = token_budget * 3 if token_budget > 0 else self.config.agent.context_budget_chars
        context = ContextBuilder(
            self.runtime,
            budget_chars=budget_chars,
            environment=lambda session=None: environment_block(self._scope_for_turn()[1], session),
            project=self._project_section,
            vision=self.vision,
            max_images=int(getattr(self.config.agent, "vision_max_images", 3) or 0),
        )
        cfg = LoopConfig(
            max_steps=self.config.agent.max_steps,
            tool_timeout_s=self.config.agent.tool_timeout_s,
            parallel_tools=self.config.agent.parallel_tools,
            notice_compaction=self.config.memory.notice_compaction,
            tool_result_max_chars=self.config.agent.tool_result_max_chars,
            verify_reminder=self.config.dev.verify_reminder,
        )
        return AgentLoop(
            self.provider,
            self.runtime.tools,
            context,
            self.events,
            self.policy,
            cfg,
            secrets=self.secrets,
            ui=self.ui,
            compact_budget=budget_chars,
            logs=self.logs,
            files_root=self.home / "sessions",
            env=env,
            compact_provider=self.role_providers.get("fast"),
            scope=self._scope_for_turn,
            subagent=self.run_subagent,
            token_budget=token_budget,
            vision=self.describe_image,
        )

    # ------------------------------------------------------------- 子代理
    async def run_subagent(
        self,
        parent: Session,
        prompt: str,
        *,
        tools: list[str],
        max_steps: int = 20,
        label: str = "sub",
    ) -> dict[str, Any]:
        """在独立子会话里运行一个子代理（ADR 0013）。

        - 工具只能是当前运行时已有工具的子集，且不含 ``spawn_agent``（不允许递归）；
        - 权限、确认 UI、工作区与父回合相同（同一个 policy 与 TurnContext），只会更少不会更多；
        - 子会话不落盘（不会被优化器与记忆整理当成真实会话），token 计入父回合；
        - 父回合被取消时子代理一起取消。
        """
        registry = ToolRegistry()
        missing: list[str] = []
        for name in dict.fromkeys(tools):
            tool = self.runtime.tools.get(name)
            if tool is None or name == "spawn_agent":
                missing.append(name)
                continue
            registry.register(tool, origin=self.runtime.tools.origin_of(name))
        with self._state_lock:
            self._sub_counter += 1
            sub_id = f"{parent.id}~{label}{self._sub_counter}"
        child = Session(
            prompt,
            session_id=sub_id,
            skills=self.runtime.skills,
            runtime=self.runtime,
            domain=getattr(parent, "domain", "general"),
            events_dir=None,
        )
        child.metadata["subagent"] = {"parent": parent.id, "label": label}
        steps = max(1, min(int(max_steps or 20), int(self.config.agent.max_steps)))
        loop = AgentLoop(
            self.role_provider("coder"),
            registry,
            self.loop._context,
            self.events,
            self.policy,
            LoopConfig(
                max_steps=steps,
                tool_timeout_s=self.config.agent.tool_timeout_s,
                parallel_tools=self.config.agent.parallel_tools,
                notice_compaction=False,
                tool_result_max_chars=self.config.agent.tool_result_max_chars,
                verify_reminder=self.config.dev.verify_reminder,
            ),
            secrets=self.secrets,
            ui=self.ui,
            compact_budget=self.loop._compactor.budget_chars if self.loop._compactor else None,
            logs=self.logs,
            files_root=self.home / "sessions",
            env=self.env,
            compact_provider=self.role_providers.get("fast"),
            scope=self._scope_for_turn,
            subagent=None,
            vision=self.describe_image,
        )
        result = await loop.run(child)
        add_turn_usage(result.total_usage)
        return {
            "session_id": sub_id,
            "text": result.text,
            "stopped_reason": result.stopped_reason,
            "error": result.error,
            "steps": child.steps,
            "tool_calls": result.tool_calls,
            "usage": dict(result.total_usage),
            "missing_tools": missing,
            "changed_files": sorted(getattr(child, "file_changes", None) or {}),
        }

    def _wire(self, runtime: RuntimeView) -> None:
        for event, owner, handler in runtime.subscriptions:
            self._unsubscribers.append(self.events.subscribe(event, owner, handler))

    def _announce_extensions(self, runtime: RuntimeView) -> None:
        for entry in runtime.entries.get("extensions", []):
            self.events.emit_sync(
                EventName.EXTENSION_LOADED,
                extension=entry.get("id", ""),
                path=entry.get("path", ""),
            )

    # ------------------------------------------------------------------- run
    def _attach_runtime(self, session: Session) -> Session:
        """让已有会话使用当前运行时视图。

        ``reload()`` 会换一份新的 RuntimeView；长驻会话若继续持有旧的技能索引与
        runtime，会出现「新写入的技能 read_skill 找不到」这类状态分叉。
        """
        session.skills = self.runtime.skills
        session.runtime = self.runtime
        return session

    # ------------------------------------------------------------- 多模态（ADR 0015）
    @property
    def vision(self) -> bool:
        """主模型能否直接看图（``[agent] vision``，auto 按 provider / 模型名判断）。"""
        from markbot.kernel.config import resolve_provider_kind

        agent = self.config.agent
        try:
            kind = resolve_provider_kind(self.config)
        except Exception:  # noqa: BLE001
            kind = str(agent.provider or "")
        return vision_enabled(getattr(agent, "vision", "auto"), kind, str(agent.model or ""))

    def _request_images(self, request: str) -> list[str]:
        """用户消息里渠道已下载的图片（``[image: 路径]``，限 ``<home>/media`` 与工作区内）。"""
        return user_images(request, [self.home / "media", self.workspace])

    async def describe_image(self, path: str, question: str = "") -> str:
        """用视觉模型描述图片：优先 ``[agent.roles.vision]``，主模型能看图时用主模型。"""
        provider = self.role_providers.get("vision") or (self.provider if self.vision else None)
        if provider is None:
            raise RuntimeError(
                "没有可用的视觉模型：主模型不支持看图，且未配置 [agent.roles.vision]（如 model = \"qwen-vl-max\"）"
            )
        if image_block(path) is None:
            raise RuntimeError(f"无法读取图片（不存在、过大或格式不支持）: {path}")
        prompt = question.strip() or "详细描述这张图片：主要内容、所有可见文字（原样转写）、界面元素与它们的位置。"
        reply = await provider.complete(
            {
                "system": "你是视觉助手，只根据图片如实回答，看不清的地方直说。",
                "messages": [{"role": "user", "content": prompt, "images": [str(path)]}],
                "max_tokens": 1500,
            }
        )
        add_turn_usage(dict(reply.usage or {}))
        return reply.text.strip()

    def _pick_session(
        self,
        request: str,
        *,
        new_session: bool,
        session_id: str | None,
        persist_events: bool = True,
    ) -> tuple[Session, bool]:
        images = self._request_images(request)
        if session_id:
            existing = self._sessions.get(session_id)
            if existing is not None:
                self._attach_runtime(existing)
                existing.append_user(request, images=images)
                self.session = existing
                return existing, False
            # 服务重启后内存里的会话已丢失：显式传 session_id 的调用方
            # （渠道）从归档恢复上下文；没传 id 的本地会话仍每次全新开始。
            restored = Session.load(
                self.home,
                session_id,
                skills=self.runtime.skills,
                runtime=self.runtime,
            )
            if restored is not None:
                restored.append_user(request, images=images)
                self._sessions[session_id] = restored
                self.session = restored
                self.logs.runtime_log(
                    "INFO",
                    "会话已从归档恢复",
                    session=session_id,
                    messages=len(restored.messages),
                )
                return restored, False
        elif not new_session and self.session is not None:
            self._attach_runtime(self.session)
            self.session.append_user(request, images=images)
            return self.session, False
        session = Session(
            request,
            session_id=session_id,
            skills=self.runtime.skills,
            runtime=self.runtime,
            domain=guess_domain(request),
            # 不归档的会话（CLI 交互/评测）不落地事件日志，避免产生无用目录。
            events_dir=(self.home / "sessions") if persist_events else None,
            request_images=images,
        )
        self._sessions[session.id] = session
        self.session = session
        return session, True

    async def run(
        self,
        request: str,
        *,
        archive: bool = True,
        new_session: bool = False,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_event: Callable[[Any], None] | None = None,
        end_previous: bool = True,
        ui: UI | None = None,
        grants: Iterable[str] = (),
        project: str | None = None,
    ) -> TurnResult:
        """执行一个回合。

        ``new_session=True`` 开新会话；``end_previous`` 决定是否同时结束「当前会话」。
        CLI 的 ``/new`` 需要结束上一个会话；HTTP 渠道与定时任务是多会话并存的，
        必须传 ``end_previous=False``，否则一个新聊天会把别的聊天或定时任务的会话结束掉。

        ``ui`` / ``grants`` 只对本回合生效（:class:`TurnContext`）：确认请求转发到哪、
        临时放行哪些程序。并发回合之间互不影响，不再需要 ``set_ui`` / ``push_grants``。
        """
        workspace = None
        if project:
            root = self.config.projects.get(project)
            if root is None:
                known = "、".join(self.config.projects) or "（无）"
                return TurnResult(
                    stopped_reason="error",
                    error=f"未知项目: {project}。已登记的项目：{known}（用 markbot project add 登记）",
                    session_id=session_id,
                )
            workspace = Path(root)
        turn = (
            TurnContext.create(ui=ui, grants=grants, workspace=workspace)
            if (ui is not None or grants or workspace is not None)
            else None
        )
        current = asyncio.current_task()
        with bind_turn(turn):
            try:
                return await self._run_turn(
                    request,
                    archive=archive,
                    new_session=new_session,
                    session_id=session_id,
                    metadata=metadata,
                    on_delta=on_delta,
                    on_event=on_event,
                    end_previous=end_previous,
                )
            except asyncio.CancelledError:
                # cancel() 恰好落在回合收尾阶段（循环已结束、归档中）：按取消返回，不让
                # CancelledError 穿出 asyncio.run 把请求线程打死
                if current is None or id(current) not in self._cancelled_tasks:
                    raise
                if hasattr(current, "uncancel"):
                    current.uncancel()
                return TurnResult(
                    stopped_reason="cancelled", error="已被用户取消", session_id=session_id
                )
            finally:
                if current is not None:
                    self._cancelled_tasks.discard(id(current))

    async def _run_turn(
        self,
        request: str,
        *,
        archive: bool,
        new_session: bool,
        session_id: str | None,
        metadata: dict[str, Any] | None,
        on_delta: Callable[[str], None] | None,
        on_event: Callable[[Any], None] | None,
        end_previous: bool,
    ) -> TurnResult:
        blocked = await self.events.emit(EventName.INPUT, text=request)
        if blocked.blocked:
            self.logs.runtime_log(
                "WARN", "输入被拦截", reason=blocked.block_reason or "input blocked"
            )
            return TurnResult(
                stopped_reason="blocked",
                error=blocked.block_reason or "input blocked",
            )
        budget = int(getattr(self.config.agent, "daily_token_budget", 0) or 0)
        if budget > 0:
            used = self.logs.metrics.tokens_today()
            if used >= budget:
                self.logs.runtime_log("WARN", "今日 token 已达上限，拒绝新回合", used=used, budget=budget)
                return TurnResult(
                    stopped_reason="budget",
                    error=(
                        f"今日模型用量已达上限（{used}/{budget} tokens），明天自动恢复；"
                        "需要继续请调大 [agent] daily_token_budget"
                    ),
                    session_id=session_id,
                )
        if new_session and end_previous and self.session is not None:
            await self.end_session(archive=archive)
        with self._state_lock:
            session, fresh = self._pick_session(
                request,
                new_session=new_session,
                session_id=session_id,
                persist_events=archive,
            )
            self._running[session.id] = self._running.get(session.id, 0) + 1
            task = asyncio.current_task()
            if task is not None:
                self._turn_tasks[session.id] = (asyncio.get_running_loop(), task)
                self._turn_started[session.id] = time.time()
        try:
            return await self._drive(session, fresh, request, archive, metadata, on_delta, on_event)
        finally:
            with self._state_lock:
                left = self._running.get(session.id, 1) - 1
                if left <= 0:
                    self._running.pop(session.id, None)
                    self._turn_tasks.pop(session.id, None)
                    self._turn_started.pop(session.id, None)
                else:
                    self._running[session.id] = left
            session.current_tool = None

    async def _drive(
        self,
        session: Session,
        fresh: bool,
        request: str,
        archive: bool,
        metadata: dict[str, Any] | None,
        on_delta: Callable[[str], None] | None,
        on_event: Callable[[Any], None] | None,
    ) -> TurnResult:
        # 工具（如记忆写入）需要把关键动作写进运行日志；日志按会话归属
        session.logs = self.logs
        # 归档是否额外写 trajectory/compacted 派生文件（ADR 0007）；实例属性覆盖类默认值
        session.write_legacy_views = self.config.sessions.legacy_views
        active_skills = self._explicit_skills(request)
        if active_skills:
            session.metadata["_active_skills"] = active_skills
        if fresh:
            await self.events.emit(EventName.SESSION_START, session=session)
            self.logs.runtime_log("INFO", "会话开始", session=session.id)
        unsubscribe = self._subscribe_session(session, on_event)
        # 运行期元数据只对本次回合可见，结束后恢复，避免污染同一会话的后续请求
        previous: dict[str, Any] = {}
        if metadata:
            for key in metadata:
                previous[key] = session.metadata.get(key, _MISSING)
            session.metadata.update(metadata)
        try:
            result = await self.loop.run(session, on_delta=on_delta)
        finally:
            if unsubscribe is not None:
                unsubscribe()
            session.metadata.pop("_active_skills", None)
            for key, old in previous.items():
                if old is _MISSING:
                    session.metadata.pop(key, None)
                else:
                    session.metadata[key] = old
        notice = self._skipped_packages_notice(session)
        if notice:
            result.text = f"{result.text}\n\n{notice}".strip()
        session.status = (
            "completed" if result.stopped_reason == "completed" else result.stopped_reason
        )
        self._activity[session.id] = time.time()
        self.logs.runtime_log(
            "INFO" if result.stopped_reason == "completed" else "WARN",
            "回合结束",
            session=session.id,
            reason=result.stopped_reason,
            tool_calls=result.tool_calls,
            error=result.error,
        )
        if archive:
            path = session.archive(
                self.home, harness_snapshot=self.runtime.harness_snapshot
            )
            self._archived_at[session.id] = time.time()
            self.logs.runtime_log("INFO", "会话已归档", session=session.id, path=str(path))
            # 归档是快照；会话仍然可以继续下一轮。
            session.ended_at = None
        if result.stopped_reason == "moderation":
            result = self._rotate_after_moderation(session, result)
        with self._state_lock:
            self._evict_sessions(keep=result.session_id or session.id)
        return result

    def _rotate_after_moderation(self, session: Session, result: TurnResult) -> TurnResult:
        """审核拒绝且无法恢复：开一个只带摘要的新会话，旧会话原样归档（ADR 0011）。

        旧实现里这种会话会一直被上游拒绝（11-20 同一会话连续 4 次失败），只能手动 /new。
        摘要只取用户与助手的正文（工具输出最常是触发源），不调用模型（可能再次被拒）；
        本轮请求本身可能就是触发源，不放进摘要。渠道按返回的 session_id 更新会话映射。
        """
        if not getattr(self.config.agent, "moderation_new_session", True):
            return result
        if not session.persists_events or str(session.id).startswith("cron_"):
            return result
        summary = _moderation_safe_summary(session.messages)
        seed = f"{COMPACTION_PREFIX}{summary}{COMPACTION_SUFFIX}"
        fresh = Session(
            seed,
            session_id=new_session_id(),
            skills=self.runtime.skills,
            runtime=self.runtime,
            domain=session.domain,
            events_dir=self.home / "sessions",
        )
        fresh.status = "completed"
        try:
            fresh.archive(self.home, harness_snapshot=self.runtime.harness_snapshot)
            fresh.ended_at = None
        except OSError as exc:
            self.logs.runtime_log("WARN", "审核后新会话归档失败", session=fresh.id, error=str(exc))
        with self._state_lock:
            self._sessions[fresh.id] = fresh
            self._activity[fresh.id] = time.time()
            self._archived_at[fresh.id] = time.time()
            if self.session is session:
                self.session = fresh
        self.logs.runtime_log(
            "WARN", "内容审核无法恢复，已切换到新会话", old=session.id, new=fresh.id
        )
        notice = (
            "（上一段对话里有内容持续触发上游内容审核，已自动开启新会话并保留了摘要；"
            "请改写后重新发送本条请求。）"
        )
        result.text = f"{result.text}\n\n{notice}".strip()
        result.session_id = fresh.id
        return result

    def _skipped_packages_notice(self, session: Session) -> str:
        """能力包因未信任被跳过时，在聊天回复末尾提示一次（ADR 0009）。

        之前只写运行日志，用户感知不到能力丢失（office 包被静默跳过十几次）。
        定时任务的结果不附加提示。
        """
        if str(session.id).startswith("cron_"):
            return ""
        skipped = [
            str(pkg.get("id"))
            for pkg in (self.runtime.packages or [])
            if not pkg.get("trusted") and not pkg.get("error")
            and str(pkg.get("id")) not in self._skip_notified
        ]
        if not skipped:
            return ""
        self._skip_notified.update(skipped)
        names = "、".join(skipped)
        commands = "；".join(f"markbot trust {name}" for name in skipped)
        return (
            f"（提示：能力包 {names} 未被信任，本次运行已跳过，相关工具与技能不可用。"
            f"需要时在终端运行 `{commands}` 启用，或运行 `markbot doctor` 查看详情。）"
        )

    def cancel(self, session_id: str) -> bool:
        """取消会话的在途回合（线程安全）；没有在途回合返回 False。

        回合会补齐被中断工具的结果（调用/结果配对不被破坏）、杀掉子进程，
        以 ``stopped_reason="cancelled"`` 结束并照常归档。
        """
        with self._state_lock:
            entry = self._turn_tasks.get(session_id)
            session = self._sessions.get(session_id)
            if entry is None:
                return False
            loop, task = entry
            if session is not None:
                session.cancel_requested = True
            self._cancelled_tasks.add(id(task))
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # 事件循环已关闭：回合其实已经结束
            return False
        self.logs.runtime_log("WARN", "请求取消回合", session=session_id)
        return True

    def turn_status(self, session_id: str) -> dict[str, Any]:
        """会话当前回合的进度（供 /status）：是否在跑、步数、当前工具、已用时长。"""
        with self._state_lock:
            session = self._sessions.get(session_id)
            running = session_id in self._running
            started = self._turn_started.get(session_id)
        info: dict[str, Any] = {"session_id": session_id, "running": running}
        if session is not None:
            info.update(
                steps=getattr(session, "steps", 0),
                tool_calls=getattr(session, "tool_call_count", 0),
                current_tool=getattr(session, "current_tool", None) if running else None,
                messages=len(session.messages),
                status=getattr(session, "status", ""),
                todos=list(getattr(session, "todos", None) or []),
                changed_files=sorted(getattr(session, "file_changes", None) or {}),
            )
        if running and started:
            info["elapsed_s"] = round(time.time() - started, 1)
        return info

    def get_session(self, session_id: str) -> Session | None:
        """内存里的会话（不从归档加载）。"""
        with self._state_lock:
            return self._sessions.get(session_id)

    def running_sessions(self) -> list[str]:
        with self._state_lock:
            return sorted(self._running)

    def is_running(self, session_id: str) -> bool:
        with self._state_lock:
            return session_id in self._running

    def forget_session(self, session_id: str) -> bool:
        """把会话移出内存（不结束、不发事件）；之后带同一 id 的请求会从归档恢复。"""
        with self._state_lock:
            session = self._sessions.pop(session_id, None)
            self._activity.pop(session_id, None)
            self._archived_at.pop(session_id, None)
            if session is not None and self.session is session:
                self.session = None
            return session is not None

    def _evict_sessions(self, *, keep: str, limit: int | None = None) -> list[str]:
        """超过常驻上限时，按最近活动淘汰可恢复的会话；淘汰前有未归档活动则补一次快照。"""
        limit = MAX_RESIDENT_SESSIONS if limit is None else limit
        if len(self._sessions) <= limit:
            return []
        protected = {keep, self.session.id if self.session is not None else "", *self._running}
        candidates = sorted(
            (
                sid
                for sid, session in self._sessions.items()
                if sid not in protected
                # 只淘汰能从磁盘恢复的会话；不落盘的交互会话留在内存
                and (session.archive_dir is not None or session.persists_events)
            ),
            key=lambda sid: self._activity.get(sid, 0.0),
        )
        evicted: list[str] = []
        for sid in candidates:
            if len(self._sessions) <= limit:
                break
            session = self._sessions[sid]
            if self._activity.get(sid, 0.0) > self._archived_at.get(sid, 0.0):
                try:
                    session.archive(self.home, harness_snapshot=self.runtime.harness_snapshot)
                    session.ended_at = None
                except OSError as exc:
                    self.logs.runtime_log("WARN", "淘汰前归档失败，保留在内存", session=sid, error=str(exc))
                    continue
            self.forget_session(sid)
            evicted.append(sid)
        if evicted:
            self.logs.runtime_log("INFO", "会话移出内存", sessions=evicted, resident=len(self._sessions))
        return evicted

    def _explicit_skills(self, request: str) -> list[str]:
        names: list[str] = []
        for name in _EXPLICIT_SKILL.findall(request):
            if name in names or self.runtime.skills.get(name) is None:
                continue
            names.append(name)
        return names

    def _subscribe_session(
        self, session: Session, on_event: Callable[[Any], None] | None
    ) -> Callable[[], None] | None:
        """把本次回合的会话事件转给调用方；返回退订函数。

        本轮用户消息在 ``_pick_session`` 里已写入，这里先补发一次，
        保证订阅者从用户消息开始就能看到完整序列。
        """
        if on_event is None:
            return None
        latest = session.events[-1] if session.events else None
        if latest is not None and latest.type == "user_message":
            try:
                on_event(latest)
            except Exception:  # noqa: BLE001 - 订阅者失败不影响会话
                pass
        return session.subscribe(on_event)

    async def archive_idle(self, idle_minutes: float) -> list[str]:
        """把空闲超过阈值的会话快照写入 ``sessions/``，让整理器看到中途内容。

        归档语义与 ``run(archive=True)`` 一致：只写快照，会话可继续使用。
        同一会话在无新活动时不会重复归档；失败只记日志并等下个周期重试。
        """
        if idle_minutes <= 0:
            return []
        now = time.time()
        idle_s = float(idle_minutes) * 60
        archived: list[str] = []
        with self._state_lock:
            snapshot = [
                (sid, session) for sid, session in self._sessions.items() if sid not in self._running
            ]
        for sid, session in snapshot:
            active = self._activity.get(sid, 0.0)
            if not active or now - active < idle_s:
                continue
            if self._archived_at.get(sid, 0.0) >= active:
                continue
            if session.ended_at:
                continue
            try:
                path = session.archive(
                    self.home, harness_snapshot=self.runtime.harness_snapshot
                )
            except OSError as exc:
                self.logs.runtime_log("WARN", "空闲归档失败", session=sid, error=str(exc))
                continue
            self._archived_at[sid] = now
            # 归档只是快照：会话仍可继续，结束时再写最终版本。
            session.ended_at = None
            self.logs.runtime_log("INFO", "空闲归档", session=sid, path=str(path))
            archived.append(sid)
        return archived

    async def end_session(self, *, archive: bool = True):
        session = self.session
        if session is None:
            return None
        self.session = None
        session.ended_at = now_iso()
        await self.events.emit(EventName.SESSION_END, session=session, result=None)
        self.logs.runtime_log("INFO", "会话结束", session=session.id)
        if archive:
            path = session.archive(
                self.home, harness_snapshot=self.runtime.harness_snapshot
            )
            self._archived_at[session.id] = time.time()
            self.logs.runtime_log("INFO", "会话已归档", session=session.id, path=str(path))
            return path
        return None

    async def run_text(self, request: str, *, archive: bool = True) -> str:
        return (await self.run(request, archive=archive)).text

    async def run_skill(
        self, name: str, args: str = "", *, archive: bool = False
    ) -> TurnResult:
        try:
            meta, body = self.runtime.skills.read_body(name)
        except KeyError:
            return TurnResult(stopped_reason="error", error=f"未知技能: {name}")
        prompt = (
            f"请按技能 {meta.name} 的说明完成任务。\n\n"
            f"<skill name=\"{meta.name}\">\n{body}\n</skill>\n\n"
            f"用户补充：{args or '无'}"
        )
        return await self.run(prompt, archive=archive)

    # ---------------------------------------------------------------- reload
    def reload(self) -> RuntimeView:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        assembler = RuntimeAssembler(
            self.config,
            trust=self.trust,
            ui=self.ui,
            include_packages=True,
            learned_dir=None,
        )
        self.runtime = assembler.build()
        self.runtime.save_snapshot(self.home / "cache" / "runtime_view.json")
        self._wire(self.runtime)
        self._announce_extensions(self.runtime)
        self.policy = self._policy()
        self.loop = self._loop()
        self.warnings = assembler.warnings
        return self.runtime

    async def shutdown(self, *, archive: bool = True) -> None:
        """结束当前会话并广播 shutdown。``archive=False`` 用于评测等一次性 Kernel，
        避免把临时会话写进 ``sessions/``（会被优化器与记忆整理当成真实会话学习）。"""
        await self.end_session(archive=archive)
        await self.events.emit(EventName.SHUTDOWN)

    def summary(self) -> dict[str, Any]:
        return self.runtime.summary()


__all__ = ["Kernel", "guess_domain"]
