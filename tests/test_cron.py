from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from markbot.api.tool import ToolResult
from markbot.cron.runner import PROMPT_TEMPLATE, build_job_runner
from markbot.cron.service import CronError, CronService, CronStoreError, resolve_timezone
from markbot.cron.tool import CronIn, build_cron_tool
from markbot.cron.types import CronSchedule, now_ms


@pytest.fixture
def service(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "cron" / "jobs.json")


def _ctx(notify: dict | None = None, session_id: str = "s1", cron: bool = False) -> SimpleNamespace:
    metadata = {}
    if notify:
        metadata["notify"] = notify
    if cron:
        metadata["cron"] = {"job_id": "other"}
    return SimpleNamespace(session=SimpleNamespace(id=session_id, metadata=metadata))


def _tool(service: CronService):
    return build_cron_tool(lambda: service)


def _call(tool, ctx, **kwargs) -> ToolResult:
    return asyncio.run(tool.handler(ctx, CronIn(**kwargs)))


def test_add_list_toggle_remove(service: CronService) -> None:
    job = service.add_job(
        name="巡检",
        message="检查构建",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        session_id="s1",
    )
    assert job.enabled
    assert job.state.next_run_at_ms and job.state.next_run_at_ms > now_ms()
    assert service.store_path.is_file()
    assert [item.id for item in service.list_jobs()] == [job.id]

    service.set_enabled(job.id, False)
    assert service.list_jobs(include_disabled=False) == []
    assert len(service.list_jobs(include_disabled=True)) == 1

    assert service.remove_job(job.id)
    assert service.list_jobs() == []
    assert not service.remove_job(job.id)


def test_cron_expression_computes_next_run(service: CronService) -> None:
    pytest.importorskip("croniter")
    job = service.add_job(
        name="每天九点",
        message="汇总",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *"),
    )
    assert job.state.next_run_at_ms and job.state.next_run_at_ms > now_ms()


def test_at_in_the_past_is_rejected(service: CronService) -> None:
    with pytest.raises(CronError):
        service.add_job(
            name="过期",
            message="x",
            schedule=CronSchedule(kind="at", at_ms=now_ms() - 1000),
        )


def test_tool_add_with_channel_delivery(service: CronService) -> None:
    notify = {"url": "http://127.0.0.1:1/notify", "chat_id": "oc_1"}
    result = _call(
        _tool(service),
        _ctx(notify),
        action="add",
        message="每天提醒我喝水",
        every_seconds=3600,
    )
    assert not result.is_error, result.content
    job = service.list_jobs()[0]
    assert job.payload.delivery == "channel"
    assert job.payload.notify == notify
    assert job.payload.session_id == "s1"


def test_tool_channel_without_hook_is_rejected(service: CronService) -> None:
    result = _call(
        _tool(service),
        _ctx(),
        action="add",
        message="提醒",
        every_seconds=60,
        delivery="channel",
    )
    assert result.is_error
    assert "渠道" in result.content


def test_tool_cannot_create_cron_inside_cron(service: CronService) -> None:
    result = _call(
        _tool(service),
        _ctx(cron=True),
        action="add",
        message="嵌套",
        every_seconds=60,
    )
    assert result.is_error
    assert "定时任务内" in result.content


def test_run_updates_history_and_writes_record(service: CronService) -> None:
    async def on_job(job) -> str:
        return "检查完成，一切正常"

    service.on_job = on_job
    job = service.add_job(
        name="周期任务",
        message="跑一次",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
    )
    record = asyncio.run(service.run_job_once(job.id))
    assert record.status == "ok"
    assert record.response.startswith("检查完成")
    updated = service.get_job(job.id)
    assert updated is not None
    assert updated.state.last_status == "ok"
    assert updated.state.history[0].run_id == record.run_id
    run_file = service.store_dir / "runs" / job.id / f"{record.run_id}.json"
    assert json.loads(run_file.read_text())["run"]["response"].startswith("检查完成")
    stored = json.loads(service.store_path.read_text())["jobs"][0]["state"]["history"]
    assert "response" not in stored[0]


def test_history_keeps_last_three_without_response(service: CronService) -> None:
    async def on_job(job) -> str:
        return "很长的执行结果" * 200

    service.on_job = on_job
    job = service.add_job(
        name="只留三次",
        message="跑",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
    )
    records = [asyncio.run(service.run_job_once(job.id)) for _ in range(5)]
    updated = service.get_job(job.id)
    assert updated is not None
    assert [run.run_id for run in updated.state.history] == [
        run.run_id for run in reversed(records[-3:])
    ]
    stored = json.loads(service.store_path.read_text())["jobs"][0]["state"]["history"]
    assert len(stored) == 3
    assert all("response" not in item for item in stored)


def test_runner_reuses_kernel_and_delivers_to_channel(service: CronService) -> None:
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args) -> None:  # noqa: D102
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        calls: list[dict] = []

        class FakeKernel:
            async def run(self, request, **kwargs):
                calls.append({"request": request, **kwargs})
                return SimpleNamespace(error="", text="到点提醒：喝水")

        service.on_job = build_job_runner(FakeKernel(), run_lock=threading.Lock())
        job = service.add_job(
            name="喝水",
            message="提醒用户喝水",
            schedule=CronSchedule(kind="at", at_ms=now_ms() + 60_000),
            session_id="chat-session",
            delivery="channel",
            notify={
                "url": f"http://127.0.0.1:{server.server_port}/notify",
                "chat_id": "oc_1",
            },
        )
        record = asyncio.run(service.run_job_once(job.id))
    finally:
        server.shutdown()

    assert record.status == "ok"
    # 每次执行独立会话，不写进创建任务的聊天；来源会话记在元数据里
    assert calls and calls[0]["session_id"].startswith(f"cron_{job.id}_")
    assert calls[0]["session_id"] != "chat-session"
    assert calls[0]["new_session"] is True and calls[0]["end_previous"] is False
    assert calls[0]["metadata"]["cron"]["origin_session"] == "chat-session"
    assert calls[0]["metadata"]["cron"]["job_id"] == job.id
    assert "提醒用户喝水" in calls[0]["request"]
    assert received == [{"chat_id": "oc_1", "text": "到点提醒：喝水"}]


def test_runner_prompt_has_no_silent_escape(service: CronService) -> None:
    assert "无需汇报" not in PROMPT_TEMPLATE


def test_runner_always_delivers_result(service: CronService) -> None:
    class FakeKernel:
        async def run(self, request, **kwargs):
            return SimpleNamespace(error="", text="本次没有新增热点")

    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args) -> None:  # noqa: D102
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        service.on_job = build_job_runner(FakeKernel(), run_lock=threading.Lock())
        job = service.add_job(
            name="静默",
            message="检查",
            schedule=CronSchedule(kind="at", at_ms=now_ms() + 60_000),
            delivery="channel",
            notify={
                "url": f"http://127.0.0.1:{server.server_port}/notify",
                "chat_id": "oc_1",
            },
        )
        record = asyncio.run(service.run_job_once(job.id))
    finally:
        server.shutdown()
    assert record.status == "ok"
    assert received == [{"chat_id": "oc_1", "text": "本次没有新增热点"}]


def test_tool_list_empty_and_with_jobs(service: CronService) -> None:
    tool = _tool(service)
    ctx = _ctx()

    empty = _call(tool, ctx, action="list")
    assert not empty.is_error
    assert "没有定时任务" in empty.content

    job = service.add_job(
        name="日报",
        message="汇总",
        schedule=CronSchedule(kind="every", every_ms=60_000),
    )
    listed = _call(tool, ctx, action="list")
    assert job.id in listed.content
    assert "日报" in listed.content
    assert "每 60 秒" in listed.content


def test_tool_enable_disable_remove_and_missing_ids(service: CronService) -> None:
    tool = _tool(service)
    ctx = _ctx()
    job = service.add_job(
        name="巡检",
        message="检查",
        schedule=CronSchedule(kind="every", every_ms=60_000),
    )

    disabled = _call(tool, ctx, action="disable", job_id=job.id)
    assert not disabled.is_error and "已停用" in disabled.content
    assert _call(tool, ctx, action="list").content == "（没有定时任务）"

    enabled = _call(tool, ctx, action="enable", job_id=job.id)
    assert not enabled.is_error and "已启用" in enabled.content

    removed = _call(tool, ctx, action="remove", job_id=job.id)
    assert not removed.is_error and "已删除" in removed.content

    missing = _call(tool, ctx, action="remove", job_id=job.id)
    assert missing.is_error and "找不到定时任务" in missing.content

    no_id = _call(tool, ctx, action="remove")
    assert no_id.is_error and "需要 job_id" in no_id.content


def test_tool_update_changes_message_and_schedule(service: CronService) -> None:
    tool = _tool(service)
    ctx = _ctx()
    job = service.add_job(
        name="旧名字",
        message="旧内容",
        schedule=CronSchedule(kind="every", every_ms=60_000),
    )

    result = _call(
        tool,
        ctx,
        action="update",
        job_id=job.id,
        name="新名字",
        message="新内容",
        every_seconds=120,
    )
    assert not result.is_error, result.content
    updated = service.get_job(job.id)
    assert updated.name == "新名字"
    assert updated.payload.message == "新内容"
    assert updated.schedule.every_ms == 120_000


def test_tool_history_and_immediate_run(service: CronService) -> None:
    async def on_job(job) -> str:
        return "执行结果" * 60

    service.on_job = on_job
    tool = _tool(service)
    ctx = _ctx()
    job = service.add_job(
        name="任务",
        message="跑",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
    )

    empty = _call(tool, ctx, action="history", job_id=job.id)
    assert "还没有运行记录" in empty.content

    asyncio.run(service.run_job_once(job.id))
    history = _call(tool, ctx, action="history", job_id=job.id, limit=3)
    assert "最近 1 次运行" in history.content
    assert "ok" in history.content
    assert "结果:" not in history.content

    triggered = _call(tool, ctx, action="run", job_id=job.id)
    assert not triggered.is_error
    assert "已触发任务" in triggered.content
    assert service.get_job(job.id).state.next_run_at_ms <= now_ms()


def test_tool_rejects_invalid_schedule(service: CronService) -> None:
    tool = _tool(service)
    ctx = _ctx()

    multiple = _call(tool, ctx, action="add", message="x", every_seconds=60, at="2030-01-01")
    assert multiple.is_error and "只选一个" in multiple.content

    zero = _call(tool, ctx, action="add", message="x", every_seconds=0)
    assert zero.is_error and "大于 0" in zero.content

    bad_at = _call(tool, ctx, action="add", message="x", at="not-a-date")
    assert bad_at.is_error and "无法解析时间" in bad_at.content

    past = _call(tool, ctx, action="add", message="x", at="2000-01-01 00:00")
    assert past.is_error and "已经过去" in past.content


def test_tool_without_service_reports_clear_error() -> None:
    tool = build_cron_tool(lambda: None)
    result = asyncio.run(tool.handler(_ctx(), CronIn(action="list")))
    assert result.is_error
    assert "定时任务服务未运行" in result.content


def test_resolve_timezone_falls_back_and_validates() -> None:
    assert resolve_timezone(None) is not None
    assert str(resolve_timezone("UTC")) == "UTC"
    with pytest.raises(CronError, match="未知时区"):
        resolve_timezone("Mars/Olympus")


def test_service_reports_corrupt_store_and_accepts_both_layouts(tmp_path: Path) -> None:
    path = tmp_path / "cron" / "jobs.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    service = CronService(path)
    with pytest.raises(CronStoreError):
        service.list_jobs()

    job = CronService(tmp_path / "other.json").add_job(
        name="a", message="m", schedule=CronSchedule(kind="every", every_ms=60_000)
    )
    path.write_text(json.dumps([job.to_dict()]), encoding="utf-8")
    assert [item.id for item in service.list_jobs()] == [job.id]

    path.write_text(json.dumps({"jobs": [job.to_dict(), 42]}), encoding="utf-8")
    assert [item.id for item in service.list_jobs()] == [job.id]


def test_service_add_and_update_validation(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(CronError, match="不能为空"):
        service.add_job(
            name="x", message="  ", schedule=CronSchedule(kind="every", every_ms=1_000)
        )
    with pytest.raises(CronError, match="every_seconds"):
        service.add_job(
            name="x", message="m", schedule=CronSchedule(kind="every", every_ms=0)
        )
    with pytest.raises(CronError, match="delivery"):
        service.add_job(
            name="x",
            message="m",
            schedule=CronSchedule(kind="every", every_ms=1_000),
            delivery="webhook",
        )

    job = service.add_job(
        name="x", message="m", schedule=CronSchedule(kind="every", every_ms=1_000)
    )
    assert job.delete_after_run is False

    at_job = service.add_job(
        name="y",
        message="m",
        schedule=CronSchedule(kind="at", at_ms=now_ms() + 60_000),
    )
    assert at_job.delete_after_run is True

    with pytest.raises(CronError, match="找不到"):
        service.update_job("missing", name="n")
    with pytest.raises(CronError, match="不能为空"):
        service.update_job(job.id, message=" ")
    with pytest.raises(CronError, match="delivery"):
        service.update_job(job.id, delivery="webhook")
    with pytest.raises(CronError, match="已经过去"):
        service.update_job(
            job.id, schedule=CronSchedule(kind="at", at_ms=now_ms() - 1_000)
        )
    with pytest.raises(CronError, match="找不到"):
        service.set_enabled("missing", True)
    with pytest.raises(CronError, match="找不到"):
        service.trigger_now("missing")
    with pytest.raises(CronError, match="找不到"):
        asyncio.run(service.run_job_once("missing"))
    assert service.remove_job("missing") is False


def test_service_list_filters_and_sorts(tmp_path: Path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")
    later = service.add_job(
        name="later",
        message="m",
        schedule=CronSchedule(kind="at", at_ms=now_ms() + 120_000),
    )
    sooner = service.add_job(
        name="sooner",
        message="m",
        schedule=CronSchedule(kind="at", at_ms=now_ms() + 60_000),
    )
    disabled = service.add_job(
        name="disabled",
        message="m",
        schedule=CronSchedule(kind="every", every_ms=30_000),
    )
    service.set_enabled(disabled.id, False)

    assert [job.id for job in service.list_jobs()] == [
        disabled.id,
        sooner.id,
        later.id,
    ]
    assert [job.id for job in service.list_jobs(include_disabled=False)] == [
        sooner.id,
        later.id,
    ]

    resumed = service.set_enabled(disabled.id, True)
    assert resumed.enabled
    assert resumed.state.next_run_at_ms is not None


