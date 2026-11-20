# MarkBot2

MarkBot2 是一个 **1 + N** 形态的个人助手 Agent：

- **1 个极薄 Kernel**：只负责上下文/模型调用、工具执行、会话持久化、生命周期事件、权限策略、运行时视图合成与配置信任。
- **N 条扩展车道**：代码扩展、技能、分发包、学习层。增删能力不改 Kernel。
- **可演化**：离线优化器读取已完成会话，产出更好的策略/技能，经回归门禁审核后晋升到学习层。
- **有边界**：能力清单 + 三档权限 + 信任表 + 子进程沙箱；所有演化可审计、可回滚。

完整设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 环境要求

- Python **3.12+**（开发使用 3.14 验证）
- 可选：`openpyxl`（Office 能力包）
- 可选：联网模型服务（Anthropic / OpenAI 兼容接口）

## 安装

```bash
# 在仓库根目录
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -e ".[dev]"          # 开发 + 测试
pip install -e ".[office]"       # 需要 xlsx 能力时
```

安装后可用的入口：

```bash
markbot --help
python -m markbot --help          # 等价
```

## 配置模型服务

所有配置都在 `~/.markbot/config.toml`（先运行 `markbot init`）：

```toml
[agent]
provider = "openai_compat"     # openai_compat | anthropic | echo
model = "sensenova-6.8-flash"
base_url = "http://10.10.2.90:3000/v1"
api_key = "sk-..."

# 供技能/扩展通过 secret:<name> 读取的密钥，不进模型上下文
[secrets]
EXAMPLE_KEY = "..."
```

- `provider` 留空时按 `model` 推断：`claude*` / `anthropic*` 走 Anthropic，其余走 OpenAI 兼容接口。
- 默认地址：Anthropic 为 `https://api.anthropic.com`，OpenAI 兼容为 `https://api.openai.com/v1`。
- 离线体验无需密钥：把 `provider` 改成 `echo`。

```toml
[agent]
provider = "echo"
```

## 快速开始

```bash
markbot init                               # 初始化 ~/.markbot（配置 + 默认目录）
markbot "把 hello.txt 的内容总结成一句话"   # 一次性执行
markbot chat                               # 交互式多轮会话（/new、/session、/skills、/skill:<name>、/exit）
markbot selftest                           # 自检：依赖方向、运行时视图、Agent Loop
markbot doctor                             # 运行时诊断
```

首次使用建议：

```bash
markbot doctor                 # 查看 provider、工具数、技能数、包状态
markbot skills                 # 查看技能索引
markbot packages               # 查看分发包与信任状态
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `markbot init [--force] [--home DIR]` | 初始化 `~/.markbot`：默认 `config.toml`、信任表与目录 |
| `markbot "<prompt>"` | 一次性执行请求 |
| `markbot chat` | 交互式会话；同一 session 保留多轮上下文，`/new` 开新会话 |
| `markbot selftest [--inject-failure <pkg>]` | 自检与故障注入隔离验证 |
| `markbot doctor [--validate] [--learned-paths]` | 诊断、manifest/能力校验、学习层越界检查 |
| `markbot skills` | 列出技能索引 |
| `markbot packages` | 列出分发包与信任状态 |
| `markbot trust <name> [--project] [--revoke]` | 信任 / 撤销信任一个包 |
| `markbot reload` | 重新合成运行时视图 |
| `markbot optimize [--dry-run] [--k N]` | 运行离线优化器 |
| `markbot rollback [--list] [--to HASH]` | 回滚学习层快照 |
| `markbot serve [--host H] [--port P]` | 启动 HTTP 入口 |
| `markbot gateway {start,stop,restart,status}` | 后台托管 HTTP 入口与飞书渠道，日志写入 `~/.markbot/gateway/YYYY-MM-DD.log` |
| `markbot cron {list,add,run,history}` | 管理定时任务；调度器随 `serve`/`gateway` 启动，聊天内使用需先 `markbot trust cron` |

> cron 表达式（`--cron "0 9 * * *"`）需要额外安装：`pip install -e ".[cron]"`；固定间隔与一次性任务不依赖 croniter。

## 配置

配置优先级：**项目 `.markbot/settings.toml` > 用户 `~/.markbot/config.toml` > 内置默认值**。

`~/.markbot/config.toml` 完整示例：

```toml
[agent]
provider = ""                  # 留空则按 model 推断
model = "claude-sonnet-4-5"
base_url = ""                  # 覆盖默认接口地址
api_key = ""                   # 模型服务密钥
max_tokens = 4096
max_steps = 30
tool_timeout_s = 60
parallel_tools = true
context_budget_chars = 60000   # 字符预算：超过 80% 触发压缩，压缩后保留最近约 40%

[permissions]
profile = "standard"           # strict | standard | trusted
confirm_on_write = true
confirm_on_exec = true
# 免逐次确认的命令：按程序名（allow_exec）或整条命令前缀（allow_commands，* 结尾）
# allow_exec = ["curl", "git"]
# allow_commands = ["curl -s http://127.0.0.1:11235/*"]

[packages]
allow_project_packages = false # 开启后才加载 <project>/.markbot/

# 供工具通过 secret:<name> 读取的密钥，不进入模型上下文
[secrets]
# EXAMPLE_KEY = ""

[ui]
show_progress = true           # 交互模式显示过程提示，false 则只看最终答案

[logging]
enabled = true                 # 总开关
runtime = true                 # logs/runtime/YYYY-MM-DD.md
interaction = true             # logs/interaction/YYYY-MM-DD.md（含完整模型报文）

[optimizer]
enabled = true
k = 1
max_rollouts_per_task = 4
max_sessions_per_task = 3
max_tasks_per_run = 5
idle_minutes = 30
```

- 唯一保留的环境变量是 `MARKBOT_HOME`（覆盖数据目录，默认 `~/.markbot`）；其余配置全部走 TOML。
- 密钥保存在 `config.toml` 的 `[agent] api_key` 与 `[secrets]`，请确保该文件仅本人可读。
- `config.toml` 语法错误会**直接报错退出**，避免带着错误配置运行。
- 其它所有加载失败（扩展、技能、包）只降级并记录警告。

### 项目级覆盖

```toml
# <project>/.markbot/settings.toml
[agent]
model = "claude-sonnet-4-5"

[permissions]
profile = "strict"
```

## 权限与信任

### 权限档位

| profile | 读文件 | 写文件 | 执行命令 | 网络 | 密钥 |
|---|---|---|---|---|---|
| `strict` | 允许能力内 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| `standard` | 允许能力内 | 需确认 | 需确认 | 允许能力内 | 允许 |
| `trusted` | 允许能力内 | 允许能力内 | 允许能力内 | 允许能力内 | 允许 |

`strict` 拒绝写/执行/网络/密钥；`standard` 对写与执行逐次确认；`trusted` 跳过逐次确认（`confirm_on_write` / `confirm_on_exec` 失效），但仍受能力清单与路径边界约束。路径越界一律拒绝；能力声明语法：

```text
fs.read:<glob>   fs.write:<glob>   net:<host|*>   exec:<program|*>   secret:<name>   ui
```

要免掉逐次确认，可以按程序名或整条命令前缀放行：

```toml
[permissions]
allow_exec = ["curl", "git"]                              # 这些程序免确认
allow_commands = ["curl -s http://127.0.0.1:11235/*"]     # 或只放行某条命令（* 结尾为前缀匹配）
```

在飞书会话里也可以临时授权：发送 `/allow curl` 让该会话免确认执行 curl，`/deny curl` 撤销，`/allow` 查看当前授权。没有放行时，机器人会在会话里询问，回复 `1` 允许、`2` 拒绝、`3` 本轮不再重复确认（3 分钟内有效；`3` 仅在当前这次问答内生效，回合结束即失效）。

飞书渠道除了文本，还会解析富文本、卡片和分享卡片；图片、文件、语音会下载到 `~/.markbot/media/feishu` 并把路径写进提示词。收到消息先加 `react_emoji` 表情，处理完成后移除并换成 `done_emoji`。回复按内容复杂度自动选择纯文本、富文本或 Markdown 卡片发送，卡片失败会回退纯文本。`group_policy` 控制群聊是否需要 @ 机器人，`topic_isolation` 让群内每个话题使用独立会话，`reply_to_message` 开启引用回复，`allow_from` 可限制发送者。

### 信任表

第三方包首次加载时 hash 不在信任表中，默认**拒绝加载**：

```bash
markbot trust office          # 信任用户目录包
markbot trust project --project
markbot trust office --revoke
```

包内容变化（hash 变化）会重新变为未信任，需要再次确认。`learned/` 由门禁自动信任，但会写入审计日志。

## 目录布局

```text
~/.markbot/
├── config.toml        # 用户配置
├── trust.json         # 信任表
├── packages/          # 用户安装的包
├── learned/           # 学习层（仅优化器可写）
├── snapshots/         # 学习层快照
├── sessions/          # 会话归档
├── gateway/           # gateway 后台进程状态与日志
├── optimizer/         # runs / rejected
├── logs/              # runtime/、interaction/ 按日期分文件 + optimizer.jsonl
└── cache/             # runtime_view.json

<project>/.markbot/
├── settings.toml      # 项目覆盖配置
├── extensions/        # 项目扩展（需项目信任）
├── skills/
└── trust.json
```

会话归档对齐 Harness² 的 task 结构：

```text
sessions/<session_id>/
├── meta.json          # 时间、领域、状态、步数、工具调用数
├── request.md         # 原始请求（已脱敏）
├── files/             # 输入附件快照
├── trajectory.jsonl   # 完整轨迹（已脱敏，压缩后为摘要锚点 + 近期原文）
├── compacted.jsonl    # 可选：被压缩掉的原始消息（审计用）
├── deliverable/       # 最终产出 final.md
└── feedback.json      # 可选：用户反馈
```

## 日志与排查

运行日志按自然日写入两个目录，当天所有会话追加到同一个 Markdown 文件：

```text
~/.markbot/logs/
├── runtime/
│   └── 2026-11-19.md      # 会话生命周期、工具调用、权限拒绝、压缩、错误与警告
├── interaction/
│   └── 2026-11-19.md      # 用户输入、发给模型的完整报文、模型回复、工具调用、返回给用户
└── optimizer.jsonl        # 优化器审计（仅 markbot optimize 写入）
```

交互日志包含完整请求报文，便于复现问题；其中密钥形状的字符串会被自动脱敏。不需要时可在 `config.toml` 关闭：

```toml
[logging]
enabled = true
runtime = true
interaction = false   # 只记录运行事件，不落盘模型报文
```

## 开发扩展

### A. 代码扩展

在 `extensions/` 下放一个 `.py` 或含 `__init__.py` 的目录，实现 `setup(api)`：

```python
from pydantic import BaseModel
from markbot.api import Tool, ToolResult


class GreetIn(BaseModel):
    name: str


def setup(api):
    async def greet(ctx, args: GreetIn):
        return ToolResult(content=f"hello {args.name}")

    api.register_tool(
        Tool(
            name="greet",
            description="向指定用户问好",
            params=GreetIn,
            handler=greet,
        )
    )

    # 事件钩子：tool_call 可返回 {"block": True, "reason": "..."} 拦截危险操作
    api.on("tool_call", lambda **kw: None)
```

注册先写入暂存区，`setup` 成功后一次性提交；失败只丢弃该扩展。

### B. 技能

```text
skills/<name>/SKILL.md
```

```markdown
---
name: weekly-report
description: 根据销售数据生成周报。
allowed-tools: [read_file, xlsx_read]
---

# 步骤
1. 读取源数据，确认字段。
2. 生成结构化汇总并落盘。
3. 回读校验后给出路径。
```

启动时只加载索引，正文由 `read_skill` 按需读取；技能脚本执行需要双重授权。

### C. 分发包

显式 manifest（`capabilities` 是包级上限，包内工具声明必须被覆盖）：

```toml
[package]
name = "office"
version = "0.1.0"
description = "Office 文档能力包"
priority = 100

[permissions]
capabilities = ["fs.read:./**", "fs.write:./**"]

[resources]
extensions = ["./extensions"]
skills = ["./skills"]
prompts = ["./prompts/*.md"]
```

也可以**不写 manifest**：只要目录含 `extensions/` 或 `skills/`，即按约定目录发现，并应用保守默认能力（`fs.read:./**`、`fs.write:./**`）。显式 manifest 优先。

### D. 学习层

优化器只能写 `~/.markbot/learned/` 与 `~/.markbot/strategy/` 相关目录。结构：

```text
learned/
├── strategy/
│   ├── system_prompt.md
│   ├── guardrails.md
│   ├── memory.md
│   └── tool_notes/
├── skills/
├── sub_agents/
└── manifest.json
```

## 优化与回滚

```bash
markbot optimize --dry-run     # 只评分与门禁，不晋升
markbot optimize               # 通过门禁后晋升到 learned/
markbot rollback --list        # 查看快照
markbot rollback --to <hash>   # 回滚到指定快照
markbot rollback               # 回滚到最新快照
```

门禁四条件（任一不满足即拒绝）：

1. 候选必须真的发生变化；
2. 只允许写 `learned/` 白名单路径；
3. 不得引入新的能力声明；
4. 回归集不得掉分。

候选被拒绝时保存在 `~/.markbot/optimizer/rejected/`，每轮审计写入 `~/.markbot/logs/optimizer.jsonl`。

## HTTP 入口

```bash
markbot serve --host 127.0.0.1 --port 8787
```

| 接口 | 说明 |
|---|---|
| `GET /health` | 健康检查，返回工具数/技能数 |
| `POST /chat` | `{"prompt": "...", "session_id": "可选"}` → `{"text", "stopped_reason", "error", "session_id"}`；带同一 `session_id` 即延续上下文；服务重启后会从 `sessions/<id>/` 归档恢复 |

会话分两类：

- **渠道会话**（飞书等，显式传 `session_id`）：上下文落盘到 `sessions/<id>/`，进程重启后仍能接上之前的对话。
- **本地会话**（CLI 等，不传 `session_id`）：只在当前进程内保留多轮上下文，重启即结束。

同一 Kernel 可同时服务 CLI 与 HTTP 入口。

## 测试与质量门禁

```bash
pytest                       # 单元测试
ruff check src tests         # 代码检查
markbot selftest             # 依赖方向 + 运行时视图 + Agent Loop
markbot selftest --inject-failure office
markbot doctor --validate
markbot doctor --learned-paths
```

强制依赖方向：

```text
schema <- api <- kernel
extensions 只依赖 api
optimizer 只依赖 schema
cli 依赖 kernel + optimizer
```

删除 `capabilities/` 下所有内容后，`markbot selftest` 仍应通过。

## 安全说明

- 密钥通过 `ctx.secret(name)` 在工具边界使用，不进入模型上下文；归档前对密钥形状字符串做脱敏。
- 写文件、执行命令遵循确认策略；命令在受限环境变量与工作目录中执行，并带超时强杀。
- 第三方扩展与包必须通过能力清单与信任校验；加载或执行失败只影响自身。
- 第一版不追求强隔离（容器化是可选增强），不可信代码建议放进容器或低权限用户运行。
