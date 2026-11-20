from __future__ import annotations

import asyncio
import json

from markbot.kernel.session import Session, redact_secrets


def test_redact_secrets_shapes():
    text = "key sk-abcdefghijklmnop1234 token=supersecretvalue"
    out = redact_secrets(text)
    assert "sk-abcdefghijklmnop1234" not in out
    assert "supersecretvalue" not in out
    assert "[REDACTED]" in out


def test_archive_redacts_and_writes_feedback(tmp_path):
    session = Session("请使用 sk-abcdefghijklmnop1234 测试")
    session.append_assistant("结果见 sk-abcdefghijklmnop1234")
    session.record_feedback({"rating": 5, "correction": "token=abcdefgh12345"})
    target = session.archive(tmp_path)

    assert "sk-abcdefghijklmnop1234" not in (target / "request.md").read_text(
        encoding="utf-8"
    )
    assert "sk-abcdefghijklmnop1234" not in (target / "deliverable" / "final.md").read_text(
        encoding="utf-8"
    )
    assert "sk-abcdefghijklmnop1234" not in (target / "meta.json").read_text(
        encoding="utf-8"
    )
    trajectory = (target / "trajectory.jsonl").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop1234" not in trajectory

    feedback = json.loads((target / "feedback.json").read_text(encoding="utf-8"))
    assert feedback["rating"] == 5
    assert "abcdefgh12345" not in json.dumps(feedback, ensure_ascii=False)


def test_archive_handles_quoted_secret_in_tool_arguments(tmp_path):
    """工具参数里 ``token=...\"`` 这类被引号包裹的密钥不能破坏归档。"""
    session = Session("测试")
    session.append_assistant(
        "",
        tool_calls=[
            type(
                "Call",
                (),
                {
                    "id": "call_1",
                    "name": "run_command",
                    "args": {
                        "command": 'DY_COOKIE="msToken=abcdefgh12345678=" curl -s https://example.com'
                    },
                },
            )()
        ],
    )
    target = session.archive(tmp_path)
    row = json.loads(
        (target / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()[1]
    )
    command = row["tool_calls"][0]["arguments"]["command"]
    assert "abcdefgh12345678" not in command
    assert "[REDACTED]" in command
    assert command.endswith('curl -s https://example.com')


def test_archive_idle_writes_snapshot_once_per_activity(make_kernel, home):
    kernel = make_kernel(["收到"])
    asyncio.run(kernel.run("你好", archive=False))
    sid = kernel.session.id
    trajectory = home / "sessions" / sid / "trajectory.jsonl"
    assert not trajectory.exists()

    # 未到阈值、开关关闭：都不动
    assert asyncio.run(kernel.archive_idle(15)) == []
    assert asyncio.run(kernel.archive_idle(0)) == []

    kernel._activity[sid] -= 16 * 60
    assert asyncio.run(kernel.archive_idle(15)) == [sid]
    assert trajectory.exists()
    # 没有新活动不会重复归档
    assert asyncio.run(kernel.archive_idle(15)) == []

    # 继续对话后再次空闲：两个时间戳一起前移，模拟又过了 16 分钟
    asyncio.run(kernel.run("继续", archive=False))
    kernel._activity[sid] -= 16 * 60
    kernel._archived_at[sid] -= 16 * 60
    assert asyncio.run(kernel.archive_idle(15)) == [sid]


def test_archive_idle_skips_ended_session(make_kernel):
    kernel = make_kernel(["ok"])
    asyncio.run(kernel.run("你好", archive=False))
    sid = kernel.session.id
    asyncio.run(kernel.end_session(archive=False))

    kernel._activity[sid] -= 3600
    assert asyncio.run(kernel.archive_idle(15)) == []
