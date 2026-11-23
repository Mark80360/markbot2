from __future__ import annotations

import json

from markbot.kernel.session import (
    COMPACTION_PREFIX,
    Session,
    project_messages,
)
from markbot.schema.models import SessionEvent


def _call(call_id: str, name: str, args: dict | None = None):
    return type(
        "Call",
        (),
        {"id": call_id, "name": name, "args": args or {}},
    )()


def test_events_append_in_order_and_project_back(tmp_path):
    session = Session("你好", events_dir=tmp_path)
    session.append_assistant(
        "", tool_calls=[_call("c1", "echo", {"text": "hi"})]
    )
    session.append_tool_results(
        [_call("c1", "echo")],
        [type("R", (), {"content": "hi", "is_error": False})()],
    )
    session.append_assistant("完成")

    assert [event.type for event in session.events] == [
        "user_message",
        "assistant_message",
        "tool_result",
        "assistant_message",
    ]
    assert [event.seq for event in session.events] == [1, 2, 3, 4]

    projected = project_messages(session.events)
    assert [message.role for message in projected] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert [message.content for message in projected] == ["你好", "", "hi", "完成"]
    assert projected[1].tool_calls[0].id == "c1"
    assert projected[2].tool_call_id == "c1"


def test_events_are_written_to_disk_live(tmp_path):
    session = Session("第一句", events_dir=tmp_path)
    session.append_assistant("回复")

    path = tmp_path / session.id / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["type"] for row in rows] == ["user_message", "assistant_message"]
    assert rows[0]["data"]["content"] == "第一句"


def test_events_are_redacted(tmp_path):
    session = Session("sk-abcdefghijklmnop1234", events_dir=tmp_path)
    path = tmp_path / session.id / "events.jsonl"
    assert "sk-abcdefghijklmnop1234" not in path.read_text(encoding="utf-8")


def test_compaction_event_replays_same_messages(tmp_path):
    session = Session("旧问题", events_dir=tmp_path)
    session.append_assistant("旧回答")
    session.append_user("最近问题")
    session.apply_compaction("摘要内容", 2)

    assert session.messages[0].content.startswith(COMPACTION_PREFIX)
    projected = project_messages(session.events)
    assert [message.content for message in projected] == [
        session.messages[0].content,
        "最近问题",
    ]
    assert session.compacted_segments[0][0]["content"] == "旧问题"
    # 压缩事件必须自带被移除的原文，重启后才能重建审计数据。
    compaction = session.events[-1]
    assert compaction.data["removed_messages"][0]["content"] == "旧问题"


def test_load_prefers_events_and_restores_compaction(tmp_path):
    session = Session("旧问题", events_dir=tmp_path)
    session.append_assistant("旧回答")
    session.append_user("最近问题")
    session.apply_compaction("摘要内容", 2)
    session.append_assistant("最新回复")
    session.archive(tmp_path)

    restored = Session.load(tmp_path, session.id)
    assert restored is not None
    assert [message.role for message in restored.messages] == [
        "user",
        "user",
        "assistant",
    ]
    assert restored.messages[0].content.startswith(COMPACTION_PREFIX)
    assert restored.messages[1].content == "最近问题"
    assert restored.compacted_segments[0][0]["content"] == "旧问题"
    assert restored.seq == len(restored.events)


def test_load_falls_back_to_trajectory_and_backfills_events(tmp_path):
    target = tmp_path / "sessions" / "sess_LEGACY"
    target.mkdir(parents=True)
    (target / "meta.json").write_text(
        json.dumps({"id": "sess_LEGACY", "request": "旧请求", "domain": "code"}),
        encoding="utf-8",
    )
    rows = [
        {"role": "user", "content": "旧请求"},
        {"role": "assistant", "content": "旧回复"},
    ]
    (target / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    restored = Session.load(tmp_path, "sess_LEGACY")
    assert restored is not None
    assert [message.content for message in restored.messages] == ["旧请求", "旧回复"]
    assert [event.type for event in restored.events] == [
        "user_message",
        "assistant_message",
    ]
    # 补写事件日志后，后续 append 不会断代。
    assert (target / "events.jsonl").is_file()


def test_session_events_match_schema_version():
    event = SessionEvent(
        seq=1, ts="2026-01-01T00:00:00+08:00", type="user_message", data={"content": "x"}
    )
    assert event.schema_version == 1
    assert event.model_dump()["schema_version"] == 1
