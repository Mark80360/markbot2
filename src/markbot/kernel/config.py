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
    context_budget_chars: int = 60_000
    tool_result_max_chars: int = 16_000


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


@dataclass
class CronConfig:
    enabled: bool = True
    # auto: 有渠道回调就发回聊天，否则仅归档；none: 仅归档；channel: 必须发回聊天
    default_delivery: str = "auto"
    timezone: str = ""


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
    secrets: dict[str, str] = field(default_factory=dict)
    home: Path = field(default_factory=markbot_home)
    project_root: Path = field(default_factory=Path.cwd)
    loaded: list[str] = field(default_factory=list)
    # 合并后的原始配置树；供内置扩展读取自身小节（如 [tools.mcp_servers]）
    raw: dict[str, Any] = field(default_factory=dict)
    # 加载期发现的非致命问题（明文密钥、文件权限过宽等），由 Kernel 写入运行日志
    warnings: list[str] = field(default_factory=list)


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
    cfg.agent.api_key = resolve_secret_ref(cfg.agent.api_key)
    cfg.secrets = {
        str(key): resolve_secret_ref(value)
        for key, value in (merged.get("secrets") or {}).items()
    }
    cfg.raw = merged
    return cfg


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
    "AgentConfig",
    "Config",
    "CronConfig",
    "MemoryConfig",
    "OptimizerConfig",
    "PackagesConfig",
    "PermissionConfig",
    "SessionsConfig",
    "SECRETS_FILE",
    "load_config",
    "markbot_home",
    "plaintext_secret_fields",
    "read_secrets_file",
    "resolve_secret_ref",
    "secrets_path",
    "resolve_provider_kind",
    "secret_values",
    "with_agent",
]
