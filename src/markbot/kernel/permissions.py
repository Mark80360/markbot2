"""能力清单、运行时权限策略与路径匹配。

能力语法：``fs.read:<glob>`` / ``fs.write:<glob>`` / ``net:<host>`` /
``exec:<program>`` / ``secret:<name>`` / ``ui``。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePath
from typing import Any, Iterable
from urllib.parse import urlsplit

from markbot.api.tool import Tool
from markbot.kernel.config import PermissionConfig

FS_READ = "fs.read"
FS_WRITE = "fs.write"
NET = "net"
EXEC = "exec"
SECRET = "secret"
UI = "ui"

_KINDS = {FS_READ, FS_WRITE, NET, EXEC, SECRET, UI}
_PATH_KEYS = {"path", "paths", "file", "files", "dir", "directory", "target", "src", "dest", "cwd"}
_URL_KEYS = {"url", "urls", "host", "hosts"}
_EXEC_KEYS = {"command", "program", "cmd"}
_DRIVE = re.compile(r"^[A-Za-z]:/")


class CapabilityError(ValueError):
    """扩展声明的能力超出所属包上限。"""


@dataclass(frozen=True, slots=True)
class Capability:
    kind: str
    value: str = ""

    @classmethod
    def parse(cls, raw: str) -> "Capability":
        text = str(raw).strip()
        if not text:
            raise CapabilityError("空能力声明")
        kind, sep, value = text.partition(":")
        kind = kind.strip()
        value = value.strip()
        if not sep:
            if kind == UI:
                return cls(UI, "")
            raise CapabilityError(f"能力声明缺少 ':' -> {raw!r}")
        if kind not in _KINDS:
            raise CapabilityError(f"未知能力类型 {kind!r} -> {raw!r}")
        if kind != UI and not value:
            raise CapabilityError(f"能力值不能为空 -> {raw!r}")
        return cls(kind, _normalize(value))

    @property
    def raw(self) -> str:
        return f"{self.kind}:{self.value}" if self.value else self.kind


def _normalize(value: str) -> str:
    value = value.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def parse_capabilities(values: Any) -> list[Capability]:
    return [Capability.parse(v) for v in (values or [])]


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(pattern: str, text: str) -> bool:
    return bool(_glob_regex(_normalize(pattern)).match(_normalize(text)))


def _is_absolute_pattern(value: str) -> bool:
    return value.startswith("/") or bool(_DRIVE.match(value))


def capability_covers(parent: Capability, child: Capability) -> bool:
    """包级上限是否覆盖工具声明。"""
    if parent.kind != child.kind:
        return False
    if parent.value == "*":
        return True
    if parent.value == child.value:
        return True
    if parent.kind in {FS_READ, FS_WRITE}:
        if parent.value.endswith("/**"):
            prefix = parent.value[:-3].rstrip("/")
            if not prefix:
                return not _is_absolute_pattern(child.value)
            return _normalize(child.value).startswith(prefix + "/")
        return glob_match(parent.value, child.value)
    if parent.kind == NET:
        return child.value == parent.value or child.value.endswith("." + parent.value)
    if parent.kind == EXEC:
        return child.value == parent.value or child.value.startswith(parent.value + " ")
    return False


def uncovered_capabilities(
    tool_caps: frozenset[str], ceiling: list[Capability] | frozenset[Capability]
) -> list[str]:
    ceiling_list = list(ceiling)
    missing: list[str] = []
    for raw in tool_caps:
        try:
            cap = Capability.parse(raw)
        except CapabilityError:
            missing.append(raw)
            continue
        if not any(capability_covers(allowed, cap) for allowed in ceiling_list):
            missing.append(raw)
    return missing


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False
    capability: Capability | None = None


def _values(args: Any, keys: set[str]) -> list[str]:
    if not isinstance(args, dict):
        return []
    out: list[str] = []
    for key, value in args.items():
        if key not in keys or value is None:
            continue
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, (list, tuple)):
            out.extend(str(v) for v in value if isinstance(v, (str, Path)))
    return out


class PermissionPolicy:
    def __init__(self, config: PermissionConfig | None = None, root: Path | None = None) -> None:
        cfg = config or PermissionConfig()
        self.profile = cfg.profile if cfg.profile in {"strict", "standard", "trusted"} else "standard"
        self.confirm_on_write = cfg.confirm_on_write
        self.confirm_on_exec = cfg.confirm_on_exec
        self.root = Path(root or Path.cwd()).resolve()
        # config.toml 里的静态放行 + 聊天里 /allow 的运行时授权
        self.allow_exec = {str(p).strip().lower() for p in cfg.allow_exec if str(p).strip()}
        self.allow_commands = [str(c).strip() for c in cfg.allow_commands if str(c).strip()]
        self._grants: set[str] = set()

    def push_grants(self, programs: Iterable[str]) -> set[str]:
        """加入运行时授权，返回本次新增项，便于回合结束后撤销。"""
        added: set[str] = set()
        for item in programs:
            name = str(item).strip().lower()
            if name and name not in self._grants:
                self._grants.add(name)
                added.add(name)
        return added

    def pop_grants(self, programs: Iterable[str]) -> None:
        for item in programs:
            self._grants.discard(str(item).strip().lower())

    def _exec_exempt(self, args: dict[str, Any]) -> bool:
        """命中 allow_exec（程序名）或 allow_commands（命令前缀）时无需逐次确认。"""
        commands = _values(args, {"command", "cmd"})
        programs = [p for p in (_first_program(c) for c in commands) if p] or _programs(args)
        if programs and all(
            self._program_name(p) in self.allow_exec or self._program_name(p) in self._grants
            for p in programs
        ):
            return True
        return any(
            self._command_matches(pattern, command)
            for pattern in self.allow_commands
            for command in commands
        )

    @staticmethod
    def _program_name(program: str) -> str:
        name = PurePath(program.replace("\\", "/")).name
        if name.lower().endswith(".exe"):
            name = name[:-4]
        return name.lower()

    @staticmethod
    def _command_matches(pattern: str, command: str) -> bool:
        # 统一空白后比较；pattern 以 * 结尾表示前缀匹配
        want = " ".join(pattern.split())
        text = " ".join(command.split())
        if want.endswith("*"):
            return text.startswith(want[:-1])
        return text == want

    def check(self, tool: Tool, args: dict[str, Any]) -> Decision:
        caps = parse_capabilities(tool.capabilities)
        if not caps:
            return Decision(True)

        fs_caps = [c for c in caps if c.kind in {FS_READ, FS_WRITE}]
        if fs_caps:
            for raw_path in _values(args, _PATH_KEYS):
                if not any(self._path_allowed(cap, raw_path) for cap in fs_caps):
                    return Decision(False, f"越权文件访问: {raw_path}")
            if any(c.kind == FS_WRITE for c in fs_caps):
                if self.profile == "strict":
                    return Decision(False, "strict 模式禁止写文件")
                if self.confirm_on_write and self.profile != "trusted":
                    return Decision(True, "写入需确认", requires_confirmation=True)

        for cap in caps:
            if cap.kind == NET:
                if self.profile == "strict":
                    return Decision(False, "strict 模式禁止网络访问")
                for raw in _values(args, _URL_KEYS):
                    host = _host_of(raw)
                    if host and cap.value != "*" and host != cap.value:
                        return Decision(False, f"网络访问不在能力范围内: {host}")
            elif cap.kind == EXEC:
                if self.profile == "strict":
                    return Decision(False, "strict 模式禁止执行命令")
                for program in _programs(args):
                    if not _program_allowed(cap, program):
                        return Decision(False, f"不允许执行: {program}")
                if (
                    self.confirm_on_exec
                    and self.profile != "trusted"
                    and not self._exec_exempt(args)
                ):
                    return Decision(True, "执行命令需确认", requires_confirmation=True)
            elif cap.kind == SECRET:
                if self.profile == "strict":
                    return Decision(False, "strict 模式禁止读取密钥")
        return Decision(True)

    def _path_allowed(self, cap: Capability, raw_path: str) -> bool:
        path = Path(raw_path).expanduser()
        try:
            resolved = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
        except OSError:
            return False
        try:
            rel = resolved.relative_to(self.root).as_posix()
            inside = True
        except ValueError:
            rel = resolved.as_posix()
            inside = False
        pattern = _normalize(cap.value)
        if inside and glob_match(pattern, rel):
            return True
        if _is_absolute_pattern(pattern) and glob_match(pattern, resolved.as_posix()):
            return True
        return False


def _host_of(raw: str) -> str:
    text = raw.strip()
    if "://" not in text:
        return text.split("/", 1)[0].lower()
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


def _programs(args: dict[str, Any]) -> list[str]:
    programs = _values(args, _EXEC_KEYS)
    out: list[str] = []
    for value in programs:
        if " " in value or "/" in value or "\\" in value:
            try:
                parts = shlex.split(value, posix=False)
            except ValueError:
                parts = value.split()
            if parts:
                out.append(parts[0].strip("'\""))
        else:
            out.append(value)
    return out


def _first_program(command: str) -> str:
    """取命令里真正的可执行程序，跳过 ``TOKEN=xxx curl …`` 这类前置赋值。"""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    for part in parts:
        token = part.strip("'\"")
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return token
    return ""


def _program_allowed(cap: Capability, program: str) -> bool:
    if cap.value == "*":
        return True
    name = PurePath(program.replace("\\", "/")).name
    if name.lower().endswith(".exe"):
        name = name[:-4]
    wanted = cap.value.lower()
    return program.lower() in {wanted, wanted + ".exe"} or name.lower() == wanted


__all__ = [
    "Capability",
    "CapabilityError",
    "Decision",
    "EXEC",
    "FS_READ",
    "FS_WRITE",
    "NET",
    "PermissionPolicy",
    "SECRET",
    "UI",
    "capability_covers",
    "glob_match",
    "parse_capabilities",
    "uncovered_capabilities",
]
