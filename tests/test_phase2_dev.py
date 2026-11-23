"""Phase 2 第一批（ADR 0012）的回归测试：精确编辑、补丁、搜索、文件结构、后台进程、测试/检查工具、
任务清单、验证提醒、并发安全、会话临时目录、运行环境说明。"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from markbot.api.ui import NullUI
from markbot.extensions import dev, fs, plan, process, search
from markbot.extensions.fs.patch import PatchError, apply_hunks, parse_patch
from markbot.kernel.config import PermissionConfig, load_config
from markbot.kernel.context import environment_block, git_branch
from markbot.kernel.execution import LocalExecution
from markbot.kernel.paths import PathGuard
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import SourceSet
from markbot.kernel.session import Session
from markbot.kernel.tools import KernelToolContext


def _tools(module, *names, config=None):
    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI(), config=config)
    module.setup(api)
    api.commit()
    registry = {tool.name: tool for tool in source.tools}
    return [registry[name] for name in names]


def _ctx(root: Path, home: Path | None = None, session: Session | None = None, **perm):
    perm = {"profile": "trusted", "trusted_exec": "all", **perm}
    policy = PermissionPolicy(PermissionConfig(**perm), root=root, home=home)
    env = LocalExecution(root, guard=policy.guard)
    return KernelToolContext(session=session or Session("测试"), policy=policy, env=env)


def _call(tool, ctx, **kwargs):
    async def invoke():
        outcome = tool.handler(ctx, tool.params(**kwargs))
        return await outcome if hasattr(outcome, "__await__") else outcome

    return asyncio.run(invoke())


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path.resolve() / "project"
    path.mkdir()
    return path


# ===================================================================== edit_file


def test_edit_file_unique_replace_returns_diff(root):
    (root / "a.py").write_text("def f():\n    return 1\n\n\ndef g():\n    return 2\n", encoding="utf-8")
    (edit,) = _tools(fs, "edit_file")
    ctx = _ctx(root)
    result = _call(edit, ctx, path="a.py", old_string="    return 1", new_string="    return 10")
    assert not result.is_error, result.content
    assert "return 10" in (root / "a.py").read_text(encoding="utf-8")
    assert "+    return 10" in result.content and result.details["added"] == 1
    assert str(root / "a.py") in ctx.session.file_changes


def test_edit_file_ambiguous_and_missing(root):
    (root / "a.py").write_text("x = 1\nx = 1\n    y = 2\n", encoding="utf-8")
    (edit,) = _tools(fs, "edit_file")
    ctx = _ctx(root)
    ambiguous = _call(edit, ctx, path="a.py", old_string="x = 1", new_string="x = 3")
    assert ambiguous.is_error and "2 次" in ambiguous.content and "第 1、2 行" in ambiguous.content
    missing = _call(edit, ctx, path="a.py", old_string="y = 2\nz", new_string="q")
    assert missing.is_error and "第 3 行附近" in missing.content
    replaced = _call(edit, ctx, path="a.py", old_string="x = 1", new_string="x = 3", replace_all=True)
    assert not replaced.is_error
    assert (root / "a.py").read_text(encoding="utf-8").count("x = 3") == 2


def test_edit_file_preserves_crlf(root):
    (root / "w.txt").write_bytes(b"line1\r\nline2\r\nline3\r\n")
    (edit,) = _tools(fs, "edit_file")
    result = _call(edit, _ctx(root), path="w.txt", old_string="line2\nline3", new_string="L2\nL3")
    assert not result.is_error, result.content
    assert (root / "w.txt").read_bytes() == b"line1\r\nL2\r\nL3\r\n"


def test_edit_file_create_and_refuse_overwrite(root):
    (edit,) = _tools(fs, "edit_file")
    ctx = _ctx(root)
    created = _call(edit, ctx, path="new/m.py", old_string="", new_string="print('hi')\n")
    assert not created.is_error and (root / "new" / "m.py").exists()
    again = _call(edit, ctx, path="new/m.py", old_string="", new_string="x")
    assert again.is_error


def test_edit_refuses_when_file_changed_after_read(root):
    target = root / "s.py"
    target.write_text("a = 1\n", encoding="utf-8")
    read, edit = _tools(fs, "read_file", "edit_file")
    ctx = _ctx(root)
    assert not _call(read, ctx, path="s.py").is_error
    time.sleep(0.02)
    target.write_text("a = 1\nb = 2  # user edit\n", encoding="utf-8")
    stale = _call(edit, ctx, path="s.py", old_string="a = 1", new_string="a = 9")
    assert stale.is_error and stale.details.get("stale")
    assert "user edit" in target.read_text(encoding="utf-8")
    # 重新读取后可以修改
    _call(read, ctx, path="s.py")
    assert not _call(edit, ctx, path="s.py", old_string="a = 1", new_string="a = 9").is_error
    # 自己的修改不算外部修改：可以连续编辑
    assert not _call(edit, ctx, path="s.py", old_string="a = 9", new_string="a = 8").is_error


def test_write_file_is_atomic_and_tracks_changes(root):
    (write,) = _tools(fs, "write_file")
    ctx = _ctx(root)
    result = _call(write, ctx, path="out.txt", content="1\n2\n")
    assert not result.is_error and result.details["added"] == 2
    assert [p.name for p in root.iterdir()] == ["out.txt"]  # 没有残留临时文件


def test_read_file_rejects_binary(root):
    (root / "b.bin").write_bytes(b"\x00\x01\x02abc")
    (read,) = _tools(fs, "read_file")
    result = _call(read, _ctx(root), path="b.bin")
    assert result.is_error and result.details.get("binary")


# ===================================================================== apply_patch

PATCH = """--- a/one.py
+++ b/one.py
@@ -1,3 +1,3 @@
 a = 1
-b = 2
+b = 20
 c = 3
--- /dev/null
+++ b/two.py
@@ -0,0 +1,2 @@
+print("new")
+x = 1
"""


def test_apply_patch_multi_file(root):
    (root / "one.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    (patch,) = _tools(fs, "apply_patch")
    result = _call(patch, _ctx(root), patch=PATCH, paths=["one.py", "two.py"])
    assert not result.is_error, result.content
    assert (root / "one.py").read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"
    assert (root / "two.py").read_text(encoding="utf-8") == 'print("new")\nx = 1\n'


def test_apply_patch_is_atomic_on_failed_hunk(root):
    (root / "one.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    (root / "three.py").write_text("q = 1\n", encoding="utf-8")
    bad = PATCH.replace("--- /dev/null\n+++ b/two.py\n@@ -0,0 +1,2 @@\n+print(\"new\")\n+x = 1\n",
                        "--- a/three.py\n+++ b/three.py\n@@ -1 +1 @@\n-not there\n+z\n")
    (patch,) = _tools(fs, "apply_patch")
    result = _call(patch, _ctx(root), patch=bad, paths=["one.py", "three.py"])
    assert result.is_error and "没有修改任何文件" in result.content
    assert (root / "one.py").read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\n"


def test_apply_patch_requires_declared_paths(root):
    (root / "one.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    (patch,) = _tools(fs, "apply_patch")
    result = _call(patch, _ctx(root), patch=PATCH, paths=["one.py"])
    assert result.is_error and "two.py" in result.content
    assert (root / "one.py").read_text(encoding="utf-8") == "a = 1\nb = 2\nc = 3\n"


def test_apply_patch_delete_file(root):
    (root / "gone.py").write_text("x\n", encoding="utf-8")
    text = "--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    (patch,) = _tools(fs, "apply_patch")
    assert not _call(patch, _ctx(root), patch=text, paths=["gone.py"]).is_error
    assert not (root / "gone.py").exists()


def test_patch_tolerates_line_offsets_and_bad_counts():
    original = "".join(f"line{i}\n" for i in range(1, 21))
    patch = parse_patch("--- a/f\n+++ b/f\n@@ -3,3 +3,3 @@\n line10\n-line11\n+LINE11\n line12\n")
    assert apply_hunks(original, patch[0]).count("LINE11") == 1
    with pytest.raises(PatchError):
        apply_hunks(original, parse_patch("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-nope\n+x\n")[0])


def test_patch_trailing_whitespace_fallback_and_no_eol():
    original = "a  \nb\n"
    patch = parse_patch("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n\\ No newline at end of file\n")
    assert apply_hunks(original, patch[0]) == "a  \nc"


# ===================================================================== search


def _tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def handler():\n    return describe_error(1)\n", encoding="utf-8")
    (root / "src" / "util.ts").write_text("export function describe_error(x) {}\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "gen.py").write_text("describe_error\n", encoding="utf-8")
    (root / "ignored").mkdir()
    (root / "ignored" / "x.py").write_text("describe_error\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / ".markbot").mkdir()
    (root / ".markbot" / "secret.py").write_text("describe_error\n", encoding="utf-8")


def test_grep_python_fallback(root, monkeypatch):
    _tree(root)
    monkeypatch.setattr(search.shutil, "which", lambda name: None)
    (grep,) = _tools(search, "grep_search")
    result = _call(grep, _ctx(root), pattern="describe_error")
    assert result.details["engine"] == "python"
    assert "src/app.py:2:" in result.content and "src/util.ts:1:" in result.content
    for hidden in ("build/", "ignored/", ".markbot"):
        assert hidden not in result.content
    only = _call(grep, _ctx(root), pattern="describe_error(", literal=True, glob="*.py", files_only=True)
    assert only.content.strip() == "src/app.py"
    bad = _call(grep, _ctx(root), pattern="(unclosed")
    assert bad.is_error and "literal" in bad.content


@pytest.mark.skipif(search.shutil.which("rg") is None, reason="需要 ripgrep")
def test_grep_ripgrep(root):
    _tree(root)
    (grep,) = _tools(search, "grep_search")
    result = _call(grep, _ctx(root), pattern="describe_error", context=1)
    assert result.details["engine"] == "rg"
    assert "src/app.py" in result.content and ".markbot" not in result.content


def test_file_outline(root):
    (root / "m.py").write_text(
        "class A:\n    def run(self, x):\n        pass\n\nasync def main():\n    pass\n", encoding="utf-8"
    )
    (root / "m.ts").write_text("export class B {}\nexport const go = async (a) => a\n", encoding="utf-8")
    (outline,) = _tools(search, "file_outline")
    py = _call(outline, _ctx(root), path="m.py")
    assert "class A" in py.content and "def run(self, x)" in py.content and "async def main" in py.content
    ts = _call(outline, _ctx(root), path="m.ts")
    assert "class B" in ts.content and "const go" in ts.content


# ===================================================================== 后台进程


def test_background_process_lifecycle(root):
    script = root / "echo_loop.py"
    script.write_text(
        "import sys\nprint('ready', flush=True)\nfor line in sys.stdin:\n"
        "    print('got:' + line.strip(), flush=True)\n",
        encoding="utf-8",
    )
    start, read, write, kill, listing = _tools(
        process, "process_start", "process_read", "process_write", "process_kill", "process_list"
    )
    ctx = _ctx(root)
    started = _call(start, ctx, command=f'"{sys.executable}" "{script}"', wait_s=5)
    assert not started.is_error, started.content
    handle = started.details["handle"]
    assert "ready" in started.content
    assert not _call(write, ctx, handle=handle, input="hello").is_error
    echoed = _call(read, ctx, handle=handle, wait_s=5)
    assert "got:hello" in echoed.content
    assert handle in _call(listing, ctx).content
    other = _ctx(root)  # 另一个会话看不到这个进程
    assert _call(read, other, handle=handle).is_error
    killed = _call(kill, ctx, handle=handle)
    assert not killed.is_error
    assert handle not in _call(listing, ctx).content


def test_process_exit_code_is_reported(root):
    start, read = _tools(process, "process_start", "process_read")
    ctx = _ctx(root)
    started = _call(start, ctx, command=f'"{sys.executable}" -c "import sys; print(1); sys.exit(3)"', wait_s=5)
    handle = started.details["handle"]
    deadline = time.time() + 5
    result = _call(read, ctx, handle=handle, wait_s=1)
    while result.details["running"] and time.time() < deadline:
        result = _call(read, ctx, handle=handle, wait_s=1)
    assert result.details["exit_code"] == 3
    process.kill_all(ctx.session.id)


# ===================================================================== 测试 / 检查


def test_parse_pytest_output():
    output = (
        "FAILED tests/test_a.py::test_x - AssertionError: 1 != 2\n"
        "ERROR tests/test_b.py::test_y\n"
        "=== 1 failed, 5 passed, 1 error, 2 skipped in 0.31s ===\n"
    )
    parsed = dev.parse_test_output("pytest", output, 1)
    assert (parsed["passed"], parsed["failed"], parsed["errors"], parsed["skipped"]) == (5, 1, 1, 2)
    assert parsed["failures"][0].startswith("tests/test_a.py::test_x - AssertionError")
    assert not parsed["ok"]


def test_parse_other_frameworks():
    jest = dev.parse_test_output("jest", "  ● suite › adds\nTests:       1 failed, 3 passed, 4 total\n", 1)
    assert (jest["failed"], jest["passed"]) == (1, 3) and jest["failures"] == ["suite › adds"]
    go = dev.parse_test_output("go", "--- PASS: TestA\n--- FAIL: TestB (0.0s)\nFAIL\n", 1)
    assert go["failures"] == ["TestB"] and go["passed"] == 1
    cargo = dev.parse_test_output(
        "cargo", "test a::b ... FAILED\ntest result: FAILED. 2 passed; 1 failed; 0 ignored\n", 101
    )
    assert (cargo["passed"], cargo["failed"]) == (2, 1) and cargo["failures"] == ["a::b"]
    crashed = dev.parse_test_output("pytest", "ImportError while loading conftest", 4)
    assert crashed["errors"] == 1 and not crashed["ok"]


def test_detect_tests_and_checks(tmp_path):
    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text("[tool.pytest.ini_options]\n[tool.ruff]\n", encoding="utf-8")
    framework, command = dev.detect_tests(py, ["tests/test_a.py"])
    assert framework == "pytest" and "-m pytest" in command and command.endswith("tests/test_a.py")
    assert any("ruff" in item and "check" in item for item in dev.detect_checks(py))
    js = tmp_path / "js"
    js.mkdir()
    (js / "package.json").write_text('{"scripts": {"test": "vitest run"}}', encoding="utf-8")
    (js / "tsconfig.json").write_text("{}", encoding="utf-8")
    assert dev.detect_tests(js, []) == ("vitest", "npm test")
    assert any("tsc" in item for item in dev.detect_checks(js))
    go = tmp_path / "go"
    go.mkdir()
    (go / "go.mod").write_text("module x\n", encoding="utf-8")
    assert dev.detect_tests(go, []) == ("go", "go test ./...")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert dev.detect_tests(empty, []) is None


def test_run_tests_end_to_end(root):
    (root / "tests").mkdir()
    (root / "tests" / "test_demo.py").write_text(
        "def test_ok():\n    assert 1 == 1\n\ndef test_bad():\n    assert 1 == 2\n", encoding="utf-8"
    )
    (run_tests,) = _tools(dev, "run_tests")
    ctx = _ctx(root)
    command = f'"{sys.executable}" -m pytest -q -rfE --tb=short -p no:cacheprovider tests'
    result = _call(run_tests, ctx, command=command, timeout_s=120)
    assert result.is_error
    assert result.details["passed"] == 1 and result.details["failed"] == 1
    assert any("test_bad" in item for item in result.details["failures"])
    assert ctx.session.verifications[-1]["kind"] == "tests"


def test_run_tests_respects_permissions(root):
    (run_tests,) = _tools(dev, "run_tests")
    strict = _call(run_tests, _ctx(root, profile="strict"), command="pytest -q")
    assert strict.is_error and strict.details.get("denied")
    standard = _call(run_tests, _ctx(root, profile="standard"), command="pytest -q")
    assert standard.is_error and "未确认" in standard.content  # NullUI 不确认


def test_run_checks_with_explicit_commands(root):
    (checks,) = _tools(dev, "run_checks")
    result = _call(checks, _ctx(root, allow_exec=["python"]), commands=[f'"{sys.executable}" --version'])
    assert not result.is_error, result.content
    assert result.details["ok"] is True


# ===================================================================== 任务清单


def test_todo_write_read_and_restore(root):
    write, read = _tools(plan, "todo_write", "todo_read")
    session = Session("任务")
    ctx = _ctx(root, session=session)
    items = [{"content": "读代码", "status": "completed"}, {"content": "修复", "status": "in_progress"}]
    assert "1/2" in _call(write, ctx, items=items).content
    two_active = [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "in_progress"}]
    assert _call(write, ctx, items=two_active).is_error
    # 模拟进程重启：会话对象上没有 todos，从事件里恢复
    session.append_assistant("", tool_calls=[ToolCall(id="c1", name="todo_write", args={"items": items})])
    del session.todos
    assert "[~] 修复" in _call(read, ctx).content


def test_todos_and_rules_injected_into_context(make_kernel):
    kernel = make_kernel(["ok"])
    seen: dict = {}

    async def capture(payload, on_delta=None):
        seen["system"] = payload["system"]
        return ModelReply(text="ok")

    kernel.provider.complete = capture  # type: ignore[method-assign]
    asyncio.run(kernel.run("修复代码里的 bug", archive=False))
    kernel.session.todos = [{"content": "跑测试", "status": "pending"}]
    asyncio.run(kernel.run("继续", archive=False))
    assert "当前任务清单" in seen["system"] and "跑测试" in seen["system"]
    assert "编码任务的工作方式" in seen["system"]


# ===================================================================== 验证提醒


def _trusted_kernel(make_kernel, home, project, replies):
    cfg = load_config(home=home, project_root=project)
    cfg.permissions.profile = "trusted"
    return make_kernel(provider=ScriptedProvider(replies), config=cfg)


def test_verify_reminder_once(make_kernel, home, project):
    (project / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    edit_call = ModelReply(
        tool_calls=[ToolCall(id="c1", name="edit_file", args={
            "path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"})]
    )
    kernel = _trusted_kernel(make_kernel, home, project, [edit_call, "改好了", "无法运行测试：项目没有测试"])
    result = asyncio.run(kernel.run("修复 add"))
    assert "return a + b" in (project / "calc.py").read_text(encoding="utf-8")
    assert result.text == "无法运行测试：项目没有测试"
    users = [m.content for m in kernel.session.messages if m.role == "user"]
    assert any(text.startswith("[系统提醒]") and "calc.py" in text for text in users)
    assert len(kernel.provider.calls) == 3


def test_no_reminder_when_disabled_or_docs_only(make_kernel, home, project):
    (project / "README.md").write_text("# t\n", encoding="utf-8")
    edit_call = ModelReply(
        tool_calls=[ToolCall(id="c1", name="edit_file", args={
            "path": "README.md", "old_string": "# t", "new_string": "# title"})]
    )
    kernel = _trusted_kernel(make_kernel, home, project, [edit_call, "文档改好了"])
    result = asyncio.run(kernel.run("改文档"))
    assert result.text == "文档改好了" and len(kernel.provider.calls) == 2


# ===================================================================== 其它


def test_concurrency_safety_inference():
    write, read = _tools(fs, "write_file", "read_file")
    assert read.is_concurrency_safe() and not write.is_concurrency_safe()
    (start,) = _tools(process, "process_start")
    assert not start.is_concurrency_safe()


def test_session_scratch_is_writable(tmp_path):
    root = tmp_path.resolve() / "p"
    home = tmp_path.resolve() / "home"
    root.mkdir()
    guard = PathGuard.build(root, home=home)
    assert guard.resolve(str(home / "sessions" / "sess_1" / "scratch" / "a.py"), write=True)
    with pytest.raises(PermissionError):
        guard.resolve(str(home / "sessions" / "sess_1" / "events.jsonl"), write=True)


def test_environment_block_has_branch_tools_and_scratch(tmp_path):
    root = tmp_path.resolve() / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/feat/x\n", encoding="utf-8")
    assert git_branch(root) == "feat/x"
    home = tmp_path.resolve() / "home"
    env = LocalExecution(root, guard=PathGuard.build(root, home=home))
    block = environment_block(env, Session("t", session_id="sess_abc"))
    assert "git 分支: feat/x" in block
    assert str(home / "sessions" / "sess_abc" / "scratch") in block


def test_execution_bytes_delete(tmp_path):
    env = LocalExecution(tmp_path)
    env.write_bytes("x/b.bin", b"\x00\x01")
    assert env.read_bytes("x/b.bin") == b"\x00\x01"
    env.delete("x/b.bin")
    assert not (tmp_path / "x" / "b.bin").exists()
