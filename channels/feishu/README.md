# 飞书渠道样例

把飞书机器人的消息转发给 MarkBot，并把回复发回飞书。

这是一个**外置网关**：独立进程，只通过 HTTP 调用 `markbot serve`，不依赖 `markbot` 包，也不需要修改 Kernel。

```text
飞书事件回调 ──POST──▶ channel.py ──POST /chat──▶ markbot serve
                          │
                          └──飞书发送消息 API──▶ 用户
```

## 1. 配置放在哪里

统一放在 `~/.markbot/config.toml`（与模型、权限、日志配置同一个文件）：

```toml
[channels.feishu]
mode = "longconn"                        # longconn（推荐，无需公网）| webhook
app_id = "cli_xxxxxxxx"
verification_token = "xxxxxxxx"          # 仅 webhook 模式需要，可留空
port = 8790                              # 仅 webhook 模式使用
host = "0.0.0.0"
path = "/feishu/event"
markbot_url = "http://127.0.0.1:8787"    # markbot serve 地址
markbot_timeout_s = 300
require_mention = true                   # 群聊必须 @ 机器人才响应
# session_db = "~/.markbot/channels/feishu/feishu.db"

[secrets]
FEISHU_APP_SECRET = "xxxxxxxx"
```

说明：

- `app_id`、回调地址等**非敏感配置**在 `[channels.feishu]`
- `app_secret` 放在 `[secrets]`，不会进入模型上下文
- 渠道配置只读用户级 `~/.markbot/config.toml`；`MARKBOT_HOME` 可覆盖目录
- 会话映射与消息去重默认落在 `~/.markbot/channels/feishu/feishu.db`

## 2. 飞书后台配置

1. 在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用
2. 开启「机器人」能力
3. 权限：至少 `im:message`（接收消息）、`im:message:send_as_bot`（发送消息）
4. 事件订阅：
   - `mode = "longconn"`：订阅方式选「**使用长连接接收事件**」，无需请求地址
   - `mode = "webhook"`：订阅方式选「将事件发送至开发者服务器」，请求地址填 `https://你的域名/feishu/event`
   - 两种模式都要订阅 `im.message.receive_v1`
5. 复制 App ID / App Secret（/ Verification Token）填入上面的配置
6. 发布应用版本，并把机器人拉进群

本地开发没有公网地址时用 `longconn`；坚持 webhook 则可用 ngrok、frp 等把 `8790` 暴露为 HTTPS 地址。

## 3. 启动

长连接模式需要安装飞书 SDK：

```bash
pip install lark-oapi          # 或 pip install -e ".[channels]"
```

先启动 MarkBot：

```bash
markbot serve --port 8787
```

再启动渠道：

```bash
python channels/feishu/channel.py
python channels/feishu/channel.py --check   # 只校验配置与 MarkBot 连通性
```

也可以交给 CLI 后台托管（同时启动 `markbot serve` 与渠道）：

```bash
markbot gateway start      # 启动
markbot gateway status     # 查看 pid / 健康检查
markbot gateway restart    # 重启
markbot gateway stop       # 停止
```

托管进程的日志追加写入 `~/.markbot/gateway/YYYY-MM-DD.log`。

## 4. 行为说明

- 每个飞书群对应一个 MarkBot 会话，私聊按 chat 独立；重启后通过 SQLite 恢复映射
- 群聊默认只处理 @ 机器人的消息，可在配置里关闭 `require_mention`
- 支持 `/new`（开始新会话）与 `/session`（查看当前会话 id）
- 按 `header.event_id` 去重，飞书重试不会重复触发
- 回复按 3500 字分片发送
- 模型处理放后台单工作线程，webhook 立即返回 200，避免飞书 3 秒超时重试

## 5. 已知限制

- 只处理文本消息，图片/文件/富文本暂不解析
- webhook 模式未实现事件加密（AES）；若后台开启了 Encrypt Key，请关闭或改用 `longconn`（加密由飞书 SDK 处理）
- 不支持流式输出与消息编辑
- 单工作线程串行处理：MarkBot 的 `/chat` 本身是串行的，多群高并发时建议先限制入口速率
- 渠道自身日志输出到 stdout；MarkBot 侧日志仍在 `~/.markbot/logs/`

## 6. 安全建议

- `config.toml` 含密钥，注意文件权限，不要提交到仓库
- 生产环境建议只监听内网/回环，由反向代理负责 HTTPS 与限流
- 不用的权限不要勾选；`app_secret` 定期轮换
