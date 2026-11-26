"""Phase 2 第二批（ADR 0013）的回归测试：项目登记与回合工作区、项目说明、读取去重、子代理、Git 工具、
危险 git 命令确认、交付报告、提示缓存、token 预算校准、编码评测、飞书 /project。"""

from __future__ import annotations

import asyncio
import importlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from markbot.api.ui import NullUI
from markbot.extensions import agents, git, report
from markbot.kernel.config import PermissionConfig, load_config, load_projects, save_project
from markbot.kernel.context import dedupe_reads, project_brief
from markbot.kernel.execution import LocalExecution
from markbot.kernel.permissions import PermissionPolicy, _risky_reason
from markbot.kernel.providers.base import ModelReply, ToolCall
from markbot.kernel.providers.echo import ScriptedProvider
from markbot.kernel.registry import StagedExtensionAPI
from markbot.kernel.runtime_view import SourceSet
from markbot.kernel.session import Session
from markbot.kernel.tools import KernelToolContext
from markbot.schema.session_log import trajectory_text

HAS_GIT = shutil.which("git") is not None


def _tools(module, *names, config=None):
    source = SourceSet(name="builtin", origin="builtin")
    api = StagedExtensionAPI("test", source=source, ui=NullUI(), config=config)
    module.setup(api)
    api.commit()
    registry = {tool.name: tool for tool in source.tools}
    return [registry[name] for name in names]


def _ctx(root: Path, home: Path | None = None, session: Session | None = None, **perm):
    perm = {"profile": "trusted", "trusted_exec": "all", **perm}
    policy = PermissionPolicy(PermissionConfig(**perm), root=root, home=home)
    return KernelToolContext(
        session=session or Session("测试"), policy=policy, env=LocalExecution(root, guard=policy.guard)
    )


def _call(tool, ctx, **kwargs):
    async def invoke():
        outcome = tool.handler(ctx, tool.params(**kwargs))
        return await outcome if hasattr(outcome, "__await__") else outcome

    return asyncio.run(invoke())


# ===================================================================== 项目


def test_project_registry_roundtrip(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    save_project(home, "demo", repo)
    assert load_projects(home) == {"demo": str(repo.resolve())}
    # config 里的 [projects] 叠加；非法名称与不存在的目录被忽略
    merged = load_projects(home, {"bad name": str(repo), "gone": str(tmp_path / "nope"), "x": str(repo)})
    assert set(merged) == {"demo", "x"}
    save_project(home, "demo", None)
    assert load_projects(home) == {}


def test_run_with_project_uses_project_workspace(make_kernel, home, project, tmp_path):
    other = tmp_path / "other-repo"
    other.mkdir()
    (other / "README.md").write_text("# Other\n项目说明", encoding="utf-8")
    (other / "AGENTS.md").write_text("所有函数必须写类型注解", encoding="utf-8")
    save_project(home, "other", other)
    cfg = load_config(home=home, project_root=project)
    cfg.permissions.profile = "trusted"
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[ToolCall(id="c1", name="write_file", args={"path": "out.txt", "content": "hi"})]),
            "写好了",
        ]
    )
    kernel = make_kernel(provider=provider, config=cfg)
    result = asyncio.run(kernel.run("写文件", project="other"))
    assert result.text == "写好了"
    assert (other / "out.txt").read_text(encoding="utf-8") == "hi"
    assert not (project / "out.txt").exists()
    system = provider.calls[0]["system"]
    assert "所有函数必须写类型注解" in system and "当前项目：other-repo" in system
    assert f"工作目录: {other.resolve()}" in system
    # 不绑定项目时回到默认工作区，也不注入项目说明
    provider.replies = ["ok"]
    asyncio.run(kernel.run("hi"))
    assert "所有函数必须写类型注解" not in provider.calls[-1]["system"]


def test_unknown_project_is_rejected(make_kernel):
    kernel = make_kernel(["不会调用"])
    result = asyncio.run(kernel.run("hi", project="nope"))
    assert result.stopped_reason == "error" and "未知项目" in result.error
    assert kernel.provider.calls == []


def test_project_brief_detects_stack(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("先写测试", encoding="utf-8")
    brief = project_brief(tmp_path)
    assert "Python（pyproject）" in brief and "先写测试" in brief


def test_cli_project_commands(tmp_path, monkeypatch, capsys):
    from markbot.cli.main import main

    home = tmp_path / "home"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("MARKBOT_HOME", str(home))
    assert main(["project", "add", "r1", str(repo)]) == 0
    assert main(["project", "list"]) == 0
    assert "r1" in capsys.readouterr().out
    assert main(["project", "add", "bad name", str(repo)]) == 2
    assert main(["project", "add", "h", str(home)]) == 2
    assert main(["project", "remove", "r1"]) == 0
    assert load_projects(home) == {}


def test_parse_chat_request_project():
    from markbot.server.http import parse_chat_request

    ok, error = parse_chat_request({"prompt": "x", "project": "markbot2"})
    assert error is None and ok["project"] == "markbot2"
    assert parse_chat_request({"prompt": "x", "project": "../etc"})[1] == "invalid project"


# ===================================================================== 读取去重


def test_dedupe_reads_keeps_latest():
    big = "x" * 600

    def call(cid):
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "name": "read_file", "arguments": {"path": "a.py"}}]}

    messages = [
        call("c1"), {"role": "tool", "tool_call_id": "c1", "content": big},
        call("c2"), {"role": "tool", "tool_call_id": "c2", "content": big + "new"},
    ]
    out = dedupe_reads(messages)
    assert out[1]["content"].startswith("[同一文件")
    assert out[3]["content"].endswith("new")
    assert messages[1]["content"] == big  # 原列表不变


# ===================================================================== 子代理


def test_spawn_agent_runs_child_and_returns_summary(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(
                tool_calls=[ToolCall(id="c1", name="spawn_agent", args={"task": "找出入口函数", "role": "explorer"})],
                usage={"prompt_tokens": 10, "completion_tokens": 1},
            ),
            ModelReply(text="子代理结论：入口在 main.py:12", usage={"prompt_tokens": 7, "completion_tokens": 3}),
            "主代理汇总完成",
        ]
    )
    kernel = make_kernel(provider=provider)
    result = asyncio.run(kernel.run("分析项目"))
    assert result.text == "主代理汇总完成"
    trajectory = trajectory_text(kernel.home / "sessions" / result.session_id)
    assert "子代理结论：入口在 main.py:12" in trajectory and "[子代理 explorer" in trajectory
    # 子代理只拿到只读工具，没有 spawn_agent
    child_tools = {tool["name"] for tool in provider.calls[1]["tools"]}
    assert "spawn_agent" not in child_tools and "write_file" not in child_tools
    assert "read_file" in child_tools
    assert "找出入口函数" in provider.calls[1]["messages"][0]["content"]
    # 子代理的 token 计入父回合
    assert result.total_usage["tokens_in"] == 17 + 0
    # 子会话不落盘
    assert not any("~" in path.name for path in (kernel.home / "sessions").iterdir())


def test_spawn_agent_unknown_role_and_list(make_kernel):
    provider = ScriptedProvider(
        [
            ModelReply(tool_calls=[ToolCall(id="c1", name="spawn_agent", args={"task": "x", "role": "wizard"})]),
            "好的",
        ]
    )
    kernel = make_kernel(provider=provider)
    result = asyncio.run(kernel.run("派个巫师"))
    assert "未知角色: wizard" in trajectory_text(kernel.home / "sessions" / result.session_id)


def test_custom_role_from_sub_agents():
    session = SimpleNamespace(runtime=SimpleNamespace(
        sub_agents={"docs": "---\ntools: [read_file, write_file]\ndescription: 写文档\n---\n你负责写文档。"}
    ))
    catalog = agents.roles(session)
    assert catalog["docs"]["tools"] == ["read_file", "write_file"]
    assert catalog["docs"]["prompt"] == "你负责写文档。"
    assert {"explorer", "reviewer", "tester", "researcher", "general"} <= set(catalog)


def test_subagent_cannot_spawn(tmp_path):
    (spawn,) = _tools(agents, "spawn_agent")
    session = Session("子")
    session.metadata["subagent"] = {"parent": "p"}

    async def runner(*args, **kwargs):
        raise AssertionError("不应调用")

    ctx = _ctx(tmp_path, session=session)
    ctx._subagent = runner
    result = _call(spawn, ctx, task="x")
    assert result.is_error and "不能再派生" in result.content


# ===================================================================== Git


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path.resolve() / "g"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.mark.skipif(not HAS_GIT, reason="需要 git")
def test_git_status_diff_commit_log(tmp_path):
    repo = _repo(tmp_path)
    status, diff, commit, log = _tools(git, "git_status", "git_diff", "git_commit", "git_log")
    ctx = _ctx(repo)
    assert _call(status, ctx).details["clean"] is True
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
    dirty = _call(status, ctx)
    assert dirty.details["count"] == 2 and "a.py" in dirty.content
    assert "+x = 2" in _call(diff, ctx).content
    committed = _call(commit, ctx, message="fix: x 改为 2", paths=["a.py"])
    assert not committed.is_error, committed.content
    assert "fix: x 改为 2" in _call(log, ctx).content
    # 只提交了列出的文件：b.py 仍未跟踪
    assert "b.py" in _call(status, ctx).content


@pytest.mark.skipif(not HAS_GIT, reason="需要 git")
def test_git_commit_refuses_secrets_and_bad_names(tmp_path):
    repo = _repo(tmp_path)
    commit, branch = _tools(git, "git_commit", "git_branch")
    ctx = _ctx(repo)
    (repo / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    refused = _call(commit, ctx, message="add env", paths=[".env"])
    assert refused.is_error and "密钥" in refused.content
    assert _call(branch, ctx, action="create", name="-D").is_error
    created = _call(branch, ctx, action="create", name="feat/x")
    assert not created.is_error, created.content
    assert "* feat/x" in _call(branch, ctx).content


def test_dangerous_git_commands_require_confirmation():
    for command in (
        "git push --force origin main", "git push -f", "git reset --hard HEAD~1", "git clean -fdx",
        "git checkout -- .", "git branch -D feat", "git rebase -i HEAD~3", "git stash drop",
    ):
        assert _risky_reason([command]) is not None, command
    for command in ("git status", "git push origin main", "git commit -m 'x'", "git diff --stat"):
        assert _risky_reason([command]) is None, command


def test_pinned_program_in_allow_exec_skips_confirmation(tmp_path):
    (status,) = _tools(git, "git_status")
    standard = PermissionPolicy(PermissionConfig(profile="standard"), root=tmp_path)
    assert standard.check(status, {}).requires_confirmation
    allowed = PermissionPolicy(PermissionConfig(profile="standard", allow_exec=["git"]), root=tmp_path)
    decision = allowed.check(status, {})
    assert decision.allowed and not decision.requires_confirmation
    strict = PermissionPolicy(PermissionConfig(profile="strict"), root=tmp_path)
    assert not strict.check(status, {}).allowed


# ===================================================================== 报告


def test_write_report_summarizes_changes(tmp_path):
    root = tmp_path.resolve() / "p"
    home = tmp_path.resolve() / "home"
    root.mkdir()
    session = Session("修复", session_id="sess_r1")
    session.file_changes = {str(root / "a.py"): {"added": 3, "removed": 1, "actions": ["edit"]}}
    session.verifications = [{"kind": "tests", "ok": True, "summary": "通过 5", "at": 0}]
    (write,) = _tools(report, "write_report")
    result = _call(
        write, _ctx(root, home=home, session=session),
        title="修复 add", summary="修好了加法", details="把减号改成加号", risks="无",
    )
    assert not result.is_error, result.content
    path = Path(result.details["path"])
    assert path.parent == home / "sessions" / "sess_r1" / "scratch" / "reports"
    text = path.read_text(encoding="utf-8")
    for expected in ("# 修复 add", "修好了加法", "a.py（+3 -1", "✅ 通过", "把减号改成加号"):
        assert expected in text
    assert "](" in result.content  # 提示在回复里附链接


# ===================================================================== 提示缓存 / token 预算


def test_anthropic_prompt_cache_markers(monkeypatch):
    from markbot.kernel.providers import anthropic

    captured: dict = {}

    async def fake_post(url, body, headers, **kwargs):
        captured["body"] = body
        return ModelReply(text="ok")

    monkeypatch.setattr(anthropic, "post_json", fake_post)
    payload = {"system": "sys", "messages": [{"role": "user", "content": "hi"}],
               "tools": [{"name": "a", "input_schema": {}}, {"name": "b", "input_schema": {}}]}
    cached = anthropic.AnthropicProvider(api_key="k", model="m", prompt_cache=True)
    asyncio.run(cached.complete(payload))
    body = captured["body"]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" in body["tools"][-1] and "cache_control" not in body["tools"][0]
    plain = anthropic.AnthropicProvider(api_key="k", model="m")
    asyncio.run(plain.complete(payload))
    assert captured["body"]["system"] == "sys"


def test_prompt_cache_auto_only_for_official_api(tmp_path, monkeypatch):
    from markbot.kernel.providers import build_provider

    monkeypatch.setenv("K", "sk")
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[agent]\nmodel = "claude-x"\napi_key = "env:K"\n', encoding="utf-8")
    assert build_provider(load_config(home=home, project_root=tmp_path)).prompt_cache is True
    (home / "config.toml").write_text(
        '[agent]\nmodel = "claude-x"\napi_key = "env:K"\nbase_url = "http://gw.local"\n', encoding="utf-8"
    )
    assert build_provider(load_config(home=home, project_root=tmp_path)).prompt_cache is False


def test_token_budget_calibrates_compaction(make_kernel, home, project):
    cfg = load_config(home=home, project_root=project)
    cfg.agent.max_tokens = 20_000
    provider = ScriptedProvider([ModelReply(text="ok", usage={"prompt_tokens": 1000})])
    kernel = make_kernel(provider=provider, config=cfg)
    before = kernel.loop._compactor.budget_chars
    assert before == 60_000
    asyncio.run(kernel.run("x" * 3000, archive=False))
    assert kernel.loop.chars_per_token != 3.0
    assert kernel.loop._compactor.budget_chars != before


# ===================================================================== 编码评测


def _case(tmp_path: Path) -> Path:
    case = tmp_path / "cases" / "fix-f"
    (case / "repo").mkdir(parents=True)
    (case / "repo" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (case / "case.yaml").write_text(
        'prompt: "让 f() 返回 2"\n'
        "check: python -c \"import m, sys; sys.exit(0 if m.f() == 2 else 1)\"\n"
        'allowed: ["m.py"]\n',
        encoding="utf-8",
    )
    return case.parent


def test_eval_harness_pass_and_fail(tmp_path, monkeypatch):
    from markbot.cli import eval_cmd

    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "realhome"))
    cases = eval_cmd.load_cases(_case(tmp_path))
    assert [case.name for case in cases] == ["fix-f"]
    fixing = ScriptedProvider(
        [
            ModelReply(tool_calls=[ToolCall(id="c1", name="edit_file", args={
                "path": "m.py", "old_string": "return 1", "new_string": "return 2"})]),
            "改好了（无法运行测试）",
            "改好了",
        ]
    )
    passed = asyncio.run(eval_cmd.run_case(cases[0], provider=fixing))
    assert passed.passed, passed.reason
    assert passed.changed == ["m.py"]
    idle = asyncio.run(eval_cmd.run_case(cases[0], provider=ScriptedProvider(["我不改"])))
    assert not idle.passed and "check" in idle.reason
    # 真实 home 没有被写入
    assert not (tmp_path / "realhome" / "sessions").exists()
    assert "通过率 1/2" in eval_cmd.render([passed, idle])


def test_repo_coding_cases_are_valid(tmp_path):
    from markbot.cli import eval_cmd

    cases = eval_cmd.load_cases(Path(__file__).resolve().parents[1] / "evals" / "coding")
    assert len(cases) >= 5
    for case in cases:
        # 复制出去再跑，避免在仓库里留下 __pycache__
        work = tmp_path / case.name
        shutil.copytree(case.path / "repo", work)
        code, output = eval_cmd.run_check(case.check, work)
        assert code != 0, f"{case.name} 的测试在修复前就通过了"


# ===================================================================== 飞书 /project


def test_feishu_project_command(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKBOT_HOME", str(tmp_path / "home"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "channels" / "feishu"))
    gateway_module = importlib.import_module("feishu_gateway")
    settings_module = importlib.import_module("feishu_settings")
    store_module = importlib.import_module("feishu_store")
    settings = settings_module.Settings(
        app_id="a", app_secret="s", verification_token="", host="127.0.0.1", port=8790,
        path="/e", markbot_url="http://127.0.0.1:8787", markbot_timeout_s=10.0, group_policy="open",
        reply_to_message=False, topic_isolation=True, react_emoji="", done_emoji="", allow_from=(),
        media_dir=tmp_path / "media", session_db=tmp_path / "f.db", workers=1,
    )
    store = store_module.Store(settings.session_db)
    gateway = gateway_module.Gateway(settings, store)
    sent: list[str] = []
    monkeypatch.setattr(gateway, "_reply", lambda chat_id, text, **kwargs: sent.append(text))
    monkeypatch.setattr(
        gateway, "_markbot_call",
        lambda method, path, payload=None, **kw: (200, {"projects": {"mb": "/src/mb"}}),
    )
    assert gateway._handle_command("k1", "oc", "/project mb", None, False)
    assert store.get_project("k1") == "mb" and "已切换到项目 mb" in sent[-1]
    gateway._handle_command("k1", "oc", "/project", None, False)
    assert "👉 mb" in sent[-1]
    gateway._handle_command("k1", "oc", "/project nope", None, False)
    assert "没有登记项目 nope" in sent[-1] and store.get_project("k1") == "mb"
    gateway._handle_command("k1", "oc", "/project off", None, False)
    assert store.get_project("k1") is None
