"""Phase 1 第二批（ADR 0011）的回归测试：结构化事件与指标、token 上限、模型角色、审核规则与自动换会话、
定时任务权限预检与运行后提示、熔断告警、飞书收件箱/发件箱。"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from markbot.kernel.config import load_config
from markbot.kernel.daily_log import DailyLog
from markbot.kernel.metrics import (
    Metrics,
    classify_tool_error,
    events_path,
    normalize_usage,
    read_events,
    render_summary,
    summarize,
)
from markbot.kernel.providers.base import (
    ContentRejectedError,
    ModelReply,
    ProviderError,
    ToolCall,
    is_moderation_rejection,
    register_moderation_markers,
)
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.providers.fallback import FallbackProvider
from markbot.kernel.retention import prune_logs
from markbot.kernel.session import COMPACTION_PREFIX
from markbot.schema.session_log import SessionLog

# ===================================================================== 事件与指标


def test_normalize_usage_across_providers():
    assert normalize_usage({"prompt_tokens": 3, "completion_tokens": 4}) == {
        "tokens_in": 3, "tokens_out": 4, "tokens_cached": 0,
    }
    assert normalize_usage({"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 2}) == {
        "tokens_in": 5, "tokens_out": 7, "tokens_cached": 2,
    }
    assert normalize_usage(None)["tokens_in"] == 0


def test_daily_log_event_writes_jsonl_and_metrics(tmp_path):
    logs = DailyLog(tmp_path)
    logs.event("provider", model="m", ok=True, latency_ms=10, tokens_in=100, tokens_out=20)
    logs.event("tool", tool="fetch_url", ok=False, latency_ms=5, error_class="blocked_by_site")
    logs.event("turn", stopped_reason="completed", duration_ms=1500, turn_kind="chat")
    rows = list(read_events(tmp_path, days=1))
    assert [row["kind"] for row in rows] == ["provider", "tool", "turn"]
    assert all("ts" in row for row in rows)
    snap = logs.metrics.snapshot()
    assert snap["tokens_today"] == 120
    assert snap["tool_failures"] == {"fetch_url": 1}
    assert snap["turns"] == {"completed": 1}
    text = logs.metrics.prometheus({"markbot_tasks_active": 2})
    assert 'markbot_tool_failures_total{tool="fetch_url"} 1' in text
    assert "markbot_tokens_today 120" in text
    assert "markbot_tasks_active 2" in text


def test_metrics_recover_today_tokens(tmp_path):
    DailyLog(tmp_path).event("provider", model="m", ok=True, tokens_in=40, tokens_out=2)
    assert Metrics(tmp_path).tokens_today() == 42


def test_events_disabled_still_counts(tmp_path):
    logs = DailyLog(tmp_path, enabled=False)
    logs.event("provider", model="m", ok=True, tokens_in=1, tokens_out=1)
    assert logs.metrics.tokens_today() == 2
    assert not events_path(tmp_path).exists()


def test_retention_prunes_old_event_files(tmp_path):
    today = date(2026, 11, 23)
    old = tmp_path / "logs" / "events" / f"{(today - timedelta(days=40)).isoformat()}.jsonl"
    old.parent.mkdir(parents=True)
    old.write_text("{}\n", encoding="utf-8")
    report = prune_logs(tmp_path, retention_days=30, today=today)
    assert any("events" in item for item in report.deleted)
    assert not old.exists()


def test_summarize_and_render(tmp_path):
    logs = DailyLog(tmp_path)
    logs.event("turn", stopped_reason="completed", turn_kind="cron", job="fb00", tokens_in=10, tokens_out=5)
    logs.event("turn", stopped_reason="error", turn_kind="chat")
    logs.event("tool", tool="fetch_url", ok=False, error_class="timeout")
    logs.event("tool", tool="fetch_url", ok=True)
    logs.event("provider", model="m", ok=True, tokens_in=10, tokens_out=5)
    report = summarize(tmp_path, days=1)
    day = next(iter(report["days"].values()))
    assert day["turns"] == 2 and day["failed"] == 1 and day["cron_turns"] == 1
    assert day["tool_calls"] == 2 and day["tool_failures"] == 1
    assert report["cron_jobs"]["fb00"]["runs"] == 1
    text = render_summary(report)
    assert "fetch_url 1/2" in text and "timeout" in text


def test_classify_tool_error():
    def result(content, **details):
        return SimpleNamespace(content=content, details=details)

    assert classify_tool_error(result("permission denied: x", denied=True)) == "denied"
    assert classify_tool_error(result("HTTP 403", blocked=True)) == "blocked_by_site"
    assert classify_tool_error(result("tool timeout: x")) == "timeout"
    assert classify_tool_error(result("boom")) == "failed"


def test_turn_emits_events_and_accumulates_usage(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[ToolCall(id="c1", name="echo", args={"text": "x"})],
                usage={"prompt_tokens": 100, "completion_tokens": 10},
            ),
            ModelReply(text="好了", usage={"prompt_tokens": 150, "completion_tokens": 20}),
        ]
    )
    kernel = make_kernel(provider=provider)
    result = asyncio.run(kernel.run("做事"))
    assert result.total_usage["tokens_in"] == 250
    assert result.total_usage["tokens_out"] == 30
    assert result.usage == {"prompt_tokens": 150, "completion_tokens": 20}  # 兼容旧字段
    rows = list(read_events(kernel.home, days=1))
    kinds = [row["kind"] for row in rows]
    assert kinds.count("provider") == 2 and kinds.count("tool") == 1 and kinds[-1] == "turn"
    turn = rows[-1]
    assert turn["stopped_reason"] == "completed" and turn["tool_calls"] == 1
    assert turn["tokens_in"] == 250 and turn["turn_kind"] == "chat"
    tool = next(row for row in rows if row["kind"] == "tool")
    assert tool["tool"] == "echo" and tool["ok"] is True
    # 事件里不出现对话正文
    assert "做事" not in json.dumps(rows, ensure_ascii=False)


def test_daily_token_budget_blocks_new_turns(make_kernel):
    kernel = make_kernel(["不会被调用"])
    kernel.config.agent.daily_token_budget = 100
    kernel.logs.metrics.today_tokens = 100
    result = asyncio.run(kernel.run("hi"))
    assert result.stopped_reason == "budget"
    assert "daily_token_budget" in (result.error or "")
    assert kernel.provider.calls == []


def test_cli_stats(tmp_path, monkeypatch, capsys):
    from markbot.cli.main import main

    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path))
    DailyLog(tmp_path).event("turn", stopped_reason="completed", turn_kind="chat")
    assert main(["stats", "--days", "2"]) == 0
    assert "回合" in capsys.readouterr().out
    assert main(["stats", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["days"]


# ===================================================================== 模型角色


def _cfg(tmp_path, body: str):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(body, encoding="utf-8")
    return load_config(home=home, project_root=tmp_path)


def test_role_inherits_main_gateway(tmp_path, monkeypatch):
    from markbot.kernel.providers import OpenAICompatProvider, build_role_providers

    monkeypatch.setenv("MAIN_KEY", "sk-main")
    cfg = _cfg(
        tmp_path,
        '[agent]\nprovider = "openai_compat"\nmodel = "big"\nbase_url = "http://gw.local/v1"\n'
        'api_key = "env:MAIN_KEY"\n[agent.roles.fast]\nmodel = "small"\n',
    )
    roles = build_role_providers(cfg)
    fast = roles["fast"]
    assert isinstance(fast, OpenAICompatProvider)
    assert fast.model == "small" and fast.base_url == "http://gw.local/v1"
    assert fast.api_key == "sk-main"


def test_roles_skipped_for_echo(tmp_path):
    from markbot.kernel.providers import build_role_providers

    cfg = _cfg(tmp_path, '[agent]\nprovider = "echo"\n[agent.roles.fast]\nmodel = "small"\n')
    assert build_role_providers(cfg) == {}


def test_compactor_uses_fast_role(make_kernel):
    fast = ScriptedProvider(["摘要"])
    kernel = make_kernel(["主模型"])
    kernel.role_providers["fast"] = fast
    kernel.reload()
    assert kernel.loop._compactor.provider is fast
    assert kernel.role_provider("fast") is fast
    assert kernel.role_provider("vision") is kernel.provider


def test_plaintext_role_key_is_detected():
    from markbot.kernel.config import plaintext_secret_fields

    assert plaintext_secret_fields({"agent": {"roles": {"fast": {"api_key": "sk"}}}}) == [
        "agent.roles.fast.api_key"
    ]


# ===================================================================== 审核


def test_custom_moderation_markers():
    try:
        assert not is_moderation_rejection('{"error":"blocked by gateway policy X1"}')
        register_moderation_markers(["Policy X1"])
        assert is_moderation_rejection('{"error":"blocked by gateway policy X1"}')
    finally:
        register_moderation_markers([])


class _AlwaysRejected:
    name = "always"

    def __init__(self):
        self.calls = 0

    async def complete(self, payload, on_delta=None):
        self.calls += 1
        raise ContentRejectedError("security reasons code 18")


class _RejectTrigger:
    """只有带触发词的请求被拒：历史消息单独探测都能通过，定位不到触发源。"""

    name = "trigger"

    async def complete(self, payload, on_delta=None):
        if "触发审核" in json.dumps(payload.get("messages") or [], ensure_ascii=False):
            raise ContentRejectedError("Inappropriate input/output rejected for security reasons")
        return ModelReply(text="ok")


def test_moderation_rotates_to_summarized_session(make_kernel):
    good = ScriptedProvider(["第一轮回答：靶点发现的三种方法"])
    kernel = make_kernel(provider=good)
    first = asyncio.run(kernel.run("介绍靶点发现", session_id="sess_mod_1"))
    assert first.stopped_reason == "completed"
    kernel.provider = _RejectTrigger()
    kernel.loop._provider = kernel.provider
    result = asyncio.run(kernel.run("触发审核的请求内容", session_id="sess_mod_1"))
    assert result.stopped_reason == "moderation"
    assert result.session_id and result.session_id != "sess_mod_1"
    fresh = kernel.get_session(result.session_id)
    seed = fresh.messages[0].content
    assert seed.startswith(COMPACTION_PREFIX)
    assert "介绍靶点发现" in seed and "第一轮回答" in seed
    assert "触发审核的请求内容" not in seed
    # 新会话已归档，可以从磁盘恢复
    assert SessionLog(kernel.home / "sessions" / result.session_id).trajectory()
    kernel.provider = ScriptedProvider(["新会话继续"])
    kernel.loop._provider = kernel.provider
    again = asyncio.run(kernel.run("换个说法", session_id=result.session_id))
    assert again.text == "新会话继续"


def test_moderation_rotation_can_be_disabled(make_kernel):
    kernel = make_kernel(provider=_AlwaysRejected())
    kernel.config.agent.moderation_new_session = False
    result = asyncio.run(kernel.run("hi", session_id="sess_mod_2"))
    assert result.stopped_reason == "moderation"
    assert result.session_id == "sess_mod_2"


# ===================================================================== 熔断告警


def test_circuit_open_callback_fires_once():
    class Down:
        name = "down"

        async def complete(self, payload, on_delta=None):
            raise ProviderError("503")

    opened: list = []
    provider = FallbackProvider(
        [("p", Down()), ("b", ScriptedProvider(["a"] * 5))], failure_threshold=2, cooldown_s=60
    )
    provider.on_circuit_open = lambda label, count: opened.append((label, count))
    for _ in range(4):
        asyncio.run(provider.complete({"messages": []}))
    assert opened == [("p", 2)]


# ===================================================================== 定时任务预检


def _policy_ctx(tmp_path, **perm):
    from markbot.kernel.config import PermissionConfig
    from markbot.kernel.permissions import PermissionPolicy
    from markbot.kernel.session import Session
    from markbot.kernel.tools import KernelToolContext

    policy = PermissionPolicy(PermissionConfig(**perm), root=tmp_path)
    return KernelToolContext(
        session=Session("t"), policy=policy, secrets={"CRAWL4AI_API_TOKEN": "x", "OTHER": "y"}
    )


def test_cron_preflight_flags_unregistered_secret(tmp_path):
    from markbot.cron.tool import preflight

    ctx = _policy_ctx(tmp_path, profile="trusted", allow_exec=["curl"])
    warnings = preflight(ctx, "用 crawl4ai 抓取，带上 CRAWL4AI_API_TOKEN 鉴权")
    assert any("CRAWL4AI_API_TOKEN" in item and "secret_env" in item for item in warnings)
    assert any("allow_exec" in item and "curl" in item for item in warnings)
    assert not any("OTHER" in item for item in warnings)


def test_cron_preflight_registered_secret_is_ok(tmp_path):
    from markbot.cron.tool import preflight

    ctx = _policy_ctx(
        tmp_path, profile="trusted", trusted_exec="all",
        secret_env={"CRAWL4AI_API_TOKEN": ["curl"]},
    )
    assert preflight(ctx, "带上 CRAWL4AI_API_TOKEN") == []


def test_cron_preflight_strict_and_standard(tmp_path):
    from markbot.cron.tool import preflight

    assert "strict" in preflight(_policy_ctx(tmp_path, profile="strict"), "x")[0]
    standard = preflight(_policy_ctx(tmp_path, profile="standard"), "x")
    assert any("写文件" in item for item in standard)


def test_cron_denial_notice_dedupes():
    from markbot.cron.runner import collect_denials, denial_notice
    from markbot.cron.types import CronJob, CronPayload, CronSchedule

    session = SimpleNamespace(
        messages=[
            SimpleNamespace(role="tool", content="permission denied: 密钥未允许注入: TOKEN（在 x 中登记）"),
            SimpleNamespace(role="tool", content="permission denied: 密钥未允许注入: TOKEN（在 x 中登记）"),
            SimpleNamespace(role="tool", content="正常结果"),
        ]
    )
    reasons = collect_denials(session)
    assert reasons == ["密钥未允许注入: TOKEN（在 x 中登记）"]
    job = CronJob(id="dn1", name="早报", enabled=True, schedule=CronSchedule(kind="every", every_ms=1),
                  payload=CronPayload(message="m"))
    first = denial_notice(job, reasons)
    assert "早报" in first and "allow_exec" in first
    assert denial_notice(job, reasons) == ""  # 原因没变不重复提示
    assert denial_notice(job, []) == ""
    assert denial_notice(job, reasons)  # 恢复正常后再次出现时重新提示


# ===================================================================== 飞书收件箱/发件箱


@pytest.fixture
def feishu(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "home"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "channels" / "feishu"))
    gateway_module = importlib.import_module("feishu_gateway")
    settings_module = importlib.import_module("feishu_settings")
    store_module = importlib.import_module("feishu_store")
    settings = settings_module.Settings(
        app_id="cli_test", app_secret="secret", verification_token="", host="127.0.0.1",
        port=8790, path="/feishu/event", markbot_url="http://127.0.0.1:8787",
        markbot_timeout_s=10.0, group_policy="open", reply_to_message=False,
        topic_isolation=True, react_emoji="", done_emoji="", allow_from=(),
        media_dir=tmp_path / "media", session_db=tmp_path / "feishu.db", workers=1,
    )
    store = store_module.Store(settings.session_db)
    gateway = gateway_module.Gateway(settings, store)
    return gateway, store, store_module


def _event(event_id: str, message_id: str = "om_1", chat_id: str = "oc_1") -> dict:
    return {
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1"},
        "event": {"message": {"message_id": message_id, "chat_id": chat_id}},
    }


def test_inbox_dedupes_and_tracks_state(feishu):
    gateway, store, _ = feishu
    assert gateway.enqueue(_event("ev1")) is True
    assert gateway.enqueue(_event("ev1")) is False
    row = store.inbox_get("event:ev1")
    assert row["state"] == "received"
    queued = gateway.queues[0].get_nowait()
    assert queued["_inbox"] == "event:ev1"
    store.inbox_mark("event:ev1", "processing")
    assert store.inbox_get("event:ev1")["attempts"] == 1


def test_recover_inbox(feishu, monkeypatch):
    gateway, store, _ = feishu
    sent: list = []
    monkeypatch.setattr(
        gateway,
        "_send",
        lambda chat_id, text, **_: (sent.append((chat_id, text)) or (True, "")),
    )
    store.inbox_add("event:a", json.dumps(_event("a", "om_a")))
    store.inbox_add("event:b", json.dumps(_event("b", "om_b")))
    store.inbox_mark("event:b", "processing")
    store.inbox_set_task("event:b", "task_1")
    store.inbox_add("event:c", json.dumps(_event("c", "om_c", "oc_c")))
    store.inbox_mark("event:c", "processing")
    store.inbox_mark("event:c", "processing")
    counts = gateway.recover_inbox()
    assert counts == {"replayed": 1, "handed_off": 1, "abandoned": 1}
    replayed = gateway.queues[0].get_nowait()
    assert replayed["_inbox"] == "event:a" and replayed["_replay"] is True
    assert store.inbox_get("event:b")["state"] == "handed_off"
    assert store.inbox_get("event:c")["state"] == "failed"
    assert sent and sent[0][0] == "oc_c"


def test_worker_marks_done_and_failed(feishu, monkeypatch):
    gateway, store, _ = feishu
    calls: list = []

    def process(data):
        calls.append(data.get("_inbox"))
        if data["header"]["event_id"] == "bad":
            raise RuntimeError("boom")

    monkeypatch.setattr(gateway, "process", process)
    gateway.enqueue(_event("good", "om_g"))
    gateway.enqueue(_event("bad", "om_b"))

    import threading

    thread = threading.Thread(target=gateway.worker, args=(0,), daemon=True)
    thread.start()
    gateway.queues[0].join()
    assert store.inbox_get("event:good")["state"] == "done"
    failed = store.inbox_get("event:bad")
    assert failed["state"] == "failed" and "boom" in failed["error"]


def test_notify_persists_and_retries(feishu, monkeypatch):
    gateway, store, _ = feishu
    results = [(False, "network down"), (True, "")]
    attempts: list = []

    def send(chat_id, text, **_):
        attempts.append(text)
        return results.pop(0)

    monkeypatch.setattr(gateway, "_send", send)
    assert gateway.notify("oc_1", "定时任务结果") is True
    assert store.outbox_counts() == {"pending": 1}
    # 模拟退避时间已到
    store._conn.execute("UPDATE outbox SET next_at = 0")
    store._conn.commit()
    due = store.outbox_due()
    assert len(due) == 1
    ok, error = gateway._send(due[0]["chat_id"], due[0]["text"])
    assert store.outbox_result(due[0]["id"], ok, error=error) == "sent"
    assert attempts == ["定时任务结果", "定时任务结果"]


def test_outbox_gives_up_after_max_attempts(feishu):
    _, store, _ = feishu
    item = store.outbox_add("oc_1", "x")
    states = [store.outbox_result(item, False, error="e", max_attempts=3) for _ in range(3)]
    assert states == ["pending", "pending", "dead"]


def test_prune_keeps_unfinished_inbox(feishu):
    _, store, _ = feishu
    store.inbox_add("event:old_done", "{}")
    store.inbox_mark("event:old_done", "done")
    store.inbox_add("event:old_open", "{}")
    store._conn.execute("UPDATE inbox SET updated_at = 0")
    store._conn.commit()
    store.prune_seen(7)
    assert store.inbox_get("event:old_done") is None
    assert store.inbox_get("event:old_open") is not None
