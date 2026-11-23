from __future__ import annotations

import asyncio

import pytest

from markbot.api.execution import ExecutionEnvironment
from markbot.kernel.execution import LocalExecution


def test_local_execution_reads_writes_lists_and_globs(tmp_path):
    env = LocalExecution(tmp_path)

    path = env.write_text("notes/a.txt", "line1\nline2")
    assert path == tmp_path / "notes" / "a.txt"
    assert env.read_text("notes/a.txt") == "line1\nline2"
    assert [item.name for item in env.list_dir("notes")] == ["a.txt"]
    assert env.glob("**/*.txt") == ["notes/a.txt"]


def test_local_execution_rejects_paths_outside_root(tmp_path):
    env = LocalExecution(tmp_path / "workspace")
    env.root.mkdir()

    with pytest.raises(PermissionError):
        env.resolve("../escape.txt")
    with pytest.raises(PermissionError):
        env.write_text("../escape.txt", "x")


def test_local_execution_runs_command(tmp_path):
    env = LocalExecution(tmp_path)
    result = asyncio.run(env.exec("printf hello"))
    assert result.ok
    assert result.stdout == "hello"


def test_local_execution_matches_protocol(tmp_path):
    assert isinstance(LocalExecution(tmp_path), ExecutionEnvironment)
