---
name: cron
description: 创建、查看和管理定时任务；任务到点后由同一个 Agent 执行，可只归档或发回聊天。
---

# 定时任务（cron）

用 `cron` 工具管理定时任务。任务到点后**复用当前 Agent** 执行一段任务内容，
不会另起模型或另一套工具；执行结果默认写入会话归档。

## 什么时候用

- 用户说「每天 9 点提醒我…」「每隔 2 小时检查…」「明天下午 3 点帮我…」
- 需要周期性巡检、汇总、提醒，而不是当场完成的一次性任务

## 调度三选一

| 参数 | 含义 | 例子 |
| --- | --- | --- |
| `every_seconds` | 固定间隔，秒 | `every_seconds=3600` 每小时 |
| `cron_expr` | crontab 表达式 | `cron_expr="0 9 * * 1-5"` 工作日 9 点 |
| `at` | 一次性时间 | `at="2026-12-01 09:00"` |

`cron_expr` 需要安装 `croniter`（`pip install 'markbot[cron]'`）。
时区用 `tz` 指定，留空使用 `[cron] timezone` 或本机时区。

## 结果投递

- `delivery="none"`：只归档到会话与 `~/.markbot/cron/runs/`，写 gateway 日志
- `delivery="channel"`：把最终回复发回创建任务的聊天（要求任务是在聊天窗口创建的）
- `delivery="auto"`（默认）：能拿到渠道回调就发回聊天，否则只归档

每次执行都会把最终回复作为结果：`delivery="channel"` 时无条件发回任务绑定的聊天，
不要用“无需汇报”之类的占位回复代替结果。

## 常用操作

```text
cron(action="add", message="检查昨天的构建结果并汇总失败原因", cron_expr="0 9 * * *", name="每日构建巡检")
cron(action="list")
cron(action="history", job_id="a1b2c3d4", limit=5)
cron(action="run", job_id="a1b2c3d4")
cron(action="disable", job_id="a1b2c3d4")
cron(action="remove", job_id="a1b2c3d4")
```

## 注意

- 任务内容要写成**可独立执行**的指令，不要依赖当前对话里没写进去的上下文。
- 不要在定时任务里再创建定时任务（工具会拒绝）。
- 调度器只在 `markbot serve` / `markbot gateway start` 进程里运行；服务停止期间不会触发。
- 命令行 `markbot cron add` 创建的任务没有聊天绑定，只归档、不发消息。
