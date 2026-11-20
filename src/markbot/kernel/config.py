"""配置加载与磁盘布局。配置语法错误必须直接失败。"""

from __future__ import annotations

import os
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
    context_budget_chars: int = 60_000
    tool_result_max_chars: int = 16_000


@dataclass
class PermissionConfig:
    profile: str = "standard"  # strict | standard | trusted
    confirm_on_write: bool = True
    confirm_on_exec: bool = True
    # 免确认放行的程序名（如 curl、git）；也支持按整条命令前缀放行（结尾写 *）
    allow_exec: list[str] = field(default_factory=list)
    allow_commands: list[str] = field(default_factory=list)


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


@dataclass
class OptimizerConfig:
    enabled: bool = True
    k: int = 1
    max_rollouts_per_task: int = 4
    max_sessions_per_task: int = 3
    max_tasks_per_run: int = 5
    idle_minutes: int = 30


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
class Config:
    agent: AgentConfig = field(default_factory=AgentConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    packages: PackagesConfig = field(default_factory=PackagesConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
    cron: CronConfig = field(default_factory=CronConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    secrets: dict[str, str] = field(default_factory=dict)
    home: Path = field(default_factory=markbot_home)
    project_root: Path = field(default_factory=Path.cwd)
    loaded: list[str] = field(default_factory=list)


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
    project_path = project_root / ".markbot" / "settings.toml"
    merged: dict[str, Any] = {}
    for path in (user_path, project_path):
        if not path.exists():
            if require and path == user_path:
                raise FileNotFoundError(f"找不到配置文件: {path}")
            continue
        merged = _merge(merged, _read_toml(path))
        cfg.loaded.append(str(path))

    _apply(merged.get("agent", {}), cfg.agent)
    _apply(merged.get("permissions", {}), cfg.permissions)
    _apply(merged.get("packages", {}), cfg.packages)
    _apply(merged.get("ui", {}), cfg.ui)
    _apply(merged.get("logging", {}), cfg.logging)
    _apply(merged.get("optimizer", {}), cfg.optimizer)
    _apply(merged.get("cron", {}), cfg.cron)
    _apply(merged.get("memory", {}), cfg.memory)
    cfg.secrets = {
        str(key): str(value)
        for key, value in (merged.get("secrets") or {}).items()
    }
    return cfg


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
    "load_config",
    "markbot_home",
    "resolve_provider_kind",
    "with_agent",
]
