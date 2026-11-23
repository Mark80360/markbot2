"""Phase 1 可靠性（ADR 0010）的回归测试：回合取消、异步任务、组件守护、定时任务、主备模型、
服务安装文件、飞书异步任务客户端。"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from markbot.api import Tool, ToolResult
from markbot.cron.service import CronService
from markbot.cron.types import CronJob, CronPayload, CronSchedule, now_ms
from markbot.kernel.providers.base import (
    ContentRejectedError,
    ModelReply,
    ProviderError,
    ToolCall,
)
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.providers.fallback import FallbackProvider
from markbot.schema.session_log import SessionLog
from markbot.server.supervisor import Supervisor
from markbot.server.tasks import TaskManager

# ===================================================================== 回合取消


class _Empty(BaseModel):
    pass


def _slow_kernel(make_kernel, seconds: float = 5.0):
    async def slow(ctx, args):
        await asyncio.sleep(seconds)
        return ToolResult(content="late")

    provider = ScriptedProvider(
        [ModelReply(tool_calls=[ToolCall(id="c1", name="slow_async", args={})]), "不会到这里"]
    )
    kernel = make_kernel(provider=provider)
    kernel.runtime.tools.register(
        Tool(name="slow_async", description="慢", params=_Empty, handler=slow, timeout_s=30)
    )
    return kernel


def _run_in_thread(kernel, prompt: str, session_id: str):
    box: dict = {}

    def target() -> None:
        box["result"] = asyncio.run(kernel.run(prompt, session_id=session_id))

    thread = threading.Thread(target=target)
    thread.start()
    return thread, box


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_cancel_stops_running_turn_and_keeps_pairing(make_kernel):
    kernel = _slow_kernel(make_kernel)
    thread, box = _run_in_thread(kernel, "跑一个慢工具", "sess_cancel_1")
    assert _wait_for(lambda: kernel.turn_status("sess_cancel_1").get("current_tool") == "slow_async")
    status = kernel.turn_status("sess_cancel_1")
    assert status["running"] is True and status["elapsed_s"] >= 0
    started = time.monotonic()
    assert kernel.cancel("sess_cancel_1") is True
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 3
    result = box["result"]
    assert result.stopped_reason == "cancelled"
    rows = SessionLog(kernel.home / "sessions" / "sess_cancel_1").trajectory()
    roles = [row["role"] for row in rows]
    assert roles == ["user", "assistant", "tool"]  # 调用与结果仍然配对
    assert "中断" in rows[-1]["content"]
    assert kernel.turn_status("sess_cancel_1")["running"] is False


def test_cancel_without_running_turn_returns_false(make_kernel):
    kernel = make_kernel(["hi"])
    assert kernel.cancel("nope") is False


def test_session_usable_after_cancel(make_kernel):
    kernel = _slow_kernel(make_kernel)
    thread, _ = _run_in_thread(kernel, "慢", "sess_cancel_2")
    assert _wait_for(lambda: kernel.is_running("sess_cancel_2"))
    time.sleep(0.1)
    kernel.cancel("sess_cancel_2")
    thread.join(timeout=5)
    kernel.provider.replies = ["继续可以用"]
    again = asyncio.run(kernel.run("还在吗", session_id="sess_cancel_2"))
    assert again.text == "继续可以用"


# ===================================================================== 异步任务


class _Result(SimpleNamespace):
    pass


def _manager(tmp_path, runner, **kwargs):
    delivered: list = []

    def deliver(notify, text):
        delivered.append((dict(notify), text))
        return True

    manager = TaskManager(tmp_path / "home", runner, deliver=deliver, **kwargs)
    return manager, delivered


def _wait_state(manager, task_id, states=("completed", "failed", "cancelled"), timeout=5):
    assert _wait_for(lambda: manager.get(task_id).state in states, timeout), manager.get(task_id)


def test_task_streams_events_and_completes(tmp_path):
    def runner(record, args, on_delta, on_event):
        on_delta("第一段")
        on_event(SimpleNamespace(type="assistant_message", data={"tool_calls": [{"name": "echo"}]}))
        on_delta("第二段")
        return _Result(text="全部完成", stopped_reason="completed", error=None,
                       session_id=record.session_id)

    manager, delivered = _manager(tmp_path, runner)
    record = manager.submit(prompt="做事", session_id="sess_t1",
                            notify={"url": "http://127.0.0.1:1/notify", "chat_id": "oc_1"})
    collected: list = []
    after = 0
    for _ in range(20):
        payload = manager.events(record.task_id, after=after, wait_s=1)
        collected.extend(payload["events"])
        after = payload["next"]
        if any(event["type"] == "done" for event in collected):
            break
    kinds = [event["type"] for event in collected]
    assert "delta" in kinds and "tool" in kinds and kinds[-1] == "done"
    assert [event["seq"] for event in collected] == sorted(event["seq"] for event in collected)
    assert collected[-1]["text"] == "全部完成"
    assert manager.get(record.task_id).tool_calls == 1
    # 客户端一直在轮询：不主动推送
    assert delivered == []
    saved = (tmp_path / "home" / "tasks" / f"{record.task_id}.json").read_text(encoding="utf-8")
    assert "全部完成" in saved


def test_detached_task_pushes_result(tmp_path):
    gate = threading.Event()

    def runner(record, args, on_delta, on_event):
        gate.wait(5)
        return _Result(text="后台结果", stopped_reason="completed", error=None,
                       session_id=record.session_id)

    manager, delivered = _manager(tmp_path, runner)
    record = manager.submit(prompt="长任务", session_id="s",
                            notify={"url": "http://127.0.0.1:1/notify", "chat_id": "oc_9"})
    manager.detach(record.task_id)
    gate.set()
    _wait_state(manager, record.task_id)
    assert _wait_for(lambda: delivered)
    notify, text = delivered[0]
    assert notify["chat_id"] == "oc_9"
    assert "后台结果" in text


def test_unclaimed_task_pushes_result(tmp_path, monkeypatch):
    import markbot.server.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "UNCLAIMED_AFTER_S", 0.0)

    def runner(record, args, on_delta, on_event):
        time.sleep(0.1)
        return _Result(text="没人等也要送到", stopped_reason="completed", error=None,
                       session_id=record.session_id)

    manager, delivered = _manager(tmp_path, runner)
    record = manager.submit(prompt="x", session_id="s",
                            notify={"url": "http://127.0.0.1:1/notify", "chat_id": "oc_2"})
    _wait_state(manager, record.task_id)
    assert _wait_for(lambda: delivered)


def test_detach_after_completion_returns_result_without_push(tmp_path):
    def runner(record, args, on_delta, on_event):
        return _Result(text="很快", stopped_reason="completed", error=None,
                       session_id=record.session_id)

    manager, delivered = _manager(tmp_path, runner)
    record = manager.submit(prompt="x", session_id="s",
                            notify={"url": "http://127.0.0.1:1/notify", "chat_id": "oc"})
    _wait_state(manager, record.task_id)
    detached = manager.detach(record.task_id)
    assert detached.state == "completed" and detached.text == "很快"
    assert delivered == []


def test_task_cancel_calls_kernel(tmp_path):
    started = threading.Event()
    release = threading.Event()
    cancelled: list[str] = []

    def runner(record, args, on_delta, on_event):
        started.set()
        release.wait(5)
        return _Result(text="", stopped_reason="cancelled", error="已被用户取消",
                       session_id=record.session_id)

    def cancel(session_id):
        cancelled.append(session_id)
        release.set()
        return True

    manager, _ = _manager(tmp_path, runner, cancel=cancel)
    record = manager.submit(prompt="x", session_id="sess_c")
    assert started.wait(5)
    assert manager.cancel_session("sess_c") == [record.task_id]
    _wait_state(manager, record.task_id)
    assert cancelled == ["sess_c"]
    assert manager.get(record.task_id).state == "cancelled"


def test_runner_exception_marks_failed(tmp_path):
    def runner(record, args, on_delta, on_event):
        raise RuntimeError("boom")

    manager, _ = _manager(tmp_path, runner)
    record = manager.submit(prompt="x", session_id="s")
    _wait_state(manager, record.task_id)
    assert manager.get(record.task_id).state == "failed"
    assert "boom" in manager.get(record.task_id).error


def test_restart_marks_running_tasks_interrupted(tmp_path):
    block = threading.Event()

    def runner(record, args, on_delta, on_event):
        block.wait(10)
        return _Result(text="", stopped_reason="completed", error=None, session_id="s")

    first, _ = _manager(tmp_path, runner)
    record = first.submit(prompt="重启前的任务", session_id="s",
                          notify={"url": "http://127.0.0.1:1/notify", "chat_id": "oc_r"})
    assert _wait_for(lambda: first.get(record.task_id).state == "running")
    # 模拟进程重启：新的管理器从磁盘恢复
    second, delivered = _manager(tmp_path, runner)
    recovered = second.get(record.task_id)
    assert recovered.state == "interrupted"
    assert second.notify_recovered() == 1
    assert "/retry" in delivered[0][1]
    block.set()


def test_events_for_unknown_task(tmp_path):
    manager, _ = _manager(tmp_path, lambda *a: None)
    assert manager.events("task_missing") is None


def test_parse_chat_request_validation():
    from markbot.server.http import parse_chat_request

    ok, error = parse_chat_request({"prompt": "hi", "grants": ["curl", "rm -rf"]})
    assert error is None and ok["grants"] == ["curl"] and ok["dropped"] == ["rm -rf"]
    assert parse_chat_request({"prompt": ""})[1] == "prompt is required"
    assert parse_chat_request({"prompt": "x", "session_id": "../../etc"})[1] == "invalid session_id"
    bad = parse_chat_request({"prompt": "x", "notify_hook": {"url": "http://evil.example/n"}})
    assert bad[1] and "local" in bad[1]
    assert parse_chat_request("not a dict")[1] == "invalid json"


# ===================================================================== 组件守护


def test_supervisor_restarts_crashing_component():
    calls: list[int] = []
    crashes: list[str] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("crash")
        threading.Event().wait(10)

    supervisor = Supervisor(on_crash=lambda name, reason: crashes.append(name))
    supervisor.spawn("flaky", flaky, backoff_s=0.05)
    assert _wait_for(lambda: len(calls) >= 3)
    status = supervisor.status()["flaky"]
    assert status["restarts"] == 2 and status["alive"] is True
    assert status["state"] == "degraded"
    assert crashes == ["flaky", "flaky"]
    supervisor.stop()


def test_supervisor_watch_restarts_dead_component():
    state = {"alive": False, "restarts": 0}

    def restart() -> None:
        state["restarts"] += 1
        state["alive"] = True

    supervisor = Supervisor()
    supervisor.watch("cron", lambda: state["alive"], restart)
    assert supervisor.overall() == "down"
    supervisor.check_watched()
    assert state["restarts"] == 1
    assert supervisor.status()["cron"]["alive"] is True


# ===================================================================== 定时任务


def _job(job_id: str, *, every_s: int = 3600, next_at: int | None = None) -> CronJob:
    job = CronJob(
        id=job_id,
        name=f"任务{job_id}",
        enabled=True,
        schedule=CronSchedule(kind="every", every_ms=every_s * 1000),
        payload=CronPayload(message="做点事", delivery="channel",
                            notify={"url": "http://127.0.0.1:1/notify", "chat_id": "oc"}),
    )
    job.state.next_run_at_ms = next_at
    return job


def _service(tmp_path, on_job, **kwargs) -> CronService:
    service = CronService(tmp_path / "cron" / "jobs.json", on_job=on_job, **kwargs)
    return service


def test_due_jobs_run_concurrently(tmp_path):
    async def on_job(job):
        await asyncio.sleep(0.5)
        return "ok"

    service = _service(tmp_path, on_job, max_concurrent=2)
    now = now_ms()
    service._save([_job("a", next_at=now - 10), _job("b", next_at=now - 10)])

    async def main():
        started = time.monotonic()
        await service._tick()
        assert sorted(service.running_jobs()) == ["a", "b"]
        await asyncio.gather(*list(service._inflight.values()))
        return time.monotonic() - started

    elapsed = asyncio.run(main())
    assert elapsed < 0.95  # 串行需要 1s 以上
    jobs = {job.id: job for job in service.list_jobs()}
    assert all(job.state.last_status == "ok" for job in jobs.values())
    assert all(job.state.next_run_at_ms > now for job in jobs.values())


def test_job_timeout_is_recorded(tmp_path):
    async def on_job(job):
        await asyncio.sleep(5)
        return "late"

    service = _service(tmp_path, on_job, job_timeout_s=0.2, max_retries=0)
    job = _job("t", next_at=now_ms())
    service._save([job])
    run = asyncio.run(service._execute(job))
    assert run.status == "error" and "超时" in run.error


def test_failed_job_retries_then_alerts(tmp_path):
    alerts: list = []

    async def on_job(job):
        raise RuntimeError("upstream down")

    service = _service(
        tmp_path, on_job, max_retries=1, retry_backoff_s=60, alert_after_failures=2,
        on_failure=lambda job, failures, error: alerts.append((job.id, failures)),
    )
    job = _job("f", next_at=now_ms())
    service._save([job])
    asyncio.run(service._execute(job))
    first = service.get_job("f")
    # 第一次失败：退避后重试，而不是等到下一个周期（1 小时后）
    assert first.state.next_run_at_ms - now_ms() < 120_000
    assert alerts == []
    asyncio.run(service._execute(first))
    second = service.get_job("f")
    assert second.state.next_run_at_ms - now_ms() > 3000_000  # 重试用尽，回到正常周期
    assert alerts == [("f", 2)]


def test_misfire_within_grace_runs_once(tmp_path):
    service = _service(tmp_path, None, misfire_policy="run_once", misfire_grace_s=6 * 3600)
    due = now_ms() - 3600 * 1000
    service._save([_job("m", next_at=due)])
    service._resume_jobs()
    assert service.get_job("m").state.next_run_at_ms == due  # 保留，启动后立即补跑一次


def test_misfire_beyond_grace_is_skipped(tmp_path):
    service = _service(tmp_path, None, misfire_policy="run_once", misfire_grace_s=60)
    service._save([_job("m", next_at=now_ms() - 10 * 3600 * 1000)])
    service._resume_jobs()
    job = service.get_job("m")
    assert job.state.next_run_at_ms > now_ms()
    assert job.state.last_status == "skipped"


def test_misfire_skip_policy(tmp_path):
    service = _service(tmp_path, None, misfire_policy="skip")
    service._save([_job("m", next_at=now_ms() - 60 * 1000)])
    service._resume_jobs()
    assert service.get_job("m").state.next_run_at_ms > now_ms()


def test_cron_runner_cancels_kernel_turn_on_timeout():
    from markbot.cron.runner import build_job_runner

    cancelled: list[str] = []
    release = threading.Event()

    class FakeKernel:
        home = None

        async def run(self, prompt, session_id=None, **kwargs):
            await asyncio.to_thread(release.wait, 5)
            return SimpleNamespace(error=None, text="x")

        def cancel(self, session_id):
            cancelled.append(session_id)
            release.set()
            return True

        def forget_session(self, session_id):
            return True

    on_job = build_job_runner(FakeKernel(), run_lock=threading.Lock())

    async def main():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(on_job(_job("k")), timeout=0.2)

    asyncio.run(main())
    assert cancelled and cancelled[0].startswith("cron_k_")


# ===================================================================== 主备模型


class _Failing:
    def __init__(self, name, exc):
        self.name = name
        self.exc = exc
        self.calls = 0

    async def complete(self, payload, on_delta=None):
        self.calls += 1
        raise self.exc


def test_fallback_switches_to_backup():
    primary = _Failing("primary", ProviderError("HTTP 503"))
    backup = ScriptedProvider(["备用模型回答"])
    provider = FallbackProvider([("p", primary), ("b", backup)])
    reply = asyncio.run(provider.complete({"messages": []}))
    assert reply.text == "备用模型回答"
    assert provider.last_used == "b"
    assert provider.primary is primary


def test_fallback_circuit_breaker_skips_failed_primary():
    primary = _Failing("primary", ProviderError("down"))
    backup = ScriptedProvider(["a", "b", "c", "d"])
    provider = FallbackProvider([("p", primary), ("b", backup)], failure_threshold=2, cooldown_s=60)
    for _ in range(4):
        asyncio.run(provider.complete({"messages": []}))
    assert primary.calls == 2  # 熔断后不再调用
    assert provider.status()[0]["open_for_s"] > 0


def test_fallback_all_rejected_raises_content_rejected():
    provider = FallbackProvider(
        [
            ("p", _Failing("p", ContentRejectedError("rejected"))),
            ("b", _Failing("b", ContentRejectedError("rejected too"))),
        ]
    )
    with pytest.raises(ContentRejectedError):
        asyncio.run(provider.complete({"messages": []}))


def test_fallback_does_not_replay_after_partial_stream():
    class Partial:
        name = "partial"

        async def complete(self, payload, on_delta=None):
            on_delta("已经输出了一半")
            raise ProviderError("连接中断")

    backup = ScriptedProvider(["不应被调用"])
    provider = FallbackProvider([("p", Partial()), ("b", backup)])
    with pytest.raises(ProviderError):
        asyncio.run(provider.complete({"messages": []}, on_delta=lambda chunk: None))
    assert backup.calls == []


def test_build_provider_with_fallbacks(tmp_path, monkeypatch):
    from markbot.kernel.config import load_config
    from markbot.kernel.providers import build_provider

    monkeypatch.setenv("BACKUP_KEY", "sk-backup")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[agent]\nprovider = "openai_compat"\nmodel = "main-model"\napi_key = "env:MAIN_KEY"\n'
        '[[agent.fallbacks]]\nmodel = "backup-model"\nbase_url = "https://backup.example/v1"\n'
        'api_key = "env:BACKUP_KEY"\n'
        '[[agent.fallbacks]]\nmodel = "no-key-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MAIN_KEY", "sk-main")
    cfg = load_config(home=home, project_root=tmp_path)
    provider = build_provider(cfg)
    assert isinstance(provider, FallbackProvider)
    assert provider.labels[1] == "openai_compat:backup-model"
    assert len(provider.labels) == 2  # 缺 key 的备用模型被忽略


def test_plaintext_fallback_key_is_detected():
    from markbot.kernel.config import plaintext_secret_fields

    fields = plaintext_secret_fields({"agent": {"fallbacks": [{"api_key": "sk-plain"}]}})
    assert fields == ["agent.fallbacks[0].api_key"]


# ===================================================================== 服务安装


def test_systemd_unit_content():
    from markbot.cli.service import systemd_unit

    unit = systemd_unit(python="/usr/bin/python3", home=Path("/h/.markbot"), workdir=Path("/repo"))
    assert "ExecStart=/usr/bin/python3 -m markbot gateway run" in unit
    assert "Restart=always" in unit
    assert "WatchdogSec=" in unit and "NotifyAccess=all" in unit
    assert "Environment=MARKBOT_HOME=/h/.markbot" in unit


def test_windows_task_xml_content(tmp_path):
    from markbot.cli.service import windows_launcher, windows_task_xml

    xml = windows_task_xml(
        pythonw="C:/Python/pythonw.exe", launcher=tmp_path / "l.pyw", workdir=tmp_path, user="PC\\me"
    )
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<Interval>PT5M</Interval>" in xml
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml
    launcher = windows_launcher(home=tmp_path / "home", workdir=tmp_path)
    compile(launcher, "launcher.pyw", "exec")
    assert '"gateway", "run"' in launcher


def test_service_dry_run_does_not_write(tmp_path, monkeypatch, capsys):
    from markbot.cli import service

    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    service.install(["--dry-run"])
    out = capsys.readouterr().out
    assert "markbot" in out.lower()
    assert not (tmp_path / "xdg").exists()
    assert not (tmp_path / "home" / "service").exists()


# ===================================================================== 飞书客户端


@pytest.fixture
def feishu(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "channels" / "feishu"))
    return importlib.import_module("feishu_gateway")


def _gateway(feishu, responses):
    gateway = object.__new__(feishu.Gateway)
    gateway._settings = SimpleNamespace(markbot_timeout_s=60, markbot_url="http://127.0.0.1:8787")
    mapped: dict = {}
    gateway._store = SimpleNamespace(set_session=lambda key, sid: mapped.__setitem__(key, sid))
    gateway.auth_token = "t"
    calls: list = []

    def call(method, path, payload=None, *, timeout=15):
        calls.append((method, path))
        return responses.pop(0)

    gateway._markbot_call = call
    return gateway, calls, mapped


def test_feishu_run_task_streams_and_returns(feishu):
    responses = [
        (202, {"task_id": "task_1", "session_id": "sess_9"}),
        (200, {"events": [{"seq": 1, "type": "delta", "text": "你好"}], "next": 1}),
        (200, {"events": [{"seq": 2, "type": "done", "text": "你好世界", "stopped_reason": "completed",
                           "session_id": "sess_9"}], "next": 2}),
    ]
    gateway, calls, mapped = _gateway(feishu, responses)
    deltas: list[str] = []
    result = gateway._run_task({"prompt": "hi"}, chat_key="chat_a", on_delta=deltas.append)
    assert result["text"] == "你好世界"
    assert deltas == ["你好"]
    assert mapped == {"chat_a": "sess_9"}  # 提交即建立会话映射
    assert calls[1][1].startswith("/tasks/task_1/events?after=0")
    assert calls[2][1].startswith("/tasks/task_1/events?after=1")


def test_feishu_run_task_unsupported_server(feishu):
    gateway, _, _ = _gateway(feishu, [(404, {"error": "not found"})])
    with pytest.raises(feishu._TasksUnsupported):
        gateway._run_task({"prompt": "hi"}, chat_key="c", on_delta=None)


def test_feishu_detach_returns_background_notice(feishu):
    gateway, _, _ = _gateway(feishu, [(200, {"state": "running"})])
    result = gateway._detach_task("task_1", "sess_1")
    assert result["stopped_reason"] == "detached"
    assert "/status" in result["text"]
    gateway, _, _ = _gateway(feishu, [(200, {"state": "completed", "text": "刚好完成"})])
    assert gateway._detach_task("task_1", "sess_1")["text"] == "刚好完成"


def test_feishu_format_status(feishu):
    text = feishu._format_status(
        {"running": True, "steps": 3, "tool_calls": 5, "elapsed_s": 12.5, "current_tool": "fetch_url",
         "tasks": [{"state": "running", "prompt_preview": "早报", "elapsed_s": 12.5}]}
    )
    assert "第 3 步" in text and "fetch_url" in text and "早报" in text
    assert "没有正在执行" in feishu._format_status({"running": False})
