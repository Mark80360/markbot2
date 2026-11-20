from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import date, timedelta

from markbot.kernel.compaction import spill_tool_result
from markbot.kernel.context import Section, compose_sections
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.memory.consolidator import Consolidator, restore_snapshot
from markbot.memory.journal import Journal, SessionBatch, load_cursor, save_cursor
from markbot.memory.lifespan import annotate, inject_line, split_meta, status
from markbot.memory.provider import MemoryProvider
from markbot.memory.store import MemoryStore
from markbot.optimizer.gate import allowed_path

SECRET = "api_key=sk-abcdefghijklmnopqrstuvwxyz012345"


def test_remember_writes_and_verifies(tmp_path):
    learned = tmp_path / "learned"
    store = MemoryStore(learned)

    receipt = store.remember("用户偏好：回复用中文")
    assert receipt.ok and receipt.changed and receipt.verified
    text = (learned / "strategy" / "memory.md").read_text(encoding="utf-8")
    assert "- 用户偏好：回复用中文" in text

    again = store.remember("用户偏好：回复用中文")
    assert again.ok and not again.changed

    replaced = store.remember("用户偏好：回复用英文", replaces="回复用中文")
    assert replaced.ok and replaced.verified and replaced.removed
    text = (learned / "strategy" / "memory.md").read_text(encoding="utf-8")
    assert "回复用英文" in text and "回复用中文" not in text


def test_remember_refuses_secret_and_oversize(tmp_path):
    learned = tmp_path / "learned"
    store = MemoryStore(learned)

    secret = store.remember(SECRET)
    assert not secret.ok and "密钥" in secret.message

    oversize = store.remember("长" * 7000)
    assert not oversize.ok and "容量" in oversize.message
    assert not (learned / "strategy" / "memory.md").exists()


def test_forget_removes_only_matching_bullets(tmp_path):
    learned = tmp_path / "learned"
    store = MemoryStore(learned)
    store.remember("用户偏好：回复用中文")
    store.remember("项目目录：D:/Source/markbot2")

    receipt = store.forget(text="回复用中文")
    assert receipt.ok and receipt.changed
    text = (learned / "strategy" / "memory.md").read_text(encoding="utf-8")
    assert "回复用中文" not in text
    assert "D:/Source/markbot2" in text

    missing = store.forget(text="不存在的条目")
    assert missing.ok and not missing.changed


def test_wiki_page_and_index(tmp_path):
    learned = tmp_path / "learned"
    store = MemoryStore(learned)

    receipt = store.remember(
        "飞书渠道发送失败时重试两次", target="wiki", topic="feishu-channel"
    )
    assert receipt.ok and receipt.verified
    page = learned / "memory" / "wiki" / "feishu-channel.md"
    assert page.exists()
    assert allowed_path("memory/wiki/feishu-channel.md")

    index = json.loads(
        (learned / "memory" / "wiki" / "index.json").read_text(encoding="utf-8")
    )
    assert "feishu-channel" in index["entries"]

    hits = store.search("重试")
    assert any(hit["path"] == "memory/wiki/feishu-channel.md" for hit in hits)


def test_provider_renders_and_refreshes(tmp_path):
    learned = tmp_path / "learned"
    provider = MemoryProvider(learned)
    assert provider.render() == ""

    store = MemoryStore(learned)
    store.remember("用户偏好：回复用中文")
    block = provider.render()
    assert "<long_term_memory>" in block and "回复用中文" in block

    store.remember("用户住在杭州")
    assert "杭州" in provider.render()


def test_provider_truncates_oversized_resident(tmp_path):
    learned = tmp_path / "learned"
    path = learned / "strategy" / "memory.md"
    path.parent.mkdir(parents=True)
    path.write_text("- " + "长" * 7000, encoding="utf-8")

    block = MemoryProvider(learned).render()
    assert "已截断" in block
    assert len(block) < 6300


def test_written_memory_enters_next_turn_system_prompt(make_kernel, home):
    learned = home / "learned"
    kernel = make_kernel(provider=ScriptedProvider(["第一轮", "第二轮"]), learned_dir=learned)

    asyncio.run(kernel.run("第一问", archive=False))
    MemoryStore(learned).remember("用户偏好：回复用中文")
    asyncio.run(kernel.run("第二问", archive=False))

    first_system = kernel.provider.calls[0]["system"]
    second_system = kernel.provider.calls[1]["system"]
    assert "回复用中文" not in first_system
    assert "回复用中文" in second_system


def test_memory_tool_roundtrip_records_receipt(make_kernel, home):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="memory_remember",
                        args={"text": "用户偏好：回复用中文"},
                    )
                ]
            ),
            "已记住",
        ]
    )
    kernel = make_kernel(provider=provider, learned_dir=home / "learned")
    result = asyncio.run(kernel.run("记住我的偏好"))
    assert result.text == "已记住"

    text = (home / "learned" / "strategy" / "memory.md").read_text(encoding="utf-8")
    assert "回复用中文" in text
    trajectory = (
        home / "sessions" / result.session_id / "trajectory.jsonl"
    ).read_text(encoding="utf-8")
    assert "校验通过" in trajectory
    assert "memory_remember" in trajectory

    runtime_log = next((home / "logs" / "runtime").glob("*.md")).read_text(encoding="utf-8")
    assert "记忆写入" in runtime_log
    assert "回复用中文" not in runtime_log  # 日志只记路径与结果，不记正文


def test_memory_tool_refuses_secret_and_off_session(make_kernel, home):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[
                    ToolCall(id="c1", name="memory_remember", args={"text": SECRET})
                ]
            ),
            "未写入",
            ModelReply(
                tool_calls=[
                    ToolCall(
                        id="c2",
                        name="memory_remember",
                        args={"text": "用户偏好：回复用中文"},
                    )
                ]
            ),
            "完成",
        ]
    )
    kernel = make_kernel(provider=provider, learned_dir=home / "learned")
    asyncio.run(kernel.run("记住密钥", archive=False))
    asyncio.run(kernel.run("记住偏好", archive=False, metadata={"memory": "off"}))

    assert not (home / "learned" / "strategy" / "memory.md").exists()
    tool_messages = [msg.content for msg in kernel.session.messages if msg.role == "tool"]
    assert any("疑似包含密钥" in text for text in tool_messages)
    assert any("关闭记忆读写" in text for text in tool_messages)


def _write_session(home, session_id: str, messages: list[dict], compacted: list[dict] | None = None):
    directory = home / "sessions" / session_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in messages) + "\n",
        encoding="utf-8",
    )
    if compacted:
        (directory / "compacted.jsonl").write_text(
            json.dumps({"segment": 1, "messages": compacted}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def test_journal_search_and_cursor(tmp_path):
    home = tmp_path / "home"
    _write_session(
        home,
        "sess_A",
        [
            {"role": "user", "content": "部署飞书渠道失败"},
            {"role": "assistant", "content": "需要重试两次"},
        ],
        [{"role": "tool", "content": "compacted 里的重试日志"}],
    )
    journal = Journal(home)

    hits = journal.search("重试")
    assert hits and hits[0].session_id == "sess_A"
    assert any(hit.source == "compacted" for hit in hits)

    cursor = load_cursor(home)
    batches = journal.scan_since(cursor, max_sessions=10)
    assert [batch.session_id for batch in batches] == ["sess_A"]
    assert batches[0].messages == 2 and batches[0].compactions == 1

    cursor.advance(batches)
    save_cursor(home, cursor)
    assert journal.scan_since(load_cursor(home), max_sessions=10) == []


class _StaticProvider:
    def __init__(self, text: str) -> None:
        self.text = text
        self.name = "static"

    async def complete(self, payload):
        return ModelReply(text=self.text)


def test_consolidator_writes_once_and_advances_cursor(tmp_path):
    home = tmp_path / "home"
    learned = home / "learned"
    _write_session(
        home,
        "sess_A",
        [
            {"role": "user", "content": "以后回复用中文"},
            {"role": "assistant", "content": "好的"},
        ],
    )
    provider = _StaticProvider(
        '[{"text": "用户偏好：回复用中文", "target": "memory", '
        '"lifespan": "permanent", "topic": ""}]'
    )
    consolidator = Consolidator(learned, home=home, provider=provider)

    report = asyncio.run(consolidator.run())
    assert report.written == 1 and report.cursor_advanced
    assert "回复用中文" in (learned / "strategy" / "memory.md").read_text(encoding="utf-8")
    assert (home / "memory" / "cursor.json").exists()

    again = asyncio.run(consolidator.run())
    assert again.no_input and not again.cursor_advanced


def test_consolidator_rolls_back_on_bad_candidate(tmp_path):
    home = tmp_path / "home"
    learned = home / "learned"
    MemoryStore(learned).remember("用户偏好：回复用中文")
    _write_session(
        home, "sess_B", [{"role": "user", "content": "记住这个密钥"}]
    )
    provider = _StaticProvider(
        '[{"text": "' + SECRET + '", "target": "memory", "lifespan": "durable"}]'
    )
    report = asyncio.run(Consolidator(learned, home=home, provider=provider).run())

    assert report.failed == 1 and report.incomplete
    assert not report.cursor_advanced
    text = (learned / "strategy" / "memory.md").read_text(encoding="utf-8")
    assert "回复用中文" in text and SECRET not in text


def test_lifespan_marks_stale_and_hides_metadata(tmp_path):
    learned = tmp_path / "learned"
    path = learned / "strategy" / "memory.md"
    path.parent.mkdir(parents=True)
    past = date.today() - timedelta(days=200)
    path.write_text(
        "- " + annotate("飞书接口为 v1", "durable", now=past, verified_by="user") + "\n",
        encoding="utf-8",
    )

    block = MemoryProvider(learned).render()
    assert "待复核" in block and "<!--" not in block

    hidden = MemoryProvider(learned, show_stale=False).render()
    assert "飞书接口为 v1" not in hidden

    annotated = annotate("x", "durable", now=past)
    assert annotated.endswith("-->")
    assert status({"review_after": (past + timedelta(days=90)).isoformat()}) == "stale"
    assert inject_line("- 新事实 <!-- m:permanent;u=2026-01-01;v=user -->").startswith("- 新事实")


def test_provider_skips_annotated_entries_without_provenance(tmp_path):
    learned = tmp_path / "learned"
    path = learned / "strategy" / "memory.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "- 无来源条目 <!-- m:durable;u=2026-01-01 -->\n- 旧格式条目\n", encoding="utf-8"
    )
    block = MemoryProvider(learned).render()
    assert "无来源条目" not in block and "旧格式条目" in block


def test_store_labels_entries_and_replaces_old_lines(tmp_path):
    store = MemoryStore(tmp_path / "learned")
    first = store.remember("用户偏好：回复用中文", lifespan="permanent")
    assert first.ok
    text = store.resident_path.read_text(encoding="utf-8")
    assert "m:permanent" in text and "<!--" in text

    replaced = store.remember("用户偏好：回复用英文", replaces="回复用中文")
    assert replaced.ok and replaced.removed
    text = store.resident_path.read_text(encoding="utf-8")
    assert "回复用英文" in text and "回复用中文" not in text


def test_compose_sections_drops_low_priority_with_marker():
    text = compose_sections(
        [
            Section("system", "S" * 100, priority=100, hard=True),
            Section("memory", "M" * 100, priority=70),
            Section("skills", "K" * 100, priority=50, cap=100, allow_partial=True),
        ],
        budget=160,
    )
    assert "S" * 100 in text
    assert "M" * 100 not in text
    assert "已省略" in text and "本轮省略：memory" in text


def test_spill_tool_result_keeps_preview_and_file(tmp_path):
    payload = "x" * 500
    preview, path = spill_tool_result(payload, tmp_path / "files", cap=100)
    assert path is not None and path.read_text(encoding="utf-8") == payload
    assert "已落盘" in preview and len(preview) < len(payload)

    small, none = spill_tool_result("ok", tmp_path / "files", cap=100)
    assert small == "ok" and none is None


def test_consolidator_prompt_lists_stale_entries(tmp_path):
    home = tmp_path / "home"
    learned = home / "learned"
    store = MemoryStore(learned)
    store.resident_path.parent.mkdir(parents=True, exist_ok=True)
    past = date.today() - timedelta(days=200)
    store.resident_path.write_text(
        "- " + annotate("飞书接口为 v1", "durable", now=past) + "\n", encoding="utf-8"
    )

    prompt = Consolidator(learned, home=home, provider=_StaticProvider("[]"))._prompt(
        [SessionBatch("sess_A", 2, 0)]
    )
    assert "待复核条目" in prompt and "飞书接口为 v1" in prompt


def test_restore_snapshot_previews_before_writing(tmp_path):
    home = tmp_path / "home"
    learned = home / "learned"
    store = MemoryStore(learned)
    store.remember("用户偏好：回复用中文")
    Consolidator(learned, home=home, provider=_StaticProvider("[]"))._snapshot()
    store.remember("用户偏好：回复用英文")

    preview = restore_snapshot(home, learned, dry_run=True)
    assert "strategy/memory.md" in preview
    assert "回复用英文" in store.resident_path.read_text(encoding="utf-8")

    restore_snapshot(home, learned)
    text = store.resident_path.read_text(encoding="utf-8")
    assert "回复用中文" in text and "回复用英文" not in text


def test_provenance_and_key_conflict_replacement(tmp_path):
    learned = tmp_path / "learned"
    store = MemoryStore(learned)
    first = store.remember(
        "用户偏好：回复用中文",
        key="pref.reply_language",
        verified_by="user",
        sources=("sess_1",),
        project_rev="abc123",
    )
    assert first.ok
    line = store.resident_path.read_text(encoding="utf-8").splitlines()[0]
    _, meta = split_meta(line)
    assert meta["verified_by"] == "user" and meta["key"] == "pref.reply_language"
    assert meta["source_sessions"] == "sess_1" and meta["project_rev"] == "abc123"

    replaced = store.remember("用户偏好：回复用英文", key="pref.reply_language")
    assert replaced.ok and replaced.changed
    text = store.resident_path.read_text(encoding="utf-8")
    assert "回复用英文" in text and "回复用中文" not in text

    archived = list((learned / "memory" / "wiki" / "archive").glob("*.md"))
    assert archived and "回复用中文" in archived[0].read_text(encoding="utf-8")


def test_skill_write_and_dedup(tmp_path):
    store = MemoryStore(tmp_path / "learned")
    receipt = store.remember_skill(
        "deploy-flow", "发布流程", ["跑测试", "构建", "部署"]
    )
    assert receipt.ok and receipt.path == "skills/deploy-flow/SKILL.md"
    text = store.skill_path("deploy-flow").read_text(encoding="utf-8")
    assert "description: 发布流程" in text and "1. 跑测试" in text

    again = store.remember_skill("deploy-flow", "发布流程", ["跑测试", "构建", "部署"])
    assert again.ok and not again.changed


def test_consolidator_can_promote_skill(tmp_path):
    home = tmp_path / "home"
    learned = home / "learned"
    _write_session(home, "sess_S", [{"role": "user", "content": "以后按这个流程部署"}])
    provider = _StaticProvider(
        '[{"text": "发布流程", "target": "skill", "topic": "deploy-flow", '
        '"steps": ["跑测试", "构建", "部署"]}]'
    )
    report = asyncio.run(Consolidator(learned, home=home, provider=provider).run())
    assert report.written == 1 and report.cursor_advanced
    assert (learned / "skills" / "deploy-flow" / "SKILL.md").exists()


def test_memory_package_imports_standalone(repo_root):
    env = dict(os.environ, PYTHONPATH=str(repo_root / "src"))
    result = subprocess.run(
        [sys.executable, "-c", "import markbot.memory"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr


def test_load_cursor_tolerates_bad_values(tmp_path):
    home = tmp_path / "home"
    path = home / "memory" / "cursor.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"consumed": {"s": {"messages": "x", "compactions": null}}}', encoding="utf-8"
    )
    assert load_cursor(home).consumed == {"s": {"messages": 0, "compactions": 0}}

    path.write_text("[]", encoding="utf-8")
    assert load_cursor(home).consumed == {}


def test_consolidator_invalid_json_keeps_cursor(tmp_path):
    home = tmp_path / "home"
    _write_session(home, "sess_J", [{"role": "user", "content": "以后回复用中文"}])
    report = asyncio.run(
        Consolidator(home / "learned", home=home, provider=_StaticProvider("not json")).run()
    )

    assert report.incomplete and not report.cursor_advanced
    assert not (home / "memory" / "cursor.json").exists()


def test_consolidator_rollback_removes_new_skill(tmp_path):
    home = tmp_path / "home"
    learned = home / "learned"
    _write_session(home, "sess_K", [{"role": "user", "content": "记住流程和密钥"}])
    provider = _StaticProvider(
        '[{"text": "部署流程", "target": "skill", "topic": "deploy", '
        '"steps": ["构建", "发布"]},'
        ' {"text": "' + SECRET + '", "target": "memory", "lifespan": "durable"}]'
    )
    report = asyncio.run(Consolidator(learned, home=home, provider=provider).run())

    assert report.failed == 1 and report.written == 0 and report.changed == []
    assert not (learned / "skills" / "deploy" / "SKILL.md").exists()


def test_consolidator_ignores_non_list_steps(tmp_path):
    home = tmp_path / "home"
    learned = home / "learned"
    _write_session(home, "sess_L", [{"role": "user", "content": "记住流程"}])
    provider = _StaticProvider(
        '[{"text": "部署流程", "target": "skill", "topic": "deploy", "steps": "构建; 发布"}]'
    )
    report = asyncio.run(Consolidator(learned, home=home, provider=provider).run())

    assert report.written == 1
    assert not (learned / "skills" / "deploy" / "SKILL.md").exists()
    assert "部署流程" in (learned / "memory" / "wiki" / "deploy.md").read_text(encoding="utf-8")

