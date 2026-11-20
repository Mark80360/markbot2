from __future__ import annotations

import tomllib

import pytest

from markbot.kernel.config import load_config


def test_invalid_toml_fails(tmp_path):
    (tmp_path / "config.toml").write_text("[agent\n", encoding="utf-8")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(home=tmp_path, project_root=tmp_path)


def test_ui_progress_can_be_disabled(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[ui]\nshow_progress = false\n", encoding="utf-8"
    )
    cfg = load_config(home=tmp_path, project_root=tmp_path)
    assert cfg.ui.show_progress is False


def test_project_overrides_user(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".markbot").mkdir(parents=True)
    home.mkdir()
    (home / "config.toml").write_text(
        '[agent]\nmodel = "user-model"\nmax_steps = 5\n', encoding="utf-8"
    )
    (project / ".markbot" / "settings.toml").write_text(
        '[agent]\nmodel = "project-model"\n', encoding="utf-8"
    )
    cfg = load_config(home=home, project_root=project)
    assert cfg.agent.model == "project-model"
    assert cfg.agent.max_steps == 5
    assert len(cfg.loaded) == 2
