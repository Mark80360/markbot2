"""第一阶段安全加固：本机令牌、回调地址白名单、密钥文件、OS 级隔离。"""

from __future__ import annotations

import os
import stat
import sys
import tomllib

import pytest

from markbot.cli.main import _hook_url, _sanitize_grants
from markbot.cli.secrets_cmd import comment_out, migrate
from markbot.kernel import local_auth, os_sandbox
from markbot.kernel.config import load_config, plaintext_secret_fields
from markbot.kernel.paths import PathGuard

# ------------------------------------------------------------ 本机令牌


def test_token_is_created_private_and_stable(tmp_path):
    token = local_auth.load_or_create_token(tmp_path)
    path = local_auth.token_path(tmp_path)
    assert len(token) >= 32
    assert local_auth.load_or_create_token(tmp_path) == token
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_verify_is_strict():
    assert local_auth.verify("abc", "abc")
    assert not local_auth.verify("", "abc")
    assert not local_auth.verify(None, "abc")
    assert not local_auth.verify("abd", "abc")
    assert not local_auth.verify("abc", "")


@pytest.mark.parametrize(
    "url,ok",
    [
        ("http://127.0.0.1:8790/notify", True),
        ("http://localhost:8790/confirm-request", True),
        ("http://[::1]:8790/notify", True),
        # TEST-NET 地址（RFC 5737）：保证不是本机网卡地址
        ("http://192.0.2.10:8790/notify", False),
        ("http://evil.example.com/notify", False),
        ("http://user:pw@127.0.0.1/notify", False),
        ("file:///etc/passwd", False),
        ("", False),
    ],
)
def test_loopback_url(url, ok):
    assert local_auth.is_loopback_url(url) is ok


def test_local_url_accepts_loopback_and_rejects_foreign():
    assert local_auth.is_local_url("http://127.0.0.1:8790/notify")
    assert not local_auth.is_local_url("http://192.0.2.10:8790/notify")
    assert not local_auth.is_local_url("http://example.com/notify")
    with_credentials = "http://" + "user:pw" + chr(64) + "127.0.0.1/x"
    assert not local_auth.is_local_url(with_credentials)
    assert not local_auth.is_local_address("0.0.0.0")


def test_hook_url_rejects_non_loopback():
    assert _hook_url({"url": "http://127.0.0.1:1/x"}) == ("http://127.0.0.1:1/x", None)
    url, reason = _hook_url({"url": "https://attacker.example/x"})
    assert url == "" and reason
    assert _hook_url({}) == ("", None)
    assert _hook_url(None) == ("", None)


def test_grants_only_accept_program_names():
    kept, dropped = _sanitize_grants(["curl", " git ", "*", "/bin/sh", "rm -rf", ""])
    assert kept == ["curl", "git"]
    assert dropped == ["*", "/bin/sh", "rm -rf"]
    assert _sanitize_grants(None) == ([], [])
    assert _sanitize_grants("curl") == ([], ["curl"])


# ------------------------------------------------------------ 密钥文件


def test_secrets_file_overlays_config_and_env_refs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[agent]\nmodel = "m"\napi_key = ""\n\n[secrets]\nFROM_ENV = "env:MB_TEST_SECRET"\n',
        encoding="utf-8",
    )
    secrets = home / "secrets.toml"
    secrets.write_text('[agent]\napi_key = "sk-file"\n\n[secrets]\nA = "1"\n', encoding="utf-8")
    secrets.chmod(0o600)
    monkeypatch.setenv("MB_TEST_SECRET", "from-env")

    cfg = load_config(home=home, project_root=tmp_path)
    assert cfg.agent.api_key == "sk-file"
    assert cfg.secrets == {"FROM_ENV": "from-env", "A": "1"}
    assert not any("明文密钥" in w for w in cfg.warnings)


def test_plaintext_and_loose_permissions_are_warned(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    config.write_text('[agent]\napi_key = "sk-plain-123456"\n', encoding="utf-8")
    config.chmod(0o644)
    cfg = load_config(home=home, project_root=tmp_path)
    joined = "\n".join(cfg.warnings)
    assert "agent.api_key" in joined
    assert "sk-plain-123456" not in joined  # 告警只出现字段名
    if os.name == "posix":
        assert "chmod 600" in joined


def test_plaintext_fields_ignore_env_refs():
    data = {
        "agent": {"api_key": "env:X"},
        "secrets": {"A": "v", "B": "", "C": "env:C"},
        "channels": {"feishu": {"app_id": "cli_x", "app_secret": "s", "verification_token": ""}},
    }
    assert plaintext_secret_fields(data) == ["secrets.A", "channels.feishu.app_secret"]


def test_comment_out_only_touches_target_section():
    text = '[agent]\napi_key = "k"  # c\nmodel = "m"\n[other]\napi_key = "keep"\n'
    out, done = comment_out(text, {"agent.api_key"})
    assert done == {"agent.api_key"}
    data = tomllib.loads(out)
    assert "api_key" not in data["agent"]
    assert data["other"]["api_key"] == "keep"


def test_migrate_moves_values_and_keeps_effective_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        "[agent]\n"
        'model = "m"\n'
        'api_key = "sk-aaaa-1111"\n\n'
        "[secrets]\n"
        'FEISHU_APP_SECRET = "fs-bbbb"\n\n'
        "[channels.feishu]\n"
        'app_id = "cli_x"\n',
        encoding="utf-8",
    )
    before = load_config(home=home, project_root=tmp_path)
    assert migrate(home, emit=lambda *_: None) == 0

    config_text = (home / "config.toml").read_text(encoding="utf-8")
    assert "sk-aaaa-1111" not in config_text and "fs-bbbb" not in config_text
    secrets = home / "secrets.toml"
    if os.name == "posix":
        assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    after = load_config(home=home, project_root=tmp_path)
    assert after.agent.api_key == before.agent.api_key == "sk-aaaa-1111"
    assert after.secrets == before.secrets
    assert after.raw["channels"]["feishu"]["app_id"] == "cli_x"
    # 再跑一次是幂等的
    assert migrate(home, emit=lambda *_: None) == 0


# ------------------------------------------------------------ OS 级隔离


def test_sandbox_profile_denies_protected_and_reopens_carve_outs(tmp_path):
    root = tmp_path / "root"
    home = tmp_path / "mbhome"
    root.mkdir()
    guard = PathGuard.build(root, home=home)
    profile = os_sandbox.build_profile(guard)
    deny = profile.index(f'(deny file-read* file-write* (subpath "{home.resolve()}"))')
    allow = profile.index(f'(subpath "{(home / "workspace").resolve()}")')
    assert deny < allow  # 例外目录写在拒绝规则之后才能生效
    assert "/files(/|$)" in profile


@pytest.mark.skipif(not os_sandbox.available("macos"), reason="需要 macOS sandbox-exec")
def test_sandbox_blocks_reading_protected_file(tmp_path):
    import asyncio

    from markbot.kernel.execution import LocalExecution

    root = tmp_path / "root"
    home = tmp_path / "mbhome"
    (home / "workspace").mkdir(parents=True)
    root.mkdir()
    (home / "config.toml").write_text("secret-value", encoding="utf-8")
    guard = PathGuard.build(root, home=home)
    env = LocalExecution(root, guard=guard, os_sandbox_mode="macos")

    # 变量拼接能绕过静态扫描，但绕不过内核
    blocked = asyncio.run(env.exec(f'D="{home}"; cat "$D/config.toml"'))
    assert "secret-value" not in blocked.stdout
    assert not blocked.ok

    ok = asyncio.run(env.exec(f'echo hi > "{home}/workspace/a.txt" && cat "{home}/workspace/a.txt"'))
    assert ok.ok and ok.stdout.strip() == "hi"


def test_sandbox_mode_degrades_with_warning_off_platform(tmp_path, monkeypatch):
    from markbot.kernel.execution import LocalExecution

    monkeypatch.setattr(sys, "platform", "linux")
    env = LocalExecution(tmp_path, os_sandbox_mode="macos")
    assert env.os_sandbox == "off"
