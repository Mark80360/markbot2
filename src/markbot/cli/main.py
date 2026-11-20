"""MarkBot2 CLI：chat / selftest / doctor / trust / reload / optimize / rollback / serve / gateway。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from markbot.api.ui import ConsoleUI, NullUI
from markbot.cli.gateway import gateway_command
from markbot.cron.cli import cron_command
from markbot.cron.runner import build_job_runner
from markbot.cron.service import CronService, set_service
from markbot.kernel import gateway_log
from markbot.kernel.app import Kernel
from markbot.kernel.config import Config, load_config, markbot_home
from markbot.kernel.permissions import parse_capabilities, uncovered_capabilities
from markbot.kernel.providers import EchoProvider, ProviderError, build_provider
from markbot.kernel.registry import discover_packages, package_manifest
from markbot.kernel.trust import TrustStore
from markbot.memory.consolidator import Consolidator
from markbot.memory.consolidator import list_snapshots as list_memory_snapshots
from markbot.memory.consolidator import restore_snapshot as restore_memory_snapshot
from markbot.memory.journal import Journal, load_cursor
from markbot.memory.lifespan import split_meta
from markbot.memory.lifespan import status as lifespan_status
from markbot.memory.store import MemoryStore
from markbot.optimizer.gate import illegal_paths
from markbot.optimizer.run import OptimizerSettings, optimize
from markbot.optimizer.snapshots import list_snapshots, restore_snapshot

HELP = """MarkBot2 —— 极薄 Kernel + 四条扩展车道

用法：
  markbot init [--force]        初始化 ~/.markbot（配置文件与默认目录）
  markbot "你的请求"            一次性执行
  markbot chat                  交互式会话（/skill:<name>、/skills、/exit）
  markbot selftest              自检（含依赖方向与故障注入）
  markbot doctor [--validate] [--learned-paths]
  markbot skills                列出可用技能
  markbot packages              列出分发包与信任状态
  markbot trust <name> [--project] [--revoke]
  markbot reload                重新合成运行时视图
  markbot cron list|add|run|history
                                管理定时任务（调度器在 serve/gateway 进程里运行）
  markbot optimize [--dry-run] [--k N]
  markbot rollback [--list] [--to HASH]
  markbot memory status|search|consolidate|restore
                                查看、检索、整理与回滚长期记忆
  markbot serve [--port 8787]
  markbot gateway start|stop|restart|status
                                后台托管 HTTP 入口与飞书渠道（日志在 ~/.markbot/gateway/）

配置：
  ~/.markbot/config.toml        模型、权限、包与优化器配置（markbot init 生成）
  ~/.markbot/trust.json         包信任表

环境变量：
  MARKBOT_HOME                  覆盖 ~/.markbot
"""


# --------------------------------------------------------------------- helpers
def _load() -> Config | None:
    try:
        return load_config()
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"error: 配置读取失败: {exc}", file=sys.stderr)
        return None


def _build(
    *,
    ui=None,
    include_packages: bool = True,
    provider=None,
    home: Path | None = None,
    learned_dir: Path | None = None,
    workspace: Path | None = None,
    inject_failure: str | None = None,
    require_provider: bool = True,
    gateway_logs: bool = False,
) -> Kernel | None:
    cfg = _load()
    if cfg is None:
        return None
    if isinstance(ui, ConsoleUI):
        ui.show_status = cfg.ui.show_progress
    if home is not None:
        cfg.home = home
    if provider is None:
        ui = ui or NullUI()
        try:
            provider = build_provider(cfg, cfg.secrets)
        except ProviderError as exc:
            if require_provider:
                print(f"error: {exc}", file=sys.stderr)
                print(
                    '提示：在 config.toml 设置 provider = "echo" 可离线演示。',
                    file=sys.stderr,
                )
                return None
            provider = EchoProvider()
        kernel = Kernel.build(
            config=cfg,
            provider=provider,
            ui=ui,
            include_packages=include_packages,
            learned_dir=learned_dir,
            workspace=workspace,
            inject_failure=inject_failure,
            gateway_logs=gateway_logs,
        )
    else:
        kernel = Kernel.build(
            config=cfg,
            provider=provider,
            ui=ui or NullUI(),
            include_packages=include_packages,
            learned_dir=learned_dir,
            workspace=workspace,
            inject_failure=inject_failure,
            gateway_logs=gateway_logs,
        )
    return kernel


def _print_summary(kernel: Kernel) -> None:
    summary = kernel.summary()
    print(
        f"provider={type(kernel.provider).__name__} "
        f"tools={summary['tools']} skills={summary['skills']} "
        f"commands={summary['commands']} packages={summary['packages']}"
    )
    for warning in kernel.warnings:
        print(f"warning: {warning}", file=sys.stderr)


DEFAULT_DIRS = (
    "packages",
    "learned",
    "snapshots",
    "sessions",
    "cron",
    "optimizer",
    "optimizer/runs",
    "optimizer/rejected",
    "logs",
    "cache",
    "memory",
)

DEFAULT_CONFIG = """\
# MarkBot2 用户配置（由 `markbot init` 生成，可自由修改）
[agent]
provider = ""                  # openai_compat | anthropic | echo；留空按 model 推断
model = "claude-sonnet-4-5"
base_url = ""                  # 覆盖默认接口地址
api_key = ""                   # 模型服务密钥
max_tokens = 4096
max_steps = 30
tool_timeout_s = 60
request_timeout_s = 120        # 单次模型请求超时
max_retries = 2                # 模型请求失败后的重试次数（总尝试 = max_retries + 1）
retry_backoff_s = 2            # 重试退避基数，第 n 次重试等待 base * 2^(n-1) 秒
parallel_tools = true
context_budget_chars = 60000   # 超过 80% 触发上下文压缩
tool_result_max_chars = 16000  # 单条工具结果超限落盘，上下文只留预览与路径

[permissions]
profile = "standard"           # strict | standard | trusted
confirm_on_write = true
confirm_on_exec = true
# 免逐次确认的命令：按程序名（allow_exec）或整条命令前缀（allow_commands，* 结尾）
# allow_exec = ["curl", "git"]
# allow_commands = ["curl -s http://127.0.0.1:11235/*"]

[packages]
allow_project_packages = false # 允许加载 <project>/.markbot/ 下的包

# 供技能/扩展通过 secret:<name> 读取的密钥，不进模型上下文
[secrets]
# EXAMPLE_KEY = ""

[ui]
show_progress = true           # 交互模式下显示模型请求/工具调用等过程提示

# 按日期写入 ~/.markbot/logs/{runtime,interaction}/YYYY-MM-DD.md
[logging]
enabled = true
runtime = true
interaction = true

[optimizer]
enabled = true
k = 1
max_rollouts_per_task = 4
max_sessions_per_task = 3
max_tasks_per_run = 5
idle_minutes = 30

# 定时任务：调度器在 `markbot serve` / `markbot gateway start` 里运行
[cron]
enabled = true
default_delivery = "auto"      # auto | none | channel
# timezone = "Asia/Shanghai"   # cron 表达式默认时区，留空用本机时区

# 长期记忆：常驻块每轮注入；整理器只读会话归档，写入仍走同一套白名单与校验
[memory]
enabled = true
auto_consolidate = true        # serve/gateway 空闲时自动整理（Dream）
idle_minutes = 120             # Dream 整理周期（分钟）；0 表示关闭
idle_archive_minutes = 15      # 长驻会话空闲多久后归档快照；0 表示关闭
max_sessions_per_run = 20
resident_max_chars = 6000      # memory.md 注入上限
index_max_chars = 2000         # wiki 索引注入上限
notice_compaction = false      # 压缩是否在交互界面提示
show_stale = true              # 过期条目是否注入（带"待复核"标注）

# 渠道是外置进程，按需启用；app_secret 写到上面的 [secrets]：
# FEISHU_APP_SECRET = "..."
# [channels.feishu]
# app_id = "cli_xxx"
# verification_token = "xxx"
# port = 8790
# markbot_url = "http://127.0.0.1:8787"
"""


def cmd_init(argv: list[str]) -> int:
    home = markbot_home()
    if "--home" in argv:
        index = argv.index("--home")
        if index + 1 < len(argv):
            home = Path(argv[index + 1]).expanduser()
    force = "--force" in argv

    home.mkdir(parents=True, exist_ok=True)
    for name in DEFAULT_DIRS:
        (home / name).mkdir(parents=True, exist_ok=True)

    config = home / "config.toml"
    if force or not config.exists():
        config.write_text(DEFAULT_CONFIG, encoding="utf-8")
        print(f"已写入配置: {config}")
    else:
        print(f"配置已存在，保留: {config}（覆盖请加 --force）")

    trust = home / "trust.json"
    if not trust.exists():
        trust.write_text("{}\n", encoding="utf-8")
        print(f"已创建信任表: {trust}")

    print(f"MarkBot2 home 已就绪: {home}")
    print(f"下一步：编辑 {config} 填写 provider/model/base_url/api_key，然后运行 markbot doctor")
    return 0


# -------------------------------------------------------------------- commands
def cmd_chat(argv: list[str]) -> int:
    kernel = _build(ui=ConsoleUI(interactive=True))
    if kernel is None:
        return 2
    _print_summary(kernel)
    # 交互模式每次回合不归档；空闲线程负责把中途快照写入 sessions/
    run_lock = threading.Lock()
    if kernel.config.memory.enabled and kernel.config.memory.idle_archive_minutes > 0:
        threading.Thread(
            target=_idle_archive_loop,
            args=(kernel, run_lock, kernel.config.memory.idle_archive_minutes),
            daemon=True,
            name="memory-idle-archive",
        ).start()
    print(
        "输入 /exit 退出，/new 开始新会话，/session 查看会话 id，"
        "/skills 查看技能，/skill:<name> [补充] 直接使用技能。"
    )
    while True:
        try:
            line = input("\nmarkbot> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            break
        if line == "/new":
            with run_lock:
                asyncio.run(kernel.end_session())
            print("已结束当前会话，下一条输入将开启新会话。")
            continue
        if line == "/session":
            print(f"session: {kernel.session.id if kernel.session else '(未开始)'}")
            continue
        if line == "/skills":
            for meta in kernel.runtime.skills.all():
                print(f"- {meta.name}: {meta.description} [{meta.source}]")
            continue
        if line.startswith("/skill:"):
            body = line[len("/skill:") :]
            name, _, extra = body.partition(" ")
            with run_lock:
                result = asyncio.run(kernel.run_skill(name, extra.strip()))
        else:
            with run_lock:
                result = asyncio.run(kernel.run(line, archive=False))
        if result.error:
            print(f"error: {result.error}", file=sys.stderr)
        if result.text:
            print(result.text)
    with run_lock:
        asyncio.run(kernel.shutdown())
    return 0


def _dependency_errors(root: Path) -> list[str]:
    src = root / "src" / "markbot"
    if not src.exists():
        return []
    rules = [
        ("kernel", ("from markbot.extensions", "import markbot.extensions")),
        ("kernel", ("from markbot.optimizer", "import markbot.optimizer")),
        ("extensions", ("from markbot.kernel", "import markbot.kernel")),
    ]
    errors: list[str] = []
    for package, needles in rules:
        for path in (src / package).rglob("*.py"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for needle in needles:
                if needle in text:
                    errors.append(f"{path.relative_to(root)}: {needle}")
    return errors


def cmd_selftest(argv: list[str]) -> int:
    root = Path.cwd()
    failures: list[str] = []

    deps = _dependency_errors(root)
    if deps:
        failures.extend(f"依赖方向违规: {item}" for item in deps)
    else:
        print("PASS 依赖方向")

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cfg = load_config(home=home, project_root=root)
        provider = EchoProvider()
        try:
            kernel = Kernel.build(
                config=cfg, provider=provider, include_packages=False, ui=NullUI()
            )
            if "echo" not in kernel.runtime.tools:
                failures.append("内置 echo 工具未注册")
            else:
                print(f"PASS 运行时视图（tools={len(kernel.runtime.tools)}）")
            result = asyncio.run(kernel.run("selftest:echo hello"))
            if result.stopped_reason != "completed" or "tool result" not in result.text:
                failures.append(f"循环自检失败: {result.stopped_reason} / {result.error} / {result.text!r}")
            else:
                print(f"PASS Agent Loop（工具调用 {result.tool_calls} 次）")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"自检异常: {exc}")

    if "--inject-failure" in argv:
        index = argv.index("--inject-failure")
        name = argv[index + 1] if index + 1 < len(argv) else "office"
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_config(home=Path(tmp) / "home", project_root=root)
            kernel = Kernel.build(
                config=cfg,
                provider=EchoProvider(),
                include_packages=True,
                inject_failure=name,
                ui=NullUI(),
            )
            loaded = [pkg for pkg in kernel.runtime.packages if pkg.get("loaded")]
            if "echo" in kernel.runtime.tools:
                print(f"PASS 故障注入隔离（{name} 失败，Kernel 仍可启动，已加载包 {len(loaded)}）")
            else:
                failures.append("故障注入后 Kernel 不可用")

    if failures:
        for item in failures:
            print(f"FAIL {item}", file=sys.stderr)
        return 1
    print("selftest 全部通过")
    return 0


def cmd_doctor(argv: list[str]) -> int:
    kernel = _build(ui=NullUI(), require_provider=False)
    if kernel is None:
        return 2
    cfg = kernel.config
    print(f"home: {cfg.home}")
    print(f"project: {cfg.project_root}")
    print(f"config: {', '.join(cfg.loaded) or '（默认值）'}")
    print(f"profile: {cfg.permissions.profile}")
    print(f"provider: {type(kernel.provider).__name__} / model={cfg.agent.model}")
    _print_summary(kernel)

    if kernel.runtime.packages:
        print("\npackages:")
        for pkg in kernel.runtime.packages:
            state = "loaded" if pkg.get("loaded") else "skipped"
            print(
                f"- {pkg['id']} v{pkg.get('version') or '?'} [{pkg['scope']}] "
                f"trusted={pkg.get('trusted')} {state}"
                + (f" error={pkg['error']}" if pkg.get("error") else "")
            )
    if kernel.runtime.conflicts:
        print("\nconflicts:")
        for conflict in kernel.runtime.conflicts:
            print(f"- {conflict.tool}: {conflict.winner} > {conflict.loser}")

    exit_code = 0
    if "--validate" in argv:
        errors = _validate_packages(kernel)
        if errors:
            exit_code = 1
            for item in errors:
                print(f"FAIL {item}", file=sys.stderr)
        else:
            print("\nPASS manifest 与能力声明")
    if "--learned-paths" in argv:
        bad = illegal_paths(cfg.home / "learned")
        if bad:
            exit_code = 1
            for item in bad:
                print(f"FAIL 学习层越界: {item}", file=sys.stderr)
        else:
            print("PASS 学习层路径")
    return exit_code


def _validate_packages(kernel: Kernel) -> list[str]:
    errors: list[str] = []
    for pkg in kernel.runtime.packages:
        path = Path(pkg["path"])
        try:
            manifest = package_manifest(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{pkg['id']}: manifest 不合法 -> {exc}")
            continue
        ceiling = parse_capabilities(manifest.permissions.capabilities)
        origin = f"package:{manifest.name}"
        for name, source in kernel.runtime.sources.items():
            if source != origin:
                continue
            tool = kernel.runtime.tools.get(name)
            if tool is None:
                continue
            missing = uncovered_capabilities(tool.capabilities, ceiling)
            if missing:
                errors.append(f"{manifest.name}.{name}: 超出包能力 {', '.join(missing)}")
    return errors


def cmd_trust(argv: list[str]) -> int:
    if not argv:
        print("用法: markbot trust <name> [--project] [--revoke]", file=sys.stderr)
        return 2
    name = argv[0]
    scope = "project" if "--project" in argv else "user"
    home = markbot_home()
    project = Path.cwd()
    trust = TrustStore(home, project)
    if "--revoke" in argv:
        print("已撤销" if trust.revoke(name) else "信任表中没有该包")
        return 0
    roots = [
        (home / "packages", "user"),
        (project / "capabilities", "repo"),
        (project / ".markbot", "project"),
    ]
    for path, _scope in discover_packages(roots):
        try:
            manifest = package_manifest(path)
        except Exception:  # noqa: BLE001
            continue
        # 项目级扩展在运行时以固定 id "project" 校验信任。
        is_project = path.resolve() == (project / ".markbot").resolve()
        package_id = "project" if is_project else manifest.name
        if name in {package_id, manifest.name, path.name}:
            digest = trust.approve(package_id, path, scope=scope)
            print(f"已信任 {package_id} -> {digest}（{scope}）")
            return 0
    print(f"找不到包: {name}", file=sys.stderr)
    return 2


def cmd_skills(argv: list[str]) -> int:
    kernel = _build(ui=NullUI(), require_provider=False)
    if kernel is None:
        return 2
    metas = kernel.runtime.skills.all()
    if not metas:
        print("（无技能）")
    for meta in metas:
        print(f"- {meta.name}: {meta.description} [{meta.source}] {meta.path}")
    return 0


def cmd_packages(argv: list[str]) -> int:
    kernel = _build(ui=NullUI(), require_provider=False)
    if kernel is None:
        return 2
    if not kernel.runtime.packages:
        print("（无分发包）")
    for pkg in kernel.runtime.packages:
        state = "loaded" if pkg.get("loaded") else "skipped"
        print(f"- {pkg['id']} v{pkg.get('version') or '?'} [{pkg['scope']}] {state}")
    return 0


def cmd_reload(argv: list[str]) -> int:
    kernel = _build(ui=NullUI(), require_provider=False)
    if kernel is None:
        return 2
    view = kernel.reload()
    print(f"已重新合成运行时视图: tools={len(view.tools)} skills={len(view.skills)}")
    for warning in kernel.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def _make_run_case(cfg: Config, provider):
    cache: dict[str, Kernel] = {}
    fixtures_root = Path(cfg.project_root) / "evals"

    async def run_case(harness_dir: Path, case) -> str:
        key = str(Path(harness_dir).resolve())
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            for rel in case.files:
                src = fixtures_root / rel
                if not src.exists():
                    continue
                dst = workspace / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            kernel = cache.get(key)
            if kernel is None:
                kernel = Kernel.build(
                    config=cfg,
                    provider=provider,
                    include_packages=False,
                    learned_dir=Path(harness_dir),
                    ui=NullUI(),
                    workspace=workspace,
                )
                cache[key] = kernel
            kernel.policy.root = workspace
            result = await kernel.run(case.prompt, archive=False)
            return result.text or ""

    return run_case


def cmd_optimize(argv: list[str]) -> int:
    cfg = _load()
    if cfg is None:
        return 2
    dry_run = "--dry-run" in argv
    k = 1
    if "--k" in argv:
        index = argv.index("--k")
        try:
            k = int(argv[index + 1])
        except (IndexError, ValueError):
            print("error: --k 需要整数", file=sys.stderr)
            return 2
    try:
        provider = build_provider(cfg, cfg.secrets)
    except ProviderError as exc:
        if not dry_run:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"warning: 未配置 Provider，dry-run 使用 echo（{exc}）", file=sys.stderr)
        provider = EchoProvider()

    settings = OptimizerSettings(
        enabled=cfg.optimizer.enabled,
        k=k,
        max_rollouts_per_task=cfg.optimizer.max_rollouts_per_task,
        max_sessions_per_task=cfg.optimizer.max_sessions_per_task,
        max_tasks_per_run=cfg.optimizer.max_tasks_per_run,
        idle_minutes=cfg.optimizer.idle_minutes,
    )
    report = asyncio.run(
        optimize(
            home=cfg.home,
            learned_dir=cfg.home / "learned",
            evals_dir=Path(cfg.project_root) / "evals" / "cases",
            provider=provider,
            run_case=_make_run_case(cfg, provider),
            settings=settings,
            dry_run=dry_run,
            emit=print,
        )
    )
    print(
        f"run={report.run_id} tasks={report.tasks} "
        f"base={report.base_score} candidate={report.candidate_score} "
        f"selected={report.selected} promoted={report.promoted}"
    )
    for reason in report.reasons:
        print(f"- {reason}")
    return 0 if (report.promoted or dry_run) else 1


def cmd_rollback(argv: list[str]) -> int:
    cfg = _load()
    if cfg is None:
        return 2
    snapshots = cfg.home / "snapshots"
    if "--list" in argv:
        items = list_snapshots(snapshots)
        if not items:
            print("（无快照）")
        for item in items:
            print(f"- {item['name']}  {item['hash']}  {item['created_at']}")
        return 0
    target = None
    if "--to" in argv:
        index = argv.index("--to")
        target = argv[index + 1] if index + 1 < len(argv) else None
    try:
        restored = restore_snapshot(cfg.home / "learned", snapshots, target)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"已回滚到快照 {restored}")
    return 0


def cmd_memory(argv: list[str]) -> int:
    cfg = _load()
    if cfg is None:
        return 2
    action = argv[0] if argv else "status"
    home = Path(cfg.home)
    learned = home / "learned"
    store = MemoryStore(learned)
    journal = Journal(home)

    if action == "status":
        resident = store.resident_path
        text = resident.read_text(encoding="utf-8") if resident.exists() else ""
        entries = [line for line in text.splitlines() if line.lstrip().startswith("- ")]
        stale = sum(
            1
            for line in entries
            if lifespan_status(split_meta(line)[1]) == "stale"
        )
        pages = sorted(store.wiki_dir.glob("*.md")) if store.wiki_dir.exists() else []
        cursor = load_cursor(home)
        pending = len(journal.scan_since(cursor, max_sessions=999))
        print(f"常驻记忆: {resident} ({len(text)} 字符, {len(entries)} 条, 待复核 {stale})")
        print(f"wiki: {len(pages)} 页  {store.wiki_dir}")
        print(f"整理游标: {cursor.last_run or '（未运行）'}  待整理会话 {pending}")
        return 0

    if action == "search":
        query = " ".join(argv[1:]).strip()
        if not query:
            print("error: 需要检索关键词", file=sys.stderr)
            return 2
        hits = store.search(query, limit=10, scope="all")
        hits += [
            {
                "source": "sessions",
                "path": f"sessions/{hit.session_id}/{hit.source}.jsonl",
                "line": hit.line,
                "text": hit.text,
            }
            for hit in journal.search(query, limit=10)
        ]
        if not hits:
            print("（无命中）")
            return 0
        for hit in hits:
            print(f"- [{hit['source']}] {hit['path']}:{hit['line']} — {hit['text']}")
        return 0

    if action == "consolidate":
        try:
            provider = build_provider(cfg, cfg.secrets)
        except ProviderError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        report = asyncio.run(
            Consolidator(
                learned,
                home=home,
                provider=provider,
                max_sessions=cfg.memory.max_sessions_per_run,
            ).run()
        )
        if report.no_input:
            print("没有待整理的会话归档")
        print(
            f"sessions={report.sessions} written={report.written} "
            f"skipped={report.skipped} failed={report.failed} "
            f"cursor_advanced={report.cursor_advanced}"
        )
        if report.incomplete:
            print(f"未完成: {report.incomplete}", file=sys.stderr)
        return 0 if not report.incomplete else 1

    if action == "restore":
        if "--list" in argv:
            names = list_memory_snapshots(home)
            print("\n".join(f"- {name}" for name in names) if names else "（无快照）")
            return 0
        target = None
        if "--to" in argv:
            index = argv.index("--to")
            target = argv[index + 1] if index + 1 < len(argv) else None
        try:
            changed = restore_memory_snapshot(home, learned, target, dry_run=True)
            if not changed:
                print("快照与当前记忆一致，无需恢复")
                return 0
            print(f"将恢复 {len(changed)} 个文件：")
            for rel in changed:
                print(f"- {rel}")
            if "--yes" not in argv:
                print("确认无误后加 --yes 执行（先预览再执行）")
                return 0
            restore_memory_snapshot(home, learned, target)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"已恢复记忆快照 {target or '（最新）'}")
        return 0

    print(
        "用法: markbot memory status|search <词>|consolidate|"
        "restore [--list|--to NAME|--yes]",
        file=sys.stderr,
    )
    return 2


_WATCH_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", ".ruff_cache"}


def _runtime_fingerprint(kernel: Kernel) -> tuple[tuple[str, int, int], ...]:
    """收集扩展相关路径的 mtime/size 指纹，用于发现插件安装、删除与信任变更。"""
    roots = [
        kernel.home / "packages",
        kernel.home / "learned",
        kernel.home / "trust.json",
        kernel.project_root / "capabilities",
        kernel.project_root / ".markbot",
    ]
    entries: list[tuple[str, int, int]] = []
    for root in roots:
        if not root.exists():
            entries.append((str(root), -1, -1))
            continue
        if root.is_file():
            stat = root.stat()
            entries.append((str(root), stat.st_mtime_ns, stat.st_size))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _WATCH_SKIP_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


def _watch_runtime(
    kernel: Kernel, run_lock: Any, interval_s: float = 2.0
) -> None:
    fingerprint = _runtime_fingerprint(kernel)
    while True:
        time.sleep(interval_s)
        current = _runtime_fingerprint(kernel)
        if current == fingerprint:
            continue
        time.sleep(0.5)  # 等待写入完成，避免读到半个文件
        fingerprint = _runtime_fingerprint(kernel)
        with run_lock:
            try:
                view = kernel.reload()
            except Exception as exc:  # noqa: BLE001
                print(f"[watch] 重新加载失败: {exc}", file=sys.stderr)
                continue
        print(
            f"[watch] 检测到扩展变更，已重新加载 tools={len(view.tools)} "
            f"skills={len(view.skills)}",
            file=sys.stderr,
        )
        for warning in kernel.warnings:
            print(f"[watch] warning: {warning}", file=sys.stderr)


_CONFIRM_LOCK = threading.Lock()
_PENDING_CONFIRMS: dict[str, dict[str, Any]] = {}


class _ChatConfirmUI(NullUI):
    """把工具的确认请求转发到聊天，并等待用户回复。

    服务端无法直接给用户发消息，因此把问题 POST 给渠道（``hook``），渠道负责
    发消息；用户在聊天里回复后，渠道回调 ``POST /confirm`` 唤醒这里。
    """

    def __init__(
        self,
        hook: str,
        chat_id: str,
        user_id: str = "",
        timeout_s: float = 180.0,
    ) -> None:
        self._hook = hook
        self._chat_id = chat_id
        self._user_id = user_id
        self._timeout_s = timeout_s

    async def confirm(self, title: str, message: str) -> bool:
        token = uuid.uuid4().hex
        slot = {"event": threading.Event(), "approved": False}
        with _CONFIRM_LOCK:
            _PENDING_CONFIRMS[token] = slot
        try:
            body = json.dumps(
                {
                    "token": token,
                    "chat_id": self._chat_id,
                    "user_id": self._user_id,
                    "title": title,
                    "message": message,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = urllib.request.Request(
                self._hook,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=10).read()
            delivered = True
        except Exception as exc:  # noqa: BLE001 - 渠道不可达时按拒绝处理
            logging.getLogger("markbot.serve").warning("确认请求发送失败: %s", exc)
            delivered = False
        if not delivered:
            with _CONFIRM_LOCK:
                _PENDING_CONFIRMS.pop(token, None)
            return False
        try:
            await asyncio.to_thread(slot["event"].wait, self._timeout_s)
        finally:
            with _CONFIRM_LOCK:
                _PENDING_CONFIRMS.pop(token, None)
        return bool(slot["approved"] and slot["event"].is_set())


def _resolve_confirm(token: str, approved: bool) -> bool:
    with _CONFIRM_LOCK:
        slot = _PENDING_CONFIRMS.get(token)
    if slot is None:
        return False
    slot["approved"] = bool(approved)
    slot["event"].set()
    return True


def _memory_idle_loop(kernel: Kernel, run_lock: threading.Lock, idle_minutes: int) -> None:
    """定期整理记忆；模型调用不占锁，只有落盘/回滚阶段与聊天串行。"""
    logger = logging.getLogger("markbot.memory")
    interval = max(60, int(idle_minutes) * 60)
    while True:
        time.sleep(interval)
        try:
            report = asyncio.run(
                Consolidator(
                    kernel.home / "learned",
                    home=kernel.home,
                    provider=kernel.provider,
                    max_sessions=kernel.config.memory.max_sessions_per_run,
                    apply_lock=run_lock,
                ).run()
            )
            logger.info(
                "记忆整理 sessions=%d written=%d skipped=%d failed=%d cursor=%s",
                report.sessions,
                report.written,
                report.skipped,
                report.failed,
                report.cursor_advanced,
            )
        except Exception:  # noqa: BLE001 - 后台线程不能因单次失败退出
            logger.exception("记忆整理异常")


def _idle_archive_loop(
    kernel: Kernel, run_lock: threading.Lock, idle_minutes: int
) -> None:
    """长驻会话空闲后补写归档，让整理器能看到聊到一半的内容。

    每 60 秒检查一次；内核忙时直接跳过，下个周期再试。
    """
    logger = logging.getLogger("markbot.memory")
    while True:
        time.sleep(60)
        if not run_lock.acquire(blocking=False):
            continue
        try:
            sessions = asyncio.run(kernel.archive_idle(idle_minutes))
            if sessions:
                logger.info("空闲归档 sessions=%d", len(sessions))
        except Exception:  # noqa: BLE001 - 后台线程不能因单次失败退出
            logger.exception("空闲归档异常")
        finally:
            run_lock.release()


def cmd_serve(argv: list[str]) -> int:
    port = 8787
    if "--port" in argv:
        index = argv.index("--port")
        port = int(argv[index + 1])
    host = "127.0.0.1"
    if "--host" in argv:
        index = argv.index("--host")
        host = argv[index + 1]
    log_file = gateway_log.install(markbot_home())
    logger = logging.getLogger("markbot.serve")
    logger.info("HTTP 入口启动 %s:%d，日志: %s", host, port, log_file)
    kernel = _build(ui=NullUI(), gateway_logs=True)
    if kernel is None:
        return 2
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # 同一 Kernel 的会话是有状态的，HTTP 请求串行执行。
    run_lock = threading.Lock()
    threading.Thread(
        target=_watch_runtime, args=(kernel, run_lock), daemon=True, name="runtime-watch"
    ).start()

    cron: CronService | None = None
    if kernel.config.cron.enabled:
        cron = CronService(
            kernel.home / "cron" / "jobs.json",
            on_job=build_job_runner(kernel, run_lock=run_lock),
            default_delivery=kernel.config.cron.default_delivery,
            default_timezone=kernel.config.cron.timezone or None,
        )
        set_service(cron)
        cron.start_in_thread()
        logger.info("定时任务调度器已启动 store=%s", cron.store_path)
    else:
        logger.info("定时任务调度器已关闭（[cron] enabled = false）")

    if (
        kernel.config.memory.enabled
        and kernel.config.memory.auto_consolidate
        and kernel.config.memory.idle_minutes > 0
    ):
        threading.Thread(
            target=_memory_idle_loop,
            args=(kernel, run_lock, kernel.config.memory.idle_minutes),
            daemon=True,
            name="memory-idle",
        ).start()
        logger.info(
            "记忆整理已启用 interval_minutes=%d", kernel.config.memory.idle_minutes
        )
    else:
        logger.info("记忆整理未启用（[memory] enabled/auto_consolidate/idle_minutes）")

    if kernel.config.memory.enabled and kernel.config.memory.idle_archive_minutes > 0:
        threading.Thread(
            target=_idle_archive_loop,
            args=(kernel, run_lock, kernel.config.memory.idle_archive_minutes),
            daemon=True,
            name="memory-idle-archive",
        ).start()
        logger.info(
            "空闲归档已启用 idle_minutes=%d",
            kernel.config.memory.idle_archive_minutes,
        )
    else:
        logger.info("空闲归档未启用（[memory] enabled/idle_archive_minutes）")

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                jobs = 0
                if cron is not None:
                    try:
                        jobs = len(cron.list_jobs())
                    except Exception:  # noqa: BLE001 - 健康检查不能因坏文件失败
                        jobs = -1
                self._json(
                    200,
                    {
                        "status": "ok",
                        "tools": len(kernel.runtime.tools),
                        "skills": len(kernel.runtime.skills),
                        "cron": {"enabled": cron is not None, "jobs": jobs},
                    },
                )
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                if self.path == "/confirm":
                    self._confirm()
                else:
                    self._chat()
            except Exception:  # noqa: BLE001 - 兜底，避免请求线程静默退出
                logging.getLogger("markbot.http").exception("处理请求失败 path=%s", self.path)
                try:
                    self._json(500, {"error": "internal error"})
                except OSError:
                    pass

        def _confirm(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "invalid json"})
                return
            token = str(data.get("token") or "")
            ok = _resolve_confirm(token, bool(data.get("approved")))
            logger.info("/confirm token=%s approved=%s ok=%s", token[:8], data.get("approved"), ok)
            self._json(200 if ok else 404, {"ok": ok})

        def _chat(self) -> None:
            if self.path != "/chat":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                prompt = str(data.get("prompt") or "")
                session_id = str(data.get("session_id") or "") or None
                grants = [str(item) for item in (data.get("grants") or []) if str(item).strip()]
                confirm_hook = data.get("confirm_hook") or {}
                notify_hook = data.get("notify_hook") or {}
            except ValueError:
                self._json(400, {"error": "invalid json"})
                return
            if not prompt:
                self._json(400, {"error": "prompt is required"})
                return
            logger.info(
                "/chat 请求 session=%s prompt_chars=%d grants=%s",
                session_id or "-",
                len(prompt),
                ",".join(grants) or "-",
            )
            with run_lock:
                added = kernel.policy.push_grants(grants) if grants else set()
                previous_ui = None
                hook_url = str(confirm_hook.get("url") or "")
                if hook_url:
                    previous_ui = kernel.set_ui(
                        _ChatConfirmUI(
                            hook_url,
                            str(confirm_hook.get("chat_id") or ""),
                            str(confirm_hook.get("user_id") or ""),
                        )
                    )
                # 渠道把“主动发消息”的回调地址带进来，cron 工具据此决定结果投递到哪
                notify_url = str(notify_hook.get("url") or "")
                notify_chat = str(notify_hook.get("chat_id") or "")
                metadata = (
                    {"notify": {"url": notify_url, "chat_id": notify_chat}}
                    if notify_url and notify_chat
                    else None
                )
                try:
                    result = asyncio.run(
                        kernel.run(
                            prompt,
                            session_id=session_id,
                            new_session=not session_id,
                            metadata=metadata,
                        )
                    )
                finally:
                    if previous_ui is not None:
                        kernel.set_ui(previous_ui)
                    if added:
                        kernel.policy.pop_grants(added)
            logger.info(
                "/chat 完成 session=%s reason=%s error=%s",
                result.session_id,
                result.stopped_reason,
                result.error,
            )
            self._json(
                200,
                {
                    "text": result.text,
                    "stopped_reason": result.stopped_reason,
                    "error": result.error,
                    "session_id": result.session_id,
                },
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            logging.getLogger("markbot.http").info(fmt % args)

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"MarkBot2 HTTP 入口: http://{host}:{port}  (GET /health, POST /chat, POST /confirm)")
    print(f"运行日志: {log_file}")
    print(
        f"定时任务: {'已启用 ' + str(cron.store_path) if cron is not None else '已禁用'}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 - 记录后退出，避免无提示挂起
        logger.exception("HTTP 服务异常退出")
        return 1
    finally:
        if cron is not None:
            cron.stop()
            set_service(None)
        server.server_close()
        asyncio.run(kernel.shutdown())
    return 0


COMMANDS = {
    "init": cmd_init,
    "chat": cmd_chat,
    "selftest": cmd_selftest,
    "doctor": cmd_doctor,
    "trust": cmd_trust,
    "skills": cmd_skills,
    "packages": cmd_packages,
    "reload": cmd_reload,
    "cron": cron_command,
    "optimize": cmd_optimize,
    "rollback": cmd_rollback,
    "memory": cmd_memory,
    "serve": cmd_serve,
    "gateway": gateway_command,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if not args:
        return cmd_chat([])
    command = args[0]
    if command in COMMANDS:
        return COMMANDS[command](args[1:])
    prompt = " ".join(args)
    kernel = _build(ui=ConsoleUI(interactive=False))
    if kernel is None:
        return 2
    try:
        result = asyncio.run(kernel.run(prompt))
    finally:
        asyncio.run(kernel.shutdown())
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    if result.text:
        print(result.text)
    return 0 if not result.error else 1


__all__ = ["main"]
