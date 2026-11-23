# MarkBot2

MarkBot2 是一个 **1 + N** 形态的个人助手 Agent：

- **1 个极薄 Kernel**：只负责上下文/模型调用、工具执行、会话持久化、生命周期事件、权限策略、运行时视图合成与配置信任。
- **N 条扩展车道**：代码扩展、技能、分发包、学习层。增删能力不改 Kernel。
- **可演化**：离线优化器读取已完成会话，产出更好的策略/技能，经回归门禁审核后晋升到学习层。
- **有边界**：能力清单 + 三档权限 + 信任表 + 子进程隔离（可选 macOS `sandbox-exec`）；所有演化可审计、可回滚。

完整设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

---

## 环境要求

- Python **3.12+**
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
pip install -e ".[mcp]"          # 需要接入 MCP server 时
```

安装后可用的入口：

```bash
markbot --help
python -m markbot --help          # 等价
```

## 配置模型服务

普通配置在 `~/.markbot/config.toml`，**密钥单独放在 `~/.markbot/secrets.toml`（0600）**，
两者结构相同、加载时叠加（先运行 `markbot init`）：

```toml
# ~/.markbot/config.toml
[agent]
provider = "openai_compat"     # openai_compat | anthropic | echo
model = "sensenova-6.8-flash"
base_url = "http://127.0.0.1:3000/v1"   # 你的 OpenAI 兼容网关
```

```toml
# ~/.markbot/secrets.toml（chmod 600，不要提交/同步）
[agent]
api_key = "sk-..."             # 也可以写 "env:OPENAI_API_KEY" 从环境变量读取

# 供技能/扩展通过 secret:<name> 读取的密钥，不进模型上下文
[secrets]
EXAMPLE_KEY = "..."
FEISHU_APP_SECRET = "..."
```

已有部署把明文密钥写在 `config.toml` 时，启动会告警；运行 `markbot secrets migrate`
无损迁移（`--dry-run` 先预览），迁移后请到对应平台**轮换**这些密钥。

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
| `markbot optimize [--dry-run] [--k N] [--approve-skills]` | 运行离线优化器（技能改动需 `--approve-skills`） |
| `markbot rollback [--list] [--to HASH]` | 回滚学习层快照 |
| `markbot serve [--host H] [--port P] [--allow-remote]` | 启动 HTTP 入口（实现位于 `markbot/server/`） |
| `markbot gateway {start,stop,restart,status}` | 后台托管 HTTP 入口与飞书渠道，日志写入 `~/.markbot/gateway/YYYY-MM-DD.log` |
| `markbot sessions {status,migrate [--dry-run]}` | 会话归档存储统计；清理可从 `events.jsonl` 重建的旧派生文件 |
| `markbot secrets {status,migrate [--dry-run]}` | 检查 / 迁移 `config.toml` 里的明文密钥到 `secrets.toml` |
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
max_concurrent_turns = 4       # serve/gateway 同时执行的回合数；同一会话始终串行
context_budget_chars = 60000   # 字符预算：超过 80% 触发压缩，压缩后保留最近约 40%

[permissions]
profile = "standard"           # strict | standard | trusted
confirm_on_write = true
confirm_on_exec = true
# 免逐次确认的命令：按程序名（allow_exec）或整条命令前缀（allow_commands，* 结尾）
# allow_exec = ["curl", "git"]
# allow_commands = ["curl -s http://127.0.0.1:11235/*"]

# 允许以环境变量注入命令的密钥 -> 允许使用它的程序（["*"] 不限）。
# 值写在 [secrets]；模型调用 run_command 时只写 secrets=["NAME"]，命令里用 "$NAME" 引用，
# 执行时才注入，模型、会话归档与日志里都只出现名称。
# [permissions.secret_env]
# CRAWL4AI_API_TOKEN = ["curl"]

# MarkBot home（~/.markbot）整体受保护：文件工具与命令都不能读写学习层、配置、信任表、
# 日志与会话归档（与档位无关）。默认开放 ~/.markbot/workspace（读写）与 ~/.markbot/media
# （只读，渠道收到的图片/文件）；需要更多目录时在 [permissions] 下追加：
# extra_writable_dirs = ["~/scratch"]
# extra_readable_dirs = ["~/Downloads"]
# extra_protected_dirs = ["~/.markbot_old"]   # 旧版数据目录等，同样禁止工具访问

[packages]
allow_project_packages = false # 开启后才加载 <project>/.markbot/

# MCP server（需要安装可选依赖：pip install -e ".[mcp]"）
# [tools.mcp_servers.filesystem]
# type = "stdio"               # stdio | sse | streamableHttp；留空按 command/url 推断
# command = "npx"
# args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
# enabled_tools = ["*"]        # ["*"] = 全部能力；指定名单时只注册这些工具，不注册资源/提示
# tool_timeout = 30
#
# [tools.mcp_servers.remote]
# url = "https://example.com/mcp"
# headers = { Authorization = "Bearer ${MCP_TOKEN}" }  # ${VAR} 从进程环境变量展开

# 供工具通过 secret:<name> 读取的密钥，不进入模型上下文
[secrets]
# EXAMPLE_KEY = ""

[ui]
show_progress = true           # 交互模式显示过程提示，false 则只看最终答案

[logging]
enabled = true                 # 总开关
runtime = true                 # logs/runtime/YYYY-MM-DD.md
interaction = true             # logs/interaction/YYYY-MM-DD.md（含完整模型报文；init 模板默认 false）
retention_days = 30            # runtime / gateway 按日文件保留天数
interaction_retention_days = 7 # 交互日志保留天数
max_total_mb = 500             # logs/ + gateway/ 总量上限，超出从最旧的交互日志开始删

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

## MCP 工具

内置扩展 `src/markbot/extensions/mcp/` 会把 MCP server 的能力映射成普通工具，模型看到的名字是
`mcp_<server>_<tool>`。需要先安装可选依赖：

```bash
pip install -e ".[mcp]"
```

```toml
[tools.mcp_servers.filesystem]          # stdio：本地进程
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"]
enabled_tools = ["*"]                   # ["*"] = 工具 + 资源 + 提示；指定名单时只注册这些工具
tool_timeout = 30                       # 单次调用超时（秒）
connect_timeout = 20

[tools.mcp_servers.remote]              # 远端：type 留空时按 URL 推断 sse / streamableHttp
url = "https://example.com/mcp"
headers = { Authorization = "Bearer ${MCP_TOKEN}" }   # ${VAR} 从进程环境变量展开
```

行为约定：

- `type` 支持 `stdio` / `sse` / `streamableHttp`，留空按 `command` / `url` 推断。
- 工具名只保留 `[A-Za-z0-9_-]`，超过 64 字符追加哈希后缀；重名时先注册者生效并记录冲突。
- 参数契约使用服务端下发的 JSON Schema，客户端只做 provider 兼容化（本地 `$ref` 提升、`nullable` 归一）。
- 声明了 `readOnlyHint` / `idempotentHint` 的工具在瞬态错误后最多重试一次；有副作用的调用
  发出后若连接中断或超时，返回「结果未知」且**不会重放**，避免重复副作用。
- 会话中断会按 server 粒度自动重连；单个 server 连不上只记 warning 并跳过，不影响其它能力。
- MCP 连接由扩展私有的事件循环线程持有，跨回合复用，并在 `markbot` 退出时关闭。
- 服务端 URL 写入日志前会去掉凭据、路径细节与查询串。
- MCP 返回的图片/二进制资源只给出占位说明，不会把 base64 塞进模型上下文。

## 权限与信任

### 权限档位

| profile | 读文件 | 写文件 | 执行命令 | 网络 | 密钥 |
|---|---|---|---|---|---|
| `strict` | 允许能力内 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| `standard` | 允许能力内 | 需确认 | 需确认 | 允许能力内 | 允许 |
| `trusted` | 允许能力内 | 允许能力内 | 允许能力内 | 允许能力内 | 允许 |

`strict` 拒绝写/执行/网络/密钥；`standard` 对写与执行逐次确认；`trusted` 跳过写入确认，执行命令时默认只有放行名单内的命令免确认（`trusted_exec = "listed"`；设为 `"all"` 恢复全部免确认），仍受能力清单与路径边界约束。

无论档位与名单如何，以下命令**总是**需要确认：内联解释器（`python -c`、`node -e`…）、
shell 与提权（`sh`/`bash`/`eval`/`sudo`…）、命令替换（`$()`、反引号）、解码管道（`base64 -d`…）。
放行名单会检查命令中的**每一个**程序，`allow_commands` 只匹配不含 `; | & > <` 的单条命令。

命令扫描只能识别字面路径。需要内核级保证时开启（仅 macOS）：

```toml
[permissions]
os_sandbox = "macos"   # 命令经 sandbox-exec 执行，受保护目录（home、学习层、配置）禁止读写
```路径越界一律拒绝；能力声明语法：

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

会话归档的 task 结构：

```text
sessions/<session_id>/
├── meta.json          # 时间、领域、状态、步数、工具调用数
├── request.md         # 原始请求（已脱敏）
├── files/             # 输入附件快照
├── events.jsonl       # 唯一事实来源：append-only 事件日志（已脱敏，含压缩移除的原文）
├── deliverable/       # 最终产出 final.md
└── feedback.json      # 可选：用户反馈
```

「轨迹」（当前上下文投影）与「压缩段」（每次压缩移除的原文）都从 `events.jsonl` 派生，
代码里统一用 `markbot.schema.session_log.SessionLog(dir).trajectory()` / `.compacted()` 读取。
旧版本额外写的 `trajectory.jsonl` / `compacted.jsonl` 默认不再生成（ADR 0007）；
外部脚本还要读它们时，在 `config.toml` 里打开：

```toml
[sessions]
legacy_views = true
```

升级后运行 `markbot sessions status` 查看占用，`markbot sessions migrate --dry-run` 预览清理。
迁移会逐条比对事件投影与旧文件，一致才删除；老归档里只存在于 `compacted.jsonl` 的压缩原文会保留。

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

声明了 `fs.*` / `net:<host>` 能力的工具，要标注哪个参数是路径/URL，权限检查才不会落空
（未标注且参数名不在兜底名单里时，加载会给出警告）：

```python
class CopyIn(BaseModel):
    source: str = Field(json_schema_extra={"x-markbot-role": "path"})
    endpoint: str = Field(json_schema_extra={"x-markbot-role": "url"})

# 或者：Tool(..., arg_roles={"source": "path", "endpoint": "url"})
```

角色有 `path` / `url` / `command` / `program`；发给模型的 schema 会去掉这个标注。

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

`learned/` 的写入方：优化器（晋升/回滚）、记忆整理（Dream）、记忆工具（remember/forget）、
`markbot memory restore`。它们共用一把跨进程学习层写锁（`<home>/.learned.lock`）；优化器晋升时
若 `learned/` 在评估期间被写过，会放弃本次晋升而不是覆盖新记忆。快照按内容去重。结构：

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

门禁条件（任一不满足即拒绝）：

1. 回归集至少 `min_eval_cases` 条（默认 10，`evals/cases/` 现有 14 条）；
2. 候选必须真的发生变化；
3. 只允许写 `learned/` 白名单路径；
4. 不得引入新的能力声明；
5. 回归集不得掉分（`contains` / `not_contains` / `regex`；`[optimizer] llm_judge = true` 时
   `llm_judge` 检查由当前模型按 rubric 判 PASS/FAIL）；
6. 新增或修改技能（`skills/**`）的候选不自动晋升，保存到 `~/.markbot/optimizer/pending/`，
   确认后用 `markbot optimize --approve-skills`；
7. 评估期间 `learned/` 被其它写入方修改过则放弃晋升（ADR 0005）。

候选里不会写入会话原文（用户请求只用于生成候选，不进入系统提示或技能正文）。

候选被拒绝时保存在 `~/.markbot/optimizer/rejected/`，每轮审计写入 `~/.markbot/logs/optimizer.jsonl`。

## HTTP 入口

```bash
markbot serve --host 127.0.0.1 --port 8787
```

所有 `POST` 必须带 `X-MarkBot-Token` 头，令牌在 `~/.markbot/gateway/auth.token`（0600，首次启动生成，
渠道与定时任务自动读取）。`confirm_hook` / `notify_hook` 只接受回环地址；`grants` 只接受程序名。
监听非回环地址需要显式加 `--allow-remote`（请放在反向代理/防火墙之后）。

```bash
curl -s -H "X-MarkBot-Token: $(cat ~/.markbot/gateway/auth.token)" \
  -d '{"prompt":"你好"}' http://127.0.0.1:8787/chat
```

| 接口 | 说明 |
|---|---|
| `GET /health` | 健康检查，返回工具数/技能数 |
| `POST /chat` | `{"prompt": "...", "session_id": "可选"}` → `{"text", "stopped_reason", "error", "session_id"}`；带同一 `session_id` 即延续上下文；服务重启后会从 `sessions/<id>/` 归档恢复 |

会话分两类：

- **渠道会话**（飞书等，显式传 `session_id`）：上下文落盘到 `sessions/<id>/`，进程重启后仍能接上之前的对话。
- **本地会话**（CLI 等，不传 `session_id`）：只在当前进程内保留多轮上下文，重启即结束。

同一 Kernel 可同时服务 CLI 与 HTTP 入口。

并发：不同会话的请求并发执行（上限 `[agent] max_concurrent_turns`），同一会话串行；
运行时重载、记忆整理、空闲归档以独占方式执行（等在途回合结束）。确认 UI 与 `/allow`
授权只对本回合生效，并发会话之间互不影响（ADR 0005）。`GET /health` 返回 `turns` 统计。

## 测试与质量门禁

```bash
pytest                       # 单元测试
ruff check src tests         # 代码检查（阻断）
bash scripts/lint_strict.sh  # 更严格的 ruff B/UP/SIM/RUF + pyright（棘轮：只许减少）
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
- 命令执行是「进程隔离 + 路径约定 + 静态扫描」，不是完整沙箱；macOS 可开 `os_sandbox = "macos"`
  获得受保护目录的内核级隔离。不可信代码仍建议放进容器或低权限用户运行。
- 内部 HTTP（serve ↔ 渠道 ↔ 定时任务）用本机令牌鉴权，回调地址限制为回环地址（ADR 0004）。
