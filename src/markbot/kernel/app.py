"""Kernel 门面：把配置、信任、运行时视图、循环组装成一个可运行对象。"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from markbot.api.events import EventName
from markbot.api.ui import UI, NullUI
from markbot.kernel.config import Config, load_config, secret_values
from markbot.kernel.context import ContextBuilder
from markbot.kernel.daily_log import DailyLog
from markbot.kernel.events import EventBus
from markbot.kernel.loop import AgentLoop, LoopConfig, TurnResult
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers import Provider, build_provider
from markbot.kernel.registry import RuntimeAssembler
from markbot.kernel.retention import prune_logs
from markbot.kernel.runtime_view import RuntimeView
from markbot.kernel.session import Session, now_iso, register_secrets
from markbot.kernel.trust import TrustStore
from markbot.kernel.turn import TurnContext, bind_turn

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
    ) -> None:
        self.config = config
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
        return cls(
            config=cfg,
            runtime=runtime,
            provider=prov,
            trust=trust,
            ui=ui,
            warnings=[*getattr(cfg, "warnings", []), *assembler.warnings],
            workspace=workspace,
            gateway_logs=gateway_logs,
        )

    # ---------------------------------------------------------------- helpers
    def _policy(self) -> PermissionPolicy:
        return PermissionPolicy(self.config.permissions, root=self.workspace, home=self.home)

    def _loop(self) -> AgentLoop:
        # 常驻记忆由内置 memory 扩展注册（runtime.entries["context_providers"]），
        # Kernel 不依赖 markbot.memory 的实现
        context = ContextBuilder(
            self.runtime,
            budget_chars=self.config.agent.context_budget_chars,
        )
        cfg = LoopConfig(
            max_steps=self.config.agent.max_steps,
            tool_timeout_s=self.config.agent.tool_timeout_s,
            parallel_tools=self.config.agent.parallel_tools,
            notice_compaction=self.config.memory.notice_compaction,
            tool_result_max_chars=self.config.agent.tool_result_max_chars,
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
            compact_budget=self.config.agent.context_budget_chars,
            logs=self.logs,
            files_root=self.home / "sessions",
        )

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

    def _pick_session(
        self,
        request: str,
        *,
        new_session: bool,
        session_id: str | None,
        persist_events: bool = True,
    ) -> tuple[Session, bool]:
        if session_id:
            existing = self._sessions.get(session_id)
            if existing is not None:
                self._attach_runtime(existing)
                existing.append_user(request)
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
                restored.append_user(request)
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
            self.session.append_user(request)
            return self.session, False
        session = Session(
            request,
            session_id=session_id,
            skills=self.runtime.skills,
            runtime=self.runtime,
            domain=guess_domain(request),
            # 不归档的会话（CLI 交互/评测）不落地事件日志，避免产生无用目录。
            events_dir=(self.home / "sessions") if persist_events else None,
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
    ) -> TurnResult:
        """执行一个回合。

        ``new_session=True`` 开新会话；``end_previous`` 决定是否同时结束「当前会话」。
        CLI 的 ``/new`` 需要结束上一个会话；HTTP 渠道与定时任务是多会话并存的，
        必须传 ``end_previous=False``，否则一个新聊天会把别的聊天或定时任务的会话结束掉。

        ``ui`` / ``grants`` 只对本回合生效（:class:`TurnContext`）：确认请求转发到哪、
        临时放行哪些程序。并发回合之间互不影响，不再需要 ``set_ui`` / ``push_grants``。
        """
        turn = TurnContext.create(ui=ui, grants=grants) if (ui is not None or grants) else None
        with bind_turn(turn):
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
        try:
            return await self._drive(session, fresh, request, archive, metadata, on_delta, on_event)
        finally:
            with self._state_lock:
                left = self._running.get(session.id, 1) - 1
                if left <= 0:
                    self._running.pop(session.id, None)
                else:
                    self._running[session.id] = left

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
        with self._state_lock:
            self._evict_sessions(keep=session.id)
        return result

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
