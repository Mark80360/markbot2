#!/usr/bin/env python3
"""飞书渠道：飞书事件 → MarkBot HTTP /chat → 飞书消息。

功能参考 nanobot 的飞书渠道：
- 文本 / 富文本(post) / 卡片(interactive) / 分享卡片解析；
- 图片、文件、语音下载到本地，路径写进提示词交给 agent；
- 收到消息先加表情，处理完成后替换为完成表情；
- 可选引用原消息（群聊内以话题回复）；
- 按 Markdown 复杂度选择 text / post / 卡片输出，卡片失败回退纯文本；
- 群聊 @ 策略、发送者白名单、群内话题级会话隔离。

设计：渠道是独立进程，只通过 HTTP 调用 `markbot serve`；唯一的例外是日志，
直接复用主 agent 的 `markbot.kernel.gateway_log`，不另起一套。
配置统一放在 ~/.markbot/config.toml：

    [channels.feishu]
    mode = "longconn"          # longconn（推荐，无需公网）| webhook
    app_id = "cli_xxx"
    verification_token = "xxx" # 仅 webhook 模式需要
    group_policy = "mention"   # mention（群聊必须 @）| open（群聊全部响应）
    ...

    [secrets]
    FEISHU_APP_SECRET = "xxx"

启动：

    python channels/feishu/channel.py
    python channels/feishu/channel.py --check

日志：复用主 agent 的 gateway_log，写入 `~/.markbot/gateway/YYYY-MM-DD.log`，
同时输出到 stdout；若 stdout 已被重定向到同一文件（`markbot gateway start` 的做法），
则只写一次，避免重复。

依赖：Python 3.11+ 标准库（tomllib）。长连接模式额外需要 `pip install lark-oapi`。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import queue
import re
import sqlite3
import sys
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 日志直接复用主 agent 的 gateway_log，不另起一套
try:
    from markbot.kernel import gateway_log
except ImportError:  # 未 `pip install -e .` 时回退到仓库源码
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from markbot.kernel import gateway_log

FEISHU_BASE = "https://open.feishu.cn"
LARK_BASE = "https://open.larksuite.com"
TEXT_LIMIT = 3500
# 单张卡片内容过长时先拆成多条，避免超出飞书卡片体积上限。
CARD_TEXT_LIMIT = 12000
CONFIRM_TTL_S = 180.0


def log_path() -> Path:
    """日志文件与主 agent 相同：~/.markbot/gateway/YYYY-MM-DD.log。"""
    return gateway_log.gateway_log_path(markbot_home())


def _stdout_is_file(path: Path) -> bool:
    """stdout 是否已经指向 path（被外部重定向），用于避免重复落盘。"""
    try:
        out = os.fstat(sys.stdout.fileno())
        target = os.stat(path)
    except (OSError, ValueError, AttributeError):
        return False
    return (out.st_dev, out.st_ino) == (target.st_dev, target.st_ino)


def log(message: str) -> None:
    """经 gateway_log 落盘，同时输出到控制台。"""
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] [feishu] {message}"
    if not _stdout_is_file(log_path()):
        gateway_log.write(markbot_home(), f"[feishu] {message}")
    print(line, flush=True)


def _preview(text: str, limit: int = 60) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= limit:
        return flat
    return f"{flat[:limit]}…(共{len(flat)}字)"


def markbot_home() -> Path:
    return Path(os.environ.get("MARKBOT_HOME") or Path.home() / ".markbot")


def default_config_path() -> Path:
    return markbot_home() / "config.toml"


def _string_list(value: Any) -> tuple[str, ...]:
    """allow_from 既支持 TOML 数组，也支持逗号分隔字符串。"""
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        return ()
    return tuple(item.strip() for item in items if item.strip())


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    verification_token: str
    host: str
    port: int
    path: str
    markbot_url: str
    markbot_timeout_s: float
    group_policy: str
    reply_to_message: bool
    topic_isolation: bool
    react_emoji: str
    done_emoji: str
    allow_from: tuple[str, ...]
    media_dir: Path
    session_db: Path
    feishu_base: str = FEISHU_BASE
    mode: str = "webhook"

    @property
    def require_mention(self) -> bool:
        """兼容旧配置语义：群聊是否必须 @ 机器人。"""
        return self.group_policy == "mention"


def load_settings(config_path: Path | None = None) -> Settings:
    path = Path(config_path) if config_path else default_config_path()
    if not path.exists():
        raise SystemExit(
            f"配置文件不存在: {path}\n"
            "请先运行 `markbot init`，或在 config.toml 中添加 [channels.feishu]。"
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    channels = data.get("channels") or {}
    section = channels.get("feishu") or {}
    secrets = data.get("secrets") or {}

    app_id = str(section.get("app_id") or "").strip()
    app_secret = str(
        section.get("app_secret") or secrets.get("FEISHU_APP_SECRET") or ""
    ).strip()
    if not app_id or not app_secret:
        raise SystemExit(
            f"{path} 缺少飞书配置：需要 [channels.feishu].app_id 与 "
            "[secrets].FEISHU_APP_SECRET（或 [channels.feishu].app_secret）。"
        )

    db = Path(
        str(section.get("session_db") or markbot_home() / "channels/feishu/feishu.db")
    ).expanduser()
    media_dir = Path(
        str(section.get("media_dir") or markbot_home() / "media/feishu")
    ).expanduser()
    mode = str(section.get("mode") or "webhook").strip().lower()
    if mode in ("longconn", "long_connection", "websocket", "ws"):
        mode = "longconn"
    elif mode != "webhook":
        raise SystemExit(f"{path} 的 [channels.feishu].mode 只能是 longconn 或 webhook")

    # 新配置优先；未写 group_policy 时沿用旧的 require_mention。
    group_policy = str(section.get("group_policy") or "").strip().lower()
    if not group_policy:
        group_policy = "mention" if bool(section.get("require_mention", True)) else "open"
    if group_policy not in ("mention", "open"):
        raise SystemExit(
            f"{path} 的 [channels.feishu].group_policy 只能是 mention 或 open"
        )

    domain = str(section.get("domain") or "feishu").strip().lower()
    if domain not in ("feishu", "lark"):
        raise SystemExit(f"{path} 的 [channels.feishu].domain 只能是 feishu 或 lark")
    base = str(
        section.get("feishu_base")
        or (LARK_BASE if domain == "lark" else FEISHU_BASE)
    ).rstrip("/")

    return Settings(
        app_id=app_id,
        app_secret=app_secret,
        verification_token=str(section.get("verification_token") or "").strip(),
        host=str(section.get("host") or "0.0.0.0"),
        port=int(section.get("port") or 8790),
        path=str(section.get("path") or "/feishu/event"),
        markbot_url=str(section.get("markbot_url") or "http://127.0.0.1:8787"),
        markbot_timeout_s=float(section.get("markbot_timeout_s") or 300),
        group_policy=group_policy,
        reply_to_message=bool(section.get("reply_to_message", False)),
        topic_isolation=bool(section.get("topic_isolation", True)),
        react_emoji=str(section.get("react_emoji") or "THUMBSUP").strip(),
        done_emoji=str(section.get("done_emoji") or "").strip(),
        allow_from=_string_list(section.get("allow_from")),
        media_dir=media_dir,
        session_db=db,
        feishu_base=base,
        mode=mode,
    )


class Store:
    """消息去重与会话映射。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, seen_at TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "chat_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS grants ("
            "chat_key TEXT NOT NULL, program TEXT NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY (chat_key, program))"
        )
        self._conn.commit()

    def first_seen(self, key: str) -> bool:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO seen(key, seen_at) VALUES (?, ?)", (key, now)
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def get_session(self, chat_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id FROM sessions WHERE chat_key = ?", (chat_key,)
            ).fetchone()
        return row[0] if row else None

    def set_session(self, chat_key: str, session_id: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(chat_key, session_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_key) DO UPDATE SET session_id = excluded.session_id, "
                "updated_at = excluded.updated_at",
                (chat_key, session_id, now),
            )
            self._conn.commit()

    def clear_session(self, chat_key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE chat_key = ?", (chat_key,))
            self._conn.commit()

    def add_grants(self, chat_key: str, programs: list[str]) -> list[str]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        added: list[str] = []
        with self._lock:
            for program in programs:
                name = program.strip().lower()
                if not name:
                    continue
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO grants(chat_key, program, created_at) VALUES (?, ?, ?)",
                    (chat_key, name, now),
                )
                if cursor.rowcount == 1:
                    added.append(name)
            self._conn.commit()
        return added

    def list_grants(self, chat_key: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT program FROM grants WHERE chat_key = ? ORDER BY program", (chat_key,)
            ).fetchall()
        return [row[0] for row in rows]

    def remove_grants(self, chat_key: str, programs: list[str] | None = None) -> None:
        with self._lock:
            if programs:
                for program in programs:
                    self._conn.execute(
                        "DELETE FROM grants WHERE chat_key = ? AND program = ?",
                        (chat_key, program.strip().lower()),
                    )
            else:
                self._conn.execute("DELETE FROM grants WHERE chat_key = ?", (chat_key,))
            self._conn.commit()

    def prune_seen(self, days: int = 7) -> None:
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._lock:
            self._conn.execute("DELETE FROM seen WHERE seen_at < ?", (cutoff,))
            self._conn.commit()


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: str | None = None
        self._expires_at = 0.0
        self._bot_open_id: str | None = None
        self._lock = threading.Lock()

    def _api(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth: bool = True,
        retry: int = 1,
    ) -> dict[str, Any]:
        url = self._settings.feishu_base + path
        for attempt in range(retry + 1):
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if auth:
                headers["Authorization"] = f"Bearer {self.token()}"
            body = (
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None
                else None
            )
            request = urllib.request.Request(
                url, data=body, headers=headers, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw_body = response.read().decode("utf-8")
                data = json.loads(raw_body) if raw_body.strip() else {"code": 0}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    data = json.loads(raw)
                except ValueError:
                    data = {"code": exc.code, "msg": raw}
                log(
                    f"飞书接口 HTTP 异常 {method} {path} "
                    f"status={exc.code} body={_preview(raw, 200)}"
                )
            except OSError as exc:
                if attempt < retry:
                    log(f"飞书接口网络错误 {method} {path}: {exc}，准备重试（第 {attempt + 1} 次）")
                    time.sleep(1 + attempt)
                    continue
                log(f"飞书接口网络错误 {method} {path}: {exc}，放弃")
                raise RuntimeError(f"飞书接口网络错误: {exc}") from exc

            code = data.get("code", 0)
            if code in (99991661, 99991663, 99991668) and auth and attempt < retry:
                log(f"飞书 token 失效（code={code}），刷新后重试 {method} {path}")
                with self._lock:
                    self._token = None
                continue
            if code not in (0, None):
                log(f"飞书接口错误 {method} {path} code={code} msg={data.get('msg')}")
                raise RuntimeError(f"飞书接口错误 {code}: {data.get('msg')}")
            return data
        raise RuntimeError("飞书接口调用失败")

    def token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            data = self._api(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                {
                    "app_id": self._settings.app_id,
                    "app_secret": self._settings.app_secret,
                },
                auth=False,
            )
            self._token = str(data["tenant_access_token"])
            self._expires_at = time.time() + int(data.get("expire") or 7200)
            log(f"已获取 tenant_access_token，有效期 {int(data.get('expire') or 7200)} 秒")
            return self._token

    def bot_open_id(self) -> str:
        """机器人自己的 open_id，用于识别并去掉消息里的 @机器人。"""
        with self._lock:
            if self._bot_open_id is not None:
                return self._bot_open_id
        open_id = ""
        try:
            data = self._api("GET", "/open-apis/bot/v3/info")
            bot = (data.get("data") or {}).get("bot") or {}
            open_id = str(bot.get("open_id") or "")
        except RuntimeError as exc:
            log(f"获取机器人 open_id 失败（不影响收发）: {exc}")
        with self._lock:
            self._bot_open_id = open_id
        return open_id

    # ---------------------------------------------------------------- 发送
    def send_message(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> str | None:
        data = self._api(
            "POST",
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            {"receive_id": receive_id, "msg_type": msg_type, "content": content},
        )
        message_id = (data.get("data") or {}).get("message_id")
        return str(message_id) if message_id else None

    def reply_message(
        self,
        parent_message_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        payload: dict[str, Any] = {"msg_type": msg_type, "content": content}
        if reply_in_thread:
            payload["reply_in_thread"] = True
        data = self._api(
            "POST",
            f"/open-apis/im/v1/messages/{parent_message_id}/reply",
            payload,
        )
        message_id = (data.get("data") or {}).get("message_id")
        return str(message_id) if message_id else None

    def send_answer(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> None:
        """按内容复杂度选择 text / post / 卡片发送；卡片失败自动回退文本。"""
        text = (text or "").strip()
        if not text:
            return
        fmt = _detect_msg_format(text)
        chunks = (
            _split_text(text) if fmt == "interactive" and len(text) > CARD_TEXT_LIMIT else [text]
        )
        first = True
        for chunk in chunks:
            if fmt == "interactive":
                for elements in _split_elements_by_table_limit(_build_card_elements(chunk)):
                    card = json.dumps(
                        {"config": {"wide_screen_mode": True}, "elements": elements},
                        ensure_ascii=False,
                    )
                    target = reply_to if first else None
                    self._deliver(
                        chat_id,
                        "interactive",
                        card,
                        target,
                        reply_in_thread if target else False,
                    )
                    first = False
                continue
            for part in _split_text(chunk):
                if fmt == "post":
                    content = _markdown_to_post(part)
                    msg_type = "post"
                else:
                    content = json.dumps({"text": part}, ensure_ascii=False)
                    msg_type = "text"
                target = reply_to if first else None
                self._deliver(
                    chat_id,
                    msg_type,
                    content,
                    target,
                    reply_in_thread if target else False,
                )
                first = False

    def _deliver(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
        reply_to: str | None,
        reply_in_thread: bool,
    ) -> None:
        try:
            if reply_to:
                message_id = self.reply_message(
                    reply_to, msg_type, content, reply_in_thread=reply_in_thread
                )
                log(
                    f"飞书消息已回复 parent={reply_to} type={msg_type} "
                    f"message_id={message_id or '-'} 长度={len(content)}"
                )
            else:
                message_id = self.send_message("chat_id", chat_id, msg_type, content)
                log(
                    f"飞书消息已发送 chat={chat_id} type={msg_type} "
                    f"message_id={message_id or '-'} 长度={len(content)}"
                )
        except RuntimeError as exc:
            if msg_type != "interactive":
                log(f"发送失败 chat={chat_id} type={msg_type}: {exc}")
                return
            log(f"卡片发送失败，回退纯文本 chat={chat_id}: {exc}")
            for chunk in _split_text(_card_to_text(content)):
                try:
                    if reply_to:
                        self.reply_message(
                            reply_to,
                            "text",
                            json.dumps({"text": chunk}, ensure_ascii=False),
                            reply_in_thread=reply_in_thread,
                        )
                        reply_to = None
                    else:
                        self.send_message(
                            "chat_id",
                            chat_id,
                            "text",
                            json.dumps({"text": chunk}, ensure_ascii=False),
                        )
                except RuntimeError as inner:
                    log(f"回退文本也失败 chat={chat_id}: {inner}")
                    return

    # ---------------------------------------------------------------- 表情
    def add_reaction(self, message_id: str, emoji_type: str) -> str | None:
        if not emoji_type:
            return None
        try:
            data = self._api(
                "POST",
                f"/open-apis/im/v1/messages/{message_id}/reactions",
                {"reaction_type": {"emoji_type": emoji_type}},
            )
        except RuntimeError as exc:
            log(f"添加表情失败 message={message_id} emoji={emoji_type}: {exc}")
            return None
        reaction_id = (data.get("data") or {}).get("reaction_id")
        return str(reaction_id) if reaction_id else None

    def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        try:
            self._api(
                "DELETE",
                f"/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
            )
        except RuntimeError as exc:
            log(f"移除表情失败 message={message_id} reaction={reaction_id}: {exc}")

    # ---------------------------------------------------------------- 拉取
    def message_text(self, message_id: str) -> str | None:
        """读取被引用消息的文本，作为回复上下文。"""
        try:
            data = self._api("GET", f"/open-apis/im/v1/messages/{message_id}")
        except RuntimeError as exc:
            log(f"读取引用消息失败 message={message_id}: {exc}")
            return None
        items = (data.get("data") or {}).get("items") or []
        if not items:
            return None
        item = items[0] if isinstance(items[0], dict) else {}
        raw = (item.get("body") or {}).get("content") or ""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        text = _content_text(str(item.get("msg_type") or ""), payload).strip()
        if not text:
            return None
        if len(text) > 200:
            text = text[:200] + "..."
        return f"[Reply to: {text}]"

    def download_resource(
        self, message_id: str, file_key: str, resource_type: str
    ) -> tuple[bytes | None, str | None]:
        """下载消息里的图片/文件；type 只接受 image 与 file。"""
        if resource_type not in ("image", "file"):
            resource_type = "file"
        path = (
            f"/open-apis/im/v1/messages/{message_id}/resources/{file_key}"
            f"?type={resource_type}"
        )
        request = urllib.request.Request(
            self._settings.feishu_base + path,
            headers={"Authorization": f"Bearer {self.token()}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                filename = _filename_from_headers(response.headers)
                return payload, filename
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            log(
                f"下载飞书资源失败 status={exc.code} key={file_key} "
                f"body={_preview(raw, 160)}"
            )
        except OSError as exc:
            log(f"下载飞书资源网络错误 key={file_key}: {exc}")
        return None, None


# ---------------------------------------------------------------- 纯文本工具
_YES_WORDS = {"1", "y", "yes", "ok", "允许", "同意", "可以"}
_NO_WORDS = {"2", "n", "no", "拒绝", "不行", "取消"}
# 3：本轮问答内后续确认一律放行，回合结束后失效
_ALL_WORDS = {"3", "all", "全部", "都允许", "不再确认", "本轮不再确认"}

MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [""]


def _filename_from_headers(headers: Any) -> str | None:
    raw = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", raw, re.IGNORECASE)
    if match:
        return urllib.parse.unquote(match.group(1).strip().strip('"'))
    match = re.search(r'filename="?([^";]+)"?', raw, re.IGNORECASE)
    if match:
        return urllib.parse.unquote(match.group(1).strip())
    return None


def _safe_filename(name: str | None, fallback: str) -> str:
    """下载落盘用的安全文件名，避免飞书返回的路径穿越写法。"""
    candidate = os.path.basename((name or fallback).replace("\\", "/"))
    candidate = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", candidate).strip(" .")
    if candidate in ("", ".", ".."):
        candidate = re.sub(r"[^\w.-]", "_", fallback).strip(" .") or "file"
    return candidate


def _failure_text(exc: Exception, elapsed: float) -> str:
    """失败时给用户的回复：超时单独说明，其余带上异常类型，避免“只显示空白错误”。"""
    detail = str(exc).strip()
    if isinstance(exc, TimeoutError) or "timed out" in detail.lower():
        return (
            f"处理失败：模型服务响应超时（已等待 {elapsed:.0f} 秒）。\n"
            "请检查模型服务是否可用，详情见服务端日志。"
        )
    if not detail:
        return f"处理失败：{type(exc).__name__}"
    return f"处理失败：{type(exc).__name__}: {detail}"


# ---------------------------------------------------------------- 消息解析
def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _as_list(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _extract_share_card_content(content_json: dict[str, Any], msg_type: str) -> str:
    parts: list[str] = []
    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")
    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: str | dict[str, Any]) -> list[str]:
    parts: list[str] = []
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []
    if not isinstance(content, dict):
        return parts

    # user_dsl 是卡片原始定义，信息最全。
    user_dsl = content.get("user_dsl")
    if isinstance(user_dsl, str) and user_dsl.strip():
        try:
            dsl = _as_dict(json.loads(user_dsl))
        except (json.JSONDecodeError, TypeError):
            dsl = None
        if dsl is not None:
            parts.extend(_extract_interactive_content(dsl))
            if parts:
                return parts

    title = content.get("title")
    if isinstance(title, dict):
        title_content = title.get("content") or title.get("text") or ""
        if title_content:
            parts.append(f"title: {title_content}")
    elif isinstance(title, str) and title:
        parts.append(f"title: {title}")

    elements = _as_list(content.get("elements"))
    if elements is not None:
        if elements and isinstance(elements[0], list):
            for row in elements:
                for element in _as_list(row) or []:
                    parts.extend(_extract_element_content(element))
        else:
            for element in elements:
                parts.extend(_extract_element_content(element))

    body = _as_dict(content.get("body"))
    if body is not None:
        for element in _as_list(body.get("elements")) or []:
            parts.extend(_extract_element_content(element))

    card = _as_dict(content.get("card"))
    if card:
        parts.extend(_extract_interactive_content(card))

    header = _as_dict(content.get("header"))
    if header is not None:
        header_title = _as_dict(header.get("title"))
        if header_title is not None:
            header_text = header_title.get("content") or header_title.get("text") or ""
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: Any) -> list[str]:
    parts: list[str] = []
    element = _as_dict(element)
    if element is None:
        return parts
    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)
    elif tag == "text":
        text = element.get("text", "")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in _as_list(element.get("fields")) or []:
            field_object = _as_dict(field)
            if field_object is None:
                continue
            field_text = _as_dict(field_object.get("text", {}))
            if field_text is not None and field_text.get("content"):
                parts.append(str(field_text["content"]))
    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)
    elif tag == "button":
        text = _as_dict(element.get("text", {}))
        if text is not None and text.get("content"):
            parts.append(str(text["content"]))
        multi_url = _as_dict(element.get("multi_url")) or {}
        url = element.get("url", "") or multi_url.get("url", "")
        if url:
            parts.append(f"link: {url}")
    elif tag == "img":
        alt = _as_dict(element.get("alt", {}))
        parts.append(alt.get("content", "[image]") if alt is not None else "[image]")
    elif tag == "note":
        for item in _as_list(element.get("elements")) or []:
            parts.extend(_extract_element_content(item))
    elif tag == "column_set":
        for column in _as_list(element.get("columns")) or []:
            column_object = _as_dict(column)
            if column_object is None:
                continue
            for item in _as_list(column_object.get("elements")) or []:
                parts.extend(_extract_element_content(item))
    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)
    elif tag == "table":
        columns: list[tuple[str, str]] = []
        for column in _as_list(element.get("columns")) or []:
            column_object = _as_dict(column)
            if column_object is None:
                continue
            name = column_object.get("name")
            if isinstance(name, str) and name:
                columns.append((name, str(column_object.get("display_name") or name)))
        if columns:
            parts.append(" | ".join(header for _, header in columns))
        for row in _as_list(element.get("rows")) or []:
            row_object = _as_dict(row)
            if row_object is None:
                continue
            values: list[str] = []
            for name, _ in columns:
                value = row_object.get(name)
                if isinstance(value, list):
                    value = " ".join(str(item).strip() for item in value if item is not None)
                values.append("" if value is None else str(value).strip())
            row_text = " | ".join(values).strip()
            if row_text:
                parts.append(row_text)
    else:
        for item in _as_list(element.get("elements")) or []:
            parts.extend(_extract_element_content(item))

    return parts


def _extract_post_content(content_json: dict[str, Any]) -> tuple[str, list[str]]:
    def _parse_block(block: dict[str, Any]) -> tuple[str | None, list[str]]:
        content = _as_list(block.get("content"))
        if content is None:
            return None, []
        texts: list[str] = []
        images: list[str] = []
        title = block.get("title")
        if isinstance(title, str) and title:
            texts.append(title)
        for row in content:
            for el in _as_list(row) or []:
                element = _as_dict(el)
                if element is None:
                    continue
                tag = element.get("tag")
                if tag in ("text", "a"):
                    text = element.get("text", "")
                    if isinstance(text, str):
                        texts.append(text)
                elif tag == "at":
                    user = element.get("user_name", "user")
                    texts.append(f"@{user if isinstance(user, str) and user else 'user'}")
                elif tag == "code_block":
                    lang = element.get("language", "")
                    code_text = element.get("text", "")
                    if not isinstance(lang, str):
                        lang = ""
                    if not isinstance(code_text, str):
                        code_text = ""
                    texts.append(f"\n```{lang}\n{code_text}\n```\n")
                elif tag == "img" and isinstance(element.get("image_key"), str):
                    images.append(str(element["image_key"]))
        return (" ".join(texts).strip() or None), images

    root = content_json
    post = _as_dict(root.get("post"))
    if post is not None:
        root = post

    if "content" in root:
        text, images = _parse_block(root)
        if text or images:
            return text or "", images

    for key in ("zh_cn", "en_us", "ja_jp"):
        block = _as_dict(root.get(key))
        if block is None:
            continue
        text, images = _parse_block(block)
        if text or images:
            return text or "", images
    for value in root.values():
        block = _as_dict(value)
        if block is not None:
            text, images = _parse_block(block)
            if text or images:
                return text or "", images
    return "", []


def _extract_post_attachments(
    content_json: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """富文本里的 files 附件；文件夹只回传名字，提示用户压缩后再发。"""

    def _files_in(block: Any) -> tuple[list[dict[str, Any]], list[str]] | None:
        obj = _as_dict(block)
        if obj is None or "files" not in obj:
            return None
        files: list[dict[str, Any]] = []
        folders: list[str] = []
        for item in _as_list(obj.get("files")) or []:
            entry = _as_dict(item)
            if entry is None:
                continue
            if entry.get("is_folder") is True:
                name = entry.get("file_name")
                folders.append(name if isinstance(name, str) and name else "folder")
            elif isinstance(entry.get("file_key"), str) and entry["file_key"]:
                files.append(entry)
        return files, folders

    root = content_json
    post = _as_dict(root.get("post"))
    if post is not None:
        root = post

    found = _files_in(root)
    if found is not None:
        return found

    for key in ("zh_cn", "en_us", "ja_jp"):
        found = _files_in(root.get(key))
        if found is not None:
            return found
    for value in root.values():
        found = _files_in(value)
        if found is not None:
            return found
    return [], []


def _content_text(msg_type: str, payload: dict[str, Any]) -> str:
    if msg_type == "text":
        text = payload.get("text", "")
        return text if isinstance(text, str) else ""
    if msg_type == "post":
        return _extract_post_content(payload)[0]
    return ""


def _mention_open_id(mention: dict[str, Any]) -> str:
    return str((_as_dict(mention.get("id")) or {}).get("open_id") or "")


def _strip_mention_keys(text: str, mentions: list[dict[str, Any]]) -> str:
    for mention in mentions:
        key = str(mention.get("key") or "")
        if key:
            text = text.replace(key, "")
    return re.sub(r"\s+", " ", text).strip()


def _strip_leading_bot_mention(
    text: str, mentions: list[dict[str, Any]], bot_open_id: str
) -> str:
    """去掉开头的 @机器人，避免它进入提示词。"""
    if not mentions or not text or not bot_open_id:
        return text
    candidate = text.lstrip()
    for mention in mentions:
        key = str(mention.get("key") or "")
        if not key or not re.match(rf"{re.escape(key)}(?![A-Za-z0-9_])", candidate):
            continue
        if _mention_open_id(mention) != bot_open_id:
            continue
        return candidate[len(key):].strip() or text
    return text


def _resolve_mentions(text: str, mentions: list[dict[str, Any]]) -> str:
    """把 @_user_n 占位符换成 @姓名 (open_id)，让模型知道被 @ 的是谁。"""
    if not mentions or not text:
        return text
    for mention in mentions:
        key = str(mention.get("key") or "")
        if not key:
            continue
        pattern = rf"{re.escape(key)}(?![A-Za-z0-9_])"
        if not re.search(pattern, text):
            continue
        mention_id = _as_dict(mention.get("id")) or {}
        open_id = str(mention_id.get("open_id") or "")
        user_id = str(mention_id.get("user_id") or "")
        name = str(mention.get("name") or key)
        if open_id and user_id:
            replacement = f"@{name} ({open_id}, user id: {user_id})"
        elif open_id:
            replacement = f"@{name} ({open_id})"
        else:
            replacement = f"@{name}"
        text = re.sub(pattern, replacement, text)
    return text


def _is_bot_mentioned(
    raw_content: str, mentions: list[dict[str, Any]], bot_open_id: str
) -> bool:
    if "@_all" in raw_content:
        return True
    if not mentions:
        return False
    if bot_open_id:
        return any(_mention_open_id(item) == bot_open_id for item in mentions)
    return True


# ---------------------------------------------------------------- Markdown → 飞书
_TABLE_RE = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~")
_COMPLEX_MD_RE = re.compile(
    r"```|^\|.+\|.*\n\s*\|[-:\s|]+\||^#{1,6}\s+", re.MULTILINE
)
_SIMPLE_MD_RE = re.compile(
    r"\*\*.+?\*\*|__.+?__|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|~~.+?~~", re.DOTALL
)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
_OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)


def _strip_md_formatting(text: str) -> str:
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    return text


def _parse_md_table(table_text: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in table_text.strip().split("\n") if line.strip()]
    if len(lines) < 3:
        return None

    def split(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    headers = [_strip_md_formatting(header) for header in split(lines[0])]
    rows = [[_strip_md_formatting(cell) for cell in split(line)] for line in lines[2:]]
    columns = [
        {"tag": "column", "name": f"c{i}", "display_name": header, "width": "auto"}
        for i, header in enumerate(headers)
    ]
    return {
        "tag": "table",
        "page_size": len(rows) + 1,
        "columns": columns,
        "rows": [
            {f"c{i}": row[i] if i < len(row) else "" for i in range(len(headers))}
            for row in rows
        ],
    }


def _split_headings(content: str) -> list[dict[str, Any]]:
    protected = content
    code_blocks: list[str] = []
    for match in _CODE_BLOCK_RE.finditer(content):
        code_blocks.append(match.group(1))
        protected = protected.replace(match.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

    elements: list[dict[str, Any]] = []
    last_end = 0
    for match in _HEADING_RE.finditer(protected):
        before = protected[last_end : match.start()].strip()
        if before:
            elements.append({"tag": "markdown", "content": before})
        text = _strip_md_formatting(match.group(2).strip())
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**{text}**" if text else ""},
            }
        )
        last_end = match.end()
    remaining = protected[last_end:].strip()
    if remaining:
        elements.append({"tag": "markdown", "content": remaining})

    for index, block in enumerate(code_blocks):
        for element in elements:
            if element.get("tag") == "markdown":
                element["content"] = element["content"].replace(f"\x00CODE{index}\x00", block)
    return elements or [{"tag": "markdown", "content": content}]


def _build_card_elements(content: str) -> list[dict[str, Any]]:
    protected = content
    code_blocks: list[str] = []
    for match in _CODE_BLOCK_RE.finditer(content):
        code_blocks.append(match.group(1))
        protected = protected.replace(match.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

    elements: list[dict[str, Any]] = []
    last_end = 0
    for match in _TABLE_RE.finditer(protected):
        before = protected[last_end : match.start()]
        if before.strip():
            elements.extend(_split_headings(before))
        elements.append(
            _parse_md_table(match.group(1)) or {"tag": "markdown", "content": match.group(1)}
        )
        last_end = match.end()
    remaining = protected[last_end:]
    if remaining.strip():
        elements.extend(_split_headings(remaining))

    for index, block in enumerate(code_blocks):
        for element in elements:
            if element.get("tag") == "markdown":
                element["content"] = element["content"].replace(f"\x00CODE{index}\x00", block)
    return elements or [{"tag": "markdown", "content": content}]


def _split_elements_by_table_limit(
    elements: list[dict[str, Any]], max_tables: int = 1
) -> list[list[dict[str, Any]]]:
    """飞书卡片每张最多一个表格（错误码 11310），超出就拆成多条。"""
    if not elements:
        return [[]]
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    table_count = 0
    for element in elements:
        if element.get("tag") == "table":
            if table_count >= max_tables:
                if current:
                    groups.append(current)
                current = []
                table_count = 0
            current.append(element)
            table_count += 1
        else:
            current.append(element)
    if current:
        groups.append(current)
    return groups or [[]]


def _detect_msg_format(content: str) -> str:
    """text：短纯文本；post：含链接的中等长度；interactive：Markdown 卡片。"""
    stripped = content.strip()
    if _COMPLEX_MD_RE.search(stripped):
        return "interactive"
    if len(stripped) > 2000:
        return "interactive"
    if _SIMPLE_MD_RE.search(stripped):
        return "interactive"
    if _LIST_RE.search(stripped) or _OLIST_RE.search(stripped):
        return "interactive"
    if _MD_LINK_RE.search(stripped):
        return "post"
    if len(stripped) <= 200:
        return "text"
    return "post"


def _markdown_to_post(content: str) -> str:
    paragraphs: list[list[dict[str, Any]]] = []
    for line in content.strip().split("\n"):
        elements: list[dict[str, Any]] = []
        last_end = 0
        for match in _MD_LINK_RE.finditer(line):
            before = line[last_end : match.start()]
            if before:
                elements.append({"tag": "text", "text": before})
            elements.append({"tag": "a", "text": match.group(1), "href": match.group(2)})
            last_end = match.end()
        remaining = line[last_end:]
        if remaining:
            elements.append({"tag": "text", "text": remaining})
        if not elements:
            elements.append({"tag": "text", "text": ""})
        paragraphs.append(elements)
    return json.dumps({"zh_cn": {"content": paragraphs}}, ensure_ascii=False)


def _card_to_text(content: str) -> str:
    """卡片发送失败时的兜底：把卡片元素还原成纯文本。"""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return content
    elements = _as_list(payload.get("elements")) or []
    parts: list[str] = []
    for element in elements:
        element = _as_dict(element)
        if element is None:
            continue
        tag = element.get("tag")
        if tag == "markdown":
            parts.append(str(element.get("content") or ""))
        elif tag == "div":
            text = element.get("text")
            if isinstance(text, dict):
                parts.append(str(text.get("content") or text.get("text") or ""))
            elif isinstance(text, str):
                parts.append(text)
        elif tag == "table":
            columns = [
                column
                for column in (_as_dict(item) for item in _as_list(element.get("columns")) or [])
                if column is not None
            ]
            if columns:
                parts.append(" | ".join(str(column.get("display_name") or "") for column in columns))
            for row in _as_list(element.get("rows")) or []:
                row = _as_dict(row) or {}
                parts.append(
                    " | ".join(str(row.get(str(column.get("name") or "")) or "") for column in columns)
                )
    return "\n\n".join(part for part in parts if part.strip()) or content


def _verify_token(settings: Settings, data: dict[str, Any]) -> bool:
    expected = settings.verification_token
    if not expected:
        return True
    header = data.get("header") or {}
    token = str(header.get("token") or data.get("token") or "")
    return token == expected


class Gateway:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._client = FeishuClient(settings)
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        # chat_id -> (token, 过期时间, 发起人的 open_id)
        self._pending: dict[str, tuple[str, float, str]] = {}
        # 本轮问答内已选择“不再重复确认”的 chat_id，回合结束即清除
        self._auto_approve: set[str] = set()
        self._pending_lock = threading.Lock()

    @property
    def confirm_hook(self) -> str:
        # 监听 0.0.0.0 时客户端要用回环地址才能连上自己
        host = self._settings.host
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self._settings.port}/confirm-request"

    @property
    def notify_hook(self) -> str:
        """后台流程（如定时任务）主动发消息的本地回调地址。"""
        host = self._settings.host
        if host in ("", "0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self._settings.port}/notify"

    def request_confirmation(self, payload: dict[str, Any]) -> bool:
        """服务端要求用户在聊天里确认时调用：发消息并登记 token。"""
        chat_id = str(payload.get("chat_id") or "")
        token = str(payload.get("token") or "")
        if not chat_id or not token:
            return False
        now = time.monotonic()
        user_id = str(payload.get("user_id") or "")
        with self._pending_lock:
            auto = chat_id in self._auto_approve
            if not auto:
                for key, (_, expire_at, _user) in list(self._pending.items()):
                    if expire_at < now:
                        self._pending.pop(key, None)
                self._pending[chat_id] = (token, now + CONFIRM_TTL_S, user_id)
        if auto:
            log(f"本轮已选择不再确认，自动放行 chat={chat_id} token={token[:8]}")
            self._send_confirm(token, True)
            return True
        log(f"请求用户确认 chat={chat_id} token={token[:8]}")
        self._reply(
            chat_id,
            f"【需要授权】{payload.get('title') or '执行命令'}\n"
            f"{payload.get('message') or ''}\n\n"
            f"回复 1 允许，回复 2 拒绝，回复 3 本轮不再确认"
            f"（{int(CONFIRM_TTL_S // 60)} 分钟内有效）",
        )
        return True

    def intercept_confirmation(self, data: dict[str, Any]) -> bool:
        """识别“1/2”这类确认回复并在收事件线程里直接处理，避免阻塞工作线程。"""
        header = data.get("header") or {}
        if str(header.get("event_type") or "") != "im.message.receive_v1":
            return False
        event = data.get("event") or {}
        message = event.get("message") or {}
        if (event.get("sender") or {}).get("sender_type") == "app":
            return False
        if str(message.get("message_type") or "") != "text":
            return False
        chat_id = str(message.get("chat_id") or "")
        with self._pending_lock:
            entry = self._pending.get(chat_id)
        if not entry:
            return False
        token, expire_at, user_id = entry
        # 群里只认发起人的回复，避免其他人替人拍板
        sender = ((event.get("sender") or {}).get("sender_id") or {}).get("open_id") or ""
        if user_id and sender and sender != user_id:
            return False
        try:
            payload = json.loads(message.get("content") or "{}")
        except ValueError:
            return False
        mentions = message.get("mentions") or []
        text = _strip_mention_keys(str(payload.get("text") or ""), mentions).lower()
        approve_all = text in _ALL_WORDS
        approved = approve_all or text in _YES_WORDS
        if not approved and text not in _NO_WORDS:
            return False
        with self._pending_lock:
            self._pending.pop(chat_id, None)
        if time.monotonic() > expire_at:
            self._reply(chat_id, "确认已过期，请重新发起请求。")
            return True
        if approve_all:
            with self._pending_lock:
                self._auto_approve.add(chat_id)
        self._send_confirm(token, approved)
        if approve_all:
            self._reply(chat_id, "已允许，本轮后续操作不再询问。")
        else:
            self._reply(chat_id, "已允许，继续执行…" if approved else "已拒绝执行。")
        return True

    def _clear_auto_approve(self, chat_id: str) -> None:
        with self._pending_lock:
            self._auto_approve.discard(chat_id)

    def _send_confirm(self, token: str, approved: bool) -> None:
        url = self._settings.markbot_url.rstrip("/") + "/confirm"
        body = json.dumps({"token": token, "approved": approved}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            log(f"确认结果已提交 token={token[:8]} approved={approved}")
        except Exception as exc:  # noqa: BLE001 - 失败只记录，不能影响收消息
            log(f"提交确认结果失败 token={token[:8]}: {type(exc).__name__}: {exc}")

    def enqueue(self, data: dict[str, Any]) -> bool:
        header = data.get("header") or {}
        event_id = str(header.get("event_id") or "")
        event_type = str(header.get("event_type") or "")
        log(f"接收到事件 event_type={event_type or '-'} event_id={event_id or '-'}")
        if event_id and not self._store.first_seen(f"event:{event_id}"):
            log(f"忽略重复事件 {event_id}")
            return False
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            log(f"队列已满（当前 {self.queue.qsize()}），丢弃事件 event_id={event_id or '-'}")
            return False
        log(f"事件已入队 event_id={event_id or '-'} 队列长度={self.queue.qsize()}")
        return True

    def worker(self) -> None:
        while True:
            data = self.queue.get()
            try:
                self.process(data)
            except Exception:  # noqa: BLE001 - 单个事件失败不退出
                header = data.get("header") or {}
                message = (data.get("event") or {}).get("message") or {}
                log(
                    f"处理事件失败 event_id={header.get('event_id') or '-'} "
                    f"chat={message.get('chat_id') or '-'}:\n{traceback.format_exc()}"
                )
            finally:
                self.queue.task_done()

    # ---------------------------------------------------------------- 处理
    def process(self, data: dict[str, Any]) -> None:
        header = data.get("header") or {}
        event_type = str(header.get("event_type") or "")
        if event_type != "im.message.receive_v1":
            log(f"忽略非消息事件 event_type={event_type or '-'}")
            return
        event = data.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        if sender.get("sender_type") in ("app", "bot"):
            log("忽略机器人自己发送的消息")
            return

        chat_id = str(message.get("chat_id") or "")
        chat_type = str(message.get("chat_type") or "")
        message_type = str(message.get("message_type") or "")
        message_id = str(message.get("message_id") or "")
        sender_ids = sender.get("sender_id") or {}
        sender_id = str(sender_ids.get("open_id") or "")
        if not chat_id or not sender_id:
            log("事件缺少 chat_id/sender_id，跳过")
            return
        if message_id and not self._store.first_seen(f"msg:{message_id}"):
            log(f"忽略重复消息 {message_id}")
            return

        log(
            f"处理消息 chat={chat_id} chat_type={chat_type or '-'} "
            f"message_type={message_type or '-'} sender={sender_id}"
        )

        if not self._is_allowed(sender_id, str(sender_ids.get("user_id") or "")):
            if chat_type == "p2p":
                self._reply(
                    chat_id,
                    f"未授权：你的 open_id 是 {sender_id}，"
                    "请把该 id 加入 [channels.feishu].allow_from 后重启渠道。",
                )
            log(f"发送者不在 allow_from，忽略 chat={chat_id} sender={sender_id}")
            return

        mentions = [item for item in (message.get("mentions") or []) if isinstance(item, dict)]
        content_json = self._parse_content(message.get("content"))
        bot_open_id = self._client.bot_open_id() if chat_type in ("group", "topic") else ""
        if chat_type in ("group", "topic") and self._settings.group_policy == "mention":
            if not _is_bot_mentioned(
                str(message.get("content") or ""), mentions, bot_open_id
            ):
                log(f"群聊消息未 @机器人，跳过 chat={chat_id}")
                return

        content, media = self._collect_content(
            message_type, content_json, message_id, mentions, bot_open_id
        )
        parent_id = str(message.get("parent_id") or "")
        if parent_id:
            reply_context = self._client.message_text(parent_id)
            if reply_context:
                content = f"{reply_context}\n{content}" if content else reply_context
        if not content and not media:
            log(f"消息内容为空，跳过 chat={chat_id}")
            return

        chat_key = self._chat_key(chat_id, chat_type, message)
        reply_to, reply_in_thread = self._reply_target(message_id, chat_type, message)

        if self._handle_command(chat_key, chat_id, content, reply_to, reply_in_thread):
            return

        log(f"收到消息 chat={chat_id} 长度={len(content)} 内容={_preview(content)}")
        reaction_id = self._client.add_reaction(message_id, self._settings.react_emoji)
        started = time.monotonic()
        try:
            result = self._ask_markbot(
                content,
                self._store.get_session(chat_key),
                chat_key=chat_key,
                chat_id=chat_id,
                sender_id=sender_id,
            )
        except Exception as exc:  # noqa: BLE001 - 失败要落日志并告知用户，不能静默
            elapsed = time.monotonic() - started
            log(
                f"处理失败 chat={chat_id} 耗时={elapsed:.1f}s "
                f"错误={type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )
            self._reply(chat_id, _failure_text(exc, elapsed))
            return
        finally:
            # “本轮不再确认”只在本次问答内有效，回合结束就失效
            self._clear_auto_approve(chat_id)
            if reaction_id:
                self._client.remove_reaction(message_id, reaction_id)
            if self._settings.done_emoji:
                self._client.add_reaction(message_id, self._settings.done_emoji)

        elapsed = time.monotonic() - started
        if result.get("session_id"):
            self._store.set_session(chat_key, str(result["session_id"]))
            log(f"更新会话映射 {chat_key} -> {result['session_id']}")
        answer = str(result.get("text") or "").strip()
        error = result.get("error")
        if not answer:
            answer = f"处理失败：{error or result.get('stopped_reason') or '未返回内容'}"
        elif error:
            answer += f"\n\n[错误] {error}"
        log(
            f"MarkBot 返回 chat={chat_id} 耗时={elapsed:.2f}s "
            f"长度={len(answer)} 错误={error or '无'} 内容={_preview(answer)}"
        )
        self._reply(chat_id, answer, reply_to=reply_to, reply_in_thread=reply_in_thread)

    def _is_allowed(self, open_id: str, user_id: str) -> bool:
        allowed = self._settings.allow_from
        if not allowed:
            return True
        return open_id in allowed or (bool(user_id) and user_id in allowed)

    @staticmethod
    def _parse_content(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, str) or not raw:
            return {}
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _collect_content(
        self,
        msg_type: str,
        content_json: dict[str, Any],
        message_id: str,
        mentions: list[dict[str, Any]],
        bot_open_id: str,
    ) -> tuple[str, list[str]]:
        parts: list[str] = []
        media: list[str] = []

        if msg_type == "text":
            text = str(content_json.get("text") or "")
            text = _strip_leading_bot_mention(text, mentions, bot_open_id)
            text = _resolve_mentions(text, mentions)
            if text.strip():
                parts.append(text.strip())
        elif msg_type == "post":
            text, image_keys = _extract_post_content(content_json)
            if text:
                parts.append(text)
            for image_key in image_keys:
                path, note = self._download_media("image", {"image_key": image_key}, message_id)
                if path:
                    media.append(path)
                parts.append(note)
            files, folders = _extract_post_attachments(content_json)
            for folder in folders:
                parts.append(f"[folder: {folder} — 文件夹无法下载，请让用户压缩后发送]")
            for entry in files:
                path, note = self._download_media("file", entry, message_id)
                if path:
                    media.append(path)
                parts.append(note)
        elif msg_type in ("image", "audio", "file", "media"):
            path, note = self._download_media(msg_type, content_json, message_id)
            if path:
                media.append(path)
            parts.append(note)
        elif msg_type in (
            "share_chat",
            "share_user",
            "interactive",
            "share_calendar_event",
            "system",
            "merge_forward",
        ):
            text = _extract_share_card_content(content_json, msg_type)
            if text:
                parts.append(text)
        else:
            parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

        return "\n".join(part for part in parts if part), media

    def _download_media(
        self, msg_type: str, content_json: dict[str, Any], message_id: str
    ) -> tuple[str | None, str]:
        if msg_type == "image":
            key = content_json.get("image_key")
            resource_type = "image"
            fallback = f"{str(key)[:16]}.jpg" if isinstance(key, str) and key else "image.jpg"
            supplied = None
        else:
            key = content_json.get("file_key")
            resource_type = "file"  # audio/media 下载时也统一用 file
            fallback = str(key)[:16] if isinstance(key, str) and key else msg_type
            supplied = content_json.get("file_name")
        if not isinstance(key, str) or not key:
            return None, f"[{msg_type}: missing file_key]"
        if not message_id:
            return None, f"[{msg_type}: missing message_id]"

        try:
            data, filename = self._client.download_resource(message_id, key, resource_type)
        except RuntimeError as exc:
            log(f"下载 {msg_type} 失败: {exc}")
            return None, f"[{msg_type}: download failed]"
        if not data:
            return None, f"[{msg_type}: download failed]"
        safe_name = _safe_filename(
            filename or (supplied if isinstance(supplied, str) else None), fallback
        )
        if msg_type == "audio" and not safe_name.lower().endswith((".opus", ".ogg", ".oga")):
            safe_name = f"{safe_name}.ogg"
        target = self._settings.media_dir / safe_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        log(f"已下载 {msg_type} -> {target} ({len(data)} 字节)")
        return str(target), f"[{msg_type}: {target}]"

    def _chat_key(self, chat_id: str, chat_type: str, message: dict[str, Any]) -> str:
        """群内按话题隔离上下文；私聊固定为 chat_id。"""
        if chat_type in ("group", "topic") and self._settings.topic_isolation:
            thread_key = str(message.get("root_id") or message.get("message_id") or "")
            if thread_key:
                return f"feishu:{chat_id}:{thread_key}"
        return f"feishu:{chat_id}"

    def _reply_target(
        self, message_id: str, chat_type: str, message: dict[str, Any]
    ) -> tuple[str | None, bool]:
        """返回（引用哪条消息, 是否在话题内回复）。"""
        if not message_id:
            return None, False
        if self._settings.reply_to_message or message.get("thread_id"):
            in_thread = chat_type == "group" and self._settings.reply_to_message
            return message_id, in_thread
        return None, False

    def _handle_command(
        self,
        chat_key: str,
        chat_id: str,
        text: str,
        reply_to: str | None,
        reply_in_thread: bool,
    ) -> bool:
        stripped = text.strip()
        if stripped in ("/new", "新会话"):
            self._store.clear_session(chat_key)
            log(f"已清除会话映射 {chat_key}")
            self._reply(chat_id, "已开始新会话。", reply_to=reply_to)
            return True
        if stripped == "/session":
            session_id = self._store.get_session(chat_key)
            log(f"查询会话 {chat_key} -> {session_id or '无'}")
            self._reply(
                chat_id,
                session_id or "当前没有进行中的会话。",
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
            )
            return True

        parts = stripped.split()
        if parts and parts[0].lower() in ("/allow", "/grants"):
            programs = parts[1:]
            if not programs or programs[0].lower() in ("list", "列表"):
                current = self._store.list_grants(chat_key)
                self._reply(
                    chat_id,
                    "本会话已授权命令: " + "、".join(current)
                    if current
                    else "本会话还没有授权任何命令。发送 /allow curl 允许执行 curl。",
                    reply_to=reply_to,
                    reply_in_thread=reply_in_thread,
                )
                return True
            added = self._store.add_grants(chat_key, programs)
            log(f"新增命令授权 {chat_key} -> {added}")
            current = self._store.list_grants(chat_key)
            self._reply(
                chat_id,
                f"已授权: {'、'.join(added) if added else '（原本已授权）'}\n"
                f"当前授权: {'、'.join(current)}",
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
            )
            return True
        if parts and parts[0].lower() == "/deny":
            programs = parts[1:]
            self._store.remove_grants(chat_key, programs or None)
            current = self._store.list_grants(chat_key)
            log(f"撤销命令授权 {chat_key} -> {programs or '全部'} 剩余={current}")
            self._reply(
                chat_id,
                "已撤销。" + ("当前授权: " + "、".join(current) if current else "本会话已无授权。"),
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
            )
            return True
        return False

    def _ask_markbot(
        self,
        prompt: str,
        session_id: str | None,
        *,
        chat_key: str = "",
        chat_id: str = "",
        sender_id: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"prompt": prompt}
        if session_id:
            payload["session_id"] = session_id
        grants = self._store.list_grants(chat_key) if chat_key else []
        if grants:
            payload["grants"] = grants
        # 让服务端在需要确认时回调本渠道，由渠道去问用户
        payload["confirm_hook"] = {
            "url": self.confirm_hook,
            "chat_id": chat_id,
            "user_id": sender_id,
        }
        # 定时任务等后台流程要主动发消息时，通过这个回调回到原聊天
        if chat_id:
            payload["notify_hook"] = {"url": self.notify_hook, "chat_id": chat_id}
        url = self._settings.markbot_url.rstrip("/") + "/chat"
        log(
            f"请求 MarkBot {url} 长度={len(prompt)} "
            f"session_id={session_id or '新建'} 超时={self._settings.markbot_timeout_s}s"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request, timeout=self._settings.markbot_timeout_s
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            log(
                f"请求 MarkBot 失败 HTTP {exc.code} 耗时={time.monotonic() - started:.1f}s "
                f"body={_preview(raw, 300)}"
            )
            raise RuntimeError(f"MarkBot HTTP {exc.code}: {_preview(raw, 200)}") from exc
        except Exception as exc:  # noqa: BLE001
            log(
                f"请求 MarkBot 失败 耗时={time.monotonic() - started:.1f}s "
                f"超时={self._settings.markbot_timeout_s}s "
                f"错误={type(exc).__name__}: {exc}"
            )
            raise
        log(f"MarkBot 响应 {len(body)} 字节 耗时={time.monotonic() - started:.1f}s")
        try:
            return json.loads(body)
        except ValueError as exc:
            log(f"MarkBot 响应不是合法 JSON: {_preview(body, 300)}")
            raise RuntimeError(f"MarkBot 响应不是合法 JSON: {_preview(body, 200)}") from exc

    def _reply(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> None:
        try:
            self._client.send_answer(
                chat_id, text, reply_to=reply_to, reply_in_thread=reply_in_thread
            )
        except Exception as exc:  # noqa: BLE001
            log(f"发送飞书消息失败 chat={chat_id}: {exc}")

    def notify(self, chat_id: str, text: str) -> bool:
        """后台流程主动发消息（定时任务结果等）。"""
        chat_id = str(chat_id or "").strip()
        text = str(text or "").strip()
        if not chat_id or not text:
            return False
        log(f"主动通知 chat={chat_id} 长度={len(text)}")
        self._reply(chat_id, text)
        return True


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
                    {"status": "ok", "queue": gateway.queue.qsize()},
                )
            else:
                log(f"[http] GET {self.path} -> 404")
                _json_response(self, 404, {"error": "not found"})

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
                str(payload.get("chat_id") or ""), str(payload.get("text") or "")
            )
            _json_response(self, 200 if ok else 400, {"ok": ok})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/confirm-request":
                self._confirm_request()
                return
            if self.path == "/notify":
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

            if gateway.intercept_confirmation(data):
                log("[http] 确认回复已处理")
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
        if gateway.intercept_confirmation(data):
            return
        gateway.enqueue(data)

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
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
    threading.Thread(target=gateway.worker, daemon=True, name="feishu-worker").start()
    log(f"日志文件: {log_file}")
    log(
        f"配置已加载 mode={settings.mode} app_id={settings.app_id} "
        f"markbot={settings.markbot_url} 会话库={settings.session_db} "
        f"群聊策略={settings.group_policy} 话题隔离={settings.topic_isolation}"
    )
    log("消息处理线程已启动")

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
