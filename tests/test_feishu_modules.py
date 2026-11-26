"""飞书渠道拆分后的模块契约：都能独立导入，入口仍暴露原来的名字。"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

CHANNEL_DIR = Path(__file__).resolve().parents[1] / "channels" / "feishu"
MODULES = [
    "feishu_common",
    "feishu_settings",
    "feishu_store",
    "feishu_parse",
    "feishu_render",
    "feishu_client",
    "feishu_gateway",
    "channel",
]


@pytest.fixture
def channel_path(monkeypatch):
    monkeypatch.syspath_prepend(str(CHANNEL_DIR))
    return CHANNEL_DIR


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(channel_path, name):
    module = importlib.import_module(name)
    assert module is not None


def test_entry_exposes_runtime_names(channel_path):
    channel = importlib.import_module("channel")
    for name in ("main", "make_handler", "run_long_connection", "check", "Gateway", "load_settings"):
        assert hasattr(channel, name), name


def test_layering_has_no_back_edges(channel_path):
    """纯函数模块不能依赖网关/客户端；公共模块不依赖任何同级模块。"""
    import ast

    forbidden = {
        "feishu_common": {m for m in MODULES if m != "feishu_common"},
        "feishu_parse": {"feishu_client", "feishu_gateway", "channel"},
        "feishu_render": {"feishu_client", "feishu_gateway", "channel"},
        "feishu_store": {"feishu_client", "feishu_gateway", "channel"},
        "feishu_client": {"feishu_gateway", "channel"},
        "feishu_gateway": {"channel"},
    }
    for module, bad in forbidden.items():
        tree = ast.parse((CHANNEL_DIR / f"{module}.py").read_text(encoding="utf-8"))
        imported = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not (imported & bad), (module, imported & bad)


def test_render_and_parse_are_pure(channel_path):
    render = importlib.import_module("feishu_render")
    parse = importlib.import_module("feishu_parse")
    assert render._detect_msg_format("hello") == "text"
    assert render._detect_msg_format("| a | b |\n|---|---|\n| 1 | 2 |") == "interactive"
    assert parse._as_list(None) is None
    assert parse._as_list([1]) == [1]


def test_feishu_confirmation_stays_in_same_topic(channel_path, tmp_path, monkeypatch):
    """话题群里的授权提示要回到当前话题，而不是在群里新开一条消息。"""
    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "home"))
    gateway_module = importlib.import_module("feishu_gateway")
    settings_module = importlib.import_module("feishu_settings")
    store_module = importlib.import_module("feishu_store")

    settings = settings_module.Settings(
        app_id="cli_test",
        app_secret="secret",
        verification_token="",
        host="127.0.0.1",
        port=8790,
        path="/feishu/event",
        markbot_url="http://127.0.0.1:8787",
        markbot_timeout_s=10.0,
        group_policy="open",
        reply_to_message=False,
        topic_isolation=True,
        react_emoji="",
        done_emoji="",
        allow_from=(),
        media_dir=tmp_path / "media",
        session_db=tmp_path / "feishu.db",
    )
    gateway = gateway_module.Gateway(settings, store_module.Store(settings.session_db))
    sent: list[dict] = []
    monkeypatch.setattr(
        gateway,
        "_reply",
        lambda chat_id, text, **kwargs: sent.append(
            {"chat_id": chat_id, "text": text, **kwargs}
        ),
    )

    # 话题群消息即使没开 reply_to_message，也要回到所在话题
    assert gateway._reply_target("om_1", "group", {"thread_id": "omt_1"}) == (
        "om_1",
        True,
    )

    gateway._remember_turn_target("oc_group", "om_1", True)
    assert gateway.request_confirmation(
        {"chat_id": "oc_group", "token": "t" * 8, "title": "执行命令"}
    )
    assert sent[-1]["reply_to"] == "om_1"
    assert sent[-1]["reply_in_thread"] is True


def _settings(tmp_path, **overrides):
    settings_module = importlib.import_module("feishu_settings")
    values = dict(
        app_id="cli_test",
        app_secret="secret",
        verification_token="",
        host="127.0.0.1",
        port=8790,
        path="/feishu/event",
        markbot_url="http://127.0.0.1:8787",
        markbot_timeout_s=10.0,
        group_policy="open",
        reply_to_message=False,
        topic_isolation=True,
        react_emoji="",
        done_emoji="",
        allow_from=(),
        media_dir=tmp_path / "media",
        session_db=tmp_path / "feishu.db",
    )
    values.update(overrides)
    return settings_module.Settings(**values)


def _gateway(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "home"))
    gateway_module = importlib.import_module("feishu_gateway")
    store_module = importlib.import_module("feishu_store")
    settings = _settings(tmp_path, **overrides)
    return gateway_module.Gateway(settings, store_module.Store(settings.session_db))


def test_topic_chat_reply_and_session_stay_in_topic(channel_path, tmp_path, monkeypatch):
    """话题群没带 thread_id 时也要回到话题；同一话题的消息共用会话。"""
    gateway = _gateway(tmp_path, monkeypatch)
    assert gateway._reply_target("om_2", "topic", {}) == ("om_2", True)
    assert (
        gateway._chat_key("oc_1", "group", {"thread_id": "omt_1", "message_id": "om_2"})
        == "feishu:oc_1:omt_1"
    )
    assert (
        gateway._chat_key("oc_1", "group", {"thread_id": "omt_1", "message_id": "om_3"})
        == "feishu:oc_1:omt_1"
    )


def test_notify_keeps_topic_target(channel_path, tmp_path, monkeypatch):
    """后台任务结果要回到发起任务的话题，发件箱重试也不能丢话题信息。"""
    gateway = _gateway(tmp_path, monkeypatch)
    sent: list[dict] = []
    monkeypatch.setattr(
        gateway,
        "_send",
        lambda chat_id, text, **kwargs: (
            sent.append({"chat_id": chat_id, "text": text, **kwargs}),
            (True, ""),
        )[1],
    )
    assert gateway.notify("oc_1", "完成", reply_to="om_9", reply_in_thread=True)
    assert sent[-1]["reply_to"] == "om_9"
    assert sent[-1]["reply_in_thread"] is True

    item = gateway._store.outbox_add(
        "oc_1", "重试", reply_to="om_8", reply_in_thread=True, lease_s=0
    )
    row = next(row for row in gateway._store.outbox_due() if row["id"] == item)
    assert row["reply_to"] == "om_8"
    assert row["reply_in_thread"] is True


def test_store_migrates_outbox_topic_columns(channel_path, tmp_path):
    """旧库没有话题字段时自动补列，历史数据不受影响。"""
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, "
        "text TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
        "next_at REAL NOT NULL, error TEXT, created_at REAL NOT NULL)"
    )
    conn.commit()
    conn.close()

    store_module = importlib.import_module("feishu_store")
    store = store_module.Store(db)
    item = store.outbox_add("oc_1", "x", reply_to="om_1", reply_in_thread=True, lease_s=0)
    row = next(row for row in store.outbox_due() if row["id"] == item)
    assert row["reply_to"] == "om_1"
    assert row["reply_in_thread"] is True


def test_send_answer_keeps_thread_after_media(channel_path, tmp_path, monkeypatch):
    """话题回复时，媒体后面的文本仍留在同一话题；引用模式仍只引用第一条。"""
    client_module = importlib.import_module("feishu_client")
    client = client_module.FeishuClient(_settings(tmp_path))
    calls: list[tuple] = []
    monkeypatch.setattr(
        client,
        "send_media",
        lambda chat_id, media, **kwargs: calls.append(
            ("media", kwargs.get("reply_to"), kwargs.get("reply_in_thread"))
        ),
    )
    monkeypatch.setattr(
        client,
        "_deliver",
        lambda chat_id, msg_type, content, reply_to, reply_in_thread: (
            calls.append((msg_type, reply_to, reply_in_thread)),
            True,
        )[1],
    )

    client.send_answer(
        "oc_1", "hello", media=["/tmp/a.png"], reply_to="om_1", reply_in_thread=True
    )
    assert calls == [("media", "om_1", True), ("text", "om_1", True)]

    calls.clear()
    client.send_answer(
        "oc_1", "hello", media=["/tmp/a.png"], reply_to="om_1", reply_in_thread=False
    )
    assert calls == [("media", "om_1", False), ("text", None, False)]

