"""路径守卫：工作区边界 + 受保护目录 + 明确开放的例外目录。

规则按顺序判定（先命中先生效）：

1. 落在可写例外目录（默认 ``<home>/workspace``）里：读写都允许；
2. 只读访问落在只读例外目录（默认 ``<home>/media``、``<home>/sessions/<id>/files``）里：允许；
3. 落在受保护目录（默认整个 MarkBot home 与 ``<root>/.markbot``）里：一律拒绝，
   与权限档位无关——学习层、配置、信任表、日志、会话归档只能经专用工具或人工修改；
4. 落在工作区 ``root`` 里：允许（具体范围再由工具声明的 ``fs.*`` 能力收窄）；
5. 其余：越界。

判定基于 ``Path.resolve()``，符号链接会被跟随到真实位置再判断。

``scan_command`` 对 shell 命令做静态检查，拦截引用受保护路径的命令。它只能识别
字面路径（含 ``~``、``$HOME``、``$MARKBOT_HOME`` 与通配符），无法看穿变量拼接、
``cd`` 之后的相对路径等写法；真正的强制隔离需要 OS 级沙箱（sandbox-exec / bwrap）。
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROTECTED_HINT = (
    "学习层（技能/记忆）只能用 memory_* 工具修改；"
    "配置、信任表、日志与会话归档不对工具开放"
)

_VAR = re.compile(r"\$\{?(HOME|MARKBOT_HOME)\}?")
# ``VAR=value`` / ``--flag=value``：只检查等号右边
_ASSIGN = re.compile(r"^-{0,2}[A-Za-z_][A-Za-z0-9_-]*=")
_GLOB_CHARS = frozenset("*?[")


def _within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _resolve_dirs(values: Iterable[str | Path]) -> tuple[Path, ...]:
    out: list[Path] = []
    for value in values or ():
        text = str(value).strip()
        if not text:
            continue
        out.append(Path(text).expanduser().resolve())
    return tuple(out)


@dataclass(frozen=True)
class PathGuard:
    root: Path
    home: Path | None = None
    protected: tuple[Path, ...] = ()
    writable: tuple[Path, ...] = ()
    readable: tuple[Path, ...] = ()

    @classmethod
    def build(
        cls,
        root: Path,
        *,
        home: Path | None = None,
        extra_writable: Iterable[str | Path] = (),
        extra_readable: Iterable[str | Path] = (),
        extra_protected: Iterable[str | Path] = (),
    ) -> "PathGuard":
        root = Path(root).resolve()
        protected: list[Path] = [root / ".markbot"]
        writable: list[Path] = []
        readable: list[Path] = []
        resolved_home: Path | None = None
        if home is not None:
            resolved_home = Path(home).expanduser().resolve()
            protected.append(resolved_home)
            writable.append(resolved_home / "workspace")
            readable.append(resolved_home / "media")
        protected.extend(_resolve_dirs(extra_protected))
        writable.extend(_resolve_dirs(extra_writable))
        readable.extend(_resolve_dirs(extra_readable))
        return cls(
            root=root,
            home=resolved_home,
            protected=tuple(dict.fromkeys(protected)),
            writable=tuple(dict.fromkeys(writable)),
            readable=tuple(dict.fromkeys(readable)),
        )

    # ------------------------------------------------------------ 解析
    def _expand_vars(self, raw: str) -> str:
        def sub(match: re.Match[str]) -> str:
            if match.group(1) == "MARKBOT_HOME" and self.home is not None:
                return str(self.home)
            return str(Path.home())

        return _VAR.sub(sub, raw)

    def expand(self, raw: str) -> Path:
        """把工具参数里的路径解析成绝对路径（相对路径以 ``root`` 为基准）。"""
        path = Path(self._expand_vars(str(raw))).expanduser()
        return (self.root / path).resolve() if not path.is_absolute() else path.resolve()

    # ------------------------------------------------------------ 判定
    def _spill_readable(self, path: Path) -> bool:
        """``<home>/sessions/<id>/files/**``：超限工具结果落盘处，模型需要能读回。"""
        if self.home is None:
            return False
        try:
            parts = path.relative_to(self.home / "sessions").parts
        except ValueError:
            return False
        return len(parts) >= 2 and parts[1] == "files"

    def carve_out(self, path: Path, *, write: bool) -> Path | None:
        """命中例外目录时返回该目录，否则 ``None``。"""
        for base in self.writable:
            if _within(path, base):
                return base
        if not write:
            for base in self.readable:
                if _within(path, base):
                    return base
            if self._spill_readable(path):
                return self.home / "sessions" if self.home is not None else None
        return None

    def is_protected(self, path: Path) -> bool:
        return any(_within(path, base) for base in self.protected)

    def denial(self, path: Path, *, write: bool) -> str | None:
        """返回拒绝原因；允许时返回 ``None``。"""
        if self.carve_out(path, write=write) is not None:
            return None
        if self.is_protected(path):
            return f"受保护路径: {path}（{PROTECTED_HINT}）"
        if _within(path, self.root):
            return None
        return f"路径越界: {path}"

    def resolve(self, raw: str, *, write: bool = False) -> Path:
        path = self.expand(raw)
        reason = self.denial(path, write=write)
        if reason is not None:
            raise PermissionError(reason)
        return path

    # ------------------------------------------------------------ 命令扫描
    def _protected_by_pattern(self, pattern: Path) -> bool:
        """带通配符的路径（``~/.mark*/learned``）：逐级前缀与受保护目录做通配匹配。"""
        parts = pattern.parts
        for base in self.protected:
            target = base.as_posix()
            for length in range(1, len(parts) + 1):
                prefix = Path(*parts[:length]).as_posix()
                if fnmatch.fnmatchcase(target, prefix):
                    return True
        return False

    def _candidates(self, command: str) -> list[str]:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            tokens = command.split()
        out: list[str] = []
        for token in tokens:
            if "://" in token:
                continue
            # ``--output=path`` / ``VAR=path``：只检查值，变量名不是路径
            match = _ASSIGN.match(token)
            out.append(token[match.end():] if match else token)
        return out

    def scan_command(self, command: str) -> str | None:
        """命令里出现受保护路径时返回拒绝原因。"""
        home_name = self.home.name if self.home is not None else ""
        for token in self._candidates(command):
            text = self._expand_vars(token)
            if not text or not (
                "/" in text or text.startswith(("~", ".")) or (home_name and home_name in text)
            ):
                continue
            try:
                path = Path(text).expanduser()
                absolute = path if path.is_absolute() else self.root / path
            except (OSError, RuntimeError, ValueError):
                # ``~nosuchuser`` 之类无法展开的写法：不是可识别路径，跳过
                continue
            if _GLOB_CHARS & set(text):
                # 只对明确指向工作区之外的通配路径（绝对路径、~、$HOME）做匹配；
                # 相对的 ``.*`` 多半是 sed/grep 正则，不是路径
                if path.is_absolute() and self._protected_by_pattern(
                    Path(os.path.normpath(absolute))
                ):
                    return self._command_reason(token)
                continue
            try:
                resolved = absolute.resolve()
            except (OSError, RuntimeError):
                continue
            if self.carve_out(resolved, write=False) is not None:
                continue
            if self.is_protected(resolved):
                return self._command_reason(token)
            # ``cd .. && cat .markbot/learned/...``：相对路径的基准目录未知。
            # 只要路径开头（跳过 ``.`` / ``..``）就是 home 目录名，就视为可疑
            if home_name and not path.is_absolute():
                head = next((part for part in path.parts if part not in {".", ".."}), "")
                if head == home_name:
                    return self._command_reason(token)
        return None

    def _command_reason(self, token: str) -> str:
        scratch = self.writable[0] if self.writable else self.root
        return (
            f"命令引用了受保护路径 {token!r}：{PROTECTED_HINT}。"
            f"临时脚本与数据请放在工作区或 {scratch}"
        )


__all__ = ["PROTECTED_HINT", "PathGuard"]
