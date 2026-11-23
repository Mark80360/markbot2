"""把定时任务接回主 Agent。

到点后不另起一套循环，而是复用 ``Kernel.run``：每次触发都在**独立的新会话**里
跑一个普通回合，``AgentLoop`` / 工具 / 权限 / 归档全部沿用；结果按任务登记的
渠道回调投递回创建它的聊天。

不复用创建任务的聊天会话：那样每次执行都会把整段聊天历史带进上下文（成本高、
会触发对用户聊天的压缩），历史里的上一次结果还会让模型偷懒；执行记录也会混进
用户的聊天里。来源会话只作为元数据记录。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from markbot.api.redaction import redact_secrets
from markbot.cron.types import CronJob, now_ms
from markbot.kernel import local_auth
from markbot.kernel.turn import session_scope

logger = logging.getLogger("markbot.cron")

PROMPT_TEMPLATE = """定时任务「{name}」现在触发了。

任务内容：
{message}

要求：
- 按任务内容执行；需要工具就直接调用。
- 这是本次独立执行：请基于当前最新信息重新给出结果，不要因为会话历史里已有类似内容就省略。
- 完成后直接把最终结果写给用户；这条回复会作为任务结果归档，并投递到任务的聊天。
- 不要在定时任务里再创建新的定时任务。
"""


def build_job_runner(
    kernel: Any,
    *,
    run_lock,
) -> Callable[[CronJob], Awaitable[str]]:
    """返回 ``CronService.on_job``：在独立线程里跑一个完整的 Agent 回合。"""
    home = getattr(kernel, "home", None)
    token = local_auth.load_or_create_token(home) if home is not None else ""

    async def on_job(job: CronJob) -> str:
        prompt = PROMPT_TEMPLATE.format(
            name=job.name,
            message=job.payload.message,
        )
        # 每次执行一个新会话：id 带任务 id，便于在 sessions/ 里按任务检索
        session_id = f"cron_{job.id}_{uuid.uuid4().hex[:8]}"
        metadata = {
            "cron": {
                "job_id": job.id,
                "job_name": job.name,
                "triggered_at_ms": now_ms(),
                "origin_session": job.payload.session_id or "",
            }
        }

        abandoned = threading.Event()
        denied: list[str] = []

        def _run() -> str:
            # 每次执行是独立会话：只占自己的会话锁，与聊天并发（RunGate）；
            # 传入普通锁时退化为整体互斥
            with session_scope(run_lock, session_id):
                if abandoned.is_set():
                    # 排队期间已经超时被放弃：不再开始执行
                    raise RuntimeError("执行前已超时取消")
                try:
                    result = asyncio.run(
                        kernel.run(
                            prompt,
                            session_id=session_id,
                            new_session=True,
                            end_previous=False,
                            metadata=metadata,
                        )
                    )
                    getter = getattr(kernel, "get_session", None)
                    session = getter(session_id) if callable(getter) else None
                    denied.extend(collect_denials(session))
                finally:
                    # 本次执行已归档，不再需要常驻内存
                    forget = getattr(kernel, "forget_session", None)
                    if callable(forget):
                        forget(session_id)
            if result.error:
                raise RuntimeError(result.error)
            return redact_secrets(result.text or "")

        try:
            text = await asyncio.to_thread(_run)
        except asyncio.CancelledError:
            # 调度器超时或停机：线程无法强杀，改为取消该会话的回合（会补齐中断结果并归档）
            abandoned.set()
            cancel = getattr(kernel, "cancel", None)
            if callable(cancel):
                cancel(session_id)
            raise
        if job.payload.delivery == "channel" and text.strip():
            await asyncio.to_thread(_deliver, job, text, token)
        notice = denial_notice(job, denied)
        if notice:
            await asyncio.to_thread(_deliver, job, notice, token)
        return text

    return on_job


# job_id -> 上次通知过的被拒原因；原因没变就不重复打扰
_NOTIFIED_DENIALS: dict[str, frozenset[str]] = {}
_DENIED_PREFIX = "permission denied:"


def collect_denials(session: Any) -> list[str]:
    """本次执行里因权限被拒的工具调用原因（去重，保持顺序）。"""
    reasons: list[str] = []
    for message in getattr(session, "messages", None) or []:
        if getattr(message, "role", "") != "tool":
            continue
        content = str(getattr(message, "content", "") or "")
        if not content.startswith(_DENIED_PREFIX):
            continue
        reason = content[len(_DENIED_PREFIX):].strip().split("。")[0][:160]
        if reason and reason not in reasons:
            reasons.append(reason)
    return reasons


def denial_notice(job: CronJob, reasons: list[str]) -> str:
    """定时任务里有操作因权限被拒时，提示用户静态放行（无人值守没有确认对象，ADR 0011）。"""
    if not reasons:
        _NOTIFIED_DENIALS.pop(job.id, None)
        return ""
    key = frozenset(reasons)
    if _NOTIFIED_DENIALS.get(job.id) == key:
        return ""
    _NOTIFIED_DENIALS[job.id] = key
    lines = "\n".join(f"- {reason}" for reason in reasons[:6])
    return (
        f"ℹ️ 定时任务「{job.name}」本次运行中有 {len(reasons)} 类操作因权限被拒（无人值守无法确认）：\n"
        f"{lines}\n"
        "如需放行，请在 config.toml 的 [permissions].allow_exec 或 [permissions.secret_env] 中登记。"
    )


def build_failure_notifier(kernel: Any) -> Callable[[CronJob, int, str], None]:
    """定时任务连续失败时通知创建它的聊天（ADR 0010）。"""
    home = getattr(kernel, "home", None)
    token = local_auth.load_or_create_token(home) if home is not None else ""

    def notify(job: CronJob, failures: int, error: str) -> None:
        text = (
            f"⚠️ 定时任务「{job.name}」已连续 {failures} 次失败。\n"
            f"最近一次错误：{redact_secrets(error)[:300]}\n"
            "可以发送消息让我排查，或在终端运行 `markbot cron history` 查看详情。"
        )
        _deliver(job, text, token)

    return notify


def _deliver(job: CronJob, text: str, token: str = "") -> None:
    """把任务结果发回创建任务的聊天（渠道提供本地回调地址）。"""
    notify = job.payload.notify or {}
    url = str(notify.get("url") or "")
    chat_id = str(notify.get("chat_id") or "")
    if not url or not chat_id:
        logger.warning("任务 %s 未绑定渠道回调，结果仅归档", job.id)
        return
    if not local_auth.is_local_url(url):
        # jobs.json 可能被手工改过：只投递到本机渠道
        logger.warning("任务 %s 的回调地址不是回环地址，拒绝投递: %s", job.id, url[:80])
        return
    body = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers[local_auth.HEADER] = token
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        # 渠道侧还要调飞书接口，给足时间，避免“消息已发出但本地判定超时”
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
    except Exception as exc:  # noqa: BLE001 - 投递失败不影响归档结果
        logger.warning("任务 %s 结果投递失败: %s", job.id, exc)
        return
    logger.info("任务 %s 结果已投递 chat=%s", job.id, chat_id)


__all__ = [
    "PROMPT_TEMPLATE",
    "build_failure_notifier",
    "build_job_runner",
    "collect_denials",
    "denial_notice",
]
