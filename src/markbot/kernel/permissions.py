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

from markbot.api.sandbox import ENV_ALLOWLIST
from markbot.api.tool import Tool
from markbot.kernel.config import PermissionConfig
from markbot.kernel.paths import PathGuard
from markbot.kernel.turn import turn_grants

FS_READ = "fs.read"
FS_WRITE = "fs.write"
NET = "net"
EXEC = "exec"
SECRET = "secret"
UI = "ui"
# 浏览器与桌面控制（ADR 0014）：browser:<域名|*>、desktop:<应用|*>
BROWSER = "browser"
DESKTOP = "desktop"

_KINDS = {FS_READ, FS_WRITE, NET, EXEC, SECRET, UI, BROWSER, DESKTOP}
_PATH_KEYS = {"path", "paths", "file", "files", "dir", "directory", "target", "src", "dest", "cwd"}
_URL_KEYS = {"url", "urls", "host", "hosts"}
_EXEC_KEYS = {"command", "program", "cmd"}
_DRIVE = re.compile(r"^[A-Za-z]:/")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# 命令替换/进程替换里的程序无法静态识别：注入密钥时一律拒绝
_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")
_OPERATOR_CHARS = frozenset(";&|()<>")
_PREFIX_TOKENS = frozenset({"!", "{", "}", "time"})
MAX_INJECTED_SECRETS = 8


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
    if parent.kind in {NET, BROWSER}:
        return child.value == parent.value or child.value.endswith("." + parent.value)
    if parent.kind == DESKTOP:
        return child.value.lower() == parent.value.lower()
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


_CMD_KEYS = frozenset({"command", "cmd"})


@dataclass(frozen=True)
class _Keys:
    """本次检查要看的参数名：显式角色声明 ∪ 按参数名的兜底规则（只会多查，不会少查）。"""

    path: frozenset[str]
    url: frozenset[str]
    command: frozenset[str]
    program: frozenset[str]


def _keys_for(tool: Tool) -> _Keys:
    roles = tool.roles() if hasattr(tool, "roles") else {}
    by_role: dict[str, set[str]] = {"path": set(), "url": set(), "command": set(), "program": set()}
    for name, role in roles.items():
        by_role.setdefault(role, set()).add(name)
    command = frozenset(_CMD_KEYS | by_role["command"])
    return _Keys(
        path=frozenset(_PATH_KEYS | by_role["path"]),
        url=frozenset(_URL_KEYS | by_role["url"]),
        command=command,
        program=frozenset(_EXEC_KEYS | command | by_role["program"]),
    )


def unchecked_capabilities(tool: Tool) -> list[str]:
    """声明了 fs/net 能力、但参数里没有任何可识别的路径/URL 字段的能力（权限检查会落空）。"""
    caps = parse_capabilities(tool.capabilities)
    if tool.input_schema is not None:
        props = set((tool.input_schema or {}).get("properties") or {})
    else:
        try:
            props = set(tool.params.model_json_schema().get("properties") or {})
        except Exception:  # noqa: BLE001
            return []
    keys = _keys_for(tool)
    missing: list[str] = []
    for cap in caps:
        if cap.kind in {FS_READ, FS_WRITE} and not (props & keys.path):
            missing.append(cap.raw)
        elif cap.kind == NET and cap.value != "*" and not (props & keys.url):
            missing.append(cap.raw)
    return missing


def _values(args: Any, keys: set[str] | frozenset[str]) -> list[str]:
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
    def __init__(
        self,
        config: PermissionConfig | None = None,
        root: Path | None = None,
        *,
        home: Path | None = None,
    ) -> None:
        cfg = config or PermissionConfig()
        self.profile = cfg.profile if cfg.profile in {"strict", "standard", "trusted"} else "standard"
        self.confirm_on_write = cfg.confirm_on_write
        self.confirm_on_exec = cfg.confirm_on_exec
        trusted_exec = str(getattr(cfg, "trusted_exec", "listed") or "listed").lower()
        self.trusted_exec = trusted_exec if trusted_exec in {"listed", "all"} else "listed"
        self.os_sandbox = str(getattr(cfg, "os_sandbox", "off") or "off").lower()
        self.root = Path(root or Path.cwd()).resolve()
        # 受保护目录与例外目录；文件工具、执行环境与命令扫描共用同一份判定
        self.guard = PathGuard.build(
            self.root,
            home=home,
            extra_writable=cfg.extra_writable_dirs,
            extra_readable=cfg.extra_readable_dirs,
            extra_protected=cfg.extra_protected_dirs,
        )
        # config.toml 里的静态放行 + 聊天里 /allow 的运行时授权
        self.allow_exec = {str(p).strip().lower() for p in cfg.allow_exec if str(p).strip()}
        self.allow_commands = [str(c).strip() for c in cfg.allow_commands if str(c).strip()]
        self.secret_env = _normalize_secret_env(cfg.secret_env)
        self._grants: set[str] = set()

    def injectable_secrets(self) -> list[str]:
        """可注入命令的密钥名（只有名称，没有值）。"""
        return sorted(self.secret_env)

    def check_secret_injection(self, names: Iterable[str], command: str) -> Decision:
        """判断能否把 ``names`` 对应的密钥以环境变量注入 ``command``。

        规则：密钥必须在 ``[permissions.secret_env]`` 登记；命令里出现的每个程序都必须
        在该密钥允许的程序名单内（``*`` 不限）；含命令替换或无法解析的命令一律拒绝，
        因为看不见里面实际执行的程序。
        """
        wanted = list(dict.fromkeys(str(name).strip() for name in names if str(name).strip()))
        if not wanted:
            return Decision(True)
        if self.profile == "strict":
            return Decision(False, "strict 模式禁止读取密钥")
        if len(wanted) > MAX_INJECTED_SECRETS:
            return Decision(False, f"单条命令最多注入 {MAX_INJECTED_SECRETS} 个密钥")
        for name in wanted:
            if not _ENV_NAME.match(name):
                return Decision(False, f"密钥名不合法: {name}")
            if name.upper() in ENV_ALLOWLIST:
                return Decision(False, f"不能用密钥覆盖系统环境变量: {name}")
        unknown = [name for name in wanted if name not in self.secret_env]
        if unknown:
            return Decision(
                False,
                f"密钥未允许注入: {', '.join(unknown)}"
                "（在 config.toml 的 [permissions.secret_env] 中登记）",
            )
        programs = command_programs(command)
        if programs is None:
            return Decision(False, "注入密钥的命令不能包含命令替换（$()、反引号）或未闭合的引号")
        if not programs:
            return Decision(False, "注入密钥的命令里没有可识别的程序")
        for name in wanted:
            allowed = self.secret_env[name]
            if "*" in allowed:
                continue
            outside = sorted({program for program in programs if program not in allowed})
            if outside:
                return Decision(
                    False,
                    f"密钥 {name} 只允许用于 {', '.join(allowed)}，命令中还包含 {', '.join(outside)}",
                )
        return Decision(True, capability=Capability(SECRET, ",".join(wanted)))

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

    def _program_exempt(self, program: str) -> bool:
        # 静态放行 + 旧式全局授权（push_grants，仅兼容）+ 本回合授权（TurnContext，按回合隔离）
        name = self._program_name(program)
        return name in self.allow_exec or name in self._grants or name in turn_grants()

    def _exec_exempt(self, args: dict[str, Any], keys: _Keys | None = None) -> bool:
        """命中 allow_exec（程序名）或 allow_commands（命令前缀）时无需逐次确认。

        必须检查命令里的**每一个**程序：只看第一个程序时，``curl x; rm -rf ~`` 会因为
        ``curl`` 在名单里而整条免确认。``allow_commands`` 的前缀匹配只适用于不含
        shell 运算符的单条命令，否则 ``<允许的前缀> ; 任意命令`` 也能蒙混过关。
        """
        command_keys = keys.command if keys is not None else _CMD_KEYS
        commands = _values(args, command_keys)
        if not commands:
            programs = _programs(args, keys.program if keys is not None else _EXEC_KEYS)
            return bool(programs) and all(self._program_exempt(p) for p in programs)
        for command in commands:
            programs = command_programs(command)
            if not programs:
                return False
            if all(self._program_exempt(p) for p in programs):
                continue
            simple = len(programs) == 1 and not _SHELL_META.search(command)
            if simple and any(
                self._command_matches(pattern, command) for pattern in self.allow_commands
            ):
                continue
            return False
        return True

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
        keys = _keys_for(tool)

        fs_caps = [c for c in caps if c.kind in {FS_READ, FS_WRITE}]
        if fs_caps:
            for raw_path in _values(args, keys.path):
                denials = [self._path_denial(cap, raw_path) for cap in fs_caps]
                if all(reason is not None for reason in denials):
                    protected = next((r for r in denials if r and r.startswith("受保护")), None)
                    detailed = next((r for r in denials if r), None)
                    return Decision(False, protected or detailed or f"越权文件访问: {raw_path}")
            if any(c.kind == FS_WRITE for c in fs_caps):
                if self.profile == "strict":
                    return Decision(False, "strict 模式禁止写文件")
                if self.confirm_on_write and self.profile != "trusted":
                    return Decision(True, "写入需确认", requires_confirmation=True)

        for cap in caps:
            if cap.kind == NET:
                if self.profile == "strict":
                    return Decision(False, "strict 模式禁止网络访问")
                for raw in _values(args, keys.url):
                    host = _host_of(raw)
                    if host and cap.value != "*" and host != cap.value:
                        return Decision(False, f"网络访问不在能力范围内: {host}")
            elif cap.kind == EXEC:
                if self.profile == "strict":
                    return Decision(False, "strict 模式禁止执行命令")
                # 与档位无关：命令里出现受保护路径（学习层、配置、日志等）直接拒绝
                for command in _values(args, keys.command):
                    reason = self.guard.scan_command(command)
                    if reason is not None:
                        return Decision(False, reason)
                for program in _programs(args, keys.program):
                    if not _program_allowed(cap, program):
                        return Decision(False, f"不允许执行: {program}")
                # 与档位、放行名单无关：看不透的命令（内联解释器、命令替换、提权等）总要确认
                risky = _risky_reason(_values(args, keys.command))
                if risky is not None:
                    return Decision(True, f"{risky}，需确认", requires_confirmation=True)
                if self.profile == "trusted":
                    # 能力本身钉死了程序（exec:python）、且参数里没有自由命令的工具
                    # （技能脚本：路径已校验在技能目录内、技能已声明 run_script）：
                    # 能执行什么由能力决定而不是模型，trusted 档位下不再逐次确认
                    pinned = cap.value != "*" and not _values(args, keys.command)
                    needs_confirm = self.trusted_exec != "all" and not pinned
                else:
                    needs_confirm = self.confirm_on_exec
                # 能力钉死程序（exec:git）且该程序在放行名单里：standard 档位也免确认（ADR 0013）
                pinned_exempt = (
                    cap.value != "*"
                    and not _values(args, keys.command)
                    and not _programs(args, keys.program)
                    and self._program_exempt(cap.value.split(" ", 1)[0])
                )
                if (
                    needs_confirm
                    and not pinned_exempt
                    and not self._exec_exempt(args, keys)
                    and getattr(tool, "confirm_exec", True)
                ):
                    return Decision(True, "执行命令需确认", requires_confirmation=True)
            elif cap.kind == SECRET:
                if self.profile == "strict":
                    return Decision(False, "strict 模式禁止读取密钥")
            elif cap.kind in {BROWSER, DESKTOP}:
                # 具体动作（点击、输入、提交）的确认由工具自己按风险判断；这里只按档位总开关
                if self.profile == "strict":
                    label = "浏览器" if cap.kind == BROWSER else "桌面"
                    return Decision(False, f"strict 模式禁止{label}控制")
                if cap.kind == BROWSER and cap.value != "*":
                    for raw in _values(args, keys.url):
                        host = _host_of(raw)
                        if host and host != cap.value and not host.endswith("." + cap.value):
                            return Decision(False, f"浏览器访问不在能力范围内: {host}")
        return Decision(True)

    def _path_allowed(self, cap: Capability, raw_path: str) -> bool:
        return self._path_denial(cap, raw_path) is None

    def _path_denial(self, cap: Capability, raw_path: str) -> str | None:
        """在 ``cap`` 范围内访问 ``raw_path`` 的拒绝原因；允许时返回 ``None``。"""
        try:
            resolved = self.guard.expand(raw_path)
        except (OSError, RuntimeError, ValueError):
            return f"路径无法解析: {raw_path}"
        write = cap.kind == FS_WRITE
        pattern = _normalize(cap.value)
        carve = self.guard.carve_out(resolved, write=write)
        if carve is not None:
            # 例外目录只对「整个工作区」级别的能力开放，窄能力（./output/**）不外溢
            return None if pattern == "**" else f"越权文件访问: {raw_path}"
        if self.guard.is_protected(resolved):
            return self.guard.denial(resolved, write=write)
        try:
            rel = resolved.relative_to(self.root).as_posix()
            inside = True
        except ValueError:
            rel = resolved.as_posix()
            inside = False
        if inside and glob_match(pattern, rel):
            return None
        if _is_absolute_pattern(pattern) and glob_match(pattern, resolved.as_posix()):
            return None
        if not inside and pattern == "**":
            # 工作区级能力访问工作区外：说清楚能去哪里，模型才不会反复试 /tmp 之类的路径
            allowed = [str(self.root), *(str(path) for path in self.guard.writable)]
            return (
                f"越权文件访问: {raw_path}（不在工作区内；可访问 {'、'.join(allowed)} 与本会话临时目录，"
                "工作区外的文件请先复制到这些目录）"
            )
        return f"越权文件访问: {raw_path}"


def _normalize_secret_env(raw: Any) -> dict[str, tuple[str, ...]]:
    """``{NAME: [程序...]}``；也接受 ``[NAME, ...]`` 简写（等价于不限程序）。

    单个值还接受 ``NAME = "curl"`` 与 ``NAME = true``（不限程序）/ ``false``（不登记），
    写错类型时不要让整个进程起不来。
    """
    if isinstance(raw, (list, tuple, set)):
        raw = {str(name): ["*"] for name in raw}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for name, programs in raw.items():
        key = str(name).strip()
        if not key:
            continue
        if programs is False:
            continue
        if programs is True:
            programs = ["*"]
        if isinstance(programs, str):
            programs = [programs]
        elif not isinstance(programs, (list, tuple, set)):
            programs = []
        names = tuple(
            "*" if str(p).strip() == "*" else PermissionPolicy._program_name(str(p).strip())
            for p in (programs or [])
            if str(p).strip()
        )
        out[key] = names or ("*",)
    return out


def command_programs(command: str) -> list[str] | None:
    """列出 shell 命令里每个简单命令的程序名；无法可靠解析时返回 ``None``。

    按 shell 运算符（``; & | && || ( )`` 与换行）切分，每段取第一个非赋值词；
    重定向目标（``> file``）不算程序。引号内的 ``&`` / ``|`` 不会被误切。
    """
    if _SUBSTITUTION.search(command):
        return None
    text = command.replace("\r", " ").replace("\n", " ; ")
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    programs: list[str] = []
    expect_program = True
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token and all(char in _OPERATOR_CHARS for char in token):
            if any(char in "<>" for char in token):
                skip_next = True  # 重定向：下一个词是文件或描述符
            else:
                expect_program = True
            continue
        if not expect_program:
            continue
        if token in _PREFIX_TOKENS or _ASSIGNMENT.match(token):
            continue
        programs.append(PermissionPolicy._program_name(token))
        expect_program = False
    return programs


# 出现即说明不是「单条简单命令」：分隔/管道/后台/重定向/换行/子 shell
_SHELL_META = re.compile(r"[;&|<>`\n()]")
# 这些程序会执行其参数里的任意代码，放行名单无法约束
_RISKY_PROGRAMS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "eval", "source", ".", "sudo", "su",
     "doas", "osascript"}
)
_INLINE_INTERPRETER = re.compile(
    r"(?:^|[\s;&|(])(?:python[0-9.]*|node|nodejs|perl|ruby|php|deno|bun|lua)\s+"
    r"(?:-[A-Za-z]*[ce]\b|--eval\b|-\s|-$)"
)
_DECODE_PIPE = re.compile(r"\b(?:base64|xxd|openssl)\b[^|]*\s(?:-d|-D|--decode|-r)\b")
# 会丢失工作或改写共享历史的 git 操作：与放行名单无关，一律确认（ADR 0013）
_DANGEROUS_GIT = re.compile(
    r"\bgit\b[^;&|]*\s(?:"
    r"push\b[^;&|]*(?:\s--force\b|\s-f\b|\s--force-with-lease\b|\s--delete\b|\s:\S)"
    r"|reset\b[^;&|]*\s--hard\b"
    r"|clean\b[^;&|]*\s-[a-zA-Z]*f"
    r"|checkout\b[^;&|]*\s(?:--\s+\.|\.)(?:\s|$)"
    r"|restore\b[^;&|]*\s\.(?:\s|$)"
    r"|branch\b[^;&|]*\s-D\b"
    r"|rebase\b|filter-branch\b|filter-repo\b|reflog\s+expire\b|gc\b[^;&|]*--prune"
    r"|stash\s+(?:drop|clear)\b"
    r")"
)


def _risky_reason(commands: list[str]) -> str | None:
    """命令内容无法静态判断时返回原因：内联代码、命令替换、提权、解码后执行等。"""
    for command in commands:
        programs = command_programs(command)
        if programs is None:
            return "命令含命令替换（$()、反引号）或无法解析"
        risky = sorted({p for p in programs if p in _RISKY_PROGRAMS})
        if risky:
            return f"命令调用了可执行任意代码的程序（{', '.join(risky)}）"
        if _INLINE_INTERPRETER.search(command):
            return "命令以内联方式执行解释器代码（如 python -c / node -e）"
        if _DECODE_PIPE.search(command):
            return "命令先解码再处理内容（base64/xxd/openssl 解码）"
        if _DANGEROUS_GIT.search(command):
            return "命令包含可能丢失工作或改写历史的 git 操作（force push、reset --hard、clean -f、rebase 等）"
    return None


def _host_of(raw: str) -> str:
    text = raw.strip()
    if "://" not in text:
        return text.split("/", 1)[0].lower()
    try:
        return (urlsplit(text).hostname or "").lower()
    except ValueError:
        return ""


def _programs(args: dict[str, Any], keys: set[str] | frozenset[str] = _EXEC_KEYS) -> list[str]:
    programs = _values(args, keys)
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
    "BROWSER",
    "DESKTOP",
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
    "command_programs",
    "glob_match",
    "parse_capabilities",
    "unchecked_capabilities",
    "uncovered_capabilities",
]
