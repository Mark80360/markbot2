from __future__ import annotations

import asyncio
from datetime import datetime

from markbot.kernel.config import load_config


def _day() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def test_runtime_and_interaction_logs_are_written(make_kernel):
    kernel = make_kernel(["你好，我是 MarkBot"])
    result = asyncio.run(kernel.run("打个招呼"))
    assert result.stopped_reason == "completed"

    logs = kernel.home / "logs"
    runtime = (logs / "runtime" / f"{_day()}.md").read_text(encoding="utf-8")
    interaction = (logs / "interaction" / f"{_day()}.md").read_text(encoding="utf-8")

    assert "会话开始" in runtime
    assert "回合结束" in runtime
    assert result.session_id in runtime

    assert "打个招呼" in interaction
    assert "模型请求 #1" in interaction
    assert "模型回复 #1" in interaction
    assert "你好，我是 MarkBot" in interaction
    assert "返回给用户" in interaction


def test_interaction_log_redacts_secret_shapes(make_kernel):
    key = "sk-abcdefghijklmnopqrstuvwxyz012345"
    env_key = "f" * 64
    kernel = make_kernel(["收到"])
    asyncio.run(kernel.run(f"帮我记住 api_key={key}"))
    asyncio.run(kernel.run(f"CRAWL4AI_API_TOKEN={env_key}"))

    interaction = (
        kernel.home / "logs" / "interaction" / f"{_day()}.md"
    ).read_text(encoding="utf-8")
    assert key not in interaction
    # 环境变量写法（前缀带下划线）也必须遮住
    assert env_key not in interaction
    assert "[REDACTED]" in interaction


def test_logging_can_be_disabled(make_kernel, home, project):
    cfg = load_config(home=home, project_root=project)
    cfg.logging.enabled = False
    kernel = make_kernel(["收到"], config=cfg)
    asyncio.run(kernel.run("你好"))

    assert not (home / "logs").exists()
