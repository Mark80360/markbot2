from __future__ import annotations

import json
from pathlib import Path

from markbot.kernel.trust import TrustStore


def _package(root: Path, name: str = "demo", content: str = "v1") -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "tool.py").write_text(content, encoding="utf-8")
    return package


def test_unknown_package_is_untrusted(tmp_path):
    store = TrustStore(tmp_path / "home", tmp_path / "project")
    package = _package(tmp_path)
    assert not store.is_trusted("demo", package)
    assert store.entries() == {}


def test_approve_by_hash_and_detect_content_change(tmp_path):
    store = TrustStore(tmp_path / "home", tmp_path / "project")
    package = _package(tmp_path)

    digest = store.approve("demo", package)
    assert digest.startswith("sha256:")
    assert store.is_trusted("demo", package)
    assert store.is_trusted("demo")  # 不比对路径时只看是否存在批准记录
    assert json.loads((tmp_path / "home" / "trust.json").read_text())["packages"]["demo"][
        "hash"
    ] == digest

    (package / "tool.py").write_text("v2", encoding="utf-8")
    assert not store.is_trusted("demo", package)


def test_project_scope_overrides_user_scope(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    store = TrustStore(home, project)
    user_package = _package(tmp_path / "user", content="user-version")
    project_package = _package(tmp_path / "project-pkg", content="project-version")

    store.approve("demo", user_package)
    store.approve("demo", project_package, scope="project")

    assert (home / "trust.json").exists()
    assert (project / ".markbot" / "trust.json").exists()
    assert store.is_trusted("demo", project_package)
    assert not store.is_trusted("demo", user_package)
    assert "demo" in store.entries()


def test_revoke_removes_entries_from_both_scopes(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    store = TrustStore(home, project)
    package = _package(tmp_path)

    store.approve("demo", package)
    store.approve("demo", package, scope="project")
    assert store.revoke("demo")
    assert not store.revoke("demo")
    assert store.entries() == {}


def test_corrupt_trust_file_warns_and_is_ignored(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "trust.json").write_text("{not json", encoding="utf-8")
    store = TrustStore(home, tmp_path / "project")

    assert store.entries() == {}
    assert any("信任表损坏" in warning for warning in store.warnings)


def test_non_dict_trust_file_is_ignored(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "trust.json").write_text("[]", encoding="utf-8")
    store = TrustStore(home, tmp_path / "project")
    assert store.entries() == {}
