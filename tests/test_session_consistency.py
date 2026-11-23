"""会话一致性回归测试：审核遮蔽落盘（H）、归档原子写（I）、工具参数解析失败（K）。"""

from __future__ import annotations

import asyncio
import json

import pytest

from markbot.kernel.providers.anthropic import _parse as anthropic_parse
from markbot.kernel.providers.base import (
    ContentRejectedError,
    ModelReply,
    ToolCall,
    make_tool_call,
    parse_tool_arguments,
)
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.providers.openai_compat import _parse as openai_parse
from markbot.kernel.session import Session, project_messages
from markbot.schema.version import SCHEMA_VERSION


class _Call:
    def __init__(self, call_id: str, name: str, args: dict | None = None) -> None:
        self.id = call_id
        self.name = name
        self.args = args or {}


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content
        self.is_error = False


def _lines(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ================================================================ H：遮蔽事件


def _session_with_banned(events_dir):
    session = Session("看看 https://banned.example/x", events_dir=events_dir)
    session.append_assistant(
        "", tool_calls=[_Call("c1", "fetch_url", {"url": "https://banned.example/x"})]
    )
    session.append_tool_results([_Call("c1", "fetch_url")], [_Result("banned.example 页面")])
    return session


def test_redaction_is_an_event_and_projection_matches(tmp_path):
    session = _session_with_banned(tmp_path / "sessions")
    removed = session.redact_messages([1, 2], "[占位]", reason="moderation")

    assert [item["index"] for item in removed] == [1, 2]
    assert all("content" not in item for item in removed)  # 摘要里不带原文
    assert session.events[-1].type == "redaction"
    assert session.messages[2].content == "[占位]"
    assert session.messages[1].tool_calls[0].arguments == {}
    assert session.messages[1].tool_calls[0].id == "c1"  # 配对结构保留
    assert [m.model_dump() for m in project_messages(session.events)] == [
        m.model_dump() for m in session.messages
    ]


def test_redaction_survives_restart(tmp_path):
    session = _session_with_banned(tmp_path / "sessions")
    session.redact_messages([1, 2], "[占位]")
    session.append_assistant("已跳过")
    session.archive(tmp_path)

    restored = Session.load(tmp_path, session.id)
    assert restored is not None
    dumped = json.dumps([m.model_dump() for m in restored.messages[1:]], ensure_ascii=False)
    assert "banned.example" not in dumped


def test_redaction_and_compaction_replay_in_order(tmp_path):
    session = Session("a", events_dir=tmp_path)
    session.append_assistant("b")
    session.append_user("c")
    session.redact_messages([1], "[X]")
    session.apply_compaction("摘要", 2)
    session.append_user("d")
    session.redact_messages([1], "[Y]")
    assert [m.content for m in project_messages(session.events)] == [
        m.content for m in session.messages
    ]


def test_redaction_ignores_out_of_range_indexes(tmp_path):
    session = Session("a", events_dir=tmp_path)
    assert session.redact_messages([5, -1], "[X]") == []
    assert session.events[-1].type == "user_message"


def test_moderation_recovery_is_persisted_across_kernels(make_kernel):
    """旧实现只改内存：重启后从 events.jsonl 恢复，触发内容回来，会话永久被拒。"""

    class Censored:
        name = "censored"

        def __init__(self, script):
            self.script = list(script)

        async def complete(self, payload, on_delta=None):
            if "banned.example" in json.dumps(payload, ensure_ascii=False):
                raise ContentRejectedError("Inappropriate input/output rejected for security reasons")
            if self.script:
                return self.script.pop(0)
            return ModelReply(text="ok")

    first = make_kernel(
        provider=Censored(
            [
                ModelReply(
                    tool_calls=[
                        ToolCall(id="c1", name="echo", args={"text": "https://banned.example/x"})
                    ]
                ),
            ]
        )
    )
    result = asyncio.run(first.run("看看这个链接"))
    assert result.stopped_reason == "completed"
    events = (first.home / "sessions" / result.session_id / "events.jsonl").read_text("utf-8")
    assert '"redaction"' in events

    second = make_kernel(provider=Censored([ModelReply(text="继续")]))
    resumed = asyncio.run(second.run("继续", session_id=result.session_id))
    assert resumed.stopped_reason == "completed", resumed.error
    assert resumed.text == "继续"


# ================================================================ I：归档写入


def test_archive_does_not_rewrite_in_sync_event_log(tmp_path):
    session = Session("a", events_dir=tmp_path / "sessions")
    session.append_assistant("b")
    target = session.archive(tmp_path)
    path = target / "events.jsonl"
    inode = path.stat().st_ino

    session.append_user("c")
    session.archive(tmp_path)
    assert path.stat().st_ino == inode  # 只追加，未被替换
    assert len(_lines(path)) == 3


def test_archive_repairs_missing_events(tmp_path):
    session = Session("a", events_dir=tmp_path / "sessions")
    session.append_assistant("b")
    target = session.archive(tmp_path)
    path = target / "events.jsonl"
    path.write_text(_lines(path)[0] + "\n", encoding="utf-8")  # 模拟磁盘上少了一条

    session.archive(tmp_path)
    assert [json.loads(line)["seq"] for line in _lines(path)] == [1, 2]


def test_torn_last_line_does_not_corrupt_next_event(tmp_path):
    session = Session("a", events_dir=tmp_path / "sessions")
    session.append_assistant("b")
    target = session.archive(tmp_path)
    path = target / "events.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 99, "ts": "x", "type": "user_mes')  # 崩溃留下的半行

    restored = Session.load(tmp_path, session.id)
    assert restored is not None
    restored.append_user("崩溃后")
    again = Session.load(tmp_path, session.id)
    assert again is not None
    assert [m.content for m in again.messages] == ["a", "b", "崩溃后"]

    again.archive(tmp_path)
    for line in _lines(path):
        json.loads(line)  # 归档后整体重写为干净文件


def test_archive_of_unpersisted_session_writes_full_log(tmp_path):
    session = Session("a")
    session.append_assistant("b")
    target = session.archive(tmp_path)
    assert len(_lines(target / "events.jsonl")) == 2


def test_archive_leaves_no_temp_files(tmp_path):
    session = Session("a", events_dir=tmp_path / "sessions")
    session.record_feedback({"rating": 5})
    session.append_assistant("b")
    target = session.archive(tmp_path)
    assert not [p for p in target.rglob("*") if p.name.endswith(".tmp")]
    assert (target / "feedback.json").is_file()
    assert (target / "deliverable" / "final.md").read_text(encoding="utf-8") == "b"


# ================================================================ K：工具参数解析


@pytest.mark.parametrize(
    ("raw", "args", "ok"),
    [
        ('{"a": 1}', {"a": 1}, True),
        ("", {}, True),
        (None, {}, True),
        ({"a": 1}, {"a": 1}, True),
        ('{"path": "a.txt", "content": "x', {}, False),
        ("[1, 2]", {}, False),
        ('"text"', {}, False),
        ("oops", {}, False),
    ],
)
def test_parse_tool_arguments(raw, args, ok):
    parsed, error = parse_tool_arguments(raw)
    assert parsed == args
    assert (error is None) is ok


def test_make_tool_call_keeps_raw_only_on_error():
    bad = make_tool_call("c1", "write_file", '{"path": "a')
    assert bad.parse_error and bad.raw_arguments == '{"path": "a' and bad.args == {}
    good = make_tool_call("c1", "write_file", '{"path": "a"}')
    assert good.parse_error is None and good.raw_arguments == "" and good.args == {"path": "a"}


def test_openai_parse_flags_truncated_arguments():
    reply = openai_parse(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path": "a.txt", "content": "x',
                                },
                            }
                        ],
                    }
                }
            ]
        }
    )
    assert reply.tool_calls[0].parse_error


def test_anthropic_parse_flags_non_object_input():
    reply = anthropic_parse(
        {"content": [{"type": "tool_use", "id": "c1", "name": "echo", "input": "oops"}]}
    )
    assert reply.tool_calls[0].parse_error


def test_loop_rejects_unparseable_arguments_before_any_hook(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="echo",
                        args={},
                        parse_error="参数不是合法 JSON",
                        raw_arguments='{"text": "hi',
                    )
                ]
            ),
            "改好了",
        ]
    )
    kernel = make_kernel(provider=provider)
    seen: list[str] = []
    kernel.events.subscribe("tool_call", "test", lambda **kw: seen.append(kw["call"].name))

    result = asyncio.run(kernel.run("调用 echo", archive=False))
    assert result.stopped_reason == "completed"
    tool_messages = [m for m in kernel.session.messages if m.role == "tool"]
    assert tool_messages[0].is_error
    assert "invalid arguments for echo" in tool_messages[0].content
    assert "合法的 JSON" in tool_messages[0].content
    assert seen == []  # 没有走到 tool_call 事件 / 权限 / 确认 / 执行


def test_schema_version_bumped_for_redaction_event():
    major, minor, _ = (int(part) for part in SCHEMA_VERSION.split("."))
    assert (major, minor) >= (1, 1)
