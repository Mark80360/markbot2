"""Agent Loop：唯一负责「模型 -> 工具 -> 模型」推进的地方。"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from markbot.api.events import EventName
from markbot.api.tool import ToolResult
from markbot.api.ui import UI, NullUI
from markbot.kernel.compaction import Compactor, estimate_chars, spill_tool_result
from markbot.kernel.context import ContextBuilder
from markbot.kernel.daily_log import DailyLog
from markbot.kernel.events import EventBus
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers.base import ModelReply, Provider, describe_error
from markbot.kernel.tools import KernelToolContext, ToolRegistry

_logger = logging.getLogger("markbot.loop")


@dataclass
class LoopConfig:
    max_steps: int = 30
    tool_timeout_s: float = 60.0
    parallel_tools: bool = True
    notice_compaction: bool = True
    tool_result_max_chars: int = 16_000


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: int = 0
    stopped_reason: str = "completed"
    error: str | None = None
    session_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


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
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._context = context
        self._events = events
        self._policy = policy
        self._cfg = cfg or LoopConfig()
        self._secrets = secrets or {}
        self._ui = ui or NullUI()
        self._logs = logs
        self._files_root = Path(files_root) if files_root else None
        self._compactor = (
            Compactor(provider, budget_chars=compact_budget)
            if compact_budget and compact_budget > 0
            else None
        )

    async def run(self, session) -> TurnResult:
        await self._events.emit(EventName.TURN_START, session=session)
        self._log("INFO", "回合开始", session=session.id)
        if self._logs:
            request = session.messages[-1].content if session.messages else session.request
            self._logs.begin_turn(session.id, request)
        steps = 0
        tool_calls_made = 0
        result = TurnResult(session_id=session.id)
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
                    )
                    break
                # self._status(f"正在请求模型（第 {steps} 步）")
                reply = await self._complete(payload, session.id)
                if self._logs:
                    self._logs.exchange(session.id, payload, reply)
                await self._events.emit(
                    EventName.AFTER_PROVIDER_RESPONSE, session=session, reply=reply
                )

                if not reply.tool_calls:
                    session.append_assistant(reply.text)
                    result = TurnResult(
                        text=reply.text,
                        tool_calls=tool_calls_made,
                        stopped_reason="completed",
                        session_id=session.id,
                        usage=reply.usage,
                    )
                    break

                if reply.text.strip():
                    self._status(f"模型: {reply.text.strip()}")
                tool_calls_made += len(reply.tool_calls)
                session.append_assistant(reply.text, tool_calls=reply.tool_calls)
                results = await self._execute_tools(session, reply.tool_calls)
                results = self._spill_large_results(session, reply.tool_calls, results)
                session.append_tool_results(reply.tool_calls, results)
            else:
                result = TurnResult(
                    tool_calls=tool_calls_made,
                    stopped_reason="max_steps",
                    error=f"达到最大步数 {self._cfg.max_steps}",
                    session_id=session.id,
                )
                self._log("WARN", "max steps reached", session=session.id)
        except Exception as exc:  # noqa: BLE001 - 顶层必须兜住
            result = TurnResult(
                tool_calls=tool_calls_made,
                stopped_reason="error",
                error=describe_error(exc),
                session_id=session.id,
            )
            self._log("ERROR", "turn failed", session=session.id, error=describe_error(exc))
            _logger.exception(
                "turn failed session=%s provider=%s",
                session.id,
                getattr(self._provider, "name", "-"),
            )
        finally:
            await self._events.emit(EventName.TURN_END, session=session, result=result)
            if self._logs:
                self._logs.end_turn(session.id, result.text, error=result.error)
        return result

    def _log(self, level: str, message: str, **fields: Any) -> None:
        if self._logs:
            self._logs.runtime_log(level, message, **fields)

    def _status(self, message: str) -> None:
        status = getattr(self._ui, "status", None)
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

    async def _complete(self, payload: dict[str, Any], session_id: str) -> ModelReply:
        """单次调用；重试由 provider 按 [agent].max_retries 控制，避免层层翻倍。"""
        started = time.monotonic()
        try:
            return await self._provider.complete(payload)
        except Exception as exc:  # noqa: BLE001 - 交给顶层统一记录并结束本回合
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

    async def _execute_tools(self, session, calls) -> list[ToolResult]:
        async def one(call) -> ToolResult:
            self._log("INFO", "tool call", session=session.id, tool=call.name)
            self._status(f"调用工具 {call.name}")
            tool = self._tools.get(call.name)
            if tool is None:
                self._log("WARN", "unknown tool", session=session.id, tool=call.name)
                self._status(f"未知工具 {call.name}")
                return ToolResult.error(f"unknown tool: {call.name}")

            event = await self._events.emit(
                EventName.TOOL_CALL, session=session, call=call, tool=tool
            )
            if event.blocked:
                self._status(f"工具被拦截 {call.name}")
                return ToolResult.error(event.block_reason or "blocked by extension")

            decision = self._policy.check(tool, call.args)
            if not decision.allowed:
                self._log(
                    "WARN",
                    "permission denied",
                    session=session.id,
                    tool=call.name,
                    reason=decision.reason,
                )
                self._status(f"权限拒绝: {call.name}（{decision.reason}）")
                return ToolResult.error(f"permission denied: {decision.reason}")

            ctx = KernelToolContext(
                session=session,
                policy=self._policy,
                secrets=self._secrets,
                ui=self._ui,
            )
            if decision.requires_confirmation:
                approved = await ctx.confirm(
                    f"确认执行工具 {tool.name}",
                    json.dumps(call.args, ensure_ascii=False)[:800],
                )
                if not approved:
                    return ToolResult.error(
                        f"permission denied: 用户未确认执行 {tool.name}。"
                        "如需授权：在聊天中发送 /allow <程序名>，"
                        "或在 config.toml 的 [permissions].allow_exec 中放行"
                    )

            try:
                args = tool.params.model_validate(call.args)
            except ValidationError as exc:
                return ToolResult.error(f"invalid arguments for {tool.name}: {exc}")

            try:
                outcome = tool.handler(ctx, args)
                if inspect.isawaitable(outcome):
                    result = await asyncio.wait_for(outcome, timeout=tool.timeout_s)
                else:
                    result = outcome
                if not isinstance(result, ToolResult):
                    result = ToolResult(content=str(result))
            except asyncio.TimeoutError:
                result = ToolResult.error(f"tool timeout: {tool.name}")
            except Exception as exc:  # noqa: BLE001 - 单个工具失败不影响循环
                result = ToolResult.error(f"tool failed: {exc}")

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

        if self._cfg.parallel_tools:
            return list(await asyncio.gather(*(one(c) for c in calls)))
        return [await one(c) for c in calls]


__all__ = ["AgentLoop", "LoopConfig", "TurnResult"]
