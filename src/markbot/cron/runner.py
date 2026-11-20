"""把定时任务接回主 Agent。

到点后不另起一套循环，而是复用 ``Kernel.run``：任务绑定创建它的会话，
以一条提醒消息驱动一个普通回合，``AgentLoop`` / 工具 / 权限 / 归档全部沿用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any

from markbot.cron.types import CronJob, now_ms
from markbot.kernel.session import redact_secrets

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

    async def on_job(job: CronJob) -> str:
        prompt = PROMPT_TEMPLATE.format(
            name=job.name,
            message=job.payload.message,
        )
        session_id = job.payload.session_id or f"cron_{job.id}"
        metadata = {
            "cron": {
                "job_id": job.id,
                "job_name": job.name,
                "triggered_at_ms": now_ms(),
            }
        }

        def _run() -> str:
            # Kernel 的会话状态是单线程的，和 /chat 共用一把锁串行执行
            with run_lock:
                result = asyncio.run(
                    kernel.run(
                        prompt,
                        session_id=session_id,
                        metadata=metadata,
                    )
                )
            if result.error:
                raise RuntimeError(result.error)
            return redact_secrets(result.text or "")

        text = await asyncio.to_thread(_run)
        if job.payload.delivery == "channel" and text.strip():
            await asyncio.to_thread(_deliver, job, text)
        return text

    return on_job


def _deliver(job: CronJob, text: str) -> None:
    """把任务结果发回创建任务的聊天（渠道提供本地回调地址）。"""
    notify = job.payload.notify or {}
    url = str(notify.get("url") or "")
    chat_id = str(notify.get("chat_id") or "")
    if not url or not chat_id:
        logger.warning("任务 %s 未绑定渠道回调，结果仅归档", job.id)
        return
    body = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
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


__all__ = ["PROMPT_TEMPLATE", "build_job_runner"]
