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
- 可选：联网模型服务（Anthropic / OpenAI 兼容接口）
- 可选：Node.js 20+（Playwright MCP）；`playwright install chromium`（内置浏览器）
- 其余能力按需安装可选依赖，见下方「安装」。

## 安装

```bash
# 在仓库根目录
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -e ".[dev]"          # 开发 + 测试
pip install -e ".[office]"       # 需要 xlsx 能力时
pip install -e ".[mcp]"          # 需要接入 MCP server 时
pip install -e ".[channels]"     # 飞书渠道（lark-oapi）
pip install -e ".[cron]"         # cron 表达式（croniter）
pip install -e ".[browser]"      # 内置 Playwright 浏览器
pip install -e ".[pdf]"          # fetch_url 解析 PDF
pip install -e ".[desktop]"      # 桌面控制（mss / pillow / pyautogui）
pip install -e ".[sysops]"       # 更完整的系统与进程信息（psutil）
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

已有部署把明文密钥写在 `config.toml` 时，`chat` / 一次性执行 / `serve` / `gateway start`
会**拒绝启动**；运行 `markbot secrets migrate` 无损迁移（`--dry-run` 先预览），
迁移后请到对应平台**轮换**这些密钥。临时放行见下文「运行环境、网络与启动检查」。

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
| `markbot optimize [--dry-run] [--k N] [--approve-skills] [--gate-suites coding,browser]` | 运行离线优化器（技能改动需 `--approve-skills`） |
| `markbot rollback [--list] [--to HASH]` | 回滚学习层快照 |
| `markbot memory {status,search,consolidate,restore}` | 查看、检索、整理与回滚长期记忆（`restore --list` / `--to NAME`） |
| `markbot project {add <名称> <路径>,list,remove <名称>}` | 登记 / 查看 / 删除可绑定的项目 |
| `markbot eval {--suite coding\|browser,--trend}` | 运行编码 / 浏览器评测，或查看评测趋势 |
| `markbot stats [--days N] [--json]` | 按天统计回合、失败、工具失败率、token 与定时任务 |
| `markbot service {install,uninstall,status} [--dry-run]` | 开机自启与守护（systemd 用户服务 / Windows 计划任务） |
| `markbot serve [--host H] [--port P] [--allow-remote]` | 启动 HTTP 入口（实现位于 `markbot/server/`） |
| `markbot gateway {start,run,stop,restart,status}` | 后台托管 HTTP 入口与飞书渠道，日志写入 `~/.markbot/gateway/YYYY-MM-DD.log`；`run` 为前台守护模式 |
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
context_budget_ratio = 0.8     # 用量达到 max_tokens 的 80% 时触发上下文压缩

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

# 供工具通过 secret:<name> 读取的密钥，不进入模型上下文；
# 值请写在 secrets.toml（0600），config.toml 里只写 env:NAME 引用
[secrets]
# EXAMPLE_KEY = "env:EXAMPLE_KEY"

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

- 配置读取：`MARKBOT_HOME` 覆盖数据目录（默认 `~/.markbot`）；业务配置全部走 TOML。
- 密钥放 `~/.markbot/secrets.toml`（0600）或写成 `env:NAME` 引用，**不要**写进 `config.toml`
  （`chat` / `serve` / `gateway` 启动时会拒绝）；其他环境变量见「运行环境、网络与启动检查」。
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
- `capabilities`（可选）给该 server 的全部工具声明能力，走统一权限策略（例如 `["net:*", "ui"]`
  在 strict 档位下会被拒绝）；`confirm_tools`（可选）列出需要逐次确认的工具名（支持 `*` 通配），
  无人值守的定时任务里这些工具一律被拒绝。

### 浏览器（Playwright MCP，快速接入）

内置的 `fetch_url` 不执行 JavaScript，遇到登录、JS 渲染、反爬页面会失败（结果里带 `blocked=true`
提示）。需要真正的浏览器时，先通过 MCP 接入微软的 [Playwright MCP](https://github.com/microsoft/playwright-mcp)
（需要 Node.js 20+；首次运行 `npx` 会下载浏览器）：

```toml
[tools.mcp_servers.playwright]
command = "npx"
args = [
  "-y", "@playwright/mcp@latest",
  "--user-data-dir", "~/.markbot/browser-profile",   # 持久化登录态；不要指向你日常用的浏览器配置
  # "--headless",                                    # 无界面运行（服务器上建议打开）
  # "--isolated",                                    # 配置只放内存，不保留登录态
  # "--proxy-server", "http://192.168.71.90:7891",   # 需要代理时
]
tool_timeout = 60                 # 页面加载与等待可能较慢
connect_timeout = 60              # 首次启动要下载/拉起浏览器
capabilities = ["net:*", "ui"]    # strict 档位下整体禁用
# 有副作用的动作逐次确认（点击、输入、提交表单、上传文件、执行脚本）
confirm_tools = [
  "browser_click", "browser_type", "browser_fill_form", "browser_select_option",
  "browser_press_key", "browser_drag", "browser_file_upload", "browser_handle_dialog",
  "browser_evaluate", "browser_run_code",
]
```

工具在模型侧显示为 `mcp_playwright_browser_navigate`、`mcp_playwright_browser_snapshot` 等。

安全注意：

- 浏览器能访问的范围不受 `[tools.web] allow_private` 约束（它是独立进程）。Playwright MCP 的
  `--allowed-origins` / `--blocked-origins` 可以收窄范围，但官方说明它们**不是安全边界**（不影响重定向）；
  对内网有顾虑时请在网络层隔离。
- 同一个 `--user-data-dir` 同时只能被一个浏览器实例使用；并发会冲突。
- `confirm_tools` 按工具名匹配，Playwright MCP 版本升级后工具名可能变化，连接后可在
  `~/.markbot/cache/runtime_view.json` 的 `sources` 里核对实际注册的工具名。
- `--user-data-dir` 里会保存登录 cookie，权限等同于你的账号；请放在 MarkBot home 内并只给本人读写。
- 网页内容可能包含提示注入，`confirm_tools` 里的动作执行前请核对确认消息里的参数。
- 定时任务里没有确认对象，`confirm_tools` 中的工具会被直接拒绝；只读动作（打开、快照、截图）仍可用。

## 编码工具（ADR 0012）

| 工具 | 作用 |
|---|---|
| `edit_file` | 把唯一出现的 `old_string` 替换为 `new_string`，保留换行风格，返回 diff；`old_string` 为空表示新建 |
| `apply_patch` | 应用 unified diff，可多文件/新建/删除；全部对上才落盘。`paths` 必须列出涉及的文件 |
| `grep_search` | 搜索代码（有 ripgrep 用 rg，否则 Python 兜底），返回 `路径:行号:内容` |
| `file_outline` | 列出类、函数、方法与行号 |
| `process_start` / `process_read` / `process_write` / `process_kill` / `process_list` | 后台运行 dev server、watch、长构建，增量读取输出 |
| `run_tests` | 自动识别 pytest / npm test / go test / cargo test，返回通过/失败数与失败用例 |
| `run_checks` | 运行 ruff / mypy / pyright / eslint / tsc 等检查 |
| `todo_write` / `todo_read` | 多步骤任务的计划清单（每轮注入上下文，聊天里 `/status` 可见） |

```toml
[dev]
verify_reminder = true            # 改了代码却没跑测试/检查时，结束前提醒模型验证一次
test_command = ""                 # run_tests 默认命令；留空自动识别
checks = ["ruff check src tests"] # run_checks 默认命令；留空自动识别
```

- 文件在本会话读取后被外部修改（例如你在编辑器里改了），`edit_file` / `apply_patch` / `write_file` 会拒绝并要求重新读取。
- 写文件都是原子的；每个会话有可读写的临时目录 `~/.markbot/sessions/<id>/scratch/`。
- `run_tests` / `run_checks` 的命令按 `run_command` 的规则检查权限：要免确认，把 `python`、`npm`、`ruff` 等加进
  `[permissions].allow_exec`。

### 项目、子代理、Git 与交付（ADR 0013）

```bash
markbot project add markbot2 D:/Source/markbot2   # 登记项目（只能在已登记的项目之间切换）
markbot project list
markbot project remove markbot2
markbot --project markbot2 "跑一下测试并修复失败的用例"
markbot eval --suite coding                       # 在 evals/coding/ 的小仓库上评测编码能力
```

- 飞书里 `/project` 查看、`/project <名称>` 切换、`/project off` 回到默认工作区。绑定项目后文件读写与命令都在项目目录里执行，
  并自动带上项目的 AGENTS.md / CLAUDE.md 与 README 开头。
- `spawn_agent`：把独立子任务交给子代理（explorer / reviewer / tester / researcher / general，或 `sub_agents/*.md`
  自定义角色），只返回结果摘要；一次回复里的多个调用并行执行。
- Git：`git_status`、`git_diff`、`git_log`、`git_commit`（默认只提交列出的文件，拒绝密钥文件）、`git_branch`、`git_worktree`。
  把 `git` 加入 `[permissions].allow_exec` 后 standard 档位也免确认；`run_command` 里的 force push、`reset --hard`、
  `clean -f`、`rebase` 等危险操作总是需要确认。
- `write_report`：生成带改动文件与测试结果的交付报告（Markdown），飞书会把文件一起发出。

```toml
[agent]
prompt_cache = "auto"           # Anthropic 提示缓存：auto 仅官方接口开启

[subagents]
max_steps = 20
max_parallel = 3
result_max_chars = 8000

[projects]                      # 也可以在这里登记项目
# markbot2 = "D:/Source/markbot2"
```

## 常驻运行与可靠性（ADR 0010）

### 开机自启与守护

```bash
markbot service install --dry-run   # 预览将写入的文件与执行的命令
markbot service install             # Linux：systemd 用户服务；Windows：计划任务「MarkBot」
markbot service status
markbot service uninstall
```

- 服务运行的是 `markbot gateway run`（前台守护模式）：serve 与飞书渠道退出后自动拉起（指数退避）。
- Linux 下未登录也要运行：`loginctl enable-linger $USER`；日志：`journalctl --user -u markbot -f`
  或 `~/.markbot/gateway/`。由 systemd 管理时请用 `systemctl --user stop|restart markbot`。
- serve 内的后台组件（定时任务、记忆整理、空闲归档、热重载、日志清理）崩溃后自动重启，并向最近一次
  对话的聊天发告警；`GET /health` 返回 `status`（ok / degraded / down）与各组件状态。
- `SIGTERM` 时优雅停机：等在途回合最多 60 秒，超时取消（中断的工具会补齐结果并归档）。

### 长任务：异步执行、停止与进度

飞书渠道默认用异步任务（`[channels.feishu] async_tasks = true`）：提交后流式显示进度；等待超过
`markbot_timeout_s` 后转为后台执行，完成后把结果发回聊天。聊天里可用：

| 命令 | 作用 |
|---|---|
| `/status`（或「状态」）、`/tasks` | 当前步数、正在调用的工具、已用时长、最近任务 |
| `/stop`（或「停止」） | 停止当前任务，已完成的部分保留在会话里 |
| `/retry`（或「重试」） | 重新执行上一条请求 |

服务重启时被中断的任务会通知原聊天。HTTP 接口（都需 `X-MarkBot-Token`）：

| 接口 | 说明 |
|---|---|
| `POST /tasks` | 请求体同 `/chat`，立即返回 `202 {task_id, session_id, state}` |
| `GET /tasks/<id>/events?after=N&wait=25` | 长轮询事件（`status`/`delta`/`tool`/`done`），用返回的 `next` 续读 |
| `GET /tasks/<id>`、`GET /tasks?session_id=&active=1` | 任务状态 |
| `POST /tasks/<id>/cancel`、`POST /tasks/<id>/detach` | 取消；不再等待（结束后主动推送） |
| `POST /sessions/<id>/cancel`、`GET /sessions/<id>/status` | 按会话停止 / 查询进度 |

### 定时任务

```toml
[cron]
max_concurrent = 2          # 同时执行的任务数
job_timeout_s = 1800        # 单次超时，超时取消；0 不限
max_retries = 1             # 失败重试次数
retry_backoff_s = 300
misfire_policy = "run_once" # 停机期间错过的触发：run_once（宽限期内补跑一次）| skip
misfire_grace_s = 21600
alert_after_failures = 2    # 连续失败几次通知聊天；0 关闭
```

注意：崩溃恢复是「至少一次」语义，执行到一半崩溃的任务重启后会再跑一次，有副作用的任务请写成幂等。

### 备用模型

```toml
[[agent.fallbacks]]
provider = "openai_compat"
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key = "env:DEEPSEEK_API_KEY"      # 或 api_key_secret = "DEEPSEEK_API_KEY"（[secrets] 里的名字）

[agent]
circuit_failures = 3        # 连续失败几次熔断该模型
circuit_cooldown_s = 300
```

主模型重试耗尽仍失败时按顺序切换；流式已经输出了内容时不会换模型重放。备用模型的密钥不要以明文写在
`config.toml`（会被启动检查拦截），`secrets.toml` 里的 `[[agent.fallbacks]]` 会整体覆盖 config.toml 的列表，
建议用 `env:` 或 `api_key_secret`。

### 消息不丢（ADR 0011）

飞书渠道的收件箱/发件箱存在 `~/.markbot/channels/feishu/feishu.db`：收到的消息先落库再处理，渠道重启后
没处理完的消息会重新处理（已提交为后台任务的不会重复提交，结果由服务端推送）；主动通知（定时任务结果、
后台任务结果、告警）发送失败会退避重试，最多 8 次。

### 用量、统计与监控（ADR 0011）

```bash
markbot stats --days 7          # 按天：回合、失败、工具失败率、token、定时任务
markbot stats --days 1 --json
curl -s http://127.0.0.1:8787/metrics    # Prometheus 文本格式（只有计数，不需要令牌）
```

- 结构化事件写在 `~/.markbot/logs/events/YYYY-MM-DD.jsonl`（回合 / 工具 / 模型调用的耗时、结果、token），
  不含对话正文，可以用 `jq` / `duckdb` 分析；保留期同 `[logging] retention_days`。
- 每日 token 上限：`[agent] daily_token_budget = 2000000`（0 不限），超过后拒绝新回合直到次日。

### 模型分工与内容审核（ADR 0011）

```toml
[agent.roles.fast]          # 上下文压缩与记忆整理用更便宜的模型
model = "<同一网关上更便宜的模型名>"   # 只写 model 时沿用主模型的网关与密钥
# provider / base_url / api_key = "env:NAME" / api_key_secret（[secrets] 里的名字） 也可以单独指定

[agent]
moderation_markers = ["risk control"]   # 追加网关特有的审核拒绝文案
moderation_new_session = true           # 审核拒绝且无法恢复时自动换到带摘要的新会话
```

### 定时任务的权限提示

创建定时任务时会检查无人值守执行可能被拒绝的操作（命令不在 `allow_exec`、写文件需要确认、用到的密钥没有在
`[permissions.secret_env]` 登记），并在回复里提示；运行中真的有操作被拒时，会向任务的聊天发一次提示
（原因不变不重复提示）。

试运行检查清单见 [docs/runbook-7day-trial.md](docs/runbook-7day-trial.md)。

## 运行环境、网络与启动检查（ADR 0009）

```toml
[shell]
program = "auto"          # auto | bash | sh | pwsh | powershell | cmd | <可执行文件路径>
                          # auto：Linux/macOS 用 bash（没有则 /bin/sh），Windows 用 pwsh（没有则 powershell，再 cmd）
passthrough_env = []      # 额外透传给子进程的环境变量名；代理变量（HTTP(S)_PROXY/NO_PROXY）默认透传

[tools.web]
# fetch_url 默认禁止访问私网/回环/链路本地等非公网地址（防 SSRF），每一跳重定向都会检查。
# 确需访问的内网服务在这里登记：主机、主机:端口，或 CIDR
allow_private = ["192.168.71.90:11235"]
proxy = ""                # 例如 "http://192.168.71.90:7891"；留空时读取环境变量 HTTPS_PROXY 等
user_agent = ""           # 留空使用内置的浏览器 UA

[security]
allow_plaintext_secrets = false   # config.toml 含明文密钥时拒绝启动 chat/serve/gateway

[packages]
trust_repo_from_git = true        # 仓库内置包已提交且无未提交改动时自动信任（开发模式）
```

- 每轮请求的系统提示里会注入 `<env>` 块：当前时间与时区、操作系统、命令 shell、工作目录，
  模型不需要再调用工具查时间，也会按正确的 shell 语法写命令。
- `fetch_url` 省略协议头时先试 `https://`，失败再退回 `http://`；二进制内容（PDF、图片等）
  不再按文本解码；`verify_tls = false` 只对 `allow_private` 里的主机生效。
- 明文密钥：`config.toml` 里有明文 `api_key` / `[secrets]` 时，`markbot chat`、一次性执行、
  `markbot serve` 与 `markbot gateway start` 会拒绝启动。先 `markbot secrets migrate --dry-run`
  预览，再 `markbot secrets migrate`，然后到对应平台轮换密钥。临时放行：命令行加
  `--allow-plaintext-secrets`，或环境变量 `MARKBOT_ALLOW_PLAINTEXT_SECRETS=1`。
- 记忆索引写满时不再拒绝写入：先缩短最旧条目的摘要，仍超出再把最旧的非永久页面移出索引
  （页面正文保留，`memory_search` 仍能检索）。内容相同、或新旧一方完整包含另一方的事实不会重复追加。
- 能力包因未信任被跳过时，第一次对话的回复末尾会提示一次如何启用；`markbot doctor` 会单独列出被跳过的包。

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
├── secrets.toml       # 密钥（0600）
├── trust.json         # 信任表
├── workspace/         # 默认可读写工作区
├── media/             # 渠道收到的图片/文件（工具只读）
├── channels/          # 渠道收件箱/发件箱（如 feishu/feishu.db）
├── packages/          # 用户安装的包
├── learned/           # 学习层（仅写入方持锁修改）
├── snapshots/         # 学习层快照
├── sessions/          # 会话归档
├── cron/              # 定时任务定义
├── memory/            # 记忆整理游标等运行状态
├── gateway/           # gateway 后台进程状态与日志
├── optimizer/         # runs / rejected
├── logs/              # runtime/、interaction/、events/ 按日期分文件 + optimizer.jsonl
└── cache/             # runtime_view.json

<project>/.markbot/
├── settings.toml      # 项目覆盖配置
├── extensions/        # 项目扩展（需项目信任）
├── skills/
└── trust.json
```

会话归档的目录结构：

```text
sessions/<session_id>/
├── meta.json          # 时间、领域、状态、步数、工具调用数
├── request.md         # 原始请求（已脱敏）
├── files/             # 输入附件与超限工具结果
├── scratch/           # 会话可读写的临时目录（按需创建）
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
schema <- api <- kernel <- server / cron <- cli
extensions 只依赖 api（以及库层 memory）
memory 依赖 api + schema，不被 kernel 依赖
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

## 附录：Phase 3 第一批 —— 搜索与浏览器（ADR 0014）

- `web_search`：SearXNG / Tavily / Brave / Bing API / Bing 网页 / DuckDuckGo，`[tools.search] backend = "auto"` 自动回退。
- `fetch_url` 默认输出正文 Markdown（保留标题、列表、绝对链接），支持 PDF 文本（`.[pdf]`），自动重试与缓存。
- 内置浏览器（`.[browser]` + `playwright install chromium`，或 `[tools.browser] channel = "msedge"`）：
  `browser_open/snapshot/click/type/...`，按元素编号操作，持久登录态，导航地址检查，
  提交/付款/删除等操作需确认，`browser_handoff` 请用户接手验证码。执行脚本默认不确认，
  需要时设 `[tools.browser] confirm_script = true`（`confirm = "all"` 时仍会确认）。
- 新能力类型 `browser:<域名|*>`、`desktop:<应用|*>`；strict 档位禁止。
- 详见 `docs/phase3-batch1-implementation-2026-11-23.md`。

## 附录：Phase 3 第二批 —— 看图、桌面控制、运维（ADR 0015）

- 多模态：会话支持图片（飞书发来的图片、浏览器/桌面截图自动附给模型）；`[agent] vision = auto|true|false`，
  `vision_max_images = 3` 控制每次请求最多附带的图片数；主模型不能看图时 `view_image` 交给 `[agent.roles.vision]` 描述。
- 桌面控制（默认关闭，`[tools.desktop] enabled = true`，`.[desktop]`）：截图 → 坐标/控件编号点击、输入、按键，动作后自动截图；
  每会话首个动作需确认，密码管理器等应用默认禁止操作。
- 运维工具：`system_info`、`list_processes`、`disk_usage`、`check_port`、`service_status`、`kill_process`（默认无需确认，拒绝结束自身/父进程/系统关键进程，strict 档位禁止）；
  安装 `.[sysops]`（psutil）后信息更完整。
- 浏览器评测：`markbot eval --suite browser`（`evals/browser/` 5 个用例）。
- 详见 `docs/phase3-batch2-implementation-2026-11-23.md`。

## 附录：Phase 4 —— CI 与评测门禁（ADR 0016）

- CI：`.github/workflows/ci.yml`，Linux + Windows 双平台跑 ruff、全量测试、shell 冒烟测试（bash / pwsh）与 selftest；严格 lint 只报告。
- 夜间评测：`.github/workflows/eval.yml`，编码 + 浏览器评测用真实模型跑，结果写入趋势文件，
  掉分超过容忍度或低于 `evals/baselines.json` 时标红。需要配置仓库变量与密钥（见文件头注释）；模型网关在内网时用自托管运行机。
- 本地：`markbot eval --suite coding --record --fail-on-regression`，`markbot eval --trend` 查看趋势
  （`<home>/logs/optimizer.jsonl`）。
- 优化器晋升门禁：`[optimizer] gate_suites = ["coding"]`（或 `markbot optimize --gate-suites coding,browser`），
  候选在任一评测集上掉分即拒绝。
- 修复：`markbot eval` 之前从空的临时 home 读配置，真实运行拿不到模型与密钥；现在读真实 home 的配置，学习层复制一份隔离。
- 详见 `docs/phase4-implementation-2026-11-23.md`。
