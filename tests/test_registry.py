from __future__ import annotations

import json
from pathlib import Path

from markbot.api.ui import NullUI
from markbot.kernel.config import load_config
from markbot.kernel.registry import RuntimeAssembler
from markbot.kernel.trust import TrustStore


def _write_package(root: Path, name: str, capabilities: list[str], tool_caps: list[str]) -> Path:
    package = root / name
    (package / "extensions").mkdir(parents=True)
    (package / "markbot.toml").write_text(
        "[package]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        "\n[permissions]\n"
        f"capabilities = {json.dumps(capabilities)}\n"
        "\n[resources]\n"
        'extensions = ["./extensions"]\n',
        encoding="utf-8",
    )
    (package / "extensions" / "demo.py").write_text(
        "from pydantic import BaseModel\n"
        "from markbot.api import Tool, ToolResult\n"
        "\n"
        "class In(BaseModel):\n"
        "    pass\n"
        "\n"
        "def setup(api):\n"
        "    async def handler(ctx, args):\n"
        "        return ToolResult(content='ok')\n"
        "    api.register_tool(Tool(name='demo_tool', description='demo', params=In,"
        f" handler=handler, capabilities=frozenset({tool_caps!r})))\n",
        encoding="utf-8",
    )
    return package


def test_trusted_package_loads(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    package = _write_package(home / "packages", "demo", ["fs.read:./**"], ["fs.read:./**"])
    TrustStore(home, project).approve("demo", package)

    cfg = load_config(home=home, project_root=project)
    view = RuntimeAssembler(cfg, ui=NullUI()).build()
    assert "demo_tool" in view.tools
    assert any(pkg["id"] == "demo" and pkg["loaded"] for pkg in view.packages)


def test_untrusted_package_is_skipped(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    _write_package(home / "packages", "demo", ["fs.read:./**"], ["fs.read:./**"])

    cfg = load_config(home=home, project_root=project)
    view = RuntimeAssembler(cfg, ui=NullUI()).build()
    assert "demo_tool" not in view.tools
    assert any("未信任" in warning for warning in view.warnings)


def test_capability_violation_skips_package(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    package = _write_package(
        home / "packages", "demo", ["fs.read:./**"], ["net:*"]
    )
    TrustStore(home, project).approve("demo", package)

    cfg = load_config(home=home, project_root=project)
    view = RuntimeAssembler(cfg, ui=NullUI()).build()
    assert "demo_tool" not in view.tools
    info = next(pkg for pkg in view.packages if pkg["id"] == "demo")
    assert info["loaded"] is False
    assert "能力" in (info.get("error") or "")


def test_broken_extension_does_not_break_kernel(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    package = home / "packages" / "broken"
    (package / "extensions").mkdir(parents=True)
    (package / "markbot.toml").write_text(
        '[package]\nname = "broken"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (package / "extensions" / "bad.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    TrustStore(home, project).approve("broken", package)

    cfg = load_config(home=home, project_root=project)
    view = RuntimeAssembler(cfg, ui=NullUI()).build()
    assert "echo" in view.tools
    assert any("boom" in warning for warning in view.warnings)


def test_convention_package_without_manifest(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    package = home / "packages" / "plain"
    (package / "extensions").mkdir(parents=True)
    (package / "extensions" / "plain.py").write_text(
        "from pydantic import BaseModel\n"
        "from markbot.api import Tool, ToolResult\n"
        "\n"
        "class In(BaseModel):\n"
        "    pass\n"
        "\n"
        "def setup(api):\n"
        "    async def handler(ctx, args):\n"
        "        return ToolResult(content='plain')\n"
        "    api.register_tool(Tool(name='plain_tool', description='plain', params=In,"
        " handler=handler, capabilities=frozenset({'fs.read:./**'})))\n",
        encoding="utf-8",
    )
    TrustStore(home, project).approve("plain", package)

    cfg = load_config(home=home, project_root=project)
    view = RuntimeAssembler(cfg, ui=NullUI()).build()
    assert "plain_tool" in view.tools
    assert any(pkg["id"] == "plain" and pkg["loaded"] for pkg in view.packages)


def test_extension_loaded_event_fires(tmp_path):
    from markbot.kernel.app import Kernel
    from markbot.kernel.providers.echo import EchoProvider

    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    package = home / "packages" / "hooked"
    (package / "extensions").mkdir(parents=True)
    marker = tmp_path / "loaded.txt"
    (package / "extensions" / "hooked.py").write_text(
        f"def setup(api):\n"
        f"    def on_loaded(extension, path):\n"
        f"        open({str(marker)!r}, 'a', encoding='utf-8').write(extension + '\\n')\n"
        f"    api.on('extension_loaded', on_loaded)\n",
        encoding="utf-8",
    )
    TrustStore(home, project).approve("hooked", package)

    cfg = load_config(home=home, project_root=project)
    Kernel.build(config=cfg, provider=EchoProvider(), ui=NullUI())
    assert marker.exists()
    assert "package:hooked:hooked" in marker.read_text(encoding="utf-8")


def test_tool_conflict_keeps_higher_priority(tmp_path):
    from pydantic import BaseModel

    from markbot.api.tool import Tool, ToolResult
    from markbot.kernel.runtime_view import SourceSet, compose

    class In(BaseModel):
        pass

    async def handler(ctx, args):
        return ToolResult(content="x")

    def make(source: str) -> Tool:
        return Tool(name="dup", description=source, params=In, handler=handler)

    low = SourceSet(name="low", origin="builtin", priority=0, tools=[make("low")])
    high = SourceSet(name="high", origin="project", priority=4000, tools=[make("high")])
    view = compose([low, high])
    assert view.tools.get("dup").description == "high"
    assert view.tools.origin_of("dup") == "project"
    assert view.conflicts
