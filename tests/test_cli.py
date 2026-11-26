from __future__ import annotations

from markbot.cli.main import main


def test_help(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "markbot chat" in out


def test_init_creates_layout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("MARKBOT_HOME", str(home))
    assert main(["init"]) == 0
    for name in (
        "config.toml",
        "trust.json",
        "packages",
        "learned",
        "snapshots",
        "sessions",
        "optimizer/runs",
        "optimizer/rejected",
        "logs",
        "cache",
    ):
        assert (home / name).exists(), name
    text = (home / "config.toml").read_text(encoding="utf-8")
    assert "[agent]" in text
    assert "context_budget_ratio" in text


def test_init_keeps_existing_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text("# custom\n", encoding="utf-8")
    monkeypatch.setenv("MARKBOT_HOME", str(home))

    assert main(["init"]) == 0
    assert (home / "config.toml").read_text(encoding="utf-8") == "# custom\n"

    assert main(["init", "--force"]) == 0
    assert "[agent]" in (home / "config.toml").read_text(encoding="utf-8")


def test_selftest(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    assert main(["selftest"]) == 0


def test_selftest_inject_failure(repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    assert main(["selftest", "--inject-failure", "office"]) == 0


def test_doctor(tmp_path, repo_root, monkeypatch):
    monkeypatch.chdir(repo_root)
    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "home"))
    assert main(["doctor"]) == 0
    assert main(["doctor", "--validate", "--learned-paths"]) == 0
