"""扩展与分发包加载：暂存提交、能力上限、失败隔离。"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from markbot.api.extension import Command
from markbot.api.tool import Tool
from markbot.api.ui import UI, NullUI
from markbot.kernel.config import Config
from markbot.kernel.permissions import (
    Capability,
    CapabilityError,
    parse_capabilities,
    unchecked_capabilities,
    uncovered_capabilities,
)
from markbot.kernel.runtime_view import (
    DEFAULT_GUARDRAILS,
    DEFAULT_SYSTEM_PROMPT,
    RuntimeView,
    SourceSet,
    Strategy,
    build_runtime_view,
    load_strategy_dir,
)
from markbot.kernel.trust import TrustStore
from markbot.schema.models import PackageManifest, PackagePermissions
from markbot.schema.version import API_VERSION

PRIORITY_LEARNED = 1000
PRIORITY_USER = 2000
PRIORITY_REPO = 3000
PRIORITY_PROJECT = 4000

CONVENTION_DIRS = ("extensions", "skills")
# 无 manifest 的约定目录包使用保守默认能力：只允许读写包内相对路径。
CONVENTION_CAPABILITIES = ["fs.read:./**", "fs.write:./**"]


@dataclass
class PackageInfo:
    id: str
    path: Path
    scope: str
    manifest: PackageManifest | None = None
    digest: str = ""
    trusted: bool = False
    loaded: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "scope": self.scope,
            "version": self.manifest.version if self.manifest else "",
            "digest": self.digest,
            "trusted": self.trusted,
            "loaded": self.loaded,
            "error": self.error,
        }


class StagedExtensionAPI:
    """扩展的注册先写入暂存区，setup 成功后才一次性提交。"""

    version = API_VERSION

    def __init__(
        self,
        ext_id: str,
        *,
        source: SourceSet,
        ui: UI,
        ceiling: list[Capability] | None = None,
        config: Mapping[str, Any] | None = None,
    ) -> None:
        self.id = ext_id
        self._source = source
        self._ui = ui
        self._ceiling = ceiling
        self.config: Mapping[str, Any] = config or {}
        self._tools: list[Tool] = []
        self._commands: list[Command] = []
        self._providers: list[Any] = []
        self._skill_roots: list[Path] = []
        self._subscriptions: list[tuple[str, str, Any]] = []
        self._entries: dict[str, list[Any]] = {}

    def on(self, event: str, handler) -> Any:
        self._subscriptions.append((event, self.id, handler))

        def unsubscribe() -> None:
            entry = (event, self.id, handler)
            if entry in self._subscriptions:
                self._subscriptions.remove(entry)

        return unsubscribe

    def register_tool(self, tool: Tool) -> None:
        if self._ceiling is not None:
            missing = uncovered_capabilities(tool.capabilities, self._ceiling)
            if missing:
                raise CapabilityError(
                    f"工具 {tool.name} 声明了包能力之外的能力: {', '.join(missing)}"
                )
        unchecked = unchecked_capabilities(tool)
        if unchecked:
            # 不拒绝加载（兼容旧扩展），但权限检查对这些能力会落空：提示作者声明参数角色
            self._source.warnings.append(
                f"工具 {tool.name} 声明了 {', '.join(unchecked)}，但参数里没有可识别的"
                "路径/URL 字段；请用 Field(json_schema_extra={'x-markbot-role': 'path'|'url'})"
                " 或 Tool(arg_roles=...) 标注"
            )
        self._tools.append(tool)

    def register_command(self, name: str, description: str, handler) -> None:
        self._commands.append(Command(name=name, description=description, handler=handler))

    def register_provider(self, provider: Any) -> None:
        self._providers.append(provider)

    def register_skill_root(self, path: Path) -> None:
        self._skill_roots.append(Path(path))

    def append_entry(self, key: str, value: Any) -> None:
        self._entries.setdefault(key, []).append(value)

    def ui(self) -> UI:
        return self._ui

    def commit(self) -> None:
        for tool in self._tools:
            self._source.tools.append(
                replace(tool, source=self._source.origin) if tool.source == "builtin" else tool
            )
        self._source.commands.extend(self._commands)
        self._source.providers.extend(self._providers)
        self._source.skill_roots.extend(self._skill_roots)
        self._source.subscriptions.extend(self._subscriptions)
        for key, values in self._entries.items():
            self._source.entries.setdefault(key, []).extend(values)


def _import_from_path(path: Path):
    file = path / "__init__.py" if path.is_dir() else path
    if not file.exists():
        raise FileNotFoundError(f"扩展文件不存在: {file}")
    token = hashlib.sha1(str(file).encode("utf-8")).hexdigest()[:10]
    module_name = f"markbot_ext_{file.stem}_{token}"
    spec = importlib.util.spec_from_file_location(module_name, str(file))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载扩展: {file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_extension(
    path: Path,
    *,
    ext_id: str,
    source: SourceSet,
    ui: UI,
    ceiling: list[Capability] | None = None,
    config: Mapping[str, Any] | None = None,
) -> bool:
    api = StagedExtensionAPI(ext_id, source=source, ui=ui, ceiling=ceiling, config=config)
    try:
        module = _import_from_path(Path(path))
        setup = getattr(module, "setup", None)
        if setup is None:
            raise TypeError(f"{Path(path).name} 缺少 setup(api) 函数")
        setup(api)
    except CapabilityError as exc:
        source.warnings.append(f"能力越界，扩展已跳过: {path} -> {exc}")
        source.entries.setdefault("__capability_violation__", []).append(str(exc))
        return False
    except Exception as exc:  # noqa: BLE001 - 失败只影响该扩展
        source.warnings.append(f"扩展加载失败，已跳过: {path} -> {exc}")
        return False
    api.commit()
    source.entries.setdefault("extensions", []).append(
        {"id": ext_id, "path": str(Path(path))}
    )
    return True


def load_extensions_from_dir(
    directory: Path,
    *,
    source: SourceSet,
    ui: UI,
    ceiling: list[Capability] | None = None,
    config: Mapping[str, Any] | None = None,
) -> None:
    directory = Path(directory)
    if not directory.exists():
        return
    candidates: list[Path] = []
    for child in sorted(directory.iterdir()):
        if child.name.startswith((".", "_")):
            continue
        if child.is_dir() and (child / "__init__.py").exists():
            candidates.append(child)
        elif child.suffix == ".py" and child.name != "__init__.py":
            candidates.append(child)
    for candidate in candidates:
        ext_id = f"{source.origin}:{candidate.stem}"
        load_extension(
            candidate,
            ext_id=ext_id,
            source=source,
            ui=ui,
            ceiling=ceiling,
            config=config,
        )


def is_convention_package(directory: Path) -> bool:
    """无 manifest 时按约定目录发现：含 extensions/ 或 skills/ 即视为包。"""
    directory = Path(directory)
    return any((directory / name).is_dir() for name in CONVENTION_DIRS)


def discover_packages(roots: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for root, scope in roots:
        root = Path(root)
        if not root.exists():
            continue
        if (root / "markbot.toml").exists() or is_convention_package(root):
            found.append((root, scope))
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            if (child / "markbot.toml").exists() or is_convention_package(child):
                found.append((child, scope))
    return found


def package_manifest(directory: Path) -> PackageManifest:
    """读取显式 manifest；缺失时合成约定目录包的保守默认清单。"""
    directory = Path(directory)
    manifest_path = directory / "markbot.toml"
    if manifest_path.exists():
        return read_manifest(manifest_path)
    return PackageManifest(
        name=directory.name,
        version="0.0.0",
        description="约定目录包（无 manifest）",
        permissions=PackagePermissions(capabilities=list(CONVENTION_CAPABILITIES)),
    )


def read_manifest(path: Path) -> PackageManifest:
    import tomllib

    with path.open("rb") as fh:
        data = tomllib.load(fh)
    # 兼容 [package] 包裹写法与顶层键写法
    payload: dict[str, Any] = {}
    if isinstance(data.get("package"), dict):
        payload.update(data["package"])
    payload.update({k: v for k, v in data.items() if k != "package"})
    return PackageManifest.model_validate(payload)


def _read_prompts(package_dir: Path, patterns: list[str]) -> list[str]:
    prompts: list[str] = []
    for pattern in patterns:
        for path in sorted(package_dir.glob(pattern)):
            if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    prompts.append(text)
    return prompts


def _load_sub_agents(package_dir: Path, source: SourceSet) -> None:
    directory = package_dir / "sub_agents"
    if not directory.exists():
        return
    for path in sorted(directory.glob("*.md")):
        source.sub_agents[path.stem] = path.read_text(encoding="utf-8", errors="replace")


class RuntimeAssembler:
    def __init__(
        self,
        config: Config,
        *,
        trust: TrustStore | None = None,
        ui: UI | None = None,
        include_packages: bool = True,
        learned_dir: Path | None = None,
        inject_failure: str | None = None,
    ) -> None:
        self.config = config
        self.home = Path(config.home)
        self.project_root = Path(config.project_root)
        self.ui = ui or NullUI()
        self.include_packages = include_packages
        self.learned_dir = Path(learned_dir) if learned_dir else self.home / "learned"
        self.trust = trust or TrustStore(self.home, self.project_root)
        self.inject_failure = inject_failure
        self.packages: list[PackageInfo] = []
        self.warnings: list[str] = []

    def build(self) -> RuntimeView:
        sources: list[SourceSet] = [self._builtin_source(), self._learned_source()]
        if self.include_packages:
            sources.extend(self._package_sources())
        view = build_runtime_view(sources, learned_dir=self.learned_dir)
        view.packages = [pkg.as_dict() for pkg in self.packages]
        view.warnings.extend(self.warnings)
        view.warnings.extend(self.trust.warnings)
        return view

    def _builtin_source(self) -> SourceSet:
        source = SourceSet(
            name="builtin",
            origin="builtin",
            priority=0,
            strategy=Strategy(
                system_prompt=DEFAULT_SYSTEM_PROMPT,
                guardrails=DEFAULT_GUARDRAILS,
            ),
        )
        extensions = Path(__file__).resolve().parents[1] / "extensions"
        load_extensions_from_dir(
            extensions,
            source=source,
            ui=self.ui,
            config=self._extension_config(),
        )
        return source

    def _extension_config(self) -> dict[str, Any]:
        """传给内置扩展的公开配置；密钥只能经 ctx.secret() 获取。"""
        return {key: value for key, value in self.config.raw.items() if key != "secrets"}

    def _learned_source(self) -> SourceSet:
        source = SourceSet(name="learned", origin="learned", priority=PRIORITY_LEARNED)
        strategy_dir = self.learned_dir / "strategy"
        source.strategy = load_strategy_dir(strategy_dir)
        # 目录可能尚未创建：学习层会在运行期写入技能，索引靠目录指纹自动刷新。
        source.skill_roots.append(self.learned_dir / "skills")
        _load_sub_agents(self.learned_dir, source)
        return source

    def _package_sources(self) -> list[SourceSet]:
        roots = [
            (self.home / "packages", "user"),
            (self.project_root / "capabilities", "repo"),
        ]
        sources: list[SourceSet] = []
        for directory, scope in discover_packages(roots):
            source = self._load_package(directory, scope)
            if source is not None:
                sources.append(source)
        project = self._project_source()
        if project is not None:
            sources.append(project)
        return sources

    def _project_source(self) -> SourceSet | None:
        directory = self.project_root / ".markbot"
        if not directory.exists():
            return None
        if not self.config.packages.allow_project_packages:
            self.warnings.append("项目级扩展未启用（packages.allow_project_packages = false）")
            return None
        if not self.trust.is_trusted("project", directory):
            self.warnings.append("项目级扩展未信任，已跳过；运行 markbot trust project --project")
            return None
        source = SourceSet(
            name="project", origin="project", priority=PRIORITY_PROJECT, strategy=Strategy()
        )
        load_extensions_from_dir(directory / "extensions", source=source, ui=self.ui)
        # 允许运行期新增项目技能；索引会在目录变化后自动刷新。
        source.skill_roots.append(directory / "skills")
        return source

    def _load_package(self, directory: Path, scope: str) -> SourceSet | None:
        try:
            manifest = package_manifest(directory)
        except Exception as exc:  # noqa: BLE001
            self.packages.append(
                PackageInfo(id=directory.name, path=directory, scope=scope, error=str(exc))
            )
            self.warnings.append(f"包 manifest 不合法，已跳过: {directory} -> {exc}")
            return None
        info = PackageInfo(id=manifest.name, path=directory, scope=scope, manifest=manifest)
        self.packages.append(info)
        if self.inject_failure == manifest.name:
            info.error = "inject-failure"
            self.warnings.append(f"注入故障，已跳过: {manifest.name}")
            return None
        try:
            info.digest = self.trust.hash_of(directory)
            info.trusted = self.trust.is_trusted(manifest.name, directory)
        except OSError as exc:
            info.error = str(exc)
            self.warnings.append(f"包哈希失败，已跳过: {manifest.name} -> {exc}")
            return None
        if not info.trusted:
            self.warnings.append(
                f"包未信任，已跳过: {manifest.name}（运行 markbot trust {manifest.name}）"
            )
            return None

        base = {
            "user": PRIORITY_USER,
            "repo": PRIORITY_REPO,
            "project": PRIORITY_PROJECT,
        }.get(scope, PRIORITY_USER)
        source = SourceSet(
            name=f"package:{manifest.name}",
            origin=f"package:{manifest.name}",
            priority=base + manifest.priority,
            strategy=Strategy(),
        )
        ceiling = parse_capabilities(manifest.permissions.capabilities)
        try:
            for entry in manifest.resources.extensions:
                target = (directory / entry).resolve()
                if not str(target).startswith(str(directory.resolve())):
                    raise CapabilityError(f"资源路径越界: {entry}")
                if target.is_dir():
                    load_extensions_from_dir(target, source=source, ui=self.ui, ceiling=ceiling)
                elif target.suffix == ".py":
                    load_extension(
                        target,
                        ext_id=f"{source.origin}:{target.stem}",
                        source=source,
                        ui=self.ui,
                        ceiling=ceiling,
                    )
            if source.entries.get("__capability_violation__"):
                raise CapabilityError("; ".join(source.entries["__capability_violation__"]))
            for entry in manifest.resources.skills:
                target = (directory / entry).resolve()
                if target.is_dir():
                    source.skill_roots.append(target)
            prompts = _read_prompts(directory, list(manifest.resources.prompts))
            if prompts:
                source.strategy.prompts.extend(prompts)
            _load_sub_agents(directory, source)
        except Exception as exc:  # noqa: BLE001 - 包加载失败只跳过该包
            info.error = str(exc)
            source = SourceSet(name=f"package:{manifest.name}", origin=f"package:{manifest.name}")
            source.warnings.append(f"包加载失败，已跳过: {manifest.name} -> {exc}")
        info.loaded = not info.error
        return source


__all__ = [
    "CONVENTION_CAPABILITIES",
    "PackageInfo",
    "RuntimeAssembler",
    "StagedExtensionAPI",
    "discover_packages",
    "is_convention_package",
    "load_extension",
    "load_extensions_from_dir",
    "package_manifest",
    "read_manifest",
]
