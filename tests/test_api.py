from __future__ import annotations

import asyncio
import sys

from markbot.api.sandbox import (
    ENV_ALLOWLIST,
    ProcessResult,
    build_env,
    run_sandboxed,
    run_shell_sandboxed,
)
from markbot.api.ui import ConsoleUI, NullUI


def test_process_result_ok_and_combined():
    result = ProcessResult(0, "out", "")
    assert result.ok
    assert result.combined() == "out"

    result = ProcessResult(1, "out", "err")
    assert not result.ok
    assert result.combined() == "out\n[stderr]\nerr"

    timed_out = ProcessResult(-1, "", "", timed_out=True)
    assert not timed_out.ok
    assert timed_out.combined().endswith("[timeout]")

    long = ProcessResult(0, "x" * 30, "")
    assert long.combined(limit=10) == "x" * 10


def test_build_env_only_allows_allowlist(monkeypatch):
    monkeypatch.setenv("MARKBOT_TEST_SECRET", "leak")
    env = build_env({"EXTRA": 123})
    assert "MARKBOT_TEST_SECRET" not in env
    assert env["EXTRA"] == "123"
    assert set(env) <= ENV_ALLOWLIST | {"EXTRA"}


def test_run_sandboxed_captures_output_and_exit_code(tmp_path):
    ok = asyncio.run(
        run_sandboxed([sys.executable, "-c", "print('hello')"], cwd=tmp_path)
    )
    assert ok.ok
    assert ok.stdout.strip() == "hello"

    failed = asyncio.run(
        run_sandboxed([sys.executable, "-c", "raise SystemExit(2)"], cwd=tmp_path)
    )
    assert not failed.ok
    assert failed.returncode == 2


def test_run_sandboxed_times_out(tmp_path):
    result = asyncio.run(
        run_sandboxed(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            timeout_s=0.05,
        )
    )
    assert result.timed_out
    assert not result.ok


def test_run_shell_sandboxed(tmp_path):
    result = asyncio.run(run_shell_sandboxed("printf shell-ok", cwd=tmp_path))
    assert result.ok
    assert result.stdout == "shell-ok"


def test_console_ui_non_interactive_never_confirms(capsys):
    ui = ConsoleUI(interactive=False)
    assert asyncio.run(ui.confirm("t", "m")) is False

    ui.info("info-line")
    ui.status("status-line")
    ui.warn("warn-line")
    ui.error("error-line")
    captured = capsys.readouterr()
    assert "info-line" in captured.out
    assert "status-line" not in captured.err
    assert "warning: warn-line" in captured.err
    assert "error: error-line" in captured.err


def test_console_ui_interactive_status_and_prompt(monkeypatch, capsys):
    ui = ConsoleUI(interactive=True)
    ui.status("step")
    assert capsys.readouterr().err == "· step\n"

    monkeypatch.setattr("builtins.input", lambda label: "yes")
    assert asyncio.run(ui.confirm("t", "m")) is True

    monkeypatch.setattr("builtins.input", lambda label: "answer")
    assert asyncio.run(ui.prompt("Q: ")) == "answer"


def test_null_ui_is_silent(capsys):
    ui = NullUI()
    ui.info("x")
    ui.status("x")
    ui.warn("x")
    ui.error("x")
    assert asyncio.run(ui.confirm("t", "m")) is False
    assert asyncio.run(ui.prompt("q")) == ""
    assert capsys.readouterr() == ("", "")
