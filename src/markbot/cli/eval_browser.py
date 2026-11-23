"""浏览器评测：``markbot eval --suite browser``（ADR 0015）。

每个用例是 ``evals/browser/<name>/``：

- ``case.yaml``：``prompt``（``{base_url}`` 会替换为本地站点地址）、``expect``（答复必须全部包含的文字）、
  ``expect_any``（至少包含其一）、``forbid``（不能出现）、``max_steps``、``timeout_s``；
- ``site/``：静态网页（通常依赖 JavaScript 渲染或交互，``fetch_url`` 拿不到答案）。

执行：在 127.0.0.1 随机端口起一个静态服务器，以 trusted 档位、无界面浏览器、``confirm = "none"``、
临时浏览器用户目录跑一个回合；答复满足 expect 且确实用过浏览器工具算通过。需要安装 playwright。
"""

from __future__ import annotations

import asyncio
import copy
import functools
import http.server
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from markbot.cli.eval_cmd import CaseResult, case_config, stage_learned
from markbot.kernel.app import Kernel


@dataclass
class BrowserCase:
    name: str
    path: Path
    prompt: str
    expect: list[str] = field(default_factory=list)
    expect_any: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    max_steps: int = 25
    timeout_s: float = 600.0


def load_browser_cases(directory: Path) -> list[BrowserCase]:
    cases: list[BrowserCase] = []
    for case_dir in sorted(Path(directory).iterdir() if Path(directory).is_dir() else []):
        spec_path = case_dir / "case.yaml"
        if not spec_path.is_file() or not (case_dir / "site").is_dir():
            continue
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
        cases.append(
            BrowserCase(
                name=case_dir.name,
                path=case_dir,
                prompt=str(spec.get("prompt") or "").strip(),
                expect=[str(item) for item in spec.get("expect") or []],
                expect_any=[str(item) for item in spec.get("expect_any") or []],
                forbid=[str(item) for item in spec.get("forbid") or []],
                max_steps=int(spec.get("max_steps") or 25),
                timeout_s=float(spec.get("timeout_s") or 600),
            )
        )
    return [case for case in cases if case.prompt]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).lower()


def judge(answer: str, case: BrowserCase) -> str | None:
    """答复是否满足期望；满足返回 None，否则返回原因。"""
    body = _norm(answer)
    missing = [item for item in case.expect if _norm(item) not in body]
    if missing:
        return f"答复缺少: {', '.join(missing)}"
    if case.expect_any and not any(_norm(item) in body for item in case.expect_any):
        return f"答复不含任一: {', '.join(case.expect_any)}"
    present = [item for item in case.forbid if _norm(item) in body]
    if present:
        return f"答复包含禁止内容: {', '.join(present)}"
    return None


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # noqa: D401 - 静默访问日志
        return


def serve(directory: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(_Quiet, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="eval-site").start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def browser_eval_raw(raw: dict[str, Any], port: int, profile: Path) -> dict[str, Any]:
    data = copy.deepcopy(raw or {})
    tools = data.setdefault("tools", {})
    web = tools.setdefault("web", {})
    allow = list(web.get("allow_private") or [])
    web["allow_private"] = [*allow, f"127.0.0.1:{port}"]
    browser = dict(tools.get("browser") or {})
    browser.update({"enabled": True, "headless": True, "confirm": "none", "user_data_dir": str(profile)})
    tools["browser"] = browser
    tools["desktop"] = {"enabled": False}
    return data


async def run_browser_case(
    case: BrowserCase, *, provider: Any = None, keep: bool = False, learned_dir: Path | None = None
) -> CaseResult:
    started = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix=f"markbot-beval-{case.name}-"))
    workspace = tmp / "work"
    workspace.mkdir()
    server, base_url = serve(case.path / "site")
    try:
        cfg = case_config(tmp / "home", workspace)
        cfg.agent.max_steps = case.max_steps
        cfg.raw = browser_eval_raw(getattr(cfg, "raw", {}) or {}, server.server_address[1], tmp / "profile")
        kernel = Kernel.build(
            config=cfg,
            provider=provider,
            include_packages=False,
            workspace=workspace,
            learned_dir=stage_learned(tmp / "home", learned_dir),
        )
        if kernel.runtime.tools.get("browser_open") is None:
            return CaseResult(case.name, False, '浏览器工具未注册（pip install -e ".[browser]" 并 playwright install chromium）')
        prompt = case.prompt.replace("{base_url}", base_url)
        try:
            result = await asyncio.wait_for(kernel.run(prompt, archive=False), timeout=case.timeout_s)
            used = sorted(
                {
                    str(message.name)
                    for message in (kernel.session.messages if kernel.session else [])
                    if message.role == "tool" and message.name
                }
            )
        except asyncio.TimeoutError:
            return CaseResult(case.name, False, "Agent 超时", seconds=time.monotonic() - started)
        finally:
            await kernel.shutdown(archive=False)
        tokens = int(result.total_usage.get("tokens_in", 0)) + int(result.total_usage.get("tokens_out", 0))
        common = dict(
            steps=getattr(kernel.session, "steps", 0) if kernel.session else 0,
            tool_calls=result.tool_calls,
            seconds=time.monotonic() - started,
            tokens=tokens,
            changed=used,
            check_output=result.text[-3000:],
        )
        if result.error:
            return CaseResult(case.name, False, f"回合出错: {result.error[:120]}", **common)
        if not any(name.startswith("browser_") for name in used):
            return CaseResult(case.name, False, "没有使用浏览器工具", **common)
        reason = judge(result.text, case)
        if reason:
            return CaseResult(case.name, False, reason, **common)
        return CaseResult(case.name, True, "通过", **common)
    finally:
        server.shutdown()
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def run_browser_suite(argv: list[str], directory: Path) -> list[CaseResult] | None:
    cases = load_browser_cases(directory)
    if "--case" in argv:
        wanted = argv[argv.index("--case") + 1]
        cases = [case for case in cases if case.name == wanted]
    if not cases:
        print(f"error: {directory} 下没有找到浏览器评测用例", file=sys.stderr)
        return None
    results: list[CaseResult] = []
    for case in cases:
        print(f"运行 {case.name} …", file=sys.stderr)
        results.append(asyncio.run(run_browser_case(case, keep="--keep" in argv)))
        if not results[-1].passed and "--verbose" in argv:
            print(results[-1].check_output, file=sys.stderr)
    return results


__all__ = [
    "BrowserCase",
    "browser_eval_raw",
    "judge",
    "load_browser_cases",
    "run_browser_case",
    "run_browser_suite",
    "serve",
]
