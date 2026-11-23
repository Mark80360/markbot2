"""运行指标：结构化事件的内存聚合，供 ``/metrics``、``markbot stats`` 与每日 token 上限使用（ADR 0011）。

事件来源是 :class:`markbot.kernel.daily_log.DailyLog.event`：每条事件同时追加到
``logs/events/YYYY-MM-DD.jsonl``（可用 jq / duckdb 分析），并交给这里聚合。

事件类型：

- ``turn``：一个回合结束（``stopped_reason``、``steps``、``tool_calls``、``duration_ms``、
  ``tokens_in``、``tokens_out``、``kind``=chat|cron|eval、``error_class``）；
- ``tool``：一次工具调用（``tool``、``ok``、``latency_ms``、``error_class``）；
- ``provider``：一次模型调用（``model``、``ok``、``latency_ms``、``tokens_in``、``tokens_out``、``error_class``）。

token 字段在各家 provider 里名字不同，统一由 :func:`normalize_usage` 归一化。
"""

from __future__ import annotations

import json
import threading
from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

EVENTS_DIR = "logs/events"
_IN_KEYS = ("input_tokens", "prompt_tokens")
_OUT_KEYS = ("output_tokens", "completion_tokens")
_CACHE_KEYS = ("cache_read_input_tokens", "cached_tokens")
DURATION_WINDOW = 1000


def normalize_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """把 provider 的 usage 归一化为 ``{tokens_in, tokens_out, tokens_cached}``。"""
    usage = usage or {}

    def pick(keys: tuple[str, ...]) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return 0

    return {
        "tokens_in": pick(_IN_KEYS),
        "tokens_out": pick(_OUT_KEYS),
        "tokens_cached": pick(_CACHE_KEYS),
    }


def add_usage(total: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
    """把一次调用的 usage 累加进 ``total``（归一化后的字段）。"""
    for key, value in normalize_usage(usage).items():
        total[key] = total.get(key, 0) + value
    return total


def events_path(home: Path, day: date | None = None) -> Path:
    day = day or datetime.now().astimezone().date()
    return Path(home) / EVENTS_DIR / f"{day.isoformat()}.jsonl"


def read_events(home: Path, days: int = 7, *, today: date | None = None) -> Iterable[dict[str, Any]]:
    """按时间顺序读取最近 ``days`` 天的事件；坏行跳过。"""
    today = today or datetime.now().astimezone().date()
    for offset in range(max(1, days) - 1, -1, -1):
        path = events_path(home, today - timedelta(days=offset))
        if not path.exists():
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        yield event
        except OSError:
            continue


class Metrics:
    """线程安全的计数器；进程启动时从当天事件文件恢复「今日 token」，其余计数从零开始。"""

    def __init__(self, home: Path | None = None) -> None:
        self._lock = threading.Lock()
        self.turns: Counter[str] = Counter()
        self.turn_kinds: Counter[str] = Counter()
        self.tool_calls: Counter[str] = Counter()
        self.tool_failures: Counter[str] = Counter()
        self.tool_errors: Counter[str] = Counter()
        self.provider_calls: Counter[str] = Counter()
        self.provider_failures: Counter[str] = Counter()
        self.tokens: Counter[str] = Counter()
        self.durations: deque[float] = deque(maxlen=DURATION_WINDOW)
        self._day = datetime.now().astimezone().date()
        self.today_tokens = 0
        if home is not None:
            for event in read_events(home, days=1):
                if event.get("kind") == "provider":
                    self.today_tokens += int(event.get("tokens_in") or 0) + int(
                        event.get("tokens_out") or 0
                    )

    def observe(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        with self._lock:
            today = datetime.now().astimezone().date()
            if today != self._day:
                self._day = today
                self.today_tokens = 0
            if kind == "turn":
                self.turns[str(event.get("stopped_reason") or "unknown")] += 1
                self.turn_kinds[str(event.get("turn_kind") or "chat")] += 1
                duration = event.get("duration_ms")
                if isinstance(duration, (int, float)):
                    self.durations.append(float(duration) / 1000)
            elif kind == "tool":
                name = str(event.get("tool") or "unknown")
                self.tool_calls[name] += 1
                if not event.get("ok"):
                    self.tool_failures[name] += 1
                    self.tool_errors[str(event.get("error_class") or "failed")] += 1
            elif kind == "provider":
                model = str(event.get("model") or "unknown")
                self.provider_calls[model] += 1
                if not event.get("ok"):
                    self.provider_failures[str(event.get("error_class") or "error")] += 1
                tokens_in = int(event.get("tokens_in") or 0)
                tokens_out = int(event.get("tokens_out") or 0)
                self.tokens["in"] += tokens_in
                self.tokens["out"] += tokens_out
                self.tokens["cached"] += int(event.get("tokens_cached") or 0)
                self.today_tokens += tokens_in + tokens_out

    def tokens_today(self) -> int:
        with self._lock:
            if datetime.now().astimezone().date() != self._day:
                return 0
            return self.today_tokens

    @staticmethod
    def _quantile(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
        return ordered[index]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            durations = list(self.durations)
            return {
                "turns": dict(self.turns),
                "turn_kinds": dict(self.turn_kinds),
                "tool_calls": dict(self.tool_calls),
                "tool_failures": dict(self.tool_failures),
                "tool_errors": dict(self.tool_errors),
                "provider_calls": dict(self.provider_calls),
                "provider_failures": dict(self.provider_failures),
                "tokens": dict(self.tokens),
                "tokens_today": self.today_tokens,
                "turn_p50_s": self._quantile(durations, 0.5),
                "turn_p95_s": self._quantile(durations, 0.95),
            }

    def prometheus(self, extra: dict[str, float] | None = None) -> str:
        """Prometheus 文本格式（0.0.4）。不含任何对话内容，只有计数。"""
        snap = self.snapshot()
        lines: list[str] = []

        def metric(name: str, kind: str, help_text: str, rows: list[tuple[dict[str, str], float]]) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in rows or [({}, 0)]:
                rendered = ",".join(f'{key}="{_escape(val)}"' for key, val in labels.items())
                lines.append(f"{name}{{{rendered}}} {value}" if rendered else f"{name} {value}")

        metric("markbot_turns_total", "counter", "Finished turns by stop reason",
               [({"reason": k}, v) for k, v in sorted(snap["turns"].items())])
        metric("markbot_turn_kinds_total", "counter", "Finished turns by kind",
               [({"kind": k}, v) for k, v in sorted(snap["turn_kinds"].items())])
        metric("markbot_turn_duration_seconds", "gauge", "Turn duration quantiles (recent window)",
               [({"quantile": "0.5"}, snap["turn_p50_s"]), ({"quantile": "0.95"}, snap["turn_p95_s"])])
        metric("markbot_tool_calls_total", "counter", "Tool calls by tool",
               [({"tool": k}, v) for k, v in sorted(snap["tool_calls"].items())])
        metric("markbot_tool_failures_total", "counter", "Failed tool calls by tool",
               [({"tool": k}, v) for k, v in sorted(snap["tool_failures"].items())])
        metric("markbot_tool_errors_total", "counter", "Failed tool calls by error class",
               [({"class": k}, v) for k, v in sorted(snap["tool_errors"].items())])
        metric("markbot_provider_calls_total", "counter", "Model calls by model",
               [({"model": k}, v) for k, v in sorted(snap["provider_calls"].items())])
        metric("markbot_provider_failures_total", "counter", "Failed model calls by error class",
               [({"class": k}, v) for k, v in sorted(snap["provider_failures"].items())])
        metric("markbot_tokens_total", "counter", "Model tokens since process start",
               [({"direction": k}, v) for k, v in sorted(snap["tokens"].items())])
        metric("markbot_tokens_today", "gauge", "Model tokens used today (input+output)",
               [({}, snap["tokens_today"])])
        for name, value in sorted((extra or {}).items()):
            metric(name, "gauge", name, [({}, value)])
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


# ---------------------------------------------------------------- 统计报表
def summarize(home: Path, days: int = 7, *, today: date | None = None) -> dict[str, Any]:
    """按天汇总事件文件：回合数、失败率、工具失败率、token、各定时任务。"""
    per_day: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"turns": 0, "failed": 0, "cancelled": 0, "tool_calls": 0, "tool_failures": 0,
                 "tokens_in": 0, "tokens_out": 0, "cron_turns": 0}
    )
    tools: Counter[str] = Counter()
    tool_failures: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    jobs: dict[str, dict[str, int]] = defaultdict(lambda: {"runs": 0, "failed": 0, "tokens": 0})
    for event in read_events(home, days, today=today):
        day = str(event.get("ts") or "")[:10] or "?"
        row = per_day[day]
        kind = event.get("kind")
        if kind == "turn":
            row["turns"] += 1
            reason = event.get("stopped_reason")
            if reason == "cancelled":
                row["cancelled"] += 1
            elif reason != "completed":
                row["failed"] += 1
            if event.get("turn_kind") == "cron":
                row["cron_turns"] += 1
                job = str(event.get("job") or "?")
                jobs[job]["runs"] += 1
                jobs[job]["failed"] += int(reason != "completed")
                jobs[job]["tokens"] += int(event.get("tokens_in") or 0) + int(
                    event.get("tokens_out") or 0
                )
        elif kind == "tool":
            row["tool_calls"] += 1
            name = str(event.get("tool") or "?")
            tools[name] += 1
            if not event.get("ok"):
                row["tool_failures"] += 1
                tool_failures[name] += 1
                errors[str(event.get("error_class") or "failed")] += 1
        elif kind == "provider":
            row["tokens_in"] += int(event.get("tokens_in") or 0)
            row["tokens_out"] += int(event.get("tokens_out") or 0)
    return {
        "days": dict(sorted(per_day.items())),
        "tools": dict(tools.most_common()),
        "tool_failures": dict(tool_failures.most_common()),
        "error_classes": dict(errors.most_common()),
        "cron_jobs": dict(jobs),
    }


def render_summary(report: dict[str, Any]) -> str:
    lines = ["日期        回合  失败  取消  工具调用  工具失败率  输入token  输出token  定时任务"]
    for day, row in report["days"].items():
        rate = row["tool_failures"] / row["tool_calls"] if row["tool_calls"] else 0.0
        lines.append(
            f"{day:<10}  {row['turns']:>4}  {row['failed']:>4}  {row['cancelled']:>4}  "
            f"{row['tool_calls']:>8}  {rate:>9.1%}  {row['tokens_in']:>9}  {row['tokens_out']:>9}  "
            f"{row['cron_turns']:>8}"
        )
    if not report["days"]:
        lines.append("（这段时间没有事件记录；事件从 ADR 0011 升级后开始写入 logs/events/）")
    if report["tool_failures"]:
        lines.append("")
        lines.append("失败最多的工具: " + "、".join(
            f"{name} {count}/{report['tools'].get(name, count)}"
            for name, count in list(report["tool_failures"].items())[:8]
        ))
    if report["error_classes"]:
        lines.append("失败类型: " + "、".join(f"{k} {v}" for k, v in report["error_classes"].items()))
    if report["cron_jobs"]:
        lines.append("")
        lines.append("定时任务: " + "；".join(
            f"{job} 运行 {row['runs']} 次、失败 {row['failed']} 次、token {row['tokens']}"
            for job, row in report["cron_jobs"].items()
        ))
    return "\n".join(lines)


def classify_tool_error(result: Any) -> str:
    """工具失败的类别，便于统计「该换浏览器了」「权限配置不对」这类问题。"""
    details = getattr(result, "details", None) or {}
    content = str(getattr(result, "content", "") or "")
    if details.get("denied"):
        return "denied"
    if details.get("guard"):
        return "guard"
    if details.get("parse_error"):
        return "parse_error"
    if details.get("interrupted"):
        return "interrupted"
    if details.get("blocked"):
        return "blocked_by_site"
    if details.get("timed_out") or content.startswith("tool timeout"):
        return "timeout"
    if content.startswith("unknown tool"):
        return "unknown_tool"
    if content.startswith("invalid arguments"):
        return "invalid_arguments"
    return "failed"


def classify_provider_error(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    name = type(exc).__name__
    if name == "ContentRejectedError":
        return "moderation"
    text = str(exc)
    for status in ("429", "401", "403", "500", "502", "503", "504"):
        if f"HTTP {status}" in text:
            return f"http_{status}"
    if "Timeout" in name or "超时" in text:
        return "timeout"
    return "error"


__all__ = [
    "EVENTS_DIR",
    "Metrics",
    "add_usage",
    "classify_provider_error",
    "classify_tool_error",
    "events_path",
    "normalize_usage",
    "read_events",
    "render_summary",
    "summarize",
]
