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
from markbot.api.execution import ExecutionEnvironment
from markbot.api.tool import ToolResult
from markbot.api.ui import UI, NullUI
from markbot.kernel.compaction import Compactor, estimate_chars, spill_tool_result
from markbot.kernel.context import ContextBuilder
from markbot.kernel.daily_log import DailyLog
from markbot.kernel.events import EventBus
from markbot.kernel.execution import LocalExecution
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

_logger = logging.getLogger("markbot.loop")

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
        env: ExecutionEnvironment | None = None,
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
        self._env = env or LocalExecution(
            policy.root,
            guard=policy.guard,
            os_sandbox_mode=getattr(policy, "os_sandbox", "off"),
        )
        self._compactor = (
            Compactor(provider, budget_chars=compact_budget)
            if compact_budget and compact_budget > 0
            else None
        )

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
        result = TurnResult(session_id=session.id)

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
        except Exception as exc:  # noqa: BLE001 - 顶层必须兜住
            result = TurnResult(
                # 流式阶段已经交付给用户的文本不能因为随后报错而丢掉。
                text="".join(delivered).strip(),
                tool_calls=tool_calls_made,
                stopped_reason="error",
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
            await self._events.emit(EventName.TURN_END, session=session, result=result)
            if self._logs:
                self._logs.end_turn(session.id, result.text, error=result.error)
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
                    raise ProviderError(
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
        try:
            await self._provider.complete(
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
                return await self._provider.complete(payload)
            return await self._provider.complete(payload, on_delta=on_delta)
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
            tasks = [
                asyncio.create_task(one(index, call))
                for index, call in enumerate(calls)
            ]
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

    async def _execute_one(self, session, call, signatures: dict[tuple[str, str], int]) -> ToolResult:
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
            return ToolResult.error(f"permission denied: {decision.reason}", denied=True)

        ctx = KernelToolContext(
            session=session,
            policy=self._policy,
            secrets=self._secrets,
            ui=self._current_ui(),
            env=self._env,
        )
        if decision.requires_confirmation:
            approved = await ctx.confirm(
                f"确认执行工具 {tool.name}",
                json.dumps(call.args, ensure_ascii=False)[:800],
            )
            if not approved:
                return ToolResult.error(
                    f"permission denied: 用户未确认执行 {tool.name}。"
                    "如需授权：在 config.toml 的 [permissions].allow_exec 在聊天中发送 /allow <程序名>中按程序名放行"
                    "（无人值守的定时任务没有确认对象，必须静态放行）",
                    denied=True,
                )

        try:
            args = tool.parse_args(call.args)
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


__all__ = ["AgentLoop", "LoopConfig", "TurnResult"]
