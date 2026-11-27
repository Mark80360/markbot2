"""飞书渠道拆分后的模块契约：都能独立导入，入口仍暴露原来的名字。"""

from __future__ import annotations

import importlib
import json
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


def test_strip_local_media_markdown_removes_standalone_line(channel_path, tmp_path):
    """单独占一行的本地图片引用整行删除，不能留下原始 Markdown 路径。"""
    client_module = importlib.import_module("feishu_client")
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"png")
    text = f"截图如下：\n\n![抖音首页截图]({picture})\n\n完成。"
    out = client_module.strip_local_media_markdown(text)
    assert out == "截图如下：\n\n完成。"
    assert str(picture) not in out


def test_strip_local_media_markdown_keeps_unrelated_refs(channel_path, tmp_path):
    """远程图片、缺失文件、代码块与行内代码里的示例都不能误删。"""
    client_module = importlib.import_module("feishu_client")
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"png")
    text = (
        "![远程](https://example.com/a.png)\n"
        "![缺失](/no/such/file.png)\n"
        f"```markdown\n![图]({picture})\n```\n"
        f"用 `![图]({picture})` 发图\n"
        f"行内 ![图]({picture}) 引用"
    )
    out = client_module.strip_local_media_markdown(text)
    assert "https://example.com/a.png" in out
    assert "/no/such/file.png" in out
    assert out.count("![图]") == 2  # 代码块与行内代码各保留一处
    assert "行内  引用" in out  # 行外引用只删引用本身，保留上下文


def test_strip_local_media_markdown_only_limits_to_given_paths(channel_path, tmp_path):
    """only 只删除指定路径（发送成功的媒体），其余引用保留成文本回退。"""
    client_module = importlib.import_module("feishu_client")
    sent = tmp_path / "sent.png"
    failed = tmp_path / "failed.png"
    sent.write_bytes(b"png")
    failed.write_bytes(b"png")
    text = f"![已发送]({sent})\n![发送失败]({failed})"
    out = client_module.strip_local_media_markdown(text, only=[str(sent)])
    assert str(sent) not in out
    assert str(failed) in out


def test_extract_local_media_skips_code_fences(channel_path, tmp_path):
    """代码块里的 ![](路径) 只是示例，不能当成真实媒体上传。"""
    client_module = importlib.import_module("feishu_client")
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"png")
    assert client_module._extract_local_media(f"```\n![图]({picture})\n```") == []
    assert client_module._extract_local_media(f"![图]({picture})") == [
        str(picture.resolve())
    ]


def test_send_answer_strips_media_ref_after_successful_send(
    channel_path, tmp_path, monkeypatch
):
    """媒体发送成功后，同一条文本里不能再出现本地路径 Markdown。"""
    client_module = importlib.import_module("feishu_client")
    client = client_module.FeishuClient(_settings(tmp_path))
    picture = tmp_path / "qr.png"
    picture.write_bytes(b"png")
    delivered: list[tuple[str, str]] = []
    monkeypatch.setattr(client, "send_media", lambda *a, **k: [str(picture.resolve())])
    monkeypatch.setattr(
        client,
        "_deliver",
        lambda chat_id, msg_type, content, reply_to, reply_in_thread: (
            delivered.append((msg_type, content)),
            True,
        )[1],
    )
    client.send_answer(
        "oc_1", f"二维码：\n![二维码]({picture})", media=[str(picture)]
    )
    assert delivered
    assert all(str(picture) not in content for _msg_type, content in delivered)


def test_send_answer_keeps_media_ref_when_send_failed(
    channel_path, tmp_path, monkeypatch
):
    """媒体没发出去时保留 Markdown，至少用户还能看到原始路径。"""
    client_module = importlib.import_module("feishu_client")
    client = client_module.FeishuClient(_settings(tmp_path))
    picture = tmp_path / "qr.png"
    picture.write_bytes(b"png")
    delivered: list[str] = []
    monkeypatch.setattr(client, "send_media", lambda *a, **k: [])
    monkeypatch.setattr(
        client,
        "_deliver",
        lambda chat_id, msg_type, content, reply_to, reply_in_thread: (
            delivered.append(content),
            True,
        )[1],
    )
    client.send_answer("oc_1", f"![二维码]({picture})", media=[str(picture)])
    assert delivered and str(picture) in json.loads(delivered[-1])["text"]


def test_process_stream_card_omits_local_media_markdown(
    channel_path, tmp_path, monkeypatch
):
    """流式卡片定稿内容必须已经去掉本地图片 Markdown（图片单独发）。"""
    gateway_module = importlib.import_module("feishu_gateway")
    gateway = _gateway(tmp_path, monkeypatch)
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"png")
    answer = f"截图如下：\n\n![抖音首页截图]({picture})\n\n完成。"
    finished: list[str] = []

    class FakeStream:
        def __init__(self, *_args, **_kwargs):
            pass

        def feed(self, _delta):
            pass

        def finish(self, text):
            finished.append(text)
            return True

    monkeypatch.setattr(gateway_module, "_StreamCard", FakeStream)
    monkeypatch.setattr(
        gateway,
        "_ask_markbot",
        lambda *a, **k: {"text": answer},
    )
    replies: list[dict] = []
    monkeypatch.setattr(
        gateway,
        "_reply",
        lambda chat_id, text, **kwargs: replies.append(
            {"chat_id": chat_id, "text": text, **kwargs}
        ),
    )

    gateway.process(
        {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "message": {
                    "chat_id": "oc_1",
                    "chat_type": "p2p",
                    "message_type": "text",
                    "message_id": "om_1",
                    "content": '{"text": "截图发我"}',
                },
                "sender": {"sender_id": {"open_id": "ou_1"}},
            },
        }
    )

    assert finished == ["截图如下：\n\n完成。"]
    assert replies and str(picture) not in replies[-1]["text"]
    assert replies[-1]["media"] == [str(picture.resolve())]


def test_stream_card_feed_drops_local_media_markdown(channel_path, tmp_path):
    """流式过程中的卡片更新也不能带本地图片路径，否则飞书报 200570 并中断流式。"""
    gateway_module = importlib.import_module("feishu_gateway")
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"png")
    pushed: list[str] = []

    class FakeClient:
        def create_stream_card(self, *_args, **_kwargs):
            return "card_1"

        def update_stream_card(self, card_id, text, sequence):
            pushed.append(text)
            return True

        def close_stream_card(self, card_id, sequence):
            return True

    stream = gateway_module._StreamCard(FakeClient(), "oc_1", "om_1", False)
    stream.feed(f"截图如下：\n\n![抖音首页截图]({picture})\n\n完成。")

    assert pushed and str(picture) not in pushed[0]
    assert "完成。" in pushed[0]
    # 没有最终文本时用累积内容兜底，同样不能带回原始路径
    assert stream.finish("") is True
    assert str(picture) not in pushed[-1]

