#!/usr/bin/env python3
"""飞书渠道：飞书事件 → MarkBot HTTP /chat → 飞书消息。

功能参考 nanobot 的飞书渠道：
- 文本 / 富文本(post) / 卡片(interactive) / 分享卡片解析；
- 图片、文件、语音下载到本地，路径写进提示词交给 agent；
- 入站语音可选调用 /audio/transcriptions 转写，结果随提示词一起交给 agent；
- 出站媒体（图片 / 语音 / 视频 / 文件）上传后按类型发送；
- 收到消息先加表情，处理完成后替换为完成表情；
- 可选引用原消息（群聊内以话题回复）；
- 模型输出用 CardKit 流式卡片渐进展示，失败回退普通消息；
- 按 Markdown 复杂度选择 text / post / 卡片输出，卡片失败回退纯文本；
- 群聊 @ 策略、发送者白名单、群内话题级会话隔离。

设计：渠道是独立进程，只通过 HTTP 调用 `markbot serve`（带本机令牌）；唯一的例外是日志，
直接复用主 agent 的 `markbot.kernel.gateway_log`，不另起一套。
配置统一放在 ~/.markbot/config.toml：

    [channels.feishu]
    mode = "longconn"          # longconn（推荐，无需公网）| webhook
    app_id = "cli_xxx"
    verification_token = "xxx" # 仅 webhook 模式需要
    group_policy = "mention"   # mention（群聊必须 @）| open（群聊全部响应）
    ...

    # ~/.markbot/secrets.toml（0600）
    [secrets]
    FEISHU_APP_SECRET = "xxx"

启动：

    python channels/feishu/channel.py
    python channels/feishu/channel.py --check

日志：复用主 agent 的 gateway_log，写入 `~/.markbot/gateway/YYYY-MM-DD.log`，
同时输出到 stdout；若 stdout 已被重定向到同一文件（`markbot gateway start` 的做法），
则只写一次，避免重复。

依赖：Python 3.11+ 标准库（tomllib）。长连接模式额外需要 `pip install lark-oapi`。

模块划分（ADR 0005）：

    channel.py         入口：回调 HTTP 服务、长连接、--check、main
    feishu_common.py   常量、日志、home、本机令牌
    feishu_settings.py 配置加载
    feishu_store.py    SQLite 存储
    feishu_client.py   飞书 API 客户端
    feishu_parse.py    入站消息解析（纯函数）
    feishu_render.py   出站 Markdown/卡片渲染（纯函数）
    feishu_gateway.py  消息处理、聊天内确认与 MarkBot 调用
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 由 `markbot gateway` 以 runpy.run_path 启动：sys.path 不含本目录，先补上再导入同级模块
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from feishu_common import (
    AUTH_HEADER,
    default_config_path,
    gateway_log,
    log,
    markbot_home,
)
from feishu_settings import (
    Settings,
    load_settings,
)
from feishu_store import (
    Store,
)
from feishu_client import (
    _transcription_endpoint,
)
from feishu_gateway import (
    Gateway,
    _verify_token,
)


# 订阅了但不需要处理的事件：不注册的话 SDK 每条都会打一行 ERROR「processor not found」。
# 机器人自己加/删表情（处理中的 react_emoji）也会触发 reaction 事件（ADR 0009）。
IGNORED_EVENTS = (
    "register_p2_im_message_reaction_created_v1",
    "register_p2_im_message_reaction_deleted_v1",
    "register_p2_im_message_message_read_v1",
)


def _ignore_event(_data) -> None:
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def register_ignored_events(builder):
    """给 lark 事件分发器注册空处理器；旧版 SDK 没有对应方法时跳过。"""
    for name in IGNORED_EVENTS:
        register = getattr(builder, name, None)
        if callable(register):
            try:
                builder = register(_ignore_event)
            except Exception:  # noqa: BLE001 - 注册失败不影响主流程
                continue
    return builder


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(gateway: Gateway, settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                _json_response(
                    self,
                    200,
                    {
                        "status": "ok",
                        "queue": gateway.queue_size(),
                        "workers": len(gateway.queues),
                    },
                )
            else:
                log(f"[http] GET {self.path} -> 404")
                _json_response(self, 404, {"error": "not found"})

        def _authorized(self) -> bool:
            import hmac

            presented = str(self.headers.get(AUTH_HEADER) or "")
            return bool(presented) and hmac.compare_digest(
                presented.encode(), gateway.auth_token.encode()
            )

        def _confirm_request(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                _json_response(self, 400, {"error": "invalid json"})
                return
            ok = gateway.request_confirmation(payload)
            _json_response(self, 200 if ok else 400, {"ok": ok})

        def _notify(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                _json_response(self, 400, {"error": "invalid json"})
                return
            ok = gateway.notify(
                str(payload.get("chat_id") or ""),
                str(payload.get("text") or ""),
                reply_to=str(payload.get("reply_to") or "") or None,
                reply_in_thread=_truthy(payload.get("reply_in_thread")),
            )
            _json_response(self, 200 if ok else 400, {"ok": ok})

        def do_POST(self) -> None:  # noqa: N802
            if self.path in ("/confirm-request", "/notify"):
                # 内部回调只接受带本机令牌的请求，防止被冒充发消息或伪造确认
                if not self._authorized():
                    log(f"[http] 拒绝未鉴权的内部回调 {self.path} peer={self.client_address[0]}")
                    _json_response(self, 401, {"error": "unauthorized"})
                    return
                if self.path == "/confirm-request":
                    self._confirm_request()
                else:
                    self._notify()
                return
            if self.path != settings.path:
                log(f"[http] POST {self.path} -> 404（期望 {settings.path}）")
                _json_response(self, 404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            log(f"[http] 收到回调请求 {length} 字节")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                log("[http] 回调内容不是合法 JSON")
                _json_response(self, 400, {"error": "invalid json"})
                return

            if data.get("encrypt"):
                log("收到加密事件：本样例未实现 AES 解密，请在飞书后台关闭加密或改用长连接 SDK")
                _json_response(self, 400, {"error": "encrypted event not supported"})
                return

            if data.get("type") == "url_verification":
                if not _verify_token(settings, data):
                    log("[http] url_verification 校验失败：token 不匹配")
                    _json_response(self, 403, {"error": "invalid token"})
                    return
                log("[http] url_verification 校验通过")
                _json_response(self, 200, {"challenge": data.get("challenge", "")})
                return

            if not _verify_token(settings, data):
                log("[http] 事件校验失败：token 不匹配")
                _json_response(self, 403, {"error": "invalid token"})
                return

            if gateway.intercept_confirmation(data) or gateway.intercept_control(data):
                log("[http] 确认/控制命令已处理")
                _json_response(self, 200, {"code": 0})
                return

            accepted = gateway.enqueue(data)
            log(f"[http] 回调处理完成 accepted={accepted}")
            _json_response(self, 200, {"code": 0})

        def log_message(self, fmt: str, *args: Any) -> None:
            log(f"[http] {fmt % args}")

    return Handler


def run_long_connection(settings: Settings, gateway: Gateway) -> None:
    """通过飞书长连接接收事件，无需公网地址与回调 URL。"""
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    except ImportError as exc:
        raise SystemExit("长连接模式需要 lark-oapi：pip install lark-oapi") from exc

    def on_message(event: P2ImMessageReceiveV1) -> None:
        raw = lark.JSON.marshal(event)
        if not raw:
            return
        data = json.loads(raw)
        # 确认回复要在 SDK 线程里直接处理：工作线程此时正阻塞等待本次回合
        # /stop、/status 同理：工作线程正在等任务，不能排在它后面
        if gateway.intercept_confirmation(data) or gateway.intercept_control(data):
            return
        gateway.enqueue(data)

    builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(
        on_message
    )
    builder = register_ignored_events(builder)
    dispatcher = builder.build()
    client = lark.ws.Client(
        settings.app_id,
        settings.app_secret,
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )
    log("飞书渠道已启动（长连接模式，无需公网地址）")
    log(f"app_id: {settings.app_id}")
    log(f"转发到 MarkBot: {settings.markbot_url}")
    log(f"会话库: {settings.session_db}")
    log(f"群聊策略: {settings.group_policy}")
    log("正在建立长连接…")
    try:
        client.start()
    except Exception as exc:  # noqa: BLE001
        log(f"长连接异常退出: {exc}")
        raise
    log("长连接已断开")


def check(settings: Settings) -> int:
    print(f"config: {default_config_path()}")
    print(f"mode: {settings.mode}")
    print(f"app_id: {settings.app_id}")
    if settings.mode == "webhook":
        print(f"listen: http://{settings.host}:{settings.port}{settings.path}")
    else:
        if importlib.util.find_spec("lark_oapi") is None:
            print("FAIL 长连接模式需要 lark-oapi：pip install lark-oapi")
            return 1
        print("PASS lark-oapi 已安装")
    print(f"markbot: {settings.markbot_url}")
    print(f"session db: {settings.session_db}")
    print(f"media dir: {settings.media_dir}")
    print(f"group_policy: {settings.group_policy}")
    print(f"reply_to_message: {settings.reply_to_message}")
    print(f"topic_isolation: {settings.topic_isolation}")
    print(f"reactions: {settings.react_emoji or '-'} -> {settings.done_emoji or '-'}")
    print(f"streaming: {'on' if settings.streaming else 'off'}")
    if settings.transcribe_enabled:
        print(
            f"transcription: on ({settings.transcribe_provider}/"
            f"{settings.transcribe_model} {_transcription_endpoint(settings)})"
        )
    else:
        print("transcription: off（[transcription] 未启用或缺少 API Key）")
    print(f"allow_from: {len(settings.allow_from) or '不限制'}")
    try:
        with urllib.request.urlopen(
            settings.markbot_url.rstrip("/") + "/health", timeout=5
        ) as response:
            health = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL 无法连接 MarkBot: {exc}")
        return 1
    print(f"PASS MarkBot 可用: {health}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="飞书渠道样例（外置网关）")
    parser.add_argument("--config", type=Path, default=None, help="config.toml 路径")
    parser.add_argument("--check", action="store_true", help="只校验配置与 MarkBot 连通性")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    if args.check:
        return check(settings)

    log_file = gateway_log.install(markbot_home())
    store = Store(settings.session_db)
    gateway = Gateway(settings, store)
    for index in range(len(gateway.queues)):
        threading.Thread(
            target=gateway.worker, args=(index,), daemon=True, name=f"feishu-worker-{index}"
        ).start()
    log(f"日志文件: {log_file}")
    log(
        f"配置已加载 mode={settings.mode} app_id={settings.app_id} "
        f"markbot={settings.markbot_url} 会话库={settings.session_db} "
        f"群聊策略={settings.group_policy} 话题隔离={settings.topic_isolation}"
    )
    log(f"消息处理线程已启动 workers={len(gateway.queues)}（同一聊天串行）")
    # 收件箱/发件箱（ADR 0011）：清理 7 天前的记录，恢复上次没处理完的消息，启动发件箱重试
    try:
        store.prune_seen(7)
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响启动
        log(f"清理过期记录失败: {exc}")
    threading.Thread(target=gateway.outbox_worker, daemon=True, name="feishu-outbox").start()
    gateway.recover_inbox()

    # 长连接模式下只需要一个本机回环端口来接收「确认请求」，不对外暴露
    bind_host = "127.0.0.1" if settings.mode == "longconn" else settings.host
    try:
        server = ThreadingHTTPServer(
            (bind_host, settings.port), make_handler(gateway, settings)
        )
    except OSError as exc:
        # 端口被占用时不影响收发消息，只是聊天内的授权确认会失败
        log(f"确认回调端口绑定失败（{bind_host}:{settings.port}）: {exc}")
        server = None

    if settings.mode == "longconn":
        if server is not None:
            threading.Thread(
                target=server.serve_forever, daemon=True, name="feishu-confirm"
            ).start()
            log(
                f"后台回调已启动: http://{bind_host}:{settings.port}"
                "/confirm-request 与 /notify"
            )
        try:
            run_long_connection(settings, gateway)
        except KeyboardInterrupt:
            log("收到退出信号")
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
        return 0

    if server is None:
        return 1
    log(f"飞书渠道已启动: http://{settings.host}:{settings.port}{settings.path}")
    log(f"转发到 MarkBot: {settings.markbot_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("收到退出信号")
    finally:
        log("正在关闭 HTTP 服务…")
        server.shutdown()
        server.server_close()
        log("HTTP 服务已关闭")
    return 0


if __name__ == "__main__":
    sys.exit(main())
