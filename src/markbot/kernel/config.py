"""配置加载与磁盘布局。配置语法错误必须直接失败。"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


def markbot_home() -> Path:
    override = os.environ.get("MARKBOT_HOME")
    return Path(override).expanduser() if override else Path.home() / ".markbot"


@dataclass
class AgentConfig:
    provider: str = ""
    model: str = "claude-sonnet-4-5"
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 4096
    max_steps: int = 30
    tool_timeout_s: float = 60.0
    request_timeout_s: float = 120.0
    max_retries: int = 2
    retry_backoff_s: float = 2.0
    parallel_tools: bool = True
    # serve/gateway 中同时执行的回合上限；同一会话始终串行
    max_concurrent_turns: int = 4
    # 上下文压缩触发点：用量达到 max_tokens 的该比例时压缩（0.8 = 80%）。
    # 初版字符数与 token 预算（context_budget_chars / context_budget_tokens）已移除。
    context_budget_ratio: float = 0.8
    tool_result_max_chars: int = 16_000
    # 备用模型（ADR 0010）：[[agent.fallbacks]] provider/model/base_url/api_key("env:NAME")
    # 或 api_key_secret（[secrets] 里的名字）。主模型失败时按顺序切换。
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    circuit_failures: int = 3        # 连续失败多少次熔断该模型
    circuit_cooldown_s: float = 300  # 熔断时长
    # 按用途路由模型（ADR 0011）：[agent.roles.fast] 用于上下文压缩与记忆整理。
    # 只写 model 时沿用主模型的 provider/base_url/api_key（同一网关的另一个模型）。
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 每日 token 上限（输入+输出，所有回合合计）；超过后拒绝新回合直到次日。0 表示不限
    daily_token_budget: int = 0
    # 额外的内容审核拒绝特征（追加到内置列表，按子串匹配，不区分大小写）
    moderation_markers: list[str] = field(default_factory=list)
    # 审核拒绝且无法定位触发内容时，自动换一个带摘要的新会话，避免该会话永久不可用
    moderation_new_session: bool = True
    # Anthropic 提示缓存：auto = 官方接口（api.anthropic.com）时开启；true / false 强制
    prompt_cache: str = "auto"
    # 多模态（ADR 0015）：模型能否直接看图。auto = 按 provider / 模型名判断；true / false 强制。
    # 不能看图时图片换成文字占位，可配置 [agent.roles.vision] 让 view_image 交给视觉模型描述
    vision: str = "auto"
    vision_max_images: int = 3   # 每次请求最多附带的图片数（最近的优先）


@dataclass
class PermissionConfig:
    profile: str = "standard"  # strict | standard | trusted
    confirm_on_write: bool = True
    confirm_on_exec: bool = True
    # trusted 档位下哪些命令免确认：listed = 只有 allow_exec / allow_commands / 运行时授权
    # 命中的命令免确认（默认，更安全）；all = 全部免确认（旧行为）。
    # 无论取值如何，内联解释器（python -c、sh -c、eval…）与含命令替换的命令始终需要确认。
    trusted_exec: str = "listed"
    # OS 级隔离：off = 只靠路径约定与命令扫描；macos = 用 sandbox-exec 在内核层面禁止
    # 命令读写受保护目录（MarkBot home、学习层、配置），仅 macOS 可用。
    os_sandbox: str = "off"
    # 免确认放行的程序名（如 curl、git）；也支持按整条命令前缀放行（结尾写 *）
    allow_exec: list[str] = field(default_factory=list)
    allow_commands: list[str] = field(default_factory=list)
    # 允许以环境变量注入命令的密钥 -> 允许使用它的程序名；["*"] 表示不限程序。
    # 密钥值放在 [secrets]，这里只登记名称与用途，模型永远拿不到值。
    secret_env: dict[str, list[str]] = field(default_factory=dict)
    # 工作区之外额外开放给工具的目录。MarkBot home 整体受保护，默认只开放
    # <home>/workspace（读写）与 <home>/media（只读）；这里可以追加。
    extra_writable_dirs: list[str] = field(default_factory=list)
    extra_readable_dirs: list[str] = field(default_factory=list)
    # 额外受保护目录（如旧版数据目录 ~/.markbot_old），与 MarkBot home 同等对待
    extra_protected_dirs: list[str] = field(default_factory=list)


@dataclass
class PackagesConfig:
    allow_project_packages: bool = False
    # 仓库内置包（scope=repo）的内容已被 git 跟踪且没有未提交改动时自动信任（开发模式）。
    # 这样改动并提交仓库里的能力包后不会被静默跳过；未提交的改动仍需 `markbot trust`。
    trust_repo_from_git: bool = True


@dataclass
class ShellConfig:
    # run_command 使用的 shell：auto | bash | sh | pwsh | powershell | cmd | <可执行文件路径>
    # auto：Linux/macOS 用 bash（没有则 /bin/sh），Windows 用 pwsh（没有则 powershell，再 cmd）
    program: str = "auto"
    # 额外透传给子进程的环境变量名（白名单之外）。代理变量已在白名单内。
    passthrough_env: list[str] = field(default_factory=list)


@dataclass
class SubagentConfig:
    max_steps: int = 20          # 子代理默认步数上限
    max_parallel: int = 3        # 一次回复里最多同时运行的子代理数
    result_max_chars: int = 8000  # 返回给主会话的结果上限


@dataclass
class DevConfig:
    # 本回合改了代码但没有运行测试/检查时，在给出最终答复前提醒模型验证一次（ADR 0012）
    verify_reminder: bool = True
    # run_tests 默认命令（留空自动识别）与 run_checks 命令列表（留空自动识别）
    test_command: str = ""
    checks: list[str] = field(default_factory=list)


@dataclass
class SecurityConfig:
    # config.toml 含明文密钥时是否仍允许启动。默认 false：拒绝启动并提示迁移（ADR 0009）。
    # 临时放行也可以用命令行 --allow-plaintext-secrets 或环境变量
    # MARKBOT_ALLOW_PLAINTEXT_SECRETS=1。
    allow_plaintext_secrets: bool = False


@dataclass
class WebToolConfig:
    # fetch_url 默认禁止访问私网/回环/链路本地地址（SSRF）。确需访问的内网服务写在这里：
    # "192.168.71.90:11235"（主机+端口）或 "nas.local"（主机任意端口）。
    allow_private: list[str] = field(default_factory=list)
    # 代理，例如 "http://192.168.71.90:7891"；留空时读取进程环境变量 HTTPS_PROXY 等
    proxy: str = ""
    user_agent: str = ""


@dataclass
class UIConfig:
    show_progress: bool = True


@dataclass
class LoggingConfig:
    enabled: bool = True
    runtime: bool = True
    interaction: bool = True
    # 按日文件的保留天数（logs/runtime、gateway）；交互日志含完整模型报文，单独更短
    retention_days: int = 30
    interaction_retention_days: int = 7
    # logs/ + gateway/ 的总量上限，超出后从最旧的交互日志开始删；0 表示不限
    max_total_mb: float = 500


@dataclass
class OptimizerConfig:
    enabled: bool = True
    k: int = 1
    max_rollouts_per_task: int = 4
    max_sessions_per_task: int = 3
    max_tasks_per_run: int = 5
    idle_minutes: int = 30
    min_eval_cases: int = 10       # 回归集少于此数不晋升
    llm_judge: bool = False        # 用当前模型给 llm_judge 检查打分
    # 晋升门禁额外评测集（ADR 0016）：["coding"]、["coding", "browser"]。每个都只许变好；
    # 会调用真实模型，候选与当前 harness 各跑一遍，按 gate_max_cases 限制用例数控制成本
    gate_suites: list[str] = field(default_factory=list)
    gate_max_cases: int = 5        # 每个评测集最多跑几个用例；0 不限
    gate_tolerance: float = 0.0    # 允许的通过率下降（0 = 严格不许掉分）


@dataclass
class CronConfig:
    enabled: bool = True
    # auto: 有渠道回调就发回聊天，否则仅归档；none: 仅归档；channel: 必须发回聊天
    default_delivery: str = "auto"
    timezone: str = ""
    # 可靠性（ADR 0010）
    max_concurrent: int = 2            # 同时执行的定时任务数（仍受 [agent] max_concurrent_turns 约束）
    job_timeout_s: float = 1800        # 单次执行超时，超时取消；0 表示不限
    max_retries: int = 1               # 失败后重试次数（退避 retry_backoff_s 秒）
    retry_backoff_s: float = 300
    misfire_policy: str = "run_once"   # 停机期间错过的触发：run_once（宽限期内补跑一次）| skip
    misfire_grace_s: float = 21600     # 补跑宽限期（秒），默认 6 小时
    alert_after_failures: int = 2      # 连续失败多少次通知创建任务的聊天；0 关闭


@dataclass
class MemoryConfig:
    enabled: bool = True
    auto_consolidate: bool = True
    # Dream 整理周期（分钟）；0 表示关闭
    idle_minutes: int = 120
    # 长驻会话空闲多久后把快照写入 sessions/（分钟）；0 表示关闭
    idle_archive_minutes: int = 15
    max_sessions_per_run: int = 20
    resident_max_chars: int = 6_000
    index_max_chars: int = 2_000
    notice_compaction: bool = False
    show_stale: bool = True


@dataclass
class SessionsConfig:
    # 是否继续写派生文件 trajectory.jsonl / compacted.jsonl。默认关闭：两者都能从
    # events.jsonl 完整重建（ADR 0007）。只有外部脚本还在直接读这两个文件时才打开。
    legacy_views: bool = False


@dataclass
class Config:
    agent: AgentConfig = field(default_factory=AgentConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    packages: PackagesConfig = field(default_factory=PackagesConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
    cron: CronConfig = field(default_factory=CronConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    sessions: SessionsConfig = field(default_factory=SessionsConfig)
    shell: ShellConfig = field(default_factory=ShellConfig)
    dev: DevConfig = field(default_factory=DevConfig)
    subagents: SubagentConfig = field(default_factory=SubagentConfig)
    # 已登记的项目（名称 -> 路径）：[projects] 小节与 <home>/projects.json 合并（ADR 0013）
    projects: dict[str, str] = field(default_factory=dict)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    web: WebToolConfig = field(default_factory=WebToolConfig)
    secrets: dict[str, str] = field(default_factory=dict)
    home: Path = field(default_factory=markbot_home)
    project_root: Path = field(default_factory=Path.cwd)
    loaded: list[str] = field(default_factory=list)
    # 合并后的原始配置树；供内置扩展读取自身小节（如 [tools.mcp_servers]）
    raw: dict[str, Any] = field(default_factory=dict)
    # 加载期发现的非致命问题（明文密钥、文件权限过宽等），由 Kernel 写入运行日志
    warnings: list[str] = field(default_factory=list)
    # config.toml 里以明文写着的密钥字段名（不含值），供启动时按 [security] 策略拦截
    plaintext_secrets: list[str] = field(default_factory=list)
    plaintext_secrets_path: str = ""


# 密钥文件：与 config.toml 同目录，结构相同（[agent].api_key、[secrets]、
# [channels.feishu].app_secret …），加载时叠加在 config.toml 之上。权限必须是 0600。
SECRETS_FILE = "secrets.toml"
# 取值写成 ``env:NAME`` 时从进程环境变量读取，配置文件里不出现密钥原值
_ENV_PREFIX = "env:"


def resolve_secret_ref(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_ENV_PREFIX):
        return os.environ.get(text[len(_ENV_PREFIX):].strip(), "")
    return text


def secrets_path(home: Path) -> Path:
    return Path(home) / SECRETS_FILE


def read_secrets_file(home: Path) -> dict[str, Any]:
    """读取 ``<home>/secrets.toml``；不存在返回空字典，语法错误向上抛出。"""
    path = secrets_path(home)
    if not path.exists():
        return {}
    return _read_toml(path)


def _loose_permissions(path: Path) -> bool:
    if os.name != "posix":
        return False
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def plaintext_secret_fields(data: dict[str, Any]) -> list[str]:
    """列出配置树里以明文写着的密钥字段（``env:`` 引用与空值不算）。"""
    found: list[str] = []

    def is_plain(value: Any) -> bool:
        return isinstance(value, str) and bool(value.strip()) and not value.startswith(
            _ENV_PREFIX
        ) and "${" not in value

    agent = data.get("agent") or {}
    if is_plain(agent.get("api_key")):
        found.append("agent.api_key")
    for index, item in enumerate(agent.get("fallbacks") or []):
        if isinstance(item, dict) and is_plain(item.get("api_key")):
            found.append(f"agent.fallbacks[{index}].api_key")
    for role, item in (agent.get("roles") or {}).items():
        if isinstance(item, dict) and is_plain(item.get("api_key")):
            found.append(f"agent.roles.{role}.api_key")
    for key, value in (data.get("secrets") or {}).items():
        if is_plain(value):
            found.append(f"secrets.{key}")
    for name, section in (data.get("channels") or {}).items():
        if isinstance(section, dict):
            for key, value in section.items():
                if any(h in str(key).lower() for h in ("secret", "token", "api_key")) and is_plain(
                    value
                ):
                    found.append(f"channels.{name}.{key}")
    transcription = data.get("transcription") or {}
    if isinstance(transcription, dict) and is_plain(transcription.get("api_key")):
        found.append("transcription.api_key")
    return found


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply(section: dict[str, Any], target: Any) -> None:
    for key, value in section.items():
        if hasattr(target, key):
            setattr(target, key, value)


def load_config(
    *,
    home: Path | None = None,
    project_root: Path | None = None,
    require: bool = False,
) -> Config:
    """读取用户配置并叠加项目覆盖。TOML 语法错误会向上抛出。"""
    home = Path(home) if home else markbot_home()
    project_root = Path(project_root) if project_root else Path.cwd()
    cfg = Config(home=home, project_root=project_root)

    user_path = home / "config.toml"
    secret_path = secrets_path(home)
    project_path = project_root / ".markbot" / "settings.toml"
    merged: dict[str, Any] = {}
    for path in (user_path, secret_path, project_path):
        if not path.exists():
            if require and path == user_path:
                raise FileNotFoundError(f"找不到配置文件: {path}")
            continue
        data = _read_toml(path)
        if path == user_path:
            plain = plaintext_secret_fields(data)
            if plain:
                cfg.plaintext_secrets = list(plain)
                cfg.plaintext_secrets_path = str(path)
                cfg.warnings.append(
                    f"{path} 含明文密钥（{', '.join(plain)}）；"
                    "运行 `markbot secrets migrate` 迁移到 secrets.toml，并尽快轮换这些密钥"
                )
        if path in (user_path, secret_path) and _loose_permissions(path):
            cfg.warnings.append(f"{path} 权限过宽，建议执行: chmod 600 {path}")
        merged = _merge(merged, data)
        cfg.loaded.append(str(path))

    _apply(merged.get("agent", {}), cfg.agent)
    _apply(merged.get("permissions", {}), cfg.permissions)
    _apply(merged.get("packages", {}), cfg.packages)
    _apply(merged.get("ui", {}), cfg.ui)
    _apply(merged.get("logging", {}), cfg.logging)
    _apply(merged.get("optimizer", {}), cfg.optimizer)
    _apply(merged.get("cron", {}), cfg.cron)
    _apply(merged.get("memory", {}), cfg.memory)
    _apply(merged.get("sessions", {}), cfg.sessions)
    _apply(merged.get("shell", {}), cfg.shell)
    _apply(merged.get("dev", {}), cfg.dev)
    _apply(merged.get("subagents", {}), cfg.subagents)
    cfg.projects = load_projects(home, merged.get("projects"))
    _apply(merged.get("security", {}), cfg.security)
    tools = merged.get("tools") if isinstance(merged.get("tools"), dict) else {}
    _apply(tools.get("web", {}) if isinstance(tools.get("web"), dict) else {}, cfg.web)
    cfg.agent.api_key = resolve_secret_ref(cfg.agent.api_key)
    cfg.secrets = {
        str(key): resolve_secret_ref(value)
        for key, value in (merged.get("secrets") or {}).items()
    }
    cfg.raw = merged
    return cfg


PROJECTS_FILE = "projects.json"
_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def valid_project_name(name: str) -> bool:
    return bool(_PROJECT_NAME.match(str(name or "")))


def load_projects(home: Path, section: Any = None) -> dict[str, str]:
    """项目登记表：``<home>/projects.json``（``markbot project add`` 写入）叠加 config 的 ``[projects]``。

    只接受合法名称与存在的目录；路径统一为绝对路径。登记表在受保护的 MarkBot home 里，
    模型和工具都不能修改，所以聊天里只能在已登记的项目之间切换。
    """
    import json

    raw: dict[str, Any] = {}
    path = Path(home) / PROJECTS_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for name, entry in (data.get("projects") or {}).items():
                raw[str(name)] = entry.get("path") if isinstance(entry, dict) else entry
        except (OSError, ValueError, AttributeError):
            pass
    if isinstance(section, dict):
        raw.update({str(k): v for k, v in section.items()})
    out: dict[str, str] = {}
    for name, value in raw.items():
        if not valid_project_name(name) or not isinstance(value, str) or not value.strip():
            continue
        target = Path(value).expanduser()
        try:
            target = target.resolve()
        except OSError:
            continue
        if target.is_dir():
            out[name] = str(target)
    return dict(sorted(out.items()))


def save_project(home: Path, name: str, path: Path | None) -> None:
    """写入（path 为 None 时删除）``<home>/projects.json`` 里的一个项目。"""
    import json
    from datetime import datetime

    file = Path(home) / PROJECTS_FILE
    data: dict[str, Any] = {"version": 1, "projects": {}}
    if file.exists():
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            data.setdefault("projects", {})
        except (OSError, ValueError):
            pass
    if path is None:
        data["projects"].pop(name, None)
    else:
        data["projects"][name] = {
            "path": str(Path(path).expanduser().resolve()),
            "added_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


ALLOW_PLAINTEXT_ENV = "MARKBOT_ALLOW_PLAINTEXT_SECRETS"


class PlaintextSecretsError(RuntimeError):
    """config.toml 含明文密钥且未显式放行。"""


def enforce_secret_policy(cfg: Config, *, allow: bool = False) -> None:
    """长驻/对话入口启动前调用：config.toml 含明文密钥时拒绝启动（ADR 0009）。

    放行方式（任一）：``allow=True``（命令行 ``--allow-plaintext-secrets``）、
    ``[security] allow_plaintext_secrets = true``、环境变量 ``MARKBOT_ALLOW_PLAINTEXT_SECRETS=1``。
    错误信息只含字段名，不含任何密钥值。
    """
    if not cfg.plaintext_secrets:
        return
    env_allow = os.environ.get(ALLOW_PLAINTEXT_ENV, "").strip().lower() in {"1", "true", "yes"}
    if allow or cfg.security.allow_plaintext_secrets or env_allow:
        return
    raise PlaintextSecretsError(
        f"{cfg.plaintext_secrets_path or 'config.toml'} 含明文密钥"
        f"（{', '.join(cfg.plaintext_secrets)}），已拒绝启动。\n"
        "请运行 `markbot secrets migrate --dry-run` 预览，再 `markbot secrets migrate` 迁移到 "
        "secrets.toml，并到对应平台轮换这些密钥。\n"
        "临时放行：加 --allow-plaintext-secrets，或设置 "
        f"{ALLOW_PLAINTEXT_ENV}=1，或在 config.toml 写 [security] allow_plaintext_secrets = true。"
    )


def normalize_list(value: Any) -> list[str]:
    """配置里的字符串列表：接受单个字符串或列表，去空白与空项。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# 键名包含这些片段的字符串配置项视为密钥（如 [channels.feishu].app_secret）
_SECRET_KEY_HINTS = ("api_key", "apikey", "secret", "token", "password", "passwd", "authorization")
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def secret_values(cfg: Config) -> list[str]:
    """收集配置里所有密钥取值，供 ``register_secrets`` 做按值脱敏。

    覆盖 ``[agent].api_key``、``[secrets]`` 全部值、键名像密钥的任意字段，以及
    配置里 ``${VAR}`` 引用到的环境变量（MCP headers 在运行期才展开）。
    """
    values: list[str] = [cfg.agent.api_key, *cfg.secrets.values()]

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if any(hint in str(key).lower() for hint in _SECRET_KEY_HINTS):
                        values.append(resolve_secret_ref(value))
                    for name in _ENV_REF.findall(value):
                        values.append(os.environ.get(name, ""))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, str):
                    for name in _ENV_REF.findall(item):
                        values.append(os.environ.get(name, ""))
                else:
                    walk(item)

    walk(cfg.raw)
    # ``${VAR}`` 占位符本身不是密钥
    return [v for v in values if isinstance(v, str) and v.strip() and "${" not in v]


def resolve_provider_kind(cfg: Config) -> str:
    explicit = cfg.agent.provider
    if explicit:
        return explicit.strip()
    name = cfg.agent.model.lower()
    if name.startswith("claude") or name.startswith("anthropic"):
        return "anthropic"
    return "openai_compat"


def with_agent(cfg: Config, **changes: Any) -> Config:
    return replace(cfg, agent=replace(cfg.agent, **changes))


__all__ = [
    "ALLOW_PLAINTEXT_ENV",
    "AgentConfig",
    "Config",
    "CronConfig",
    "DevConfig",
    "MemoryConfig",
    "OptimizerConfig",
    "PackagesConfig",
    "PermissionConfig",
    "PlaintextSecretsError",
    "SubagentConfig",
    "load_projects",
    "save_project",
    "valid_project_name",
    "SecurityConfig",
    "SessionsConfig",
    "ShellConfig",
    "SECRETS_FILE",
    "WebToolConfig",
    "enforce_secret_policy",
    "load_config",
    "markbot_home",
    "normalize_list",
    "plaintext_secret_fields",
    "read_secrets_file",
    "resolve_secret_ref",
    "secrets_path",
    "resolve_provider_kind",
    "secret_values",
    "with_agent",
]
