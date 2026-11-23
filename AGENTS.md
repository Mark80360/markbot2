# AGENTS.md

给在本仓库工作的 AI / 人类协作者的约束。架构细节见 `docs/ARCHITECTURE.md`。

## 依赖方向（不可逆）

```
schema  <-  api  <-  kernel  <-  server / cron  <-  cli
                \           (memory 库层：extensions / server / cli 可用，kernel 不可用)
                 <-  extensions (+ memory)
optimizer -> schema          channels -> HTTP（+ kernel.gateway_log / local_auth）
```

- `schema` 只放纯数据模型与纯规则（白名单、哈希、学习层写锁），不 import 任何其他层。
- `api` 是冻结契约，只能依赖 `schema`；新增字段要兼容旧值。
- `kernel` 可以依赖 `api` 与 `schema`；**不依赖 `memory`**——常驻记忆由 memory 扩展经
  `api.append_entry("context_providers", factory)` 注入；不要反向 import 具体渠道。
- `extensions` 只依赖 `api`（以及库层 `memory`）；子进程一律走 `ctx.env`（`exec` / `exec_argv`）。
- `server` 是应用服务层（HTTP 入口与后台线程），不依赖 `cli`；`cli` 只解析参数。

## 回合不变量

任何改动都不能破坏以下性质（`tests/test_loop_guard.py` 覆盖）：

1. 一次模型回复里的 `tool_calls` 与写回会话的 `tool` 消息 1:1 配对；取消或异常
   也要补齐 `interrupted` 结果。
2. 已经通过 `on_delta` 交付给用户的文本，不能在顶层异常时被清空。
3. 会话状态只能通过 `Session.append_*` / `apply_compaction` 修改；`messages` 是
   `events.jsonl` 的投影缓存，不要直接改它。
4. 读归档一律经 `schema.session_log.SessionLog`；不要新增直接解析 `trajectory.jsonl` /
   `compacted.jsonl` 的代码，也不要新增「从事件派生、再落盘一份」的文件（ADR 0007）。

## 并发

- 回合级状态（确认 UI、临时授权）只能经 `Kernel.run(ui=, grants=)` / `TurnContext` 传递，
  不要再改 `kernel.ui`、`policy._grants` 这类共享字段。
- 长驻进程里：普通回合用 `RunGate.session(sid)`，维护任务（重载、整理、归档）用 `with gate:`。
- 任何修改 `learned/` 的代码必须包在 `schema.locking.learned_write_lock(learned_dir)` 里。

## 执行与权限

- 文件与命令访问统一走 `ctx.env`（`ExecutionEnvironment`），不要在扩展里直接
  `Path.read_text` 或 `subprocess`。
- 带 `fs.*` / `net:` 能力的工具必须标注参数角色（`x-markbot-role` 或 `Tool.arg_roles`），
  不要依赖参数名碰巧叫 `path` / `url`。
- 权限判断在 `PermissionPolicy.check`；需要用户确认的操作返回
  `requires_confirmation=True`，由 `ToolContext.confirm` 完成。
- 密钥只在工具边界通过 `ctx.secret()` 获取，永不进入模型上下文。
- 密钥值只放 `<home>/secrets.toml`（0600）或 `env:NAME` 引用，不写进 `config.toml`。
- 放行判断必须覆盖命令里的每个程序；看不透的命令（内联解释器、命令替换、提权）
  一律 `requires_confirmation=True`，不要为了方便加例外。

## 进程间调用

- serve / 渠道 / 定时任务之间的 HTTP 调用必须带 `X-MarkBot-Token`
  （`markbot.kernel.local_auth`）；回调地址只允许回环地址。
- 不要从请求体里信任能扩大权限的字段；新增此类字段要先写 ADR。

## 日志与脱敏

- 会话归档、事件日志、运行日志的写入入口必须经过 `redact_secrets`。
- 不记录 provider 原始 payload；需要复现时用 `events.jsonl` + `logs/` 的组合。

## 门禁

提交前必须全绿：

```bash
.venv/bin/ruff check src tests
.venv/bin/python -m pytest -q
```

新增顶层模块时补一条契约测试（参考 `tests/test_contracts.py`）。
不阻断但只许变好：`bash scripts/lint_strict.sh`（ruff B/UP/SIM/RUF + pyright）。
改动 `learned/` 晋升逻辑或提示词模板时，跑一次 `markbot optimize --dry-run` 看回归分。
变更会话/循环语义时同步更新 `docs/ARCHITECTURE.md` 与 `docs/decisions/`。
