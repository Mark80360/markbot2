"""MarkBot2 CLI：chat / selftest / doctor / trust / reload / optimize / rollback / serve / gateway。"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

from markbot.api.ui import ConsoleUI, NullUI
from markbot.cli.eval_cmd import eval_command
from markbot.cli.gateway import gateway_command
from markbot.cli.secrets_cmd import secrets_command
from markbot.cli.service import service_command
from markbot.cli.sessions_cmd import sessions_command
from markbot.cron.cli import cron_command
from markbot.kernel import gateway_log, local_auth
from markbot.kernel.app import Kernel
from markbot.kernel.config import (
    ALLOW_PLAINTEXT_ENV,
    Config,
    PlaintextSecretsError,
    enforce_secret_policy,
    load_config,
    load_projects,
    markbot_home,
    save_project,
    valid_project_name,
)
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
from markbot.server.http import (  # noqa: F401 - 兼容旧引用（测试与外部脚本）
    _ChatConfirmUI,
    _hook_url,
    _idle_archive_loop,
    _memory_idle_loop,
    _resolve_confirm,
    _runtime_fingerprint,
    _sanitize_grants,
    _watch_runtime,
    run_server,
)

HELP = """MarkBot —— 极薄 Kernel + 四条扩展车道

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
  markbot optimize [--dry-run] [--k N] [--approve-skills] [--gate-suites coding,browser]
  markbot rollback [--list] [--to HASH]
  markbot memory status|search|consolidate|restore
                                查看、检索、整理与回滚长期记忆
  markbot serve [--port 8787] [--host 127.0.0.1] [--allow-remote]
  markbot gateway start|run|stop|restart|status
                                run = 前台守护模式（子进程退出自动拉起）
  markbot project add <名称> <路径> | list | remove <名称>
                                登记项目；聊天里 /project <名称> 切换，CLI 用 --project <名称>
  markbot eval --suite coding|browser [--case 名称] [--json] [--keep] [--max-cases N]
               [--record] [--fail-on-regression] [--baseline FILE] [--min-ratio X]
                                评测：编码（evals/coding/ 小仓库 + 测试判分）、浏览器（evals/browser/）；
                                --record 写入趋势，--fail-on-regression 与上次比较
  markbot eval --trend [--suite coding]
                                查看评测趋势（<home>/logs/optimizer.jsonl）
  markbot stats [--days 7] [--json]
                                按天统计回合、失败率、工具失败、token 与定时任务
  markbot service install|uninstall|status [--dry-run]
                                开机自启：Linux systemd 用户服务 / Windows 计划任务
  markbot sessions status|migrate [--dry-run]
                                会话归档存储统计；清理可从 events.jsonl 重建的派生文件
  markbot secrets status|migrate [--dry-run]
                                检查 / 迁移 config.toml 里的明文密钥到 secrets.toml（0600）
  --allow-plaintext-secrets     全局选项：config.toml 含明文密钥时仍允许启动（临时放行）
                                后台托管 HTTP 入口与飞书渠道（日志在 ~/.markbot/gateway/）

配置：
  ~/.markbot/config.toml        模型、权限、包与优化器配置（markbot init 生成）
  ~/.markbot/trust.json         包信任表
  ~/.markbot/secrets.toml       密钥（api_key、[secrets]、渠道密钥），权限 0600

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
    enforce_secrets: bool = False,
) -> Kernel | None:
    cfg = _load()
    if cfg is None:
        return None
    if enforce_secrets:
        # 对话/服务入口：config.toml 含明文密钥时拒绝启动（ADR 0009）
        try:
            enforce_secret_policy(cfg)
        except PlaintextSecretsError as exc:
            print(f"error: {exc}", file=sys.stderr)
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


DEFAULT_SECRETS = """\
# MarkBot 密钥文件：权限必须是 0600，不要提交、同步或分享。
# 结构与 config.toml 相同，加载时叠加在 config.toml 之上。
[agent]
# api_key = ""

[secrets]
# FEISHU_APP_SECRET = ""
"""

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
# MarkBot 用户配置（由 `markbot init` 生成，可自由修改）
# 初始化即最高权限：trusted 档位 + 不逐次确认 + 常用程序全部放行，
# 尽量减少确认弹窗与命令限制。需要收紧时改 [permissions] 一节即可。
[agent]
provider = ""                  # openai_compat | anthropic | echo；留空按 model 推断
model = "claude-sonnet-4-5"
base_url = ""                  # 覆盖默认接口地址
api_key = ""                   # 不要写在这里：放到 secrets.toml，或写 "env:OPENAI_API_KEY"
max_tokens = 4096
max_steps = 30
tool_timeout_s = 60
request_timeout_s = 120        # 单次模型请求超时
max_retries = 2                # 模型请求失败后的重试次数（总尝试 = max_retries + 1）
retry_backoff_s = 2            # 重试退避基数，第 n 次重试等待 base * 2^(n-1) 秒
parallel_tools = true
context_budget_ratio = 0.8     # 用量达到 max_tokens 的 80% 时触发上下文压缩
tool_result_max_chars = 16000  # 单条工具结果超限落盘，上下文只留预览与路径

[permissions]
profile = "trusted"            # strict | standard | trusted（trusted = 最高权限，改这里收紧）
confirm_on_write = false       # 写文件不再逐次确认
confirm_on_exec = false        # 执行命令不再逐次确认
trusted_exec = "all"           # trusted 档位下所有命令免确认（"listed" 则只有 allow_exec 内免确认）
# 免逐次确认的命令：按程序名（allow_exec）或整条命令前缀（allow_commands，* 结尾）
allow_exec = [
  "curl", "git", "date", "tar", "zip", "unzip",                 # 网络 / 版本 / 时间 / 打包
  "ls", "cat", "head", "tail", "wc", "grep", "sort", "uniq",    # 只读查看与统计
  "cut", "file", "stat", "du", "df", "which", "pwd", "find", "echo", "touch", "cd",
  "mkdir", "cp", "mv", "sed", "awk", "tr",                      # 目录/文本处理（sed -i、cp/mv 会改文件）
  "python", "python3", "pip", "pip3", "node", "npm", "npx", "playwright",
  "chmod", "ps", "pgrep", "kill", "pkill", "ss", "ip",
]
# allow_commands = ["curl -s http://127.0.0.1:11235/*"]
# 需要访问工作区外目录时（默认只允许工作区与本会话临时目录）：
# extra_writable_dirs = ["D:/data"]
#
# 无论档位如何，以下命令仍会确认一次：内联解释器（python -c、node -e）、
# shell 与提权（sh/bash/sudo…）、命令替换（$()、反引号）、解码管道、危险 git 操作。
#
# 允许注入到命令环境变量的密钥（值放 secrets.toml），["*"] 表示不限程序：
# [permissions.secret_env]
# CRAWL4AI_API_TOKEN = ["*"]

[packages]
allow_project_packages = true  # 允许加载 <project>/.markbot/ 下的包

# 浏览器与桌面控制：默认不确认（"none"），需要时改回 "risky" 或 "all"
[tools.browser]
confirm = "none"
confirm_script = false

# MCP server（需要安装可选依赖：pip install "markbot[mcp]"）
# [tools.mcp_servers.filesystem]
# type = "stdio"                # stdio | sse | streamableHttp；留空按 command/url 推断
# command = "npx"
# args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
# enabled_tools = ["*"]         # ["*"] = 全部能力；指定名单时只注册这些工具，不注册资源/提示
# tool_timeout = 30             # 单次调用超时（秒）
#
# [tools.mcp_servers.remote]
# url = "https://example.com/mcp"
# headers = { Authorization = "Bearer ${MCP_TOKEN}" }  # ${VAR} 从进程环境变量展开

# 供技能/扩展通过 secret:<name> 读取的密钥，不进模型上下文。
# 取值请写在 secrets.toml（0600）里；这里只适合写 "env:NAME" 形式的引用。
[secrets]
# EXAMPLE_KEY = "env:EXAMPLE_KEY"

[ui]
show_progress = true           # 交互模式下显示模型请求/工具调用等过程提示

# 按日期写入 ~/.markbot/logs/{runtime,interaction}/YYYY-MM-DD.md
[logging]
enabled = true
runtime = true
interaction = false            # 交互日志含完整模型报文（已脱敏），排查问题时再打开
retention_days = 30            # runtime / gateway 按日文件保留天数
interaction_retention_days = 7
max_total_mb = 500             # logs/ + gateway/ 总量上限，超出从最旧的交互日志开始删

[optimizer]
enabled = true
k = 1
max_rollouts_per_task = 4
max_sessions_per_task = 3
max_tasks_per_run = 5
idle_minutes = 30
min_eval_cases = 10            # 回归集少于此数不晋升
llm_judge = false              # 用当前模型给 llm_judge 检查打分

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

# 渠道是外置进程，按需启用；app_secret 写到 secrets.toml 的 [secrets]：
# FEISHU_APP_SECRET = "..."
# [channels.feishu]
# app_id = "cli_xxx"
# verification_token = "xxx"
# port = 8790
# markbot_url = "http://127.0.0.1:8787"
# streaming = true             # 用 CardKit 流式卡片展示模型输出
#
# 入站语音转写（可选，密钥也可以放到 [secrets].TRANSCRIPTION_API_KEY）：
# [transcription]
# enabled = true
# provider = "groq"            # groq | openai
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

    secrets_file = home / "secrets.toml"
    if not secrets_file.exists():
        fd = os.open(secrets_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_SECRETS)
        print(f"已创建密钥文件: {secrets_file}（0600）")

    trust = home / "trust.json"
    if not trust.exists():
        trust.write_text("{}\n", encoding="utf-8")
        print(f"已创建信任表: {trust}")

    print(f"MarkBot2 home 已就绪: {home}")
    print(
        f"下一步：编辑 {config} 填写 provider/model/base_url，"
        f"把 api_key 写进 {secrets_file}，然后运行 markbot doctor"
    )
    return 0


# -------------------------------------------------------------------- commands
def cmd_chat(argv: list[str]) -> int:
    kernel = _build(ui=ConsoleUI(interactive=True), enforce_secrets=True)
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
                result = asyncio.run(kernel.run(line, archive=False, project=_PROJECT_FLAG[0]))
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
        # 记忆是扩展车道上的提供者（runtime.entries["context_providers"]），Kernel 不依赖它
        ("kernel", ("from markbot.memory", "import markbot.memory")),
        ("extensions", ("from markbot.kernel", "import markbot.kernel")),
        # schema 是纯数据、api 是冻结契约：都不能依赖实现层
        ("schema", ("from markbot.api", "from markbot.kernel", "from markbot.memory",
                    "from markbot.optimizer", "from markbot.cron")),
        ("api", ("from markbot.kernel", "from markbot.memory", "from markbot.optimizer",
                 "from markbot.cron", "from markbot.extensions")),
        # memory 是扩展与 CLI 使用的库层：不能依赖 kernel/optimizer
        ("memory", ("from markbot.kernel", "import markbot.kernel",
                    "from markbot.optimizer", "import markbot.optimizer")),
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
                f"trusted={pkg.get('trusted')}"
                + (f"({pkg['trust_source']})" if pkg.get("trust_source") else "")
                + f" {state}"
                + (f" error={pkg['error']}" if pkg.get("error") else "")
            )
    skipped = [
        pkg for pkg in kernel.runtime.packages if not pkg.get("loaded") and not pkg.get("error")
    ]
    if skipped:
        print("\nskipped packages（未信任，未加载）:")
        for pkg in skipped:
            print(f"- {pkg['id']} [{pkg['scope']}] -> 运行 `markbot trust {pkg['id']}` 启用")
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
    fixtures_root = Path(cfg.project_root) / "evals"

    async def run_case(harness_dir: Path, case) -> str:
        # 每条用例一个全新 Kernel：工作区、权限守卫与执行环境都绑定在本用例的临时目录上。
        # 旧实现按 harness 缓存 Kernel 并只改 policy.root，执行环境仍指向第一条用例
        # 已删除的临时目录，第二条起的用例读不到 fixtures，评分被系统性拉低。
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            for rel in case.files:
                src = fixtures_root / rel
                if not src.exists():
                    continue
                dst = workspace / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            kernel = Kernel.build(
                config=cfg,
                provider=provider,
                include_packages=False,
                learned_dir=Path(harness_dir),
                ui=NullUI(),
                workspace=workspace,
            )
            try:
                result = await kernel.run(case.prompt, archive=False)
            finally:
                # 不归档：评测会话不能进入 sessions/，否则会被当成「已完成的真实会话」
                await kernel.shutdown(archive=False)
            return result.text or ""

    return run_case


JUDGE_SYSTEM = (
    "你是严格的评分员。根据评分标准判断回答是否合格。"
    "只输出一个词：PASS 或 FAIL，不要解释。"
)


def _make_judge(provider):
    async def judge(answer: str, rubric: str) -> bool:
        reply = await provider.complete(
            {
                "system": JUDGE_SYSTEM,
                "messages": [
                    {
                        "role": "user",
                        "content": f"评分标准：\n{rubric}\n\n待评回答：\n{answer[:8000]}",
                    }
                ],
                "max_tokens": 8,
            }
        )
        verdict = (getattr(reply, "text", "") or "").strip().upper()
        return verdict.startswith("PASS")

    return judge


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
        min_eval_cases=cfg.optimizer.min_eval_cases,
        approve_skills="--approve-skills" in argv,
        judge=_make_judge(provider) if cfg.optimizer.llm_judge else None,
        extra_suites=_gate_suites(cfg, provider, argv),
        gate_tolerance=float(getattr(cfg.optimizer, "gate_tolerance", 0.0) or 0.0),
    )
    if settings.extra_suites:
        print("晋升门禁评测集：" + "、".join(settings.extra_suites), file=sys.stderr)
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
    for name, score in report.suite_scores.items():
        print(f"  {name}: {score}")
    for reason in report.reasons:
        print(f"- {reason}")
    return 0 if (report.promoted or dry_run) else 1


def _gate_suites(cfg: Config, provider, argv: list[str]) -> dict:
    """``[optimizer] gate_suites``（或 ``--gate-suites coding,browser``）-> 门禁评测集（ADR 0016）。"""
    from markbot.cli.eval_cmd import SUITES, suite_runner

    names = list(getattr(cfg.optimizer, "gate_suites", []) or [])
    if "--gate-suites" in argv:
        index = argv.index("--gate-suites")
        raw = argv[index + 1] if index + 1 < len(argv) else ""
        names = [part.strip() for part in raw.split(",") if part.strip() and part.strip() != "none"]
    suites: dict = {}
    for name in dict.fromkeys(names):
        if name not in SUITES:
            print(f"warning: 未知门禁评测集 {name}，已忽略（可选 {', '.join(SUITES)}）", file=sys.stderr)
            continue
        directory = Path(cfg.project_root) / "evals" / name
        if not directory.is_dir():
            print(f"warning: 门禁评测集 {name} 不存在 {directory}，已忽略", file=sys.stderr)
            continue
        suites[name] = suite_runner(
            name, directory, provider=provider, max_cases=int(getattr(cfg.optimizer, "gate_max_cases", 5) or 0)
        )
    return suites


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


def cmd_stats(argv: list[str]) -> int:
    """按天汇总 logs/events/*.jsonl：回合、失败、工具失败率、token、定时任务（ADR 0011）。"""
    from markbot.kernel.metrics import render_summary, summarize

    days = 7
    if "--days" in argv:
        try:
            days = max(1, int(argv[argv.index("--days") + 1]))
        except (IndexError, ValueError):
            print("用法: markbot stats [--days N] [--json]", file=sys.stderr)
            return 2
    report = summarize(markbot_home(), days)
    if "--json" in argv:
        import json

        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_summary(report))
    return 0


def cmd_project(argv: list[str]) -> int:
    """项目登记：add <名称> <路径> | list | remove <名称>（ADR 0013）。"""
    home = markbot_home()
    action = argv[0] if argv else "list"
    if action == "list":
        projects = load_projects(home, (_load_raw_projects()))
        if not projects:
            print("还没有登记项目。用法: markbot project add <名称> <路径>")
        for name, path in projects.items():
            print(f"{name}\t{path}")
        return 0
    if action == "add" and len(argv) >= 3:
        name, raw = argv[1], argv[2]
        target = Path(raw).expanduser()
        if not valid_project_name(name):
            print("error: 项目名只能包含字母、数字、. _ -，且以字母或数字开头", file=sys.stderr)
            return 2
        if not target.is_dir():
            print(f"error: 目录不存在: {target}", file=sys.stderr)
            return 2
        resolved = target.resolve()
        if resolved == home.resolve() or home.resolve() in resolved.parents:
            print("error: 不能把 MarkBot home 登记为项目", file=sys.stderr)
            return 2
        save_project(home, name, resolved)
        print(f"已登记项目 {name} -> {resolved}（运行中的服务需要 markbot reload 或重启后生效）")
        return 0
    if action == "remove" and len(argv) >= 2:
        save_project(home, argv[1], None)
        print(f"已移除项目 {argv[1]}")
        return 0
    print("用法: markbot project add <名称> <路径> | list | remove <名称>", file=sys.stderr)
    return 2


def _load_raw_projects() -> dict:
    cfg = _load()
    return dict(cfg.raw.get("projects") or {}) if cfg is not None else {}


def cmd_serve(argv: list[str]) -> int:
    port = 8787
    if "--port" in argv:
        index = argv.index("--port")
        port = int(argv[index + 1])
    host = "127.0.0.1"
    if "--host" in argv:
        index = argv.index("--host")
        host = argv[index + 1]
    if not local_auth.is_loopback_host(host) and "--allow-remote" not in argv:
        print(
            f"error: 拒绝监听非回环地址 {host}：/chat 可以驱动工具执行。"
            "确需对外提供服务请加 --allow-remote，并放在反向代理/防火墙之后。",
            file=sys.stderr,
        )
        return 2
    auth_token = local_auth.load_or_create_token(markbot_home())
    log_file = gateway_log.install(markbot_home())
    logger = logging.getLogger("markbot.serve")
    logger.info("HTTP 入口启动 %s:%d，日志: %s", host, port, log_file)
    kernel = _build(ui=NullUI(), gateway_logs=True, enforce_secrets=True)
    if kernel is None:
        return 2
    return run_server(
        kernel, host=host, port=port, log_file=log_file, auth_token=auth_token
    )


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
    "secrets": secrets_command,
    "service": service_command,
    "project": cmd_project,
    "eval": eval_command,
    "stats": cmd_stats,
    "sessions": sessions_command,
}


ALLOW_PLAINTEXT_FLAG = "--allow-plaintext-secrets"
# chat / 一次性执行的 --project <名称>（ADR 0013）
_PROJECT_FLAG: list[str | None] = [None]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if ALLOW_PLAINTEXT_FLAG in args:
        # 全局开关：写进环境变量，gateway 拉起的 serve 子进程也能继承
        args = [arg for arg in args if arg != ALLOW_PLAINTEXT_FLAG]
        os.environ[ALLOW_PLAINTEXT_ENV] = "1"
    _PROJECT_FLAG[0] = None
    if "--project" in args and (args[0] in ("chat",) or args[0] not in COMMANDS):
        index = args.index("--project")
        if index + 1 >= len(args):
            print("error: --project 需要项目名", file=sys.stderr)
            return 2
        _PROJECT_FLAG[0] = args[index + 1]
        args = args[:index] + args[index + 2 :]
    if args and args[0] in {"-h", "--help", "help"}:
        print(HELP)
        return 0
    if not args:
        return cmd_chat([])
    command = args[0]
    if command in COMMANDS:
        return COMMANDS[command](args[1:])
    prompt = " ".join(args)
    kernel = _build(ui=ConsoleUI(interactive=False), enforce_secrets=True)
    if kernel is None:
        return 2
    try:
        result = asyncio.run(kernel.run(prompt, project=_PROJECT_FLAG[0]))
    finally:
        asyncio.run(kernel.shutdown())
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    if result.text:
        print(result.text)
    return 0 if not result.error else 1


__all__ = [
    "main",
    # 兼容旧引用：HTTP 服务实现已移到 markbot.server.http
    "_ChatConfirmUI",
    "_hook_url",
    "_idle_archive_loop",
    "_memory_idle_loop",
    "_resolve_confirm",
    "_runtime_fingerprint",
    "_sanitize_grants",
    "_watch_runtime",
    "run_server",
]
