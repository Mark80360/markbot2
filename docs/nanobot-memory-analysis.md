# nanobot 记忆机制详解：何时读、何时写、何时与用户交互

本文是对 `D:\Source\nanobot` 长记忆实现的深入分析，聚焦三个决策问题：

1. **什么时候该读记忆**
2. **什么时候该写记忆**
3. **什么时候该把记忆操作变成与用户的交互（确认 / 告知 / 不打扰）**

结论先行：nanobot 把"记忆"拆成人（模型）负责的**内容判断**和运行时负责的**时机调度**。模型只回答"这条信息值不值得留、放哪里"，而"什么时候压缩、什么时候整理、cursor 推到哪"全部由运行时决定，模型无法越权。

---

## 0. 一句话总览

```
用户消息
  │
  ├─ 显式指令（记住 / 忘记 / 查一下以前） ──────► 直接读或写，改完回报
  │
  ├─ 任务需要背景知识 ────────────────────────► 先读（MEMORY.md → wiki/index.md → 相关页）
  │
  ├─ 出现"已确认 + 长期有效"的信息 ───────────► 读目标文件 → 就地改 → 验证 → 回报
  │
  ├─ 信息不确定 / 无写权限 / 要覆盖旧结论 ─────► 交互（一句话确认或明确告知未保存）
  │
  └─ 其余 ────────────────────────────────────► 不读不写，继续任务
```

运行时在旁边独立做四件事，与模型决策解耦：

| 时机 | 触发者 | 动作 |
|---|---|---|
| 每轮请求 | 运行时 | 注入 `SOUL.md` / `USER.md` / `memory/MEMORY.md` / 会话摘要 |
| token 超预算 | 运行时 | 在线压缩成 checkpoint，替换已接受的 H（已接受的历史） |
| 空闲 15 分钟（可配） | 运行时 | AutoCompact 归档会话 |
| 每 2 小时（可配）或 `/dream` | 运行时 | Dream 消费 `history.jsonl` 并整理长期记忆 |

---

## 1. 什么时候该读

### 1.1 常驻层：不需要决策

以下内容每轮由 [context.py](file:///D:/Source/nanobot/nanobot/agent/context.py#L82-L141) 自动拼进 system prompt，模型无需"决定去读"：

- `AGENTS.md`、`SOUL.md`、`USER.md`
- `memory/MEMORY.md`（全文）
- 会话摘要 `[Archived Context Summary]`

设计约束：正因为是全文常驻，`MEMORY.md` 必须短。模板本身是空壳（[MEMORY.md](file:///D:/Source/nanobot/nanobot/templates/memory/MEMORY.md)），并且与内置模板完全一致时会跳过注入，避免占用 token。

### 1.2 按需层：读到"相关"为止

当任务涉及**项目/领域知识**时，按固定顺序读：

1. 看 `memory/MEMORY.md` 里指向 `wiki/index.md` 的那一行入口；
2. 打开 `memory/wiki/index.md`，根据条目旁的"何时来读"提示定位页；
3. 只读命中的页，链接按所在文件相对解析。

这条规则写在 [memory SKILL](file:///D:/Source/nanobot/nanobot/skills/memory/SKILL.md#L10-L14) 和 [identity.md](file:///D:/Source/nanobot/nanobot/templates/agent/identity.md#L12)。

**信任边界**：wiki 页是"资料"，不是"指令"，不能覆盖运行规则（[SKILL.md#L14](file:///D:/Source/nanobot/nanobot/skills/memory/SKILL.md#L14)）。这与"外部内容不可信"是同一条安全原则。

### 1.3 历史检索：需要"以前发生过什么"

触发词是"上次 / 之前 / 当时怎么决定的 / 以前那个方案"：

| 需要什么 | 用什么 |
|---|---|
| 本会话较早的摘要 | 摘要已注入；需要细节时 `grep memory/history.jsonl` |
| 其他会话的对话 | `search_sessions` → `read_session`（[sessions.py#L81-L165](file:///D:/Source/nanobot/nanobot/agent/tools/sessions.py#L81-L166)） |
| journal 时间段 | 按 `cursor` + `timestamp` 读 `history.jsonl`（每行一条，必要时看相邻条目） |

`history.jsonl` 在文件工具白名单里显式放开读权限（[filesystem.py#L88-L89](file:///D:/Source/nanobot/nanobot/agent/tools/filesystem.py#L88-L90)），但**写权限被运行时收回**（[files.py#L566-L575](file:///D:/Source/nanobot/nanobot/execution/files.py#L566-L575)）。

### 1.4 写之前必须先读

任何"就地替换"的写入都要求先读目标文件，否则会覆盖别人刚写的内容。Dream 的提示词第一句就是"Read relevant existing notes before editing"（[dream.md](file:///D:/Source/nanobot/nanobot/templates/agent/dream.md#L3-L5)）。

### 1.5 读取的成本规则

- 常驻层越短越好，项目细节一律下沉到 wiki；
- wiki 一次只读命中页，禁止整目录通读；
- 大历史用 `grep` / 分页读，不整文件塞进上下文；
- 工具结果超过 16k 字符时会被运行时落盘，只给"路径 + 头尾预览"，需要时再读（[helpers.py#L524-L562](file:///D:/Source/nanobot/nanobot/utils/helpers.py#L524-L562)）。

---

## 2. 什么时候该写

### 2.1 三个"该写"的触发器

**A. 用户显式要求（最高优先级）**

"记住…"、"以后都…"、"我的偏好是…" → 本回合直接写，不要反问"要保存吗"。

写完后必须**回报改了哪个文件**，因为用户无法从一句"好的，记住了"判断是否真的落盘（[docs/memory.md](file:///D:/Source/nanobot/docs/memory.md#L5-L7) 专门提示了这一点）。

**B. 已确认的纠正**

用户说"不对，是 X 而不是 Y"，且 X 已确认 → 找到旧事实，**就地替换**，并清理其他文件里的过时副本（Dream 的 `[correction]` 语义，[dream.md#L25-L31](file:///D:/Source/nanobot/nanobot/templates/agent/dream.md#L25-L32)）。

**C. 已验证、跨任务稳定的事实**

判据用 SNIP 四准则（[consolidator_archive.md#L23-L35](file:///D:/Source/nanobot/nanobot/templates/agent/consolidator_archive.md#L23-L35)），**全部满足**才留：

- **S**ignal：记住它能省掉用户重复说明
- **N**ovel：是新增事实，不是重复
- **I**mportant：丢掉会造成返工，或丢偏好/规则
- **P**ersistent：至少未来两周仍然有效

另外，一个流程**重复出现至少两次**且步骤明确，才升级成 `skills/<name>/SKILL.md`（[dream.md#L39-L41](file:///D:/Source/nanobot/nanobot/templates/agent/dream.md#L41)）。

### 2.2 写到哪个文件

| 内容 | 目标 | 判据 |
|---|---|---|
| Agent 的跨任务行为规则 | `SOUL.md` | 与具体任务无关的"怎么做" |
| 用户事实、长期偏好、沟通风格 | `USER.md` | 关于"人"的稳定信息 |
| 跨任务稳定事实 + wiki 入口 | `memory/MEMORY.md` | 不属于上面两个文件，且每轮都可能用到 |
| 项目/领域知识、决策与理由 | `memory/wiki/<topic>.md` | 只在相关任务里才需要 |
| 可复用流程 | `skills/<name>/SKILL.md` | 出现 ≥2 次、步骤明确 |

原则：**一个事实只出现在一个地方**；wiki 的页面链接和"何时读"提示放在 `index.md`，不放进 `MEMORY.md`。

### 2.3 明确"不写"的清单

- 临时请求（"这次短一点"）
- 例行进度播报、状态汇报
- 未确认的计划 / 结果（不能写成"已完成"）
- 推断出来的偏好（没被用户确认）
- 凭据、密钥、token
- 用户明确说不要保留的内容
- 随时可查的参考资料（代码、文档原文）
- 一次性事件

这些内容的共同归宿是 **`history.jsonl`（原文/摘要留在 journal），而不是长期记忆文件**。Dream 提示词对此有专门说明（[dream.md#L15](file:///D:/Source/nanobot/nanobot/templates/agent/dream.md#L15)）。

### 2.4 写入的前置条件（硬约束）

写入不是永远允许的，运行时有三道闸：

1. **会话策略**：`session.policy.persist == False` 的临时/隔离会话，不注入 `SOUL/USER/MEMORY`，memory skill 也被排除（[context.py#L310-L314](file:///D:/Source/nanobot/nanobot/agent/context.py#L310-L314)）。此类会话只允许用当前上下文工作。
2. **工作区边界**：受限项目会话只能读 `memory/wiki/`，不能写 agent 记忆目录（[docs/memory.md#L63-L64](file:///D:/Source/nanobot/docs/memory.md#L63-L64)）。
3. **运行时专属文件**：`memory/history.jsonl`、`.cursor`、`.dream_cursor` 由运行时管理，文件工具写它们会直接报错（[files.py#L566-L575](file:///D:/Source/nanobot/nanobot/execution/files.py#L566-L575)）。

### 2.5 写入的收尾动作（缺一不可）

1. 读目标文件；
2. 就地替换（不是追加重复条目）；
3. 更新受影响的索引/相对链接；
4. 再读一遍验证；
5. 回报"改了哪个文件、改了什么"；
6. 如果没写成功：**说明哪项没保存、为什么**，不要绕过限制。

Dream 侧则多一步：用真实文件 diff 生成 commit message 并 Git 提交（[dream.py#L50-L66](file:///D:/Source/nanobot/nanobot/agent/dream.py#L50-L66)），保证可审计、可回滚。

---

## 3. 什么时候该交互

"交互"指：把记忆操作显式地呈现给用户——确认、告知、或刻意保持安静。判断标准是**用户是否拥有模型不知道的信息，或模型是否改变了用户可感知的持久状态**。

### 3.1 直接写、不打断（默认）

- 用户明确要求记住 → 写 + 一句话回报文件路径；
- 明显的长期偏好且已被用户确认 → 写 + 回报；
- 修正一个已确认的事实 → 就地替换 + 回报。

理由：过度确认会打断任务，而且"记住"本身就是用户的授权。

### 3.2 必须先问（一句话确认）

- 偏好是**推断**出来的（用户没说"以后都这样"）；
- 两条来源冲突、且都未被确认；
- 要**覆盖或删除**既有记忆，尤其是 `SOUL.md` 中的行为规则、跨任务偏好；
- 要把某条规则从"项目级"提升为"全局级"；
- 要把某段内容从 wiki 迁走或删除页面（涉及链接修复）。

确认时说明"将替换什么"，而不是笼统问"可以吗"。

### 3.3 必须主动告知（不需要等用户问）

| 场景 | 告知内容 |
|---|---|
| 无写权限 / 受限会话 | 哪项没保存、原因、可选做法（切到 agent workspace / 手动编辑 / `/compact` + `/dream`） |
| 会话是临时的 | 明确说明"本会话不读写长期记忆" |
| Dream 完成 | 确认的变更摘要；无变更就说无变更 |
| Dream 未完成 | 哪个区间没读完 / 哪个编辑没验证，剩下什么 |
| Dream 无输入 | 短对话还没进 archive，需要先 `/compact` 或等空闲归档 |
| 压缩发生 | 由运行时的 compaction notice 通知（默认隐藏，可开 `channels.showCompactionNotices`） |

### 3.4 破坏性操作：先预览再执行

`/dream-restore` 会**整体回滚**所有记忆文件和 wiki，不是单页恢复：

1. 先 `/dream-restore` 列版本；
2. `/dream-log <sha>` 预览该版本的 diff；
3. 有未提交的手工改动时，先提交或回滚；
4. 确认后再恢复。

这条链路在 [docs/memory.md#L66-L82](file:///D:/Source/nanobot/docs/memory.md#L66-L83) 里写死。

### 3.5 不该交互的场景

- 自动压缩、自动归档：后台执行，只在开启提示时发一条通知；
- 正常工作状态已记录在 checkpoint：下次续接时**直接用**，不要问"我们上次做到哪了"；
- 中间结果、工具调用过程：不需要每步征求同意。

### 3.6 交互的"显式技能"通道

用户可以用 `$memory` 这类引用显式激活技能（[skills.py#L211-L227](file:///D:/Source/nanobot/nanobot/agent/skills.py#L211-L227)），技能正文会作为本轮 runtime context 注入。这是"用户主动要求按记忆规范操作"的信号，优先级高于模型自行判断。

---

## 4. 运行时时机表（模型不参与）

| 事件 | 默认值 | 代码位置 | 说明 |
|---|---|---|---|
| 空闲自动归档 | 15 分钟（`idleCompactAfterMinutes`，0 关闭） | [schema.py#L209-L218](file:///D:/Source/nanobot/nanobot/config/schema.py#L209-L218) | 扫描过期会话，后台归档 |
| 空闲扫描频率 | 60 秒 | [schema.py#L215-L218](file:///D:/Source/nanobot/nanobot/config/schema.py#L215-L218) | 仅在 gateway 运行 |
| token 压力压缩 | `context_window - max_tokens - buffer` | [context_governance.py#L825-L838](file:///D:/Source/nanobot/nanobot/agent/context_governance.py#L825-L838) | 请求前判定，超了就压缩 |
| 工具结果落盘 | > 16,000 字符 | [schema.py#L193](file:///D:/Source/nanobot/nanobot/config/schema.py#L193) | 只留路径 + 头尾预览 |
| Dream 周期 | 每 2 小时（`intervalH`，可用 cron 覆盖） | [schema.py#L71-L97](file:///D:/Source/nanobot/nanobot/config/schema.py#L71-L97) | `enabled` 控制开关 |
| journal 裁剪 | 最多 1000 条 | [memory.py#L386-L414](file:///D:/Source/nanobot/nanobot/agent/memory.py#L386-L414) | 只裁已消费条目，不丢待 Dream 数据 |
| 手动压缩 | `/compact` | [builtin.py#L269-L286](file:///D:/Source/nanobot/nanobot/command/builtin.py#L269-L286) | 立即归档当前会话 |
| 手动整理 | `/dream` | [builtin.py#L370-L400](file:///D:/Source/nanobot/nanobot/command/builtin.py#L370-L400) | 只处理 cursor 之后的新条目 |
| 新会话 | `/new` | [builtin.py#L233-L266](file:///D:/Source/nanobot/nanobot/command/builtin.py#L233-L266) | 先归档再重置 |

注意 Dream 的幂等性：它读的是 `cursor > last_dream_cursor` 的区间，成功才推进 cursor（[dream.py#L32-L49](file:///D:/Source/nanobot/nanobot/agent/dream.py#L32-L49)），失败则下轮重试，不会漏也不会重复。

---

## 5. 可落地的判定伪代码

```python
def handle_turn(msg, session, task):
    # 0. 会话能力检查
    if not session.policy.persist:
        return run_without_memory(msg)          # 不读不写，必要时告知用户

    # 1. 读：显式检索需求
    if asks_about_past(msg):
        context = search_history(msg)           # history.jsonl / search_sessions
    elif task.needs_domain_knowledge:
        context = read_wiki_on_demand(task)     # MEMORY.md -> wiki/index.md -> 命中页
    else:
        context = resident_memory()             # 已自动注入

    # 2. 写：内容判断
    fact = extract_candidate(msg, session)
    if fact is None:
        return respond(msg, context)

    if fact.is_explicit_request or fact.is_confirmed_correction or fact.passes_snip():
        if not can_write(session, fact.target_file):
            return respond_and_inform(msg, fact, "not_saved")   # 交互：告知
        if fact.overwrites_global_rule and not user_confirmed(msg):
            return ask_confirmation(fact)                       # 交互：确认
        old = read(fact.target_file)                            # 写前必读
        write(fact.target_file, merge_in_place(old, fact))
        verify(fact.target_file)
        return respond_and_report_files(msg)                    # 交互：回报改了哪里

    # 3. 不写
    return respond(msg, context)
```

对应 nanobot 的真实分支：`build_system_prompt()` 决定读什么（[context.py#L82-L141](file:///D:/Source/nanobot/nanobot/agent/context.py#L82-L141)），memory skill 决定写什么（[SKILL.md](file:///D:/Source/nanobot/nanobot/skills/memory/SKILL.md#L16-L26)），Dream 决定何时整理（[dream.py](file:///D:/Source/nanobot/nanobot/agent/dream.py#L24-L67)）。

---

## 6. 反模式清单

- 把"好的，记住了"当成已写入；
- 不读就写，覆盖用户刚改的内容；
- 把项目细节堆进 `MEMORY.md`，导致每轮 token 成本上升；
- 同一事实抄到多个文件，后续纠正只改一处；
- 把 wiki 页当指令执行，或把外部内容当可信输入；
- 把未确认的计划/未验证的结论写成"已完成"；
- 压缩时只留摘要、丢掉"目标 / 状态 / 阻塞 / 下一步 / 关键 ID"；
- 摘要失败时直接丢弃历史（nanobot 的做法是 raw archive 兜底，[memory.py#L633-L666](file:///D:/Source/nanobot/nanobot/agent/memory.py#L633-L666)）；
- 在临时/受限会话里偷偷写记忆；
- 每次请求都通读整个 wiki。

---

## 7. 如果要在 markbot2 复刻，最小实现顺序

1. **常驻 + 按需分层**：`SOUL/USER/MEMORY` 每轮注入，其余落 `wiki/`，靠 `index.md` 按需读。
2. **journal + cursor**：append-only 的 `history.jsonl`，每条带自增 `cursor`；消费方各自持有自己的 cursor，天然增量、可重放、可并发。
3. **两级压缩**：会话内 checkpoint（替换已接受历史）+ 工具结果落盘（只留引用）。
4. **离线整理器**：受限工具集 + Git 提交 + 真实 diff 生成 commit message，先做 `/dream-log` 可审计，再做 `/dream-restore`。
5. **三道写闸**：会话策略、工作区边界、运行时专属文件只读。
6. **交互三原则**：显式要求直接写、推断内容先确认、无权限必告知。

---

## 附录：关键源码索引

| 主题 | 文件 |
|---|---|
| 记忆文件读写、journal、cursor、Dream 工具集 | [memory.py](file:///D:/Source/nanobot/nanobot/agent/memory.py#L53-L86) |
| 会话内压缩与 checkpoint | [memory.py#L1040-L1259](file:///D:/Source/nanobot/nanobot/agent/memory.py#L1040-L1259) / [manager.py#L155-L174](file:///D:/Source/nanobot/nanobot/session/manager.py#L155-L174) |
| 空闲自动归档 | [autocompact.py](file:///D:/Source/nanobot/nanobot/agent/autocompact.py#L25-L126) |
| token 压力压缩与工具结果预算 | [context_governance.py#L629-L838](file:///D:/Source/nanobot/nanobot/agent/context_governance.py#L629-L838) / [L1062-L1082](file:///D:/Source/nanobot/nanobot/agent/context_governance.py#L1062-L1082) |
| 上下文注入 | [context.py#L82-L141](file:///D:/Source/nanobot/nanobot/agent/context.py#L82-L141) |
| Dream 执行与 Git 版本化 | [dream.py](file:///D:/Source/nanobot/nanobot/agent/dream.py#L24-L67) |
| Dream / 摘要提示词 | [dream.md](file:///D:/Source/nanobot/nanobot/templates/agent/dream.md) / [consolidator_archive.md](file:///D:/Source/nanobot/nanobot/templates/agent/consolidator_archive.md) |
| 记忆写入规范（模型侧） | [skills/memory/SKILL.md](file:///D:/Source/nanobot/nanobot/skills/memory/SKILL.md) |
| 历史会话检索工具 | [tools/sessions.py](file:///D:/Source/nanobot/nanobot/agent/tools/sessions.py#L81-L165) |
| 写保护与路径边界 | [execution/files.py#L556-L583](file:///D:/Source/nanobot/nanobot/execution/files.py#L556-L583) |
| 用户文档 | [docs/memory.md](file:///D:/Source/nanobot/docs/memory.md) |

## 延伸：markbot2 落地设计

基于本文判定表的读 / 写 / 交互伪代码、存储布局与集成点见
[markbot2 记忆系统设计](./memory-design-pseudocode.md)。
