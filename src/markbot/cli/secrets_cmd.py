"""`markbot secrets status|migrate`：把 config.toml 里的明文密钥迁到 secrets.toml。

迁移只做「搬家」，不改变取值：先写 ``secrets.toml``（0600、原子替换），再把
``config.toml`` 里对应的行注释掉，最后重新加载两份文件确认合并结果与迁移前一致。
迁移之后仍建议轮换密钥——旧值可能已经进过备份、同步盘或索引。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from markbot.kernel.config import (
    _merge,
    markbot_home,
    plaintext_secret_fields,
    read_secrets_file,
    secrets_path,
)

_SECTION = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
_KEY = re.compile(r"^\s*(\"[^\"]+\"|'[^']+'|[A-Za-z0-9_\-]+)\s*=")


def _get(data: dict[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _set(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_\-]+", key) else json.dumps(key)


def dump_secrets(data: dict[str, Any]) -> str:
    """把只含字符串叶子的两层/三层字典写成 TOML（json 字符串转义与 TOML 基本字符串兼容）。"""
    lines = ["# MarkBot 密钥文件：权限必须是 0600，不要提交、同步或分享", ""]

    def emit(prefix: str, node: dict[str, Any]) -> None:
        leaves = {k: v for k, v in node.items() if not isinstance(v, dict)}
        if leaves:
            lines.append(f"[{prefix}]")
            for key, value in leaves.items():
                lines.append(f"{_toml_key(key)} = {json.dumps(str(value), ensure_ascii=False)}")
            lines.append("")
        for key, value in node.items():
            if isinstance(value, dict):
                emit(f"{prefix}.{_toml_key(key)}" if prefix else _toml_key(key), value)

    emit("", {k: v for k, v in data.items() if isinstance(v, dict)})
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def comment_out(text: str, fields: set[str]) -> tuple[str, set[str]]:
    """把 ``fields``（``section.key``）对应的行注释掉，返回新文本与实际处理到的字段。"""
    section = ""
    out: list[str] = []
    done: set[str] = set()
    for line in text.splitlines(keepends=True):
        match = _SECTION.match(line)
        if match:
            section = ".".join(p.strip().strip("\"'") for p in match.group(1).split("."))
            out.append(line)
            continue
        key_match = _KEY.match(line)
        if key_match and not line.lstrip().startswith("#"):
            key = key_match.group(1).strip("\"'")
            dotted = f"{section}.{key}" if section else key
            if dotted in fields:
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}# {key} = \"\"  # 已迁移到 secrets.toml\n")
                done.add(dotted)
                continue
        out.append(line)
    return "".join(out), done


def migrate(home: Path, *, dry_run: bool = False, emit=print) -> int:
    config_path = home / "config.toml"
    if not config_path.exists():
        emit(f"未找到 {config_path}")
        return 1
    text = config_path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    fields = plaintext_secret_fields(data)
    if not fields:
        emit("config.toml 中没有明文密钥，无需迁移。")
        return 0
    existing = read_secrets_file(home)
    moved: dict[str, Any] = {}
    for dotted in fields:
        _set(moved, dotted, _get(data, dotted))
    merged_secrets = _merge(existing, moved)
    new_config, done = comment_out(text, set(fields))
    missing = sorted(set(fields) - done)
    if missing:
        emit(f"error: 以下字段写法无法自动迁移（内联表/多行值？），请手工处理: {', '.join(missing)}")
        return 1

    # 校验：迁移后 config.toml + secrets.toml 的合并结果必须与迁移前一致
    before = _merge(data, existing)
    after = _merge(tomllib.loads(new_config), merged_secrets)
    for dotted in fields:
        if _get(before, dotted) != _get(after, dotted):
            emit(f"error: 迁移校验失败（{dotted}），未写入任何文件")
            return 1

    emit("将迁移以下字段（只显示名称）：")
    for dotted in fields:
        emit(f"  - {dotted}")
    if dry_run:
        emit("--dry-run：未写入任何文件。")
        return 0
    target = secrets_path(home)
    _atomic_write_private(target, dump_secrets(merged_secrets))
    _atomic_write_private(config_path, new_config)
    emit(f"已写入 {target}（0600），并注释掉 config.toml 中的原值。")
    emit("下一步：请到对应平台轮换这些密钥，再把新值写进 secrets.toml。")
    return 0


def status(home: Path, *, emit=print) -> int:
    config_path = home / "config.toml"
    target = secrets_path(home)
    plain: list[str] = []
    if config_path.exists():
        plain = plaintext_secret_fields(tomllib.loads(config_path.read_text(encoding="utf-8")))
    emit(f"config.toml 明文密钥: {', '.join(plain) if plain else '无'}")
    if target.exists():
        mode = target.stat().st_mode & 0o777
        emit(f"secrets.toml: {target}（权限 {mode:o}{'，过宽！' if mode & 0o077 else ''}）")
    else:
        emit("secrets.toml: 不存在")
    return 1 if plain else 0


def secrets_command(argv: list[str]) -> int:
    home = markbot_home()
    sub = argv[0] if argv else "status"
    if sub == "status":
        return status(home)
    if sub == "migrate":
        return migrate(home, dry_run="--dry-run" in argv)
    print("用法: markbot secrets status | migrate [--dry-run]", file=sys.stderr)
    return 2


__all__ = ["comment_out", "dump_secrets", "migrate", "secrets_command", "status"]
