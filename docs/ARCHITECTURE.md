# MarkBot2 架构设计文档

| 项 | 值 |
|---|---|
| 版本 | v1.0 |
| 状态 | 设计基线，可用于开发 |
| 语言 | Python 3.12+ |
| 参考实现 | Pi（`D:\Source\pi`，TypeScript）用于验证 1+N 形态 |
| 优化内核 | Harness²（`D:\Source\harness2`，Python） |

---

## 目录

- [1. 背景与目标](#1-背景与目标)
- [2. 设计原则](#2-设计原则)
- [3. 总体架构](#3-总体架构)
- [4. 仓库结构与依赖方向](#4-仓库结构与依赖方向)
- [5. Kernel（1）](#5-kernel1)
- [6. 扩展面 N：四条车道](#6-扩展面-n四条车道)
- [7. 权限与沙箱](#7-权限与沙箱)
- [8. 学习层与 Harness² 集成](#8-学习层与-harness²-集成)
- [9. 运行流程](#9-运行流程)
- [10. 配置与磁盘布局](#10-配置与磁盘布局)
- [11. 扩展点矩阵](#11-扩展点矩阵)
- [12. 质量门禁](#12-质量门禁)
- [13. 开发里程碑](#13-开发里程碑)
- [14. 附录](#14-附录)

---

## 1. 背景与目标

### 1.1 要解决的问题

MarkBot2 是一个个人助手 agent。它必须同时满足两件相互拉扯的要求：

1. **功能持续增长**：今天接日历，明天接邮件，后天接代码仓库，能力会不断增加和替换。
2. **主程序不能跟着一起烂**：每加一个功能，主程序的复杂度不应该上升。

传统做法是把每个功能都写进主程序，最终得到一个「什么都懂、什么都改不动」的单体 agent。MarkBot2 的目标是相反的：**主程序极薄，能力全部外挂，并且外挂的能力要能被系统自己逐步优化。**

### 1.2 目标

- 支持 **1（Kernel）+ N（扩展）** 的架构，N 可以增删而不改 Kernel。
- 支持四类扩展：代码扩展、技能、分发包、学习层。
- 具备**权限边界**，第三方扩展不能无约束地读文件、联网、执行命令。
- 具备**自演化能力**：通过 Harness² 的离线递归，把真实会话中的经验沉淀成新的策略和技能，并安全晋升。
- 所有演化**可审计、可回滚**。

### 1.3 非目标

- 不做模型训练或微调。
- 不做多租户 SaaS；第一版是单用户本地/单机服务。
- 不在用户请求路径上做任何优化计算。
- 不追求扩展的完全隔离（第一版用能力清单 + 子进程约束，容器化作为可选增强）。

---

## 2. 设计原则

### P1. Kernel 只认识契约，不认识能力

Kernel 不 `import` 任何具体扩展、技能或包。它只依赖 `markbot.api` 和 `markbot.schema`。判断标准：**删掉 `capabilities/` 下所有内容，`markbot selftest` 仍然通过。**

### P2. 按风险分层，而不是按功能分类

不同扩展的风险等级不同，加载和信任策略也必须不同：

| 车道 | 形态 | 风险 | 信任要求 |
|---|---|---|---|
| A 代码扩展 | 可执行 Python 模块 | 高 | 需复核 + 权限清单 |
| B 技能 | 惰性文本 + 可选脚本 | 中 | 脚本执行需授权 |
| C 分发包 | A/B 的集合，带版本 | 高 | 安装前复核 |
| D 学习层 | 优化器生成的文本/脚本 | 中 | 门禁 + 可回滚 |

### P3. 控制面与数据面分离

- **数据面（在线）**：服务用户。加载当前运行时视图，执行，返回。
- **控制面（离线）**：改进系统。对已完成的会话做复盘和晋升。

用户请求路径上不运行 Propose–Probe–Compose。

### P4. 只有学习层可写

前三类车道是只读的、带版本号的、可审计的。优化器只能写 `learned/` 和 `strategy/`。这条约束保证：一次糟糕的自动优化不会破坏你手工安装的能力包。

### P5. 失败必须局部化

任何扩展、技能、包的加载或执行失败，只能影响它自己。Kernel 应当记录警告并继续运行。

### P6. 契约稳定，实现自由

`markbot.api` 是冻结的扩展契约，遵循语义化版本。Kernel 内部可以随意重构，扩展不需要跟着改。

---

## 3. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│                        Kernel（1）                            │
│  AgentLoop · ToolRegistry · EventBus · Provider · Session      │
│  ContextBuilder · PermissionPolicy · TrustStore · Config       │
│  —— 不含任何领域逻辑 ——                                        │
└───────────────────────────┬──────────────────────────────────┘
                            │ 只依赖 markbot.api / markbot.schema
┌───────────────────────────▼──────────────────────────────────┐
│                        扩展面 N                                │
│                                                              │
│  A 代码扩展        B 技能          C 分发包       D 学习层       │
│  ExtensionAPI     SKILL.md        markbot.toml   learned/     │
│  进程内注册        惰性加载        版本化分发      优化器写入     │
│  只读             只读            只读          可写、可回滚    │
└──────────────────────────────────────────────────────────────┘
                            ▲
                            │ 离线读取会话、写入 learned/
┌───────────────────────────┴──────────────────────────────────┐
│                     Optimizer（控制面）                        │
│  Harness² 递归 · 会话归档适配器 · 回归门禁 · 快照与回滚         │
└──────────────────────────────────────────────────────────────┘
```

### 3.1 为什么是四条车道

参考 Pi 的实践，能力按「风险 × 加载时机」分为三类：代码扩展（启动时、全权限）、技能（惰性、纯文本）、分发包（分发单元）。MarkBot2 增加第四条学习层，因为 Harness² 的核心价值就是让系统自己长出能力。

把四类混成一种「插件」会导致：每写一个纯文本技能都要走代码审查流程；自动生成的内容和手工安装的包无法区分；出问题时不知道回滚谁。

### 3.2 与 Harness² 八组件的关系

Harness² 的 `c1..c8` 不是八个目录，而是**八个逻辑插槽**。MarkBot2 的运行时视图由四条车道合成：

| Harness² 组件 | MarkBot2 中的来源 | 可写 |
|---|---|---|
| c1 `systemprompt.md` | `learned/strategy/system_prompt.md` + Kernel 默认 | 是 |
| c2 `guardrails.md` | `learned/strategy/guardrails.md` + 包追加约束 | 是 |
| c3 `LongTermMEMORY.md` | `learned/strategy/memory.md` | 是 |
| c4 `tool_descriptions/` | 由注册的 Tool 自动生成；`learned/tool_notes/` 可补充 | 部分是 |
| c5 `skills/` | 车道 B + `learned/skills/` | 部分是 |
| c6 `sub_agents/` | 车道 A/B + `learned/sub_agents/` | 部分是 |
| c7 `scripts/` | 技能内 `scripts/`、包内脚本 | 否 |
| c8 `plugin/` | 车道 A（事件钩子） | 否 |

Kernel 启动时把这些来源合成为一份**运行时视图（RuntimeView）**，并持久化一份快照用于审计。运行时视图是计算产物，不是手工维护的目录。

---

## 4. 仓库结构与依赖方向

### 4.1 依赖方向（强制）

```
markbot.schema      纯数据模型，无内部依赖
markbot.api         稳定扩展契约，只依赖 schema
markbot.kernel      依赖 api + schema
markbot.extensions  依赖 api（不得依赖 kernel）
markbot.optimizer   依赖 schema（可依赖外部 harness2）
markbot.cli         依赖 kernel + optimizer
```

反向依赖在 CI 中直接判定失败：

```bash
# 扩展不得 import kernel 内部实现
! grep -rE "from markbot\.kernel|import markbot\.kernel" markbot/extensions/
# Kernel 不得 import 具体扩展
! grep -rE "from markbot\.extensions|import markbot\.extensions" markbot/kernel/
# Kernel 不得依赖优化器
! grep -rE "from markbot\.optimizer|import markbot\.optimizer" markbot/kernel/
```

### 4.2 仓库布局

```
markbot2/
├── pyproject.toml
├── docs/
│   └── ARCHITECTURE.md
├── src/
│   └── markbot/
│       ├── schema/           # pydantic 模型
│       │   ├── models.py
│       │   └── version.py
│       ├── api/              # 稳定契约（扩展唯一可依赖的入口）
│       │   ├── __init__.py
│       │   ├── tool.py
│       │   ├── events.py
│       │   ├── extension.py
│       │   └── ui.py
│       ├── kernel/
│       │   ├── loop.py
│       │   ├── tools.py
│       │   ├── events.py
│       │   ├── session.py
│       │   ├── context.py
│       │   ├── registry.py
│       │   ├── runtime_view.py
│       │   ├── permissions.py
│       │   ├── trust.py
│       │   ├── config.py
│       │   └── providers/
│       │       ├── base.py
│       │       ├── openai_compat.py
│       │       └── anthropic.py
│       ├── extensions/       # 内置扩展（最小集）
│       │   ├── fs/
│       │   ├── shell/
│       │   ├── web/
│       │   └── skills/
│       ├── optimizer/
│       │   ├── adapter.py
│       │   ├── gate.py
│       │   ├── snapshots.py
│       │   └── run.py
│       └── cli/
│           └── main.py
├── capabilities/             # 外部能力包（非内置）
│   └── office/
│       ├── markbot.toml
│       ├── extensions/
│       ├── skills/
│       └── prompts/
├── evals/
│   └── cases/
└── tests/
```

---

## 5. Kernel（1）

### 5.1 职责边界

Kernel 只做七件事：

1. 组装上下文并调用模型
2. 解析工具调用并执行
3. 管理会话状态与持久化
4. 派发生命周期事件
5. 执行权限策略
6. 加载/合成运行时视图
7. 管理配置与信任

Kernel **不做**：领域推理、特定业务流程、UI 布局、优化计算。

规模目标：不含注释与测试，核心代码控制在 1500 行以内。超过时优先怀疑某个能力被写进了 Kernel。

### 5.2 Agent Loop

```python
# src/markbot/kernel/loop.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from markbot.api.events import EventName
from markbot.api.tool import ToolContext, ToolResult
from markbot.kernel.context import ContextBuilder
from markbot.kernel.events import EventBus
from markbot.kernel.permissions import PermissionPolicy
from markbot.kernel.providers.base import Provider, ModelReply
from markbot.kernel.tools import ToolRegistry


@dataclass
class LoopConfig:
    max_steps: int = 30
    tool_timeout_s: float = 60.0
    parallel_tools: bool = True


@dataclass
class TurnResult:
    text: str = ""
    tool_calls: int = 0
    stopped_reason: str = "completed"
    error: str | None = None


class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        context: ContextBuilder,
        events: EventBus,
        policy: PermissionPolicy,
        cfg: LoopConfig | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._context = context
        self._events = events
        self._policy = policy
        self._cfg = cfg or LoopConfig()

    async def run(self, session) -> TurnResult:
        await self._events.emit(EventName.TURN_START, session=session)
        steps = 0
        try:
            while steps < self._cfg.max_steps:
                steps += 1
                payload = await self._context.build(session, self._tools.schemas())
                await self._events.emit(
                    EventName.BEFORE_PROVIDER_REQUEST, session=session, payload=payload
                )
                reply: ModelReply = await self._provider.complete(payload)
                await self._events.emit(
                    EventName.AFTER_PROVIDER_RESPONSE, session=session, reply=reply
                )

                if not reply.tool_calls:
                    session.append_assistant(reply.text)
                    return TurnResult(text=reply.text, stopped_reason="completed")

                session.append_assistant(reply.text, tool_calls=reply.tool_calls)
                results = await self._execute_tools(session, reply.tool_calls)
                session.append_tool_results(results)
            return TurnResult(stopped_reason="max_steps")
        except Exception as exc:  # noqa: BLE001 - 顶层必须兜住
            return TurnResult(stopped_reason="error", error=str(exc))
        finally:
            await self._events.emit(EventName.TURN_END, session=session)

    async def _execute_tools(self, session, calls) -> list[ToolResult]:
        async def one(call) -> ToolResult:
            tool = self._tools.get(call.name)
            if tool is None:
                return ToolResult.error(f"unknown tool: {call.name}")

            event = await self._events.emit(
                EventName.TOOL_CALL, session=session, call=call, tool=tool
            )
            if event.blocked:
                return ToolResult.error(event.block_reason or "blocked by extension")

            decision = self._policy.check(tool, call.args)
            if not decision.allowed:
                return ToolResult.error(f"permission denied: {decision.reason}")

            ctx = ToolContext(session=session, policy=self._policy)
            try:
                result = await asyncio.wait_for(
                    tool.handler(ctx, call.args), timeout=tool.timeout_s
                )
                return result
            except asyncio.TimeoutError:
                return ToolResult.error(f"tool timeout: {tool.name}")
            except Exception as exc:  # noqa: BLE001 - 单个工具失败不影响循环
                return ToolResult.error(f"tool failed: {exc}")

        if self._cfg.parallel_tools:
            return list(await asyncio.gather(*(one(c) for c in calls)))
        return [await one(c) for c in calls]
```

要点：

- 工具失败被转换成 `ToolResult.error`，模型可以看到错误并自行纠正，循环不崩。
- 权限检查放在执行前，而不是注册时。注册时只校验声明，运行时才判断实际参数。
- 每个事件都可以被扩展 `block`，这是实现「确认危险操作」的标准入口。

### 5.3 工具契约

```python
# src/markbot/api/tool.py
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str) -> "ToolResult":
        return cls(content=message, is_error=True)


class ToolContext(Protocol):
    session: Any
    policy: Any

    def secret(self, name: str) -> str:
        """在边界处取密钥；密钥永不进入模型上下文。"""
        ...

    def confirm(self, title: str, message: str) -> Awaitable[bool]:
        ...


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    handler: ToolHandler
    capabilities: frozenset[str] = frozenset()
    source: str = "builtin"          # builtin | package | learned
    timeout_s: float = 60.0
    expose: Literal["direct", "deferred", "hidden"] = "direct"

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.params.model_json_schema(),
        }
```

`expose` 的语义：

- `direct`：工具声明直接进入模型上下文。
- `deferred`：只在技能或命令中被按需拉起，减少上下文占用。
- `hidden`：只允许扩展之间调用，模型不可见。

### 5.4 事件总线

```python
# src/markbot/kernel/events.py
from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

Handler = Callable[..., Any | Awaitable[Any]]


@dataclass
class EventOutcome:
    blocked: bool = False
    block_reason: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[str, Handler]]] = defaultdict(list)

    def subscribe(self, event: str, owner: str, handler: Handler) -> Callable[[], None]:
        entry = (owner, handler)
        self._handlers[event].append(entry)

        def unsubscribe() -> None:
            if entry in self._handlers[event]:
                self._handlers[event].remove(entry)

        return unsubscribe

    async def emit(self, event: str, **kwargs: Any) -> EventOutcome:
        outcome = EventOutcome()
        for owner, handler in list(self._handlers.get(event, ())):
            try:
                result = handler(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001 - 单个扩展失败不影响其它扩展
                outcome.data.setdefault("errors", []).append(
                    {"owner": owner, "error": str(exc)}
                )
                continue
            if isinstance(result, dict):
                if result.get("block"):
                    outcome.blocked = True
                    outcome.block_reason = result.get("reason")
                outcome.data.update(result.get("data") or {})
        return outcome
```

事件按注册顺序执行；`block` 一旦出现即短路后续扩展。异常被吞掉并记录，符合 P5。

### 5.5 Provider 抽象

Provider 只负责「把消息和工具 schema 发给模型，拿回文本或工具调用」。

```python
# src/markbot/kernel/providers/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class Provider(Protocol):
    name: str

    async def complete(self, payload: dict[str, Any]) -> ModelReply: ...
```

第一版实现两个：`openai_compat`（覆盖绝大多数兼容接口）和 `anthropic`。

### 5.6 上下文组装

`ContextBuilder` 每次请求重新合成，不做增量拼接，避免上下文污染。

```python
# src/markbot/kernel/context.py
class ContextBuilder:
    def __init__(self, strategy, skills, memory, tools, budget_chars: int = 60_000):
        ...

    async def build(self, session, tool_schemas) -> dict:
        return {
            "system": self._compose_system(),
            "messages": session.messages_for_model(),
            "tools": tool_schemas,
        }

    def _compose_system(self) -> str:
        parts = [
            self.strategy.system_prompt,     # learned 或默认
            self.strategy.guardrails,        # 约束
            self.memory.render(),            # 长期记忆
            self.skills.index_prompt(),      # 仅名称 + 描述 + 路径
            self.strategy.tool_notes,        # 可选的工具使用提示
        ]
        return "\n\n".join(p for p in parts if p)
```

三条硬约束：

1. **技能只放索引**。正文按需读取，避免上下文被撑爆。
2. **密钥永不入上下文**。只在工具边界注入。
3. **证据落盘**。会话轨迹写入磁盘，上下文里只放路径与摘要；这与 Harness² 的 `context.py` 思路一致。

### 5.7 运行时视图合成

```python
@dataclass
class RuntimeView:
    tools: ToolRegistry
    skills: SkillIndex
    strategy: Strategy
    sub_agents: dict[str, str]
    sources: dict[str, str]      # name -> builtin | package:<id> | learned


def compose(sources: list[SourceSet]) -> RuntimeView:
    """按优先级合并：project > user > learned > builtin。

    重名冲突时高优先级胜出，并记录一次 warning。
    任何单个来源解析失败只跳过该来源。
    """
```

`compose` 是 Kernel 中唯一需要知道「来源」的地方，且它只处理统一的 `SourceSet`，不处理具体格式。

---

## 6. 扩展面 N：四条车道

### 6.1 车道 A：代码扩展

#### 形态

一个 Python 模块，暴露 `setup(api)`。

```python
# capabilities/office/extensions/xlsx.py
from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult


class XlsxIn(BaseModel):
    path: str = Field(description="Excel 文件路径")
    sheet: str | None = Field(default=None, description="工作表名")


def setup(api: ExtensionAPI) -> None:
    async def xlsx_read(ctx, args: XlsxIn) -> ToolResult:
        # 具体实现略
        return ToolResult(content="...")

    api.register_tool(
        Tool(
            name="xlsx_read",
            description="读取 Excel 工作表为 Markdown 表格",
            params=XlsxIn,
            handler=xlsx_read,
            capabilities=frozenset({"fs.read:./**"}),
            source="package:office",
        )
    )
```

#### 扩展 API

```python
# src/markbot/api/extension.py
class ExtensionAPI(Protocol):
    version: str
    id: str

    def on(self, event: str, handler) -> Callable[[], None]: ...
    def register_tool(self, tool: Tool) -> None: ...
    def register_command(self, name: str, description: str, handler) -> None: ...
    def register_provider(self, provider) -> None: ...
    def register_skill_root(self, path: Path) -> None: ...
    def append_entry(self, key: str, value) -> None: ...
    def ui(self): ...
```

#### 生命周期

1. Kernel 扫描扩展路径，得到模块列表。
2. 为每个模块创建一个**暂存版** `ExtensionAPI`。
3. 调用 `setup(api)`。所有注册写入暂存区。
4. `setup` 成功则一次性提交；失败则整体丢弃并记录警告。
5. 提交后触发 `extension_loaded` 事件。

暂存再提交的价值：一个扩展加载到一半失败，不会留下半个工具。

#### 加载隔离

```python
def load_extension(path: Path, runtime) -> ExtensionHandle:
    api = StagedExtensionAPI(runtime, ext_id=path.stem)
    try:
        module = _import_from_path(path)
        setup = getattr(module, "setup", None)
        if setup is None:
            raise TypeError(f"{path.name} 缺少 setup(api) 函数")
        setup(api)
    except Exception as exc:  # noqa: BLE001
        log.warning("扩展加载失败，已跳过: %s -> %s", path, exc)
        return ExtensionHandle.failed(path, exc)
    api.commit()
    return ExtensionHandle.active(path)
```

#### 安全提示

代码扩展与 Kernel 同进程，拥有同等系统权限。因此：

- 安装第三方扩展前必须复核源码（与 Pi 的立场一致）。
- 扩展声明的 `capabilities` 会在工具执行时被检查，但扩展在 `setup` 期间仍可执行任意代码。
- 需要更强隔离时，把扩展放进容器运行，见第 7 章。

### 6.2 车道 B：技能

#### 规范

采用与 Pi / Agent Skills 兼容的目录结构：

```
skills/pdf-tools/
├── SKILL.md
├── scripts/
│   └── extract.py
├── references/
│   └── formats.md
└── assets/
    └── template.json
```

`SKILL.md`：

```markdown
---
name: pdf-tools
description: 提取 PDF 中的文本和表格。当用户需要读取、转换或检查 PDF 时使用。
allowed-tools: [read, run_script]
disable-model-invocation: false
---

# PDF tools

转换前先阅读 `references/formats.md`。脚本路径相对于本技能目录。
```

#### 惰性加载

启动时只建立索引：

```python
@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    path: Path
    source: str
    allowed_tools: tuple[str, ...]
    auto_invocable: bool = True
```

索引内容（只有这些）进入系统提示词：

```
可用技能：
- pdf-tools: 提取 PDF 中的文本和表格……（路径：/…/skills/pdf-tools/SKILL.md）
```

模型判断需要时，调用内置工具 `read_skill(name)` 读取正文，或用户显式输入 `/skill:pdf-tools`。

#### 脚本执行

技能内脚本不是普通工具，必须经过两道关：

1. 技能 frontmatter 声明 `allowed-tools` 包含 `run_script`。
2. 运行时权限策略允许 `exec`。

执行参数固定：工作目录为技能目录，环境变量只保留白名单，超时强制终止。

```python
async def run_skill_script(ctx, skill_name: str, script: str, args: list[str], timeout_s: float = 30):
    meta = ctx.session.skills.get(skill_name)
    if meta is None:
        return ToolResult.error(f"未知技能: {skill_name}")
    if "run_script" not in meta.allowed_tools:
        return ToolResult.error(f"技能 {skill_name} 未声明 run_script 权限")
    ...
```

### 6.3 车道 C：分发包

#### 目录约定

```
capabilities/office/
├── markbot.toml
├── extensions/
│   └── xlsx.py
├── skills/
│   └── xlsx-edit/SKILL.md
├── prompts/
│   └── weekly-report.md
└── README.md
```

`markbot.toml`：

```toml
[package]
name = "office"
version = "1.3.0"
description = "Office 文档读写能力"
priority = 100
license = "Apache-2.0"

[permissions]
capabilities = ["fs.read:./**", "fs.write:./output/**", "exec:python"]
```

也可以不写 manifest，此时按约定目录发现。显式 manifest 的优先级更高，且允许自定义路径和过滤。

#### 安装源

| 源 | 写法 | 行为 |
|---|---|---|
| 本地目录 | `./capabilities/office` | 直接加载，不复制 |
| Git | `git:github.com/you/markbot-office@v1` | 克隆到 `~/.markbot/packages/`，锁定 ref |
| pip | `pip:markbot-office==1.3.0` | 通过 entry point `markbot.extensions` 发现 |

版本锁定的规则：显式指定 `@v1` 或 `==1.3.0` 即固定；不指定则跟随最新，但每次变更都要重新确认信任。

#### 失败隔离

包安装或加载失败时：记录 warning、跳过该包的全部贡献、继续启动。下次启动重试。

```python
for pkg in discover_packages():
    try:
        sources.append(load_package(pkg))
    except Exception as exc:  # noqa: BLE001
        log.warning("包加载失败，已跳过: %s -> %s", pkg.id, exc)
        continue
```

### 6.4 车道 D：学习层

#### 目录

```
~/.markbot/learned/
├── manifest.json
├── strategy/
│   ├── system_prompt.md
│   ├── guardrails.md
│   └── memory.md
├── skills/
│   └── <name>/SKILL.md
├── sub_agents/
│   └── <name>.md
└── tool_notes/
    └── <tool>.md
```

`manifest.json` 记录当前生效的快照：

```json
{
  "snapshot": "sha256:9f2c...",
  "parent": "sha256:41ab...",
  "created_at": "2026-11-19T10:00:00+08:00",
  "entries": [
    {"path": "strategy/system_prompt.md", "source": "learned", "origin_task": "sess_01J..."},
    {"path": "skills/report-format/SKILL.md", "source": "learned", "origin_task": "sess_01J..."}
  ]
}
```

#### 可写范围

优化器只能写 `~/.markbot/learned/`。Kernel 在晋升时执行一次白名单检查：任何越界路径直接拒绝。

#### 回滚

每次晋升前生成快照，存放在 `~/.markbot/snapshots/<hash>/`。回滚命令恢复目录并更新 manifest 的 `parent` 链。

---

## 7. 权限与沙箱

Pi 明确不提供权限系统，这是它最大的短板。MarkBot2 必须补上，因为个人助手会接触邮件、日历、本地文件和 API 密钥。

### 7.1 能力清单

| 能力 | 语法 | 含义 |
|---|---|---|
| 文件读 | `fs.read:<glob>` | 只允许匹配的路径 |
| 文件写 | `fs.write:<glob>` | 只允许匹配的路径 |
| 网络 | `net:<host>` 或 `net:*` | 域名白名单 |
| 执行 | `exec:<program>` | 只允许指定可执行文件 |
| 密钥 | `secret:<name>` | 按名称取用 |
| 交互 | `ui` | 允许弹确认框 |

工具声明的能力必须是所属包声明能力的子集，否则注册失败。

### 7.2 信任模型

```json
// ~/.markbot/trust.json
{
  "packages": {
    "office": {"hash": "sha256:1a2b...", "approved_at": "2026-11-18T09:00:00+08:00"},
    "pdf-tools": {"hash": "sha256:77c1...", "approved_at": "2026-11-18T09:10:00+08:00"}
  }
}
```

规则：

1. 首次加载第三方包时，若 hash 不在信任表，默认拒绝加载，并提示运行 `markbot trust <pkg>`。
2. 包内容变化（hash 变化）视为未信任，需要重新确认。
3. 项目级包（`.markbot/`）必须显式授予项目信任。
4. `learned/` 由门禁自动信任，但同样写入审计。

### 7.3 执行隔离

分三档，按信任等级选择：

| 档位 | 方式 | 适用 |
|---|---|---|
| standard | 同进程 + 能力检查 | 内置扩展、已信任包 |
| sandbox | 子进程 + 受限环境 + 工作目录隔离 | 未完全信任的脚本 |
| container | 容器 + 只读挂载 + 网络策略 | 第三方代码、高风险工具 |

第一版实现 `standard` 与 `sandbox`；`container` 预留接口。

`sandbox` 的约束：

```python
ENV_ALLOWLIST = {"PATH", "HOME", "LANG", "TZ", "TMPDIR"}

async def run_sandboxed(cmd: list[str], cwd: Path, timeout_s: float) -> ProcessResult:
    env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
    return await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
```

Windows 上没有可靠的轻量沙箱，建议：默认把不可信代码放到容器里，或至少用独立的低权限用户运行。

### 7.4 密钥处理

- 密钥只保存在 `~/.markbot/config.toml` 的 `[agent] api_key` 与 `[secrets]` 表中，该文件不应提交到版本库。
- 工具通过 `ctx.secret(name)` 取用，返回值绝不写入日志、上下文或会话归档。
- 归档时对密钥形状的字符串做一次脱敏扫描。

---

## 8. 学习层与 Harness² 集成

### 8.1 定位

Harness² 是**离线优化器**，不是运行时。它读取已完成的会话，产出更好的策略和技能，交给门禁审核，通过后写入 `learned/`。

两边的边界：

| Harness² | MarkBot2 |
|---|---|
| 任务（benchmark 提供） | 会话归档 |
| rollout | 用同一请求重放 |
| judge 打分 | 不用；改为回归集 + 门禁 |
| library 领域经验 | 直接沿用 |
| ledger 预算 | 参数化后沿用 |

### 8.2 会话归档

每次会话结束写入一条记录，结构对齐 Harness² 的 task 概念：

```
~/.markbot/sessions/<session_id>/
├── meta.json          # 时间、领域、耗时、状态
├── request.md         # 用户的原始请求
├── files/             # 输入附件快照
├── trajectory.jsonl   # 完整轨迹
├── deliverable/       # 最终产出
└── feedback.json      # 可选：用户反馈（点赞/纠正/评分）
```

`meta.json`：

```json
{
  "id": "sess_01JABCD",
  "ended_at": "2026-11-19T09:41:02+08:00",
  "domain": "office",
  "status": "completed",
  "harness_snapshot": "sha256:9f2c...",
  "steps": 7,
  "tool_calls": 12
}
```

### 8.3 适配器

```python
# src/markbot/optimizer/adapter.py
class AssistantAdapter:
    """把 MarkBot2 的会话归档映射成 Harness² 的 task。"""

    spec = BenchSpec(
        name="markbot",
        runs_root=Path.home() / ".markbot" / "optimizer" / "runs",
        install_mode="workspace_agents_md",
    )

    def tasks(self, split: str = "all") -> list[str]:
        # 顺序必须稳定；只返回已结束且早于本次运行起点的会话
        return [
            s.id
            for s in self.store.completed(before=self.run_started_at)
            if s.status == "completed"
        ]

    def domain_of(self, task_id: str) -> str:
        return self.store.meta(task_id)["domain"]

    def task_prompt(self, task_id: str) -> str:
        return self.store.read(task_id, "request.md")

    def task_files(self, task_id: str) -> Path:
        return self.store.path(task_id, "files")

    async def execute(self, task_id: str, harness_dir: Path, run_id: str):
        return await self.runner.replay(task_id, harness_dir, run_id)

    def base_harness(self) -> Path:
        return Path.home() / ".markbot" / "learned"

    async def score(self, run_dir: Path, task_id: str):
        # 个人场景没有 rubric：返回 None，由门禁在回归集上统一判定
        return None
```

注意 `score` 返回 `None` 是合法语义：未评分不等于 0 分，不会污染统计。

### 8.4 预算

Harness² 默认 `R = 4k`、`P = 3k`，这对个人助手过重。MarkBot2 参数化：

```toml
[optimizer]
enabled = true
k = 1
max_rollouts_per_task = 4
max_sessions_per_task = 3
max_tasks_per_run = 5
idle_minutes = 30
```

默认 `k = 1`，即每轮只提 2 个候选（INV + DOM）、各跑一次、合成 1 个终稿。总成本约 4 次执行 + 3 次会话，可以放在夜间。

### 8.5 晋升门禁

这是 Harness² 原本没有、但个人助手必须有的环节。

```python
# src/markbot/optimizer/gate.py
@dataclass
class GateResult:
    passed: bool
    reasons: list[str]


async def evaluate(candidate_dir: Path, current_dir: Path) -> GateResult:
    reasons: list[str] = []

    # 1. 必须真的变了
    if tree_hash(candidate_dir) == tree_hash(current_dir):
        reasons.append("候选与当前 harness 完全相同")

    # 2. 只允许写 learned/
    if illegal_paths(candidate_dir):
        reasons.append("候选包含越界路径")

    # 3. 不得引入新能力声明
    if new_capabilities(candidate_dir, current_dir):
        reasons.append("候选请求了新的权限")

    # 4. 回归集不得掉分
    base = await run_eval(current_dir)
    cand = await run_eval(candidate_dir)
    if cand.score < base.score - EPSILON:
        reasons.append(f"回归掉分: {base.score:.3f} -> {cand.score:.3f}")

    return GateResult(passed=not reasons, reasons=reasons)
```

任何一条不通过就不晋升，候选留在 `optimizer/rejected/` 供人工查看。

### 8.6 回归集

`evals/cases/*.yaml` 描述「输入 + 可验证的期望」：

```yaml
id: office-xlsx-summary
domain: office
prompt: 把 sales.xlsx 的 Q3 数据整理成一张汇总表
files: [fixtures/sales.xlsx]
checks:
  - type: contains
    value: "Q3"
  - type: llm_judge
    rubric: 汇总表包含地区和金额两列，数字与源文件一致
```

回归集不追求覆盖所有场景，只守住已经做对的事。三到五条起步即可。

---

## 9. 运行流程

### 9.1 启动

```
1. 读取 ~/.markbot/config.toml
2. 解析信任表 trust.json
3. 加载 builtin 扩展
4. 扫描 packages（本地 / git / pip），逐个隔离加载
5. 读取 learned/
6. 合成 RuntimeView，检测重名冲突
7. 校验：每个工具的能力声明 ⊆ 所属包声明
8. 打印能力摘要（工具数、技能数、来源分布）
9. 启动会话循环或进入 CLI
```

任何一步失败都只降级，不阻断启动。唯一例外：`config.toml` 语法错误时直接报错退出，避免带着错误配置运行。

### 9.2 一次请求

```
用户输入
  → context.build()：策略 + 记忆 + 技能索引 + 工具 schema
  → provider.complete()
  → 若无工具调用：返回文本，写会话归档
  → 若有工具调用：
       event: tool_call（可被扩展 block）
       → permission.check()
       → tool.handler()
       → event: tool_result
       → 结果写回消息，继续循环
  → 达到步数上限或完成：写归档
```

### 9.3 一次优化

```
触发条件（空闲 / 定时 / 手动 markbot optimize）
  → 选取 eligible 会话（已完成、早于本次运行、未处理）
  → Harness² run_parallel(k=1)
       Propose（INV + DOM）→ Probe → Compose
  → 盲选交付物
  → 若选中 base：本轮无变化，结束
  → 若选中候选：写临时目录
  → 门禁 evaluate()
       ├─ 不通过 → optimizer/rejected/
       └─ 通过   → 快照当前 learned/ → 替换 → 更新 manifest
  → 记录审计日志
```

### 9.4 热重载

`markbot reload` 重新执行「步骤 4–8」，替换 RuntimeView。进行中的会话继续使用旧视图，新会话使用新视图，避免请求中途能力集合突变。

---

## 10. 配置与磁盘布局

### 10.1 目录

```
~/.markbot/
├── config.toml
├── trust.json
├── packages/            # git / pip 安装的包
├── learned/             # 车道 D
├── snapshots/           # 学习层快照
├── sessions/            # 会话归档
├── optimizer/
│   ├── runs/
│   └── rejected/
├── logs/
└── cache/
```

项目级：

```
<project>/.markbot/
├── settings.toml        # 项目覆盖配置
├── extensions/          # 项目扩展（需项目信任）
├── skills/
└── trust.json
```

### 10.2 配置示例

```toml
# ~/.markbot/config.toml
[agent]
model = "claude-sonnet-4-5"
max_steps = 30
tool_timeout_s = 60
parallel_tools = true
context_budget_chars = 60000

[permissions]
profile = "standard"          # strict | standard | trusted
confirm_on_write = true
confirm_on_exec = true

[packages]
allow_project_packages = false

[optimizer]
enabled = true
k = 1
max_rollouts_per_task = 4
max_sessions_per_task = 3
idle_minutes = 30
```

### 10.3 优先级

```
项目扩展 > 用户安装包 > 学习层 > 内置
```

冲突只发生在同名条目上，例如两个包都注册 `xlsx_read`：

- 高优先级生效。
- 低优先级条目不加载，但会在 `markbot doctor` 中列出冲突。
- 不做静默合并，避免行为不可预测。

---

## 11. 扩展点矩阵

| 你想加 | 需要做什么 | Kernel 改动 |
|---|---|---|
| 一个工具 | 车道 A 注册 | 0 |
| 一个事件钩子 | `api.on()` | 0 |
| 一个斜杠命令 | `api.register_command()` | 0 |
| 一段专业流程 | 车道 B 写 `SKILL.md` | 0 |
| 一个确定性脚本 | 技能内 `scripts/` | 0 |
| 一个子 agent | 包内 `sub_agents/*.md` | 0 |
| 一套完整能力 | 车道 C 建包 | 0 |
| 接 MCP server | 包内声明 | 0 |
| 换模型 | 换 Provider 实现 | 0 |
| 加一个入口（CLI / IM / 语音） | 实现 Channel 适配 | 0 |
| 换优化算法 | 换 optimizer 实现 | 0 |
| 新增一种「资源类型」 | 改 `markbot.schema`，升 minor | 0 |
| 改会话协议 / 权限模型 | 改 schema，升 major | 需要 |

最后两行是唯一会影响 Kernel 的改动。策略是：**把这类改动收敛到 `markbot.schema` 这一个包里**，让它的版本号成为唯一的兼容性判断依据。

---

## 12. 质量门禁

### 12.1 CI 必跑检查

```bash
# 1. 依赖方向
! grep -rE "from markbot\.kernel" src/markbot/extensions/
! grep -rE "from markbot\.extensions" src/markbot/kernel/
! grep -rE "from markbot\.optimizer" src/markbot/kernel/

# 2. 删除所有外部包后仍可启动
mv capabilities /tmp/capabilities && markbot selftest
mv /tmp/capabilities capabilities

# 3. 单个包故障不影响其它
markbot selftest --inject-failure office

# 4. manifest 与权限声明校验
markbot doctor --validate

# 5. 学习层越界检查
markbot doctor --learned-paths
```

### 12.2 单元测试重点

| 目标 | 用例 |
|---|---|
| Loop | 无工具直接返回；工具失败可恢复；达到步数上限；事件 block 生效 |
| Registry | 重名冲突按优先级解决；单扩展加载失败被跳过 |
| Permission | 越权路径被拒；临时授权不持久化 |
| Skills | 启动只加载索引；正文按需读取；脚本越权被拒 |
| Optimizer | 门禁拒绝相同候选；拒绝新增权限；拒绝回归掉分 |
| Snapshot | 晋升后可回滚到上一快照 |

### 12.3 性能与成本约束

- 启动到可交互不超过 2 秒（不含包安装）。
- 单次请求的工具调用并发执行，串行时延不应叠加。
- 技能索引超过 8k 字符时自动降级为「仅名称 + 一行描述」。

---

## 13. 开发里程碑

### M0：骨架

- `markbot.api` 与 `markbot.schema` 定型。
- Kernel 跑通：一个内置 `echo` 工具 + 一个 Provider。
- `markbot selftest` 可用。

验收：`python -m markbot "你好"` 能返回模型回复。

### M1：技能车道

- `SKILL.md` 发现、索引、惰性加载。
- `/skill:<name>` 命令。
- 内置 `read_skill` 工具。

验收：放入一个技能目录，模型能在相关任务中自动读取正文。

### M2：权限与信任

- 能力清单解析与校验。
- 运行时策略。
- `trust.json` 与 `markbot trust` 命令。
- 沙箱子进程执行。

验收：越权文件访问被拒绝，并返回可读错误。

### M3：分发包

- `markbot.toml` 发现。
- 本地 / git / pip 三种安装源。
- 失败隔离与 `markbot doctor`。

验收：装一个第三方包，删掉它后系统仍正常启动。

### M4：代码扩展与命令

- 暂存式 `setup(api)` 提交。
- 事件钩子。
- 自定义命令与确认框 UI。

验收：写一个「删除前确认」扩展并生效。

### M5：学习层与优化器

- 会话归档。
- Harness² 适配器与 `k=1` 预算。
- 回归集、门禁、快照、回滚。
- `markbot optimize` / `markbot rollback`。

验收：跑一轮优化，只有通过回归集的候选被晋升，且能回滚。

### M6：多入口

- RPC / HTTP 入口。
- 第二个前端（IM 或语音）。

验收：同一 Kernel 同时服务 CLI 与另一个入口。

---

## 14. 附录

### A. 事件列表

| 事件 | 时机 | 可否 block | 典型用途 |
|---|---|---|---|
| `session_start` | 会话创建 | 否 | 注入上下文、初始化状态 |
| `session_end` | 会话结束 | 否 | 归档、清理 |
| `turn_start` | 一轮开始 | 否 | 计时、日志 |
| `turn_end` | 一轮结束 | 否 | 统计 |
| `before_provider_request` | 调用模型前 | 是 | 修改提示词、裁剪上下文 |
| `after_provider_response` | 模型返回后 | 否 | 记录用量、后处理 |
| `tool_call` | 工具执行前 | 是 | 危险操作确认、参数改写 |
| `tool_result` | 工具执行后 | 否 | 审计、脱敏 |
| `before_compaction` | 上下文压缩前 | 是 | 自定义压缩策略 |
| `input` | 用户输入进入前 | 是 | 预处理、拦截 |
| `shutdown` | 进程退出前 | 否 | 落盘 |

### B. manifest schema

```toml
[package]
name = "office"                 # 必填，唯一
version = "1.3.0"               # 必填，语义化版本
description = "..."             # 必填
priority = 100                  # 可选，默认 0
license = "Apache-2.0"          # 可选

[permissions]
capabilities = [                # 必填，可为空
  "fs.read:./**",
  "fs.write:./output/**",
  "net:api.example.com",
  "exec:python",
  "secret:EXAMPLE_KEY",
]

[resources]
extensions = ["./extensions"]
skills = ["./skills"]
prompts = ["./prompts/*.md"]
```

`capabilities` 是包级上限。包内任何工具声明的能力必须被它覆盖，否则启动时报错并跳过该包。

### C. 错误处理与降级

| 场景 | 行为 |
|---|---|
| 单个扩展 `setup` 抛异常 | 记录警告，丢弃该扩展 |
| 单个工具执行抛异常 | 转成错误结果返回模型 |
| 工具超时 | 中止并返回超时错误 |
| 包加载失败 | 跳过该包，继续启动 |
| 技能 frontmatter 不合法 | 跳过该技能，记录警告 |
| Provider 不可用 | 重试一次；仍失败则返回明确错误 |
| 学习层损坏 | 回退到上一个快照；无快照则回退到内置 |
| 门禁不通过 | 候选移入 `rejected/`，当前学习层不变 |

### D. 术语

| 术语 | 含义 |
|---|---|
| Kernel | 主程序，负责循环、工具箱、事件、会话、权限 |
| Lane | 扩展车道，共四条 |
| RuntimeView | 启动时合成的当前能力视图 |
| Strategy | 系统提示词、约束、记忆、工具提示的集合 |
| Source | 条目来源：`builtin` / `package:<id>` / `learned` |
| Snapshot | 学习层的一次完整快照，可回滚 |
| Gate | 晋升门禁 |
| Replay | 用同一请求重放，用于优化探测 |

---

## 附：与参考实现的关系

- **Pi** 验证了「注入式 ExtensionAPI + 约定式包 + 惰性技能」这套形态可行，MarkBot2 沿用其接口风格。
- **Pi 缺少的两点**，MarkBot2 必须自行补齐：权限/信任边界、学习层的来源标记与回滚。
- **Harness²** 提供了自演化算法与经验库，作为独立优化器接入，不进 Kernel。

这三者的关系是：Pi 提供形态，Harness² 提供演化，MarkBot2 负责把它们收敛成一套有边界、可回滚的个人助手。

## 参考项目
- D:\Source\pi （Pi 项目）
- D:\Source\harness2 （Harness² 项目）