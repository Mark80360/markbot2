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

