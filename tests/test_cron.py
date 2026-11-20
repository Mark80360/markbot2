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
from markbot.cron.service import CronError, CronService
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
    assert calls and calls[0]["session_id"] == "chat-session"
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
