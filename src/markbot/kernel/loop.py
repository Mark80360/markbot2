"""Agent Loop：唯一负责「模型 -> 工具 -> 模型」推进的地方。"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from markbot.api.events import EventName
from markbot.api.execution import ExecutionEnvironment
from markbot.api.tool import ToolResult
from markbot.api.ui import UI, NullUI
from markbot.kernel.compaction import Compactor, estimate_chars, spill_tool_result
from markbot.kernel.context import ContextBuilder
from markbot.kernel.daily_log import DailyLog
from markbot.kernel.events import EventBus
from markbot.kernel.execution import LocalExecution
from markbot.kernel.metrics import (
    add_usage,
    classify_provider_error,
    classify_tool_error,
    normalize_usage,
)
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers.base import (
    ContentRejectedError,
    ModelReply,
    Provider,
    ProviderError,
    describe_error,
)
from markbot.kernel.session import redact_known
from markbot.kernel.tools import KernelToolContext, ToolRegistry
from markbot.kernel.turn import turn_ui


def add_turn_usage(usage: dict[str, int] | None) -> None:
    """把子代理等额外调用的 usage（归一化字段）计入当前回合（ADR 0013）。"""
    total = _TURN_USAGE.get()
    if total is None or not usage:
        return
    for key in ("tokens_in", "tokens_out", "tokens_cached"):
        total[key] = total.get(key, 0) + int(usage.get(key) or 0)

_logger = logging.getLogger("markbot.loop")
# 本回合累计 usage：AgentLoop 被并发回合共享，不能放实例属性
_TURN_USAGE: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "markbot_turn_usage", default=None
)

# 被上游内容安全策略拒绝的上下文用这条占位替换，保留消息结构（工具调用配对不破坏）。
_MODERATION_PLACEHOLDER = "[该条内容触发上游内容安全策略，已从上下文中移除]"
_PROBE_CHARS = 20_000  # 单条消息参与探测的最大字符数
_PROBE_BATCH_CHARS = 30_000  # 一次探测合并内容的上限，超出先拆分再探测
_MASK_NOTICE = (
    "\n[提示：输出中含已登记的密钥，已替换为 [REDACTED]。"
    "需要用到密钥时请交给工具在执行时注入，不要尝试还原、拆分或转写密钥。]"
)


def _mask_known_secrets(result: ToolResult) -> ToolResult:
    """工具结果写回会话（进而进入模型上下文）之前，按已登记密钥值遮蔽。

    例如 ``cat ~/.markbot/config.toml`` 的输出：不遮蔽的话密钥会进入上下文、
    交互日志和 provider 侧，违反「密钥永不进入模型上下文」。
    """
    masked = redact_known(result.content)
    if masked == result.content:
        return result
    details = dict(result.details)
    details["redacted"] = True
    return ToolResult(content=masked + _MASK_NOTICE, is_error=result.is_error, details=details)


async def run_sync_handler(handler: Any, ctx: Any, args: Any, *, name: str = "tool") -> Any:
    """在守护线程里执行同步工具 handler，结果回到当前事件循环。

    不用 ``asyncio.to_thread``：默认线程池在 ``asyncio.run`` 结束时会等待所有线程退出，
    超时的工具会把整个回合拖到它自己结束为止。守护线程超时后被放弃（Python 线程无法强杀，
    handler 应自行尽快返回），不阻塞回合结束。contextvars（回合 UI、授权）会复制进线程。
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    context = contextvars.copy_context()

    def deliver(setter: Any, value: Any) -> None:
        if not future.done():
            setter(value)

    def target() -> None:
        try:
            value = context.run(handler, ctx, args)
        except BaseException as exc:  # noqa: BLE001 - 原样交回事件循环
            setter, value = future.set_exception, exc
        else:
            setter = future.set_result
        try:
            loop.call_soon_threadsafe(deliver, setter, value)
        except RuntimeError:
            pass  # 事件循环已关闭（超时后回合已结束）：结果丢弃

    threading.Thread(target=target, daemon=True, name=f"tool-{name}").start()
    outcome = await future
    if inspect.isawaitable(outcome):
        outcome = await outcome
    return outcome


CONFIRM_PREVIEW_CHARS = 800
_COMMAND_ARGS = ("command", "cmd", "script", "code")


def confirm_preview(args: Any, limit: int = CONFIRM_PREVIEW_CHARS) -> str:
    """确认弹窗里展示的参数。

    命令类参数（command/cmd/script/code）完整展示：用户批准的必须是将要执行的全部内容，
    只看开头可能漏掉结尾拼接的危险操作。其余参数超长时保留首尾并标注总长度。
    """
    if isinstance(args, dict):
        commands = [
            f"{key}:\n{value}"
            for key, value in args.items()
            if key in _COMMAND_ARGS and isinstance(value, str)
        ]
        rest = {key: value for key, value in args.items() if key not in _COMMAND_ARGS}
    else:
        commands, rest = [], args
    text = json.dumps(rest, ensure_ascii=False) if rest else ""
    if len(text) > limit:
        head = limit * 2 // 3
        tail = limit - head
        text = f"{text[:head]}\n…（中间省略，共 {len(text)} 字符）…\n{text[-tail:]}"
    parts = [*commands, text] if text else commands
    return "\n\n".join(parts) if parts else "{}"


@dataclass
class LoopConfig:
    max_steps: int = 30
    tool_timeout_s: float = 60.0
    parallel_tools: bool = True
    notice_compaction: bool = True
    tool_result_max_chars: int = 16_000
    moderation_recovery: bool = True
    moderation_max_probes: int = 24
    # 防跑飞护栏：重复调用、总调用数、连续权限拒绝。
    repeat_tool_limit: int = 3
    max_tool_calls: int = 100
    permission_denial_limit: int = 3
    # 改了代码却没验证就要结束时，提醒模型验证一次（ADR 0012）
    verify_reminder: bool = True


VERIFY_REMINDER = (
    "[系统提醒] 你在本轮修改了代码（{files}），但还没有运行测试或检查。"
    "请先用 run_tests / run_checks 验证改动并修复发现的问题；"
    "如果确实无法或不需要验证，直接说明原因后结束。"
)


_NON_CODE = frozenset(
    {".md", ".markdown", ".txt", ".rst", ".log", ".csv", ".json", ".yaml", ".yml", ".toml",
     ".ini", ".cfg", ".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".pdf"}
)


def _within(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _needs_verification(session: Any, since: float | None = None, root: Path | None = None) -> bool:
    """改了代码却没验证。``since`` / ``root`` 给出时只看本回合、工作区内的代码改动。

    旧行为（会话级 code_changed_at）会让后续每个回合都被提醒一次，哪怕这个回合什么都没改；
    改的若是工作区外的临时脚本（<home>/workspace、会话临时目录），也不该要求跑项目测试。
    """
    changed = getattr(session, "code_changed_at", None)
    if not changed:
        return False
    verified = getattr(session, "verified_at", None) or 0
    if since is None and root is None:
        return verified < changed
    changes = getattr(session, "file_changes", None) or {}
    if not changes:
        return verified < changed and (since is None or changed >= since)
    for path, entry in changes.items():
        at = float(entry.get("at") or 0)
        if since is not None and at < since:
            continue
        if Path(path).suffix.lower() in _NON_CODE:
            continue
        if root is not None and not _within(path, root):
            continue
        if verified < at:
            return True
    return False


def _changed_files(session: Any, limit: int = 5) -> str:
    names = [Path(path).name for path in (getattr(session, "file_changes", None) or {})]
    shown = "、".join(names[:limit])
    return shown + (f" 等 {len(names)} 个文件" if len(names) > limit else "")


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: int = 0
    stopped_reason: str = "completed"
    error: str | None = None
    session_id: str | None = None
    # 最后一步的原始 usage（兼容旧字段）；total_usage 是本回合所有模型调用的累计（归一化字段）
    usage: dict[str, int] = field(default_factory=dict)
    total_usage: dict[str, int] = field(default_factory=dict)


class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        context: ContextBuilder,
        events: EventBus,
        policy: PermissionPolicy,
        cfg: LoopConfig | None = None,
        *,
        secrets: dict[str, str] | None = None,
        ui: UI | None = None,
        compact_budget: int | None = None,
        logs: DailyLog | None = None,
        files_root: Path | None = None,
        env: ExecutionEnvironment | None = None,
        compact_provider: Provider | None = None,
        scope: Any = None,
        subagent: Any = None,
        token_budget: int = 0,
        vision: Any = None,
    ) -> None:
        self._provider = provider
        # 视觉描述器：Kernel 注入，经 ToolContext.describe_image 暴露给 view_image（ADR 0015）
        self._vision = vision
        # 回合级的 (policy, env)：绑定项目时工作区不同（ADR 0013）；None = 固定用构造参数
        self._scope = scope
        # 子代理执行器：Kernel 注入，经 ToolContext.run_subagent 暴露给 agents 扩展
        self._subagent = subagent
        # token 预算：>0 时用 provider 返回的 usage 校准「每 token 多少字符」，换算压缩阈值
        self._token_budget = max(0, int(token_budget or 0))
        self._chars_per_token = 3.0
        self._tools = tools
        self._context = context
        self._events = events
        self._policy = policy
        self._cfg = cfg or LoopConfig()
        self._secrets = secrets or {}
        self._ui = ui or NullUI()
        self._logs = logs
        self._files_root = Path(files_root) if files_root else None
        self._env = env or LocalExecution(
            policy.root,
            guard=policy.guard,
            os_sandbox_mode=getattr(policy, "os_sandbox", "off"),
        )
        self._compactor = (
            # 压缩摘要可以交给更便宜的 fast 角色模型（ADR 0011）
            Compactor(compact_provider or provider, budget_chars=compact_budget)
            if compact_budget and compact_budget > 0
            else None
        )

    def _scope_root(self) -> Path | None:
        """本回合工作区（绑定项目时是项目目录）。"""
        try:
            policy = self._scope()[0] if self._scope is not None else self._policy
        except Exception:  # noqa: BLE001
            policy = self._policy
        root = getattr(policy, "root", None)
        return Path(root) if root is not None else None

    def _current_ui(self) -> UI:
        """本回合的 UI（TurnContext）优先；没有时用构造时注入的默认 UI。"""
        return turn_ui(self._ui)

    async def run(self, session, on_delta: Any = None) -> TurnResult:
        await self._events.emit(EventName.TURN_START, session=session)
        self._log("INFO", "回合开始", session=session.id)
        if self._logs:
            request = session.messages[-1].content if session.messages else session.request
            self._logs.begin_turn(session.id, request)
        steps = 0
        tool_calls_made = 0
        notes: list[str] = []
        delivered: list[str] = []
        last_usage: dict[str, int] = {}
        call_signatures: dict[tuple[str, str], int] = {}
        denial_streak = 0
        reminded = False
        result = TurnResult(session_id=session.id)
        started = time.monotonic()
        turn_started_at = time.time()
        # 本回合所有模型调用的累计 usage；_complete 每次调用都往里加
        session_usage = {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0}
        # 子代理在同一个任务里嵌套运行：结束后必须恢复外层回合的累计
        usage_token = _TURN_USAGE.set(session_usage)

        def deliver(chunk: str) -> None:
            """记录已交付给调用方的流式文本；异常时用它兜底 TurnResult.text。"""
            if chunk:
                delivered.append(chunk)
            if on_delta is not None:
                on_delta(chunk)

        try:
            while steps < self._cfg.max_steps:
                steps += 1
                session.steps = steps
                payload = await self._context.build(session, self._tools.schemas())
                payload = await self._maybe_compact(session, payload)
                self._observe_context(session, payload)
                outcome = await self._events.emit(
                    EventName.BEFORE_PROVIDER_REQUEST, session=session, payload=payload
                )
                if outcome.blocked:
                    result = TurnResult(
                        stopped_reason="blocked",
                        error=outcome.block_reason or "blocked before provider request",
                        session_id=session.id,
                        tool_calls=tool_calls_made,
                        usage=last_usage,
                    )
                    break
                # self._status(f"正在请求模型（第 {steps} 步）")
                reply, payload = await self._complete_with_recovery(
                    session,
                    payload,
                    notes,
                    # 每一步都流式：工具回合之后的最终答复才是用户最关心的部分。旧实现只流式
                    # 第 1 步，带工具的回合里用户只能看到「我先查一下」，答复最后一次性出现。
                    # 渠道在回合结束时会用最终答复定稿（飞书卡片 finish），中间文本只是过程展示。
                    self._step_stream(deliver, delivered) if on_delta is not None else None,
                )
                last_usage = dict(reply.usage or {})
                if self._logs:
                    self._logs.exchange(session.id, payload, reply)
                await self._events.emit(
                    EventName.AFTER_PROVIDER_RESPONSE, session=session, reply=reply
                )

                if (
                    not reply.tool_calls
                    and self._cfg.verify_reminder
                    and not reminded
                    and steps < self._cfg.max_steps - 1
                    and _needs_verification(
                        session, since=turn_started_at, root=self._scope_root()
                    )
                ):
                    # 不结束回合：把提醒作为一条用户消息写回会话，让模型先验证（只提醒一次）
                    reminded = True
                    session.append_assistant(reply.text)
                    session.append_user(VERIFY_REMINDER.format(files=_changed_files(session)))
                    self._log("INFO", "验证提醒", session=session.id)
                    self._status("提醒：改动尚未验证，继续运行测试/检查")
                    continue
                if not reply.tool_calls:
                    session.append_assistant(reply.text)
                    text = reply.text.strip()
                    if notes:
                        text = (text + "\n\n" + "\n".join(notes)).strip()
                    result = TurnResult(
                        text=text,
                        tool_calls=tool_calls_made,
                        stopped_reason="completed",
                        session_id=session.id,
                        usage=last_usage,
                    )
                    break

                if reply.text.strip():
                    self._status(f"模型: {reply.text.strip()}")
                if tool_calls_made + len(reply.tool_calls) > self._cfg.max_tool_calls:
                    tool_calls_made += len(reply.tool_calls)
                    session.append_assistant(reply.text, tool_calls=reply.tool_calls)
                    # 即使拒绝执行也要补齐 tool result，保持调用/结果配对。
                    guarded = [
                        ToolResult.error("guard: 工具调用总数超过上限，已跳过", guard=True)
                        for _ in reply.tool_calls
                    ]
                    session.append_tool_results(reply.tool_calls, guarded)
                    result = TurnResult(
                        tool_calls=tool_calls_made,
                        stopped_reason="guarded",
                        error=f"工具调用总数超过上限 {self._cfg.max_tool_calls}",
                        session_id=session.id,
                        usage=last_usage,
                    )
                    self._log("WARN", "tool call budget exhausted", session=session.id)
                    break

                tool_calls_made += len(reply.tool_calls)
                session.append_assistant(reply.text, tool_calls=reply.tool_calls)
                slots: list[ToolResult | None] = [None] * len(reply.tool_calls)
                try:
                    await self._execute_tools(
                        session, reply.tool_calls, slots, call_signatures
                    )
                except BaseException:
                    # 回合被取消/中断也必须补齐结果，否则会留下无配对的 tool_call。
                    session.append_tool_results(
                        reply.tool_calls, self._finalize_tool_slots(slots)
                    )
                    raise
                results = self._finalize_tool_slots(slots)
                results = self._spill_large_results(session, reply.tool_calls, results)
                session.append_tool_results(reply.tool_calls, results)
                denial_streak = self._update_denials(denial_streak, results)
                if denial_streak >= self._cfg.permission_denial_limit:
                    result = TurnResult(
                        tool_calls=tool_calls_made,
                        stopped_reason="guarded",
                        error=f"连续 {denial_streak} 次工具调用被拒绝，已停止",
                        session_id=session.id,
                        usage=last_usage,
                    )
                    self._log(
                        "WARN",
                        "连续权限拒绝达到上限",
                        session=session.id,
                        streak=denial_streak,
                    )
                    break
            else:
                result = TurnResult(
                    tool_calls=tool_calls_made,
                    stopped_reason="max_steps",
                    error=f"达到最大步数 {self._cfg.max_steps}",
                    session_id=session.id,
                    usage=last_usage,
                )
                self._log("WARN", "max steps reached", session=session.id)
        except asyncio.CancelledError:
            # 只吞掉 Kernel.cancel() 发起的取消；进程退出、Ctrl-C 等外部取消照常向上抛
            if not getattr(session, "cancel_requested", False):
                raise
            session.cancel_requested = False
            task = asyncio.current_task()
            if task is not None and hasattr(task, "uncancel"):
                task.uncancel()
            result = TurnResult(
                text="".join(delivered).strip(),
                tool_calls=tool_calls_made,
                stopped_reason="cancelled",
                error="已被用户取消",
                session_id=session.id,
                usage=last_usage,
            )
            self._log("WARN", "回合被取消", session=session.id, steps=steps)
        except Exception as exc:  # noqa: BLE001 - 顶层必须兜住
            result = TurnResult(
                # 流式阶段已经交付给用户的文本不能因为随后报错而丢掉。
                text="".join(delivered).strip(),
                tool_calls=tool_calls_made,
                # 内容审核拒绝且恢复失败：单独标出，Kernel 会换新会话避免该会话永久不可用
                stopped_reason="moderation" if isinstance(exc, ContentRejectedError) else "error",
                error=describe_error(exc),
                session_id=session.id,
                usage=last_usage,
            )
            self._log("ERROR", "turn failed", session=session.id, error=describe_error(exc))
            _logger.exception(
                "turn failed session=%s provider=%s",
                session.id,
                getattr(self._provider, "name", "-"),
            )
        finally:
            _TURN_USAGE.reset(usage_token)
            result.total_usage = dict(session_usage)
            await self._events.emit(EventName.TURN_END, session=session, result=result)
            if self._logs:
                self._logs.end_turn(session.id, result.text, error=result.error)
                cron = (getattr(session, "metadata", None) or {}).get("cron") or {}
                self._logs.event(
                    "turn",
                    session=session.id,
                    turn_kind="cron" if cron else ("chat" if session.persists_events else "local"),
                    job=cron.get("job_id") if cron else None,
                    stopped_reason=result.stopped_reason,
                    steps=steps,
                    tool_calls=tool_calls_made,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_class=(
                        result.stopped_reason if result.stopped_reason != "completed" else None
                    ),
                    **session_usage,
                )
        return result

    @staticmethod
    def _step_stream(deliver: Any, delivered: list[str]) -> Any:
        """本步的流式回调：前面已有输出时，本步第一段文本前补一个空行分隔。"""
        pending_separator = [bool("".join(delivered).strip())]

        def on_step_delta(chunk: str) -> None:
            if pending_separator[0] and chunk:
                pending_separator[0] = False
                deliver("\n\n")
            deliver(chunk)

        return on_step_delta

    @staticmethod
    def _finalize_tool_slots(slots: list[ToolResult | None]) -> list[ToolResult]:
        return [
            result
            if result is not None
            else ToolResult.error("回合被中断，工具未完成", interrupted=True)
            for result in slots
        ]

    @staticmethod
    def _update_denials(streak: int, results: list[ToolResult]) -> int:
        for result in results:
            streak = streak + 1 if result.details.get("denied") else 0
        return streak

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self._logs:
            self._logs.runtime_log(level, message, **fields)

    def _status(self, message: str) -> None:
        status = getattr(self._current_ui(), "status", None)
        if callable(status):
            status(message)

    async def _maybe_compact(
        self, session: Any, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self._compactor is None or not self._compactor.should_compact(payload):
            return payload
        outcome = await self._events.emit(
            EventName.BEFORE_COMPACTION,
            session=session,
            chars=estimate_chars(payload),
            messages=len(session.messages),
        )
        if outcome.blocked:
            return payload
        compacted = await self._compactor.compact(session)
        if compacted is None:
            return payload
        self._log(
            "INFO",
            "context compacted",
            session=session.id,
            removed=compacted.removed,
            kept=compacted.kept,
            summary_chars=compacted.summary_chars,
        )
        if self._cfg.notice_compaction:
            self._status(
                f"上下文已压缩：移除 {compacted.removed} 条，保留 {compacted.kept} 条"
            )
        return await self._context.build(session, self._tools.schemas())

    def _observe_context(self, session: Any, payload: dict[str, Any]) -> None:
        """上下文用量观测：只记字符数与比例，不记正文。"""
        chars = estimate_chars(payload)
        budget = getattr(self._context, "budget_chars", 0) or 0
        _logger.debug("context usage session=%s chars=%d budget=%d", session.id, chars, budget)
        if budget and chars > budget * 0.95:
            self._log(
                "WARN", "上下文超预算，强制压缩", session=session.id, chars=chars, budget=budget
            )
        elif budget and chars > budget * 0.8:
            self._log(
                "WARN", "上下文接近预算", session=session.id, chars=chars, budget=budget
            )

    def _spill_large_results(self, session, calls, results: list[ToolResult]) -> list[ToolResult]:
        """超限工具结果落盘；``read_file`` 已是分页读取，保持原样。"""
        if self._files_root is None:
            return results
        files_dir = self._files_root / session.id / "files"
        out: list[ToolResult] = []
        for call, result in zip(calls, results):
            cap = self._cfg.tool_result_max_chars
            if call.name == "read_file" or result.is_error or len(result.content) <= cap:
                out.append(result)
                continue
            try:
                preview, path = spill_tool_result(result.content, files_dir, cap=cap)
            except OSError as exc:
                self._log(
                    "WARN", "工具结果落盘失败", session=session.id, tool=call.name, error=str(exc)
                )
                out.append(result)
                continue
            self._log(
                "INFO",
                "工具结果落盘",
                session=session.id,
                tool=call.name,
                chars=len(result.content),
                path=str(path),
            )
            details = dict(result.details)
            details["spilled"] = str(path)
            out.append(ToolResult(content=preview, details=details))
        return out

    async def _complete_with_recovery(
        self,
        session: Any,
        payload: dict[str, Any],
        notes: list[str],
        on_delta: Any = None,
    ) -> tuple[ModelReply, dict[str, Any]]:
        """模型调用；被上游内容安全策略拒绝时，剔除触发内容后重发。

        内容审核拒绝是确定性的：同一段历史每次都会命中，单靠重试无法恢复，
        而且会让该会话永久不可用。这里按“探测定位 -> 占位替换 -> 重发”降级，
        最多两轮，避免漏掉的触发源需要再次人工干预。
        """
        rounds = 2 if self._cfg.moderation_recovery else 1
        for round_no in range(1, rounds + 1):
            try:
                return await self._complete(payload, session.id, on_delta), payload
            except ContentRejectedError as exc:
                if round_no >= rounds:
                    raise
                self._log(
                    "WARN",
                    "上游内容审核拒绝，尝试定位并移除触发内容",
                    session=session.id,
                    error=describe_error(exc)[:200],
                )
                removed = await self._remove_rejected_messages(session)
                if not removed:
                    # 仍归为审核拒绝：Kernel 据此换新会话，否则这条触发内容留在历史里，
                    # 该会话之后的每一轮都会被拒（ADR 0011）
                    raise ContentRejectedError(
                        "上游内容安全策略拒绝了请求，且未在历史消息中定位到触发内容；"
                        "可能是本轮输入本身被拦截，请修改后重试。原错误：" + describe_error(exc)
                    ) from exc
                self._log(
                    "WARN", "已移除触发内容审核的上下文", session=session.id, removed=removed
                )
                notes.append(
                    f"（提示：有 {len(removed)} 条上下文触发上游内容安全策略，已移除后重试，"
                    "相关细节可能不完整；需要时可重新提供。）"
                )
                payload = await self._context.build(session, self._tools.schemas())
        raise ProviderError("模型调用失败")

    async def _remove_rejected_messages(self, session: Any) -> list[dict[str, Any]]:
        """定位并占位替换触发审核的消息（当前这条用户输入不参与，避免吞掉请求）。"""
        last = len(session.messages) - 1
        candidates = [
            index
            for index, msg in enumerate(session.messages)
            if not (index == last and msg.role == "user")
            and (
                (msg.content.strip() and msg.content != _MODERATION_PLACEHOLDER)
                or any(call.arguments for call in msg.tool_calls)
            )
        ]
        if not candidates:
            return []
        flagged = await self._find_flagged(session, candidates)
        if not flagged:
            return []
        # 经事件落盘：直接改 messages 的话，重启后从 events.jsonl 恢复，触发内容会回来，
        # 该会话会再次被拒绝。工具调用参数一并清空，调用结构保留以维持配对。
        return session.redact_messages(flagged, _MODERATION_PLACEHOLDER, reason="moderation")

    async def _find_flagged(self, session: Any, candidates: list[int]) -> list[int]:
        """按整批探测 + 二分拆分定位被拒消息；探测调用有上限，避免拖垮回合。"""
        flagged: list[int] = []
        queue: list[list[int]] = [candidates]
        probes = 0
        while queue and probes < self._cfg.moderation_max_probes:
            group = queue.pop(0)
            text = self._probe_text(session, group)
            if len(group) > 1 and len(text) > _PROBE_BATCH_CHARS:
                mid = len(group) // 2
                queue[0:0] = [group[:mid], group[mid:]]
                continue
            probes += 1
            if await self._probe(text):
                continue
            if len(group) == 1:
                flagged.append(group[0])
                continue
            mid = len(group) // 2
            queue[0:0] = [group[:mid], group[mid:]]
        if queue:
            self._log(
                "WARN",
                "内容审核探测达到上限，可能有未定位的触发内容",
                session=session.id,
                remaining=len(queue),
            )
        return flagged

    def _probe_text(self, session: Any, indexes: list[int]) -> str:
        parts: list[str] = []
        for index in indexes:
            msg = session.messages[index]
            parts.append(msg.content[:_PROBE_CHARS])
            for call in msg.tool_calls:
                parts.append(json.dumps(call.arguments, ensure_ascii=False)[:_PROBE_CHARS])
        return "\n\n".join(parts)

    async def _probe(self, text: str) -> bool:
        """返回 True 表示该内容能被上游接受；探测本身出错时不误删。"""
        # 主备切换时只用主模型探测：备用模型接受不代表主模型接受
        provider = getattr(self._provider, "primary", None) or self._provider
        try:
            await provider.complete(
                {"messages": [{"role": "user", "content": text}], "max_tokens": 8}
            )
            return True
        except ContentRejectedError:
            return False
        except Exception as exc:  # noqa: BLE001 - 探测失败只记日志，不据此删内容
            self._log("WARN", "内容审核探测失败", error=describe_error(exc)[:200])
            return True

    async def _complete(
        self, payload: dict[str, Any], session_id: str, on_delta: Any = None
    ) -> ModelReply:
        """单次调用；重试由 provider 按 [agent].max_retries 控制，避免层层翻倍。"""
        started = time.monotonic()
        try:
            if on_delta is None:
                reply = await self._provider.complete(payload)
            else:
                reply = await self._provider.complete(payload, on_delta=on_delta)
        except Exception as exc:  # noqa: BLE001 - 交给顶层统一记录并结束本回合
            self._provider_event(started, None, exc)
            error = describe_error(exc)
            self._log(
                "ERROR",
                "provider 调用失败（重试已耗尽）",
                provider=getattr(self._provider, "name", "-"),
                error=error,
                elapsed_s=round(time.monotonic() - started, 1),
            )
            if self._logs:
                # 失败的请求也要留档，否则交互日志里只剩一个空回合
                self._logs.exchange(session_id, payload, None, error=error)
            raise
        turn_usage = _TURN_USAGE.get()
        if turn_usage is not None:
            add_usage(turn_usage, reply.usage)
        self._calibrate(payload, reply)
        self._provider_event(started, reply, None)
        return reply

    @property
    def chars_per_token(self) -> float:
        return self._chars_per_token

    def _calibrate(self, payload: dict[str, Any], reply: Any) -> None:
        """用真实的输入 token 数校准字符/token 比例，并据此调整压缩阈值（ADR 0013）。"""
        if not self._token_budget:
            return
        tokens = normalize_usage(getattr(reply, "usage", None))["tokens_in"]
        chars = estimate_chars(payload)
        if tokens < 200 or chars < 500:
            return
        ratio = max(1.0, min(6.0, chars / tokens))
        self._chars_per_token = 0.7 * self._chars_per_token + 0.3 * ratio
        budget = int(self._token_budget * self._chars_per_token)
        if self._compactor is not None and abs(budget - self._compactor.budget_chars) > 1000:
            self._compactor.resize(budget)
            if hasattr(self._context, "budget_chars"):
                self._context.budget_chars = budget

    def _scoped(self) -> tuple[Any, Any]:
        if self._scope is not None:
            try:
                return self._scope()
            except Exception:  # noqa: BLE001 - 解析失败退回默认工作区
                _logger.exception("回合工作区解析失败，使用默认工作区")
        return self._policy, self._env

    def _provider_event(self, started: float, reply: Any, exc: BaseException | None) -> None:
        if not self._logs:
            return
        usage = normalize_usage(getattr(reply, "usage", None) if reply is not None else None)
        self._logs.event(
            "provider",
            model=str(getattr(self._provider, "last_used", "") or getattr(self._provider, "name", "-")),
            ok=exc is None,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_class=classify_provider_error(exc) or None,
            **usage,
        )

    async def _execute_tools(
        self,
        session,
        calls,
        slots: list[ToolResult | None],
        signatures: dict[tuple[str, str], int],
    ) -> None:
        """并发/顺序执行工具，结果写入定长 ``slots``。

        写入而不是直接返回，是为了在取消/异常时仍能拿到已完成部分，
        由调用方补齐剩余结果，保证 tool_call 与 tool_result 一一配对。
        """

        async def one(index: int, call) -> None:
            slots[index] = await self._execute_one(session, call, signatures)

        if self._cfg.parallel_tools:
            # 只读/无副作用的调用并发执行；写文件、执行命令等串行（ADR 0012）
            serial = [
                index for index, call in enumerate(calls) if not self._concurrency_safe(call)
            ]
            tasks = [
                asyncio.create_task(one(index, call))
                for index, call in enumerate(calls)
                if index not in serial
            ]

            async def run_serial() -> None:
                for index in serial:
                    await one(index, calls[index])

            if serial:
                tasks.append(asyncio.create_task(run_serial()))
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        else:
            for index, call in enumerate(calls):
                slots[index] = await self._execute_one(session, call, signatures)

    def _concurrency_safe(self, call: Any) -> bool:
        tool = self._tools.get(call.name)
        check = getattr(tool, "is_concurrency_safe", None)
        return bool(check()) if callable(check) else True

    async def _execute_one(self, session, call, signatures: dict[tuple[str, str], int]) -> ToolResult:
        started = time.monotonic()
        result = await self._execute_one_inner(session, call, signatures)
        if self._logs:
            self._logs.event(
                "tool",
                session=session.id,
                tool=call.name,
                ok=not result.is_error,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_class=classify_tool_error(result) if result.is_error else None,
            )
        return result

    async def _execute_one_inner(
        self, session, call, signatures: dict[tuple[str, str], int]
    ) -> ToolResult:
        try:
            # 供 /status 查询「正在调用哪个工具」
            session.current_tool = call.name
        except AttributeError:
            pass
        self._log("INFO", "tool call", session=session.id, tool=call.name)
        self._status(f"调用工具 {call.name}")

        signature = (call.name, json.dumps(call.args or {}, ensure_ascii=False, sort_keys=True))
        seen = signatures.get(signature, 0) + 1
        signatures[signature] = seen
        if seen > self._cfg.repeat_tool_limit:
            self._log(
                "WARN", "重复工具调用被拦截", session=session.id, tool=call.name, count=seen
            )
            self._status(f"重复工具调用 {call.name}，已跳过")
            return ToolResult.error(
                f"guard: 相同工具调用已重复 {seen} 次，已跳过；请更换参数或结束任务",
                guard=True,
            )

        tool = self._tools.get(call.name)
        if tool is None:
            self._log("WARN", "unknown tool", session=session.id, tool=call.name)
            self._status(f"未知工具 {call.name}")
            return ToolResult.error(f"unknown tool: {call.name}")

        parse_error = getattr(call, "parse_error", None)
        if parse_error:
            # 参数 JSON 解析失败：绝不能当成 {} 继续——默认值会被执行，权限检查也因为
            # 看不到路径/命令而直接放行。在任何事件、权限、确认之前就返回错误。
            self._log(
                "WARN", "tool arguments unparseable", session=session.id, tool=call.name
            )
            preview = redact_known(str(getattr(call, "raw_arguments", "") or ""))[:300]
            return ToolResult.error(
                f"invalid arguments for {call.name}: {parse_error}。"
                f"原始参数开头：{preview!r}。请重新生成完整、合法的 JSON 对象参数后再调用。",
                parse_error=True,
            )

        event = await self._events.emit(
            EventName.TOOL_CALL, session=session, call=call, tool=tool
        )
        if event.blocked:
            self._status(f"工具被拦截 {call.name}")
            return ToolResult.error(event.block_reason or "blocked by extension", denied=True)

        policy, env = self._scoped()
        decision = policy.check(tool, call.args)
        if not decision.allowed:
            self._log(
                "WARN",
                "permission denied",
                session=session.id,
                tool=call.name,
                reason=decision.reason,
            )
            self._status(f"权限拒绝: {call.name}（{decision.reason}）")
            return ToolResult.error(f"permission denied: {decision.reason}", denied=True)

        ctx = KernelToolContext(
            session=session,
            policy=policy,
            secrets=self._secrets,
            ui=self._current_ui(),
            env=env,
            subagent=self._subagent,
            vision=self._vision,
        )
        ctx.main_vision = bool(getattr(self._context, "vision", False))
        if decision.requires_confirmation:
            approved = await ctx.confirm(
                f"确认执行工具 {tool.name}",
                confirm_preview(call.args),
            )
            if not approved:
                return ToolResult.error(
                    f"permission denied: 用户未确认执行 {tool.name}。"
                    "如需免确认：在 config.toml 的 [permissions].allow_exec 中添加程序名，"
                    "或在聊天中发送 /allow <程序名>（无人值守的定时任务没有确认对象，必须静态放行）",
                    denied=True,
                )

        try:
            args = tool.parse_args(call.args)
        except ValidationError as exc:
            return ToolResult.error(f"invalid arguments for {tool.name}: {exc}")

        try:
            if inspect.iscoroutinefunction(tool.handler):
                result = await asyncio.wait_for(tool.handler(ctx, args), timeout=tool.timeout_s)
            else:
                # 同步 handler 放进独立线程：直接调用会阻塞事件循环，超时也无从生效
                result = await asyncio.wait_for(
                    run_sync_handler(tool.handler, ctx, args, name=tool.name),
                    timeout=tool.timeout_s,
                )
            if not isinstance(result, ToolResult):
                result = ToolResult(content=str(result))
        except asyncio.TimeoutError:
            result = ToolResult.error(f"tool timeout: {tool.name}")
        except Exception as exc:  # noqa: BLE001 - 单个工具失败不影响循环
            result = ToolResult.error(f"tool failed: {exc}")
        result = _mask_known_secrets(result)

        await self._events.emit(
            EventName.TOOL_RESULT, session=session, call=call, result=result
        )
        if result.is_error:
            self._log(
                "WARN",
                "tool failed",
                session=session.id,
                tool=call.name,
                error=result.content[:500],
            )
            self._status(f"工具失败 {call.name}: {result.content[:160]}")
        else:
            self._status(f"工具完成 {call.name}")
        return result


__all__ = [
    "AgentLoop",
    "LoopConfig",
    "TurnResult",
    "add_turn_usage",
    "confirm_preview",
    "run_sync_handler",
]
