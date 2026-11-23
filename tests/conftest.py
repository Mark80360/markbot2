from __future__ import annotations

from pathlib import Path

import pytest

from markbot.kernel.app import Kernel
from markbot.kernel.config import load_config
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.session import clear_registered_secrets


@pytest.fixture(autouse=True)
def _isolate_registered_secrets():
    """Kernel 启动会把配置里的密钥登记到进程级脱敏表；每个用例前后清空，避免互相影响。"""
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def make_kernel(tmp_path, home, project):
    def factory(
        replies=None,
        *,
        include_packages: bool = False,
        learned_dir: Path | None = None,
        workspace: Path | None = None,
        provider=None,
        config=None,
    ) -> Kernel:
        cfg = config or load_config(home=home, project_root=project)
        return Kernel.build(
            config=cfg,
            provider=provider or ScriptedProvider(replies or []),
            include_packages=include_packages,
            learned_dir=learned_dir,
            workspace=workspace or project,
        )

    return factory


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
