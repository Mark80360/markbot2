"""按日期落盘的运行日志与模型交互日志。

目录：
- ``~/.markbot/logs/runtime/YYYY-MM-DD.md``      运行事件、提示、警告、错误
- ``~/.markbot/logs/interaction/YYYY-MM-DD.md``  用户输入、模型请求/回复、最终输出
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from markbot.kernel import gateway_log
from markbot.kernel.session import redact_secrets


def _fence_for(body: str) -> str:
    """围栏长度要超过正文里最长的一段反引号，否则内容里的 ```` ``` ```` 会提前闭合外层块。"""
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    return "`" * max(3, longest + 1)


def _text_block(title: str, body: str, language: str = "text") -> str:
    fence = _fence_for(body)
    return f"### {title}\n\n{fence}{language}\n{body}\n{fence}\n\n"


class DailyLog:
    def __init__(
        self,
        home: Path,
        *,
        enabled: bool = True,
        runtime: bool = True,
        interaction: bool = True,
        mirror_gateway: bool = False,
    ) -> None:
        self.home = Path(home)
        self.enabled = enabled
        self.runtime_enabled = runtime
        self.interaction_enabled = interaction
        self.mirror_gateway = mirror_gateway
        self._lock = threading.Lock()
        self._turns: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------- runtime
    def runtime_log(self, level: str, message: str, **fields: Any) -> None:
        if not (self.enabled and self.runtime_enabled):
            return
        line = f"- `{self._clock()}` **{level.upper()}** {message}"
        if fields:
            detail = json.dumps(fields, ensure_ascii=False)
            line += f" — `{detail}`"
        self._append("runtime", line + "\n")
        if self.mirror_gateway:
            mirror = f"[{threading.current_thread().name}] [{level.upper()}] {message}"
            if fields:
                mirror += f" — {json.dumps(fields, ensure_ascii=False)}"
            gateway_log.write(self.home, redact_secrets(mirror))

    # --------------------------------------------------------- interaction
    def begin_turn(self, session_id: str, request: str) -> None:
        if not self._interaction_on():
            return
        self._turns[session_id] = {"index": 0, "sent": 0, "reply": None}
        block = f"## {self._clock()} · {session_id}\n\n"
        block += _text_block("用户输入", request)
        self._append("interaction", block)

    def exchange(
        self,
        session_id: str,
        payload: dict[str, Any],
        reply: Any,
        *,
        error: str | None = None,
    ) -> None:
        """记录一次模型交互；``reply`` 为 ``None`` 表示这次请求失败。

        第一步记录完整 payload，后续步骤只记录新增消息：每步都重写全量历史会让
        日志按平方级膨胀，而增量串起来同样能还原完整请求。
        """
        if not self._interaction_on():
            return
        state = self._turns.setdefault(
            session_id, {"index": 0, "sent": 0, "reply": None}
        )
        state["index"] += 1
        index = state["index"]

        messages = list(payload.get("messages") or [])
        # 历史变短说明上下文被压缩过，增量已无法还原，改记完整 payload
        compacted = index > 1 and len(messages) < state["sent"]
        if index == 1:
            view: dict[str, Any] = payload
        elif compacted:
            view = {**payload, "note": "上下文已压缩，此处记录完整 payload"}
        else:
            view = {
                "messages": messages[state["sent"] :],
                "note": f"system/tools 同请求 #{index - 1}，前 {state['sent']} 条消息已在上文",
            }
        state["sent"] = len(messages)

        title = f"模型请求 #{index}" if reply is not None else f"模型请求 #{index}（失败）"
        block = _text_block(title, json.dumps(view, ensure_ascii=False, indent=2), "json")

        if reply is None:
            block += _text_block("错误", error or "请求失败（未提供错误信息）")
        else:
            text = getattr(reply, "text", "") or ""
            state["reply"] = text
            block += _text_block(f"模型回复 #{index}", text)
            calls = getattr(reply, "tool_calls", None) or []
            if calls:
                rendered = "\n".join(
                    f"{call.name}({json.dumps(call.args, ensure_ascii=False)})"
                    for call in calls
                )
                block += _text_block("工具调用", rendered)
        self._append("interaction", block)

    def end_turn(self, session_id: str, output: str, *, error: str | None = None) -> None:
        if not self._interaction_on():
            return
        state = self._turns.pop(session_id, {}) or {}
        text = output or ""
        block = ""
        # 最后一步的模型回复已经原样记录过，正文不重复写，保留章节结构即可
        if text and text == state.get("reply"):
            block += _text_block(
                "返回给用户", f"同模型回复 #{state.get('index', 1)}，正文不重复"
            )
        elif text:
            block += _text_block("返回给用户", text)
        if error is not None:
            block += _text_block("错误", error or "未知错误")
        if not block:
            block = "_本回合未产生输出_\n\n"
        block += "---\n\n"
        self._append("interaction", block)

    # --------------------------------------------------------------- utils
    def _interaction_on(self) -> bool:
        return self.enabled and self.interaction_enabled

    @staticmethod
    def _clock() -> str:
        return datetime.now().astimezone().strftime("%H:%M:%S")

    def _append(self, kind: str, content: str) -> None:
        now = datetime.now().astimezone()
        day = now.strftime("%Y-%m-%d")
        path = self.home / "logs" / kind / f"{day}.md"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            header = not path.exists()
            with path.open("a", encoding="utf-8") as fh:
                if header:
                    fh.write(f"# MarkBot2 {kind} log — {day}\n\n")
                fh.write(redact_secrets(content))


__all__ = ["DailyLog"]
