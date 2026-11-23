"""验证闭环：``run_tests`` 与 ``run_checks``（ADR 0012）。

- ``run_tests``：自动识别 pytest / npm（jest、vitest）/ go test / cargo test（也可以直接给命令），
  解析输出得到通过、失败、错误数与失败用例清单，失败时附上输出尾部；
- ``run_checks``：运行 lint / 类型检查。命令来自 ``[dev] checks``，没有配置时按项目文件自动识别
  （ruff、mypy、pyright、eslint、tsc）。

命令由工具在执行前按 ``run_command`` 同样的规则做权限检查（放行名单、确认、受保护路径、strict 档位），
因此工具本身不声明 exec 能力。每次运行都记为一次「验证」，供「改了代码要验证」的提醒使用。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.extensions import _tracking as tracking

TAIL_CHARS = 4000
MAX_FAILURES = 30


# 权限检查用的「影子工具」：与 run_command 相同的能力与参数角色
class _CommandArgs(BaseModel):
    command: str = Field(json_schema_extra={"x-markbot-role": "command"})


_SHADOW = Tool(
    name="run_command",
    description="",
    params=_CommandArgs,
    handler=lambda ctx, args: ToolResult(content=""),
    capabilities=frozenset({"exec:*"}),
)


async def _authorize(ctx, command: str) -> ToolResult | None:
    """按 run_command 的规则检查命令；拒绝或用户未确认时返回错误结果。"""
    policy = getattr(ctx, "policy", None)
    check = getattr(policy, "check", None)
    if not callable(check):
        return None
    decision = check(_SHADOW, {"command": command})
    if not decision.allowed:
        return ToolResult.error(f"permission denied: {decision.reason}", denied=True)
    if decision.requires_confirmation:
        confirm = getattr(ctx, "confirm", None)
        approved = bool(confirm and await confirm("确认执行验证命令", f"command:\n{command}"))
        if not approved:
            return ToolResult.error(
                f"permission denied: 用户未确认执行 {command}。如需免确认，在 [permissions].allow_exec "
                "中添加程序名（定时任务里必须静态放行）",
                denied=True,
            )
    return None


# ------------------------------------------------------------------ 识别项目
def _python(root: Path) -> str:
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python",
                      root / "venv" / "Scripts" / "python.exe", root / "venv" / "bin" / "python"):
        if candidate.exists():
            return _quote(str(candidate))
    if shutil.which("python"):
        return "python"
    if shutil.which("python3"):
        return "python3"
    return _quote(sys.executable)


def _quote(text: str) -> str:
    return f'"{text}"' if " " in text else text


def _tool(root: Path, name: str) -> str:
    for candidate in (root / ".venv" / "Scripts" / f"{name}.exe", root / ".venv" / "bin" / name,
                      root / "node_modules" / ".bin" / name):
        if candidate.exists():
            return _quote(str(candidate))
    return name


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect_tests(root: Path, paths: list[str]) -> tuple[str, str] | None:
    """返回 ``(框架, 命令)``；识别不了返回 None。"""
    targets = " ".join(_quote(p) for p in paths)
    pyproject = _read(root / "pyproject.toml")
    if (
        "[tool.pytest" in pyproject
        or (root / "pytest.ini").exists()
        or "[tool:pytest]" in _read(root / "setup.cfg")
        or (root / "conftest.py").exists()
        or (root / "tests").is_dir()
    ) and (pyproject or (root / "setup.py").exists() or (root / "tests").is_dir()):
        command = f"{_python(root)} -m pytest -q -rfE --tb=short --color=no -p no:cacheprovider"
        return "pytest", f"{command} {targets}".strip()
    package = root / "package.json"
    if package.exists():
        try:
            scripts = json.loads(_read(package) or "{}").get("scripts") or {}
        except ValueError:
            scripts = {}
        if "test" in scripts:
            runner = "pnpm" if (root / "pnpm-lock.yaml").exists() else (
                "yarn" if (root / "yarn.lock").exists() else "npm"
            )
            kind = "vitest" if "vitest" in str(scripts["test"]) else "jest"
            extra = f" -- {targets}" if targets else ""
            return kind, f"{runner} test{extra}" if runner == "npm" else f"{runner} test {targets}".strip()
    if (root / "go.mod").exists():
        return "go", f"go test {targets or './...'}"
    if (root / "Cargo.toml").exists():
        return "cargo", f"cargo test {targets}".strip()
    return None


def detect_checks(root: Path) -> list[str]:
    pyproject = _read(root / "pyproject.toml")
    checks: list[str] = []
    if "[tool.ruff" in pyproject or (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        checks.append(f"{_tool(root, 'ruff')} check .")
    if "[tool.mypy" in pyproject or (root / "mypy.ini").exists():
        checks.append(f"{_tool(root, 'mypy')} .")
    if "[tool.pyright" in pyproject or (root / "pyrightconfig.json").exists():
        checks.append(f"{_tool(root, 'pyright')}")
    eslint = any((root / name).exists() for name in (
        ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", "eslint.config.js",
        "eslint.config.mjs", "eslint.config.cjs",
    ))
    if eslint:
        checks.append(f"{_tool(root, 'eslint')} .")
    if (root / "tsconfig.json").exists():
        checks.append(f"{_tool(root, 'tsc')} --noEmit")
    return checks


def _framework_of(command: str) -> str:
    low = command.lower()
    for key, name in (("pytest", "pytest"), ("vitest", "vitest"), ("jest", "jest"),
                      ("go test", "go"), ("cargo test", "cargo"), ("npm test", "jest"),
                      ("pnpm test", "jest"), ("yarn test", "jest")):
        if key in low:
            return name
    return "generic"


# ------------------------------------------------------------------ 解析输出
_PYTEST_COUNT = re.compile(
    r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)"
)
_PYTEST_FAIL = re.compile(r"^(FAILED|ERROR) (\S+)(?: - (.*))?$", re.MULTILINE)
_JEST_TESTS = re.compile(r"^Tests:\s+(.*)$", re.MULTILINE)
_VITEST_TESTS = re.compile(r"^\s*Tests\s+(.*)$", re.MULTILINE)
_JEST_FAIL = re.compile(r"^\s*●\s+(.+?)$", re.MULTILINE)
_GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+)", re.MULTILINE)
_GO_PASS = re.compile(r"^\s*--- PASS: ", re.MULTILINE)
_CARGO_RESULT = re.compile(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored")
_CARGO_FAIL = re.compile(r"^test (\S+) \.\.\. FAILED", re.MULTILINE)


def parse_test_output(framework: str, output: str, returncode: int) -> dict[str, Any]:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    failures: list[str] = []
    if framework == "pytest":
        tail = output[-3000:]
        for number, kind in _PYTEST_COUNT.findall(tail):
            key = {"error": "errors", "xfailed": "skipped", "deselected": "skipped", "xpassed": "passed"}.get(kind, kind)
            if key in counts:
                counts[key] += int(number)
        failures = [
            f"{name}" + (f" - {message[:200]}" if message else "")
            for _, name, message in _PYTEST_FAIL.findall(output)
        ]
    elif framework in ("jest", "vitest"):
        match = _JEST_TESTS.search(output) or _VITEST_TESTS.search(output)
        if match:
            for number, kind in re.findall(r"(\d+) (passed|failed|skipped|todo)", match.group(1)):
                key = "skipped" if kind == "todo" else kind
                counts[key] += int(number)
        failures = [item.strip()[:200] for item in _JEST_FAIL.findall(output)]
    elif framework == "go":
        failures = _GO_FAIL.findall(output)
        counts["failed"] = len(failures)
        counts["passed"] = len(_GO_PASS.findall(output))
    elif framework == "cargo":
        for passed, failed, ignored in _CARGO_RESULT.findall(output):
            counts["passed"] += int(passed)
            counts["failed"] += int(failed)
            counts["skipped"] += int(ignored)
        failures = _CARGO_FAIL.findall(output)
    ok = returncode == 0 and counts["failed"] == 0 and counts["errors"] == 0
    if returncode != 0 and not counts["failed"] and not counts["errors"]:
        counts["errors"] = 1  # 收集失败、语法错误、命令不存在等
    unique: list[str] = []
    for item in failures:
        if item not in unique:
            unique.append(item)
    return {"ok": ok, **counts, "failures": unique[:MAX_FAILURES], "returncode": returncode}


def _issue_count(command: str, output: str, returncode: int) -> int:
    patterns = (
        r"Found (\d+) errors?",           # ruff
        r"(\d+) errors?, \d+ warnings?",  # pyright
        r"Found (\d+) errors? in",        # mypy
        r"✖ (\d+) problems?",             # eslint
    )
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return int(match.group(1))
    tsc = len(re.findall(r"error TS\d+", output))
    if tsc:
        return tsc
    return 0 if returncode == 0 else 1


def _tail(text: str, limit: int = TAIL_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…（前面省略）\n" + text[-limit:]


class RunTestsIn(BaseModel):
    command: str = Field(
        default="",
        description="测试命令；留空自动识别（pytest / npm test / go test / cargo test）",
        json_schema_extra={"x-markbot-role": "command"},
    )
    paths: list[str] = Field(default_factory=list, description="只运行这些测试文件/目录（自动识别时使用）")
    timeout_s: float = Field(default=600, ge=10, le=1800)


class ChecksIn(BaseModel):
    commands: list[str] = Field(
        default_factory=list, description="要运行的检查命令；留空用 [dev] checks 或自动识别"
    )
    timeout_s: float = Field(default=300, ge=10, le=1800)


def _dev_config(config: Any) -> dict[str, Any]:
    dev = (config or {}).get("dev") if isinstance(config, dict) else None
    return dev if isinstance(dev, dict) else {}


def setup(api: ExtensionAPI) -> None:
    config = getattr(api, "config", None)

    async def run_tests(ctx, args: RunTestsIn) -> ToolResult:
        root = Path(getattr(ctx.env, "root", os.getcwd()))
        command = args.command.strip() or str(_dev_config(config).get("test_command") or "").strip()
        framework = _framework_of(command) if command else ""
        if not command:
            detected = detect_tests(root, args.paths)
            if detected is None:
                return ToolResult.error(
                    "没有识别到测试框架：请直接提供 command，或在 config.toml 的 [dev] test_command 配置"
                )
            framework, command = detected
        denied = await _authorize(ctx, command)
        if denied is not None:
            return denied
        result = await ctx.env.exec(command, timeout_s=args.timeout_s)
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        if result.timed_out:
            tracking.mark_verified(ctx.session, ok=False, kind="tests", summary="timeout")
            return ToolResult(
                content=f"测试超时（{int(args.timeout_s)}s）: {command}\n{_tail(output)}",
                is_error=True,
                details={"command": command, "timed_out": True},
            )
        parsed = parse_test_output(framework, output, result.returncode)
        status = "✅ 测试通过" if parsed["ok"] else "❌ 测试未通过"
        summary = (
            f"{status}（{framework}）：通过 {parsed['passed']}，失败 {parsed['failed']}，"
            f"错误 {parsed['errors']}，跳过 {parsed['skipped']}"
        )
        tracking.mark_verified(ctx.session, ok=parsed["ok"], kind="tests", summary=summary)
        lines = [summary, f"命令: {command}"]
        if parsed["failures"]:
            lines.append("失败用例:")
            lines.extend(f"- {item}" for item in parsed["failures"])
        lines.append("输出尾部:" if not parsed["ok"] else "输出摘要:")
        lines.append(_tail(output, TAIL_CHARS if not parsed["ok"] else 600))
        return ToolResult(
            content="\n".join(lines),
            is_error=not parsed["ok"],
            details={"command": command, "framework": framework, **parsed},
        )

    async def run_checks(ctx, args: ChecksIn) -> ToolResult:
        root = Path(getattr(ctx.env, "root", os.getcwd()))
        commands = [c for c in args.commands if c.strip()]
        if not commands:
            configured = _dev_config(config).get("checks")
            if isinstance(configured, str):
                configured = [configured]
            commands = [str(c) for c in (configured or []) if str(c).strip()] or detect_checks(root)
        if not commands:
            return ToolResult.error(
                "没有识别到检查工具：请提供 commands，或在 config.toml 的 [dev] checks 配置"
            )
        results: list[dict[str, Any]] = []
        lines: list[str] = []
        for command in commands:
            denied = await _authorize(ctx, command)
            if denied is not None:
                results.append({"command": command, "ok": False, "issues": None, "denied": True})
                lines.append(f"⛔ {command}: {denied.content}")
                continue
            result = await ctx.env.exec(command, timeout_s=args.timeout_s)
            output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
            issues = _issue_count(command, output, result.returncode)
            ok = result.returncode == 0 and not result.timed_out
            results.append({"command": command, "ok": ok, "issues": issues, "returncode": result.returncode})
            if ok:
                lines.append(f"✅ {command}")
            else:
                state = "超时" if result.timed_out else f"{issues} 个问题（退出码 {result.returncode}）"
                lines.append(f"❌ {command}: {state}\n{_tail(output, 3000)}")
        all_ok = all(item["ok"] for item in results)
        tracking.mark_verified(
            ctx.session, ok=all_ok, kind="checks",
            summary="; ".join(f"{r['command']}={'ok' if r['ok'] else 'fail'}" for r in results),
        )
        head = "✅ 全部检查通过" if all_ok else "❌ 有检查未通过"
        return ToolResult(
            content=head + "\n" + "\n".join(lines),
            is_error=not all_ok,
            details={"checks": results, "ok": all_ok},
        )

    api.register_tool(
        Tool(
            name="run_tests",
            description=(
                "运行项目测试并返回结构化结果（通过/失败数、失败用例、输出尾部）。"
                "修改代码后用它验证；可只运行指定文件"
            ),
            params=RunTestsIn,
            handler=run_tests,
            timeout_s=1900.0,
            concurrency_safe=False,
        )
    )
    api.register_tool(
        Tool(
            name="run_checks",
            description="运行 lint / 类型检查（ruff、mypy、pyright、eslint、tsc 等）并汇总结果",
            params=ChecksIn,
            handler=run_checks,
            timeout_s=1900.0,
            concurrency_safe=False,
        )
    )


__all__ = ["detect_checks", "detect_tests", "parse_test_output", "setup"]
