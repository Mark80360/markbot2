"""Kernel 门面：把配置、信任、运行时视图、循环组装成一个可运行对象。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from markbot.api.events import EventName
from markbot.api.ui import UI, NullUI
from markbot.kernel.config import Config, load_config
from markbot.kernel.context import ContextBuilder
from markbot.kernel.daily_log import DailyLog
from markbot.kernel.events import EventBus
from markbot.kernel.loop import AgentLoop, LoopConfig, TurnResult
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers import Provider, build_provider
from markbot.kernel.registry import RuntimeAssembler
from markbot.kernel.runtime_view import RuntimeView
from markbot.kernel.session import Session, now_iso
from markbot.kernel.trust import TrustStore
from markbot.memory.provider import MemoryProvider

_DOMAIN_HINTS = {
    "office": ("xlsx", "excel", "ppt", "word", "docx", "表格", "文档", "周报"),
    "code": ("code", "git", "repo", "代码", "仓库", "测试"),
    "web": ("http", "url", "网页", "抓取", "搜索"),
    "pdf": ("pdf", "扫描件", "提取"),
}

_MISSING = object()


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
        self.session: Session | None = None
        self._sessions: dict[str, Session] = {}
        # 空闲归档用的时间戳：最近活动、最近一次成功归档（均为 time.time()）
        self._activity: dict[str, float] = {}
        self._archived_at: dict[str, float] = {}
        self._unsubscribers: list[Callable[[], None]] = []
        self._wire(runtime)
        self._announce_extensions(runtime)
        self.policy = self._policy()
        self.loop = self._loop()

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
            warnings=assembler.warnings,
            workspace=workspace,
            gateway_logs=gateway_logs,
        )

    # ---------------------------------------------------------------- helpers
    def _policy(self) -> PermissionPolicy:
        return PermissionPolicy(self.config.permissions, root=self.workspace)

    def _loop(self) -> AgentLoop:
        memory = None
        if self.config.memory.enabled and getattr(self.runtime, "learned_dir", None) is not None:
            memory = MemoryProvider(
                self.runtime.learned_dir,
                resident_limit=self.config.memory.resident_max_chars,
                index_limit=self.config.memory.index_max_chars,
                show_stale=self.config.memory.show_stale,
            )
        context = ContextBuilder(
            self.runtime,
            budget_chars=self.config.agent.context_budget_chars,
            memory=memory,
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
    def _pick_session(
        self, request: str, *, new_session: bool, session_id: str | None
    ) -> tuple[Session, bool]:
        if session_id:
            existing = self._sessions.get(session_id)
            if existing is not None:
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
            self.session.append_user(request)
            return self.session, False
        session = Session(
            request,
            session_id=session_id,
            skills=self.runtime.skills,
            runtime=self.runtime,
            domain=guess_domain(request),
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
        if new_session and self.session is not None:
            await self.end_session(archive=archive)
        session, fresh = self._pick_session(
            request, new_session=new_session, session_id=session_id
        )
        # 工具（如记忆写入）需要把关键动作写进运行日志；日志按会话归属
        session.logs = self.logs
        if fresh:
            await self.events.emit(EventName.SESSION_START, session=session)
            self.logs.runtime_log("INFO", "会话开始", session=session.id)
        # 运行期元数据只对本次回合可见，结束后恢复，避免污染同一会话的后续请求
        previous: dict[str, Any] = {}
        if metadata:
            for key in metadata:
                previous[key] = session.metadata.get(key, _MISSING)
            session.metadata.update(metadata)
        try:
            result = await self.loop.run(session)
        finally:
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
        return result

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
        for sid, session in list(self._sessions.items()):
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

    async def shutdown(self) -> None:
        await self.end_session()
        await self.events.emit(EventName.SHUTDOWN)

    def summary(self) -> dict[str, Any]:
        return self.runtime.summary()


__all__ = ["Kernel", "guess_domain"]
