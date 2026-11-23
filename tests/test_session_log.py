"""ADR 0007：events.jsonl 是会话归档的唯一事实来源；旧派生文件可迁移清理。"""

from __future__ import annotations

import asyncio
import json

from markbot.cli.sessions_cmd import migrate_sessions
from markbot.kernel.session import Session
from markbot.memory.journal import Journal, load_cursor
from markbot.optimizer.adapter import SessionStore
from markbot.schema.session_log import SessionLog, project_messages, trajectory_text


def _session_with_compaction() -> Session:
    session = Session("旧问题")
    session.append_assistant("旧回答")
    session.append_user("新问题")
    session.apply_compaction("用户问过旧问题", 2)
    session.append_assistant("新回答")
    return session


def test_archive_writes_only_event_log_by_default(tmp_path):
    target = _session_with_compaction().archive(tmp_path)
    assert (target / "events.jsonl").is_file()
    assert not (target / "trajectory.jsonl").exists()
    assert not (target / "compacted.jsonl").exists()

    log = SessionLog(target)
    assert [row["role"] for row in log.trajectory()] == ["user", "user", "assistant"]
    assert log.compacted()[0]["messages"][0]["content"] == "旧问题"


def test_legacy_views_can_be_enabled(tmp_path):
    session = _session_with_compaction()
    target = session.archive(tmp_path, legacy_views=True)
    legacy = [
        json.loads(line)
        for line in (target / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert legacy == SessionLog(target).trajectory()
    assert (target / "compacted.jsonl").is_file()
    # 再次以默认设置归档：派生文件被清理
    session.archive(tmp_path)
    assert not (target / "trajectory.jsonl").exists()
    assert not (target / "compacted.jsonl").exists()


def test_kernel_respects_sessions_config(make_kernel, home, project):
    from markbot.kernel.config import load_config

    cfg = load_config(home=home, project_root=project)
    cfg.sessions.legacy_views = True
    kernel = make_kernel(["ok"], config=cfg)
    result = asyncio.run(kernel.run("你好"))
    assert (home / "sessions" / result.session_id / "trajectory.jsonl").is_file()


def test_readers_use_event_projection(tmp_path):
    home = tmp_path
    _session_with_compaction().archive(home)
    sid = next((home / "sessions").iterdir()).name

    rows = SessionStore(home).trajectory(sid)
    assert [row["content"] for row in rows][-1] == "新回答"

    journal = Journal(home)
    hits = journal.search("旧问题")
    assert {hit.source for hit in hits} >= {"compacted"}
    batches = journal.scan_since(load_cursor(home))
    assert batches[0].messages == 3 and batches[0].compactions == 1
    assert "旧问题" in journal.excerpt(batches[0])


def test_unarchived_live_session_is_not_consolidated(tmp_path):
    session = Session("还在聊", events_dir=tmp_path / "sessions")
    session.append_assistant("嗯")
    assert (tmp_path / "sessions" / session.id / "events.jsonl").is_file()
    assert Journal(tmp_path).scan_since(load_cursor(tmp_path)) == []


def test_migration_removes_redundant_files_and_keeps_old_compacted(tmp_path):
    home = tmp_path
    # 新归档：开着派生文件写出，迁移后应删除
    _session_with_compaction().archive(home, legacy_views=True)
    # 老归档：只有 trajectory + compacted（ADR 0001 之前），压缩原文只在旧文件里
    old = home / "sessions" / "sess_OLD"
    old.mkdir(parents=True)
    (old / "meta.json").write_text('{"id": "sess_OLD", "started_at": "x"}', encoding="utf-8")
    (old / "trajectory.jsonl").write_text(
        json.dumps({"role": "user", "content": "老问题"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (old / "compacted.jsonl").write_text(
        json.dumps({"segment": 1, "messages": [{"role": "user", "content": "更老的原文"}]},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    preview = migrate_sessions(home, dry_run=True)
    assert preview.sessions == 2 and "sess_OLD" in preview.events_written
    assert (old / "trajectory.jsonl").exists()

    report = migrate_sessions(home)
    assert (old / "events.jsonl").is_file()
    assert not (old / "trajectory.jsonl").exists()
    assert (old / "compacted.jsonl").exists()  # 事件里没有压缩段：保留
    assert any("sess_OLD/compacted.jsonl" in item for item in report.kept)
    assert SessionLog(old).compacted()[0]["messages"][0]["content"] == "更老的原文"
    assert "老问题" in trajectory_text(old)
    for directory in (home / "sessions").iterdir():
        if directory.name != "sess_OLD":
            assert not (directory / "trajectory.jsonl").exists()
            assert not (directory / "compacted.jsonl").exists()


def test_migration_keeps_trajectory_that_disagrees(tmp_path):
    home = tmp_path
    target = _session_with_compaction().archive(home, legacy_views=True)
    (target / "trajectory.jsonl").write_text(
        json.dumps({"role": "user", "content": "被人改过"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    report = migrate_sessions(home)
    assert (target / "trajectory.jsonl").exists()
    assert any("不一致" in item for item in report.kept)


def test_projection_is_shared_with_kernel():
    from markbot.kernel import session as kernel_session

    assert kernel_session.project_messages is project_messages
