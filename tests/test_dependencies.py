"""依赖方向契约：schema / api / memory 不能反向依赖实现层（与 markbot selftest 同口径）。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "markbot"

FORBIDDEN = {
    "schema": ("markbot.api", "markbot.kernel", "markbot.memory", "markbot.optimizer", "markbot.cron"),
    "api": ("markbot.kernel", "markbot.memory", "markbot.optimizer", "markbot.cron", "markbot.extensions"),
    "memory": ("markbot.kernel", "markbot.optimizer"),
    "kernel": (
        "markbot.extensions",
        "markbot.optimizer",
        "markbot.cron",
        "markbot.cli",
        "markbot.memory",
    ),
    "extensions": ("markbot.kernel", "markbot.optimizer", "markbot.cli"),
    # 应用服务层：可以依赖 kernel/cron/memory，但不能反向依赖 CLI
    "server": ("markbot.cli",),
    "optimizer": ("markbot.kernel", "markbot.cli", "markbot.memory"),
}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    # 含函数体内的延迟导入：延迟导入只是把环藏起来，不是消除
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return found


@pytest.mark.parametrize("package", sorted(FORBIDDEN))
def test_dependency_direction(package):
    violations = []
    for path in sorted((SRC / package).rglob("*.py")):
        for module in _imports(path):
            if any(module == bad or module.startswith(bad + ".") for bad in FORBIDDEN[package]):
                violations.append(f"{path.relative_to(SRC)} -> {module}")
    assert not violations, "\n".join(violations)


def test_redaction_has_single_implementation():
    from markbot.api import redaction
    from markbot.kernel import session

    assert session.redact_secrets is redaction.redact_secrets
    assert session.register_secrets is redaction.register_secrets


def test_learned_whitelist_has_single_implementation():
    from markbot.optimizer import gate
    from markbot.schema import learned

    assert gate.allowed_path is learned.allowed_path
