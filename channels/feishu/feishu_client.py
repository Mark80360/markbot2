"""飞书开放平台客户端：鉴权、收发消息、表情、媒体上传下载、流式卡片；以及文本切分等辅助。

从 channel.py 拆出（ADR 0005）；入口仍是 channel.py。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from feishu_common import (
    AUDIO_EXTS,
    CARD_TEXT_LIMIT,
    FILE_TYPE_MAP,
    IMAGE_EXTS,
    STREAM_ELEMENT_ID,
    TEXT_LIMIT,
    VIDEO_EXTS,
    _preview,
    log,
    markbot_home,
)
from feishu_settings import (
    Settings,
)
from feishu_parse import (
    _content_text,
)
from feishu_render import (
    _build_card_elements,
    _card_to_text,
    _detect_msg_format,
    _markdown_to_post,
    _split_elements_by_table_limit,
)


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
        media: list[str] | None = None,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> None:
        """先发媒体，再按内容复杂度选择 text / post / 卡片发送。"""
        if media:
            try:
                self.send_media(
                    chat_id, media, reply_to=reply_to, reply_in_thread=reply_in_thread
                )
            except RuntimeError as exc:
                # 媒体失败不应拖累文本回复
                log(f"发送媒体失败 chat={chat_id}: {exc}")
            # 媒体已用掉引用位，后续文本单独发送，避免多条消息叠在一起
            reply_to = None
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
                message_id = self.send_message(
                    _receive_id_type(chat_id), chat_id, msg_type, content
                )
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

    # ---------------------------------------------------------------- 媒体上传
    def _upload(
        self, path: str, fields: dict[str, str], file_field: str, file_path: Path
    ) -> dict[str, Any]:
        body, content_type = _multipart_body(fields, file_field, file_path)
        request = urllib.request.Request(
            self._settings.feishu_base + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token()}",
                "Content-Type": content_type,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw) if raw.strip() else {"code": 0}
        code = data.get("code", 0)
        if code not in (0, None):
            raise RuntimeError(f"飞书接口错误 {code}: {data.get('msg')}")
        return data

    def upload_image(self, file_path: Path) -> str | None:
        try:
            data = self._upload(
                "/open-apis/im/v1/images",
                {"image_type": "message"},
                "image",
                file_path,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            log(f"上传图片失败 {file_path}: {exc}")
            return None
        key = (data.get("data") or {}).get("image_key")
        if not key:
            log(f"上传图片未返回 image_key: {file_path}")
            return None
        log(f"图片已上传 {file_path.name} -> {key}")
        return str(key)

    def upload_file(self, file_path: Path) -> str | None:
        file_type = FILE_TYPE_MAP.get(file_path.suffix.lower(), "stream")
        try:
            data = self._upload(
                "/open-apis/im/v1/files",
                {"file_type": file_type, "file_name": file_path.name},
                "file",
                file_path,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            log(f"上传文件失败 {file_path}: {exc}")
            return None
        key = (data.get("data") or {}).get("file_key")
        if not key:
            log(f"上传文件未返回 file_key: {file_path}")
            return None
        log(f"文件已上传 {file_path.name} -> {key}")
        return str(key)

    def send_media(
        self,
        chat_id: str,
        paths: list[str],
        *,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> None:
        """按扩展名选择 image / audio / media / file 消息逐条发送。"""
        first = True
        for raw in paths:
            path = Path(str(raw)).expanduser()
            if not path.is_file():
                log(f"媒体文件不存在，跳过: {raw}")
                continue
            ext = path.suffix.lower()
            if ext in IMAGE_EXTS:
                key = self.upload_image(path)
                msg_type = "image"
                content = {"image_key": key}
            else:
                key = self.upload_file(path)
                if ext in AUDIO_EXTS:
                    msg_type = "audio"
                elif ext in VIDEO_EXTS:
                    # 飞书把视频消息命名为 media
                    msg_type = "media"
                else:
                    msg_type = "file"
                content = {"file_key": key}
            if not key:
                continue
            target = reply_to if first else None
            try:
                self._deliver(
                    chat_id,
                    msg_type,
                    json.dumps(content, ensure_ascii=False),
                    target,
                    reply_in_thread if target else False,
                )
            except RuntimeError as exc:
                log(f"发送媒体消息失败 type={msg_type} path={path}: {exc}")
                continue
            first = False

    # ---------------------------------------------------------------- 流式卡片
    def create_stream_card(
        self,
        chat_id: str,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
    ) -> str | None:
        card = {
            "schema": "2.0",
            "config": {
                "wide_screen_mode": True,
                "update_multi": True,
                "streaming_mode": True,
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "",
                        "element_id": STREAM_ELEMENT_ID,
                    }
                ]
            },
        }
        try:
            data = self._api(
                "POST",
                "/open-apis/cardkit/v1/cards",
                {"type": "card_json", "data": json.dumps(card, ensure_ascii=False)},
            )
        except RuntimeError as exc:
            log(f"创建流式卡片失败: {exc}")
            return None
        card_id = (data.get("data") or {}).get("card_id")
        if not card_id:
            log("创建流式卡片未返回 card_id")
            return None
        content = json.dumps(
            {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
        )
        try:
            if reply_to:
                self.reply_message(
                    reply_to, "interactive", content, reply_in_thread=reply_in_thread
                )
            else:
                self.send_message(
                    _receive_id_type(chat_id), chat_id, "interactive", content
                )
        except RuntimeError as exc:
            log(f"发送流式卡片失败: {exc}")
            return None
        log(f"流式卡片已创建 card={card_id} chat={chat_id}")
        return str(card_id)

    def update_stream_card(self, card_id: str, text: str, sequence: int) -> bool:
        try:
            self._api(
                "PUT",
                f"/open-apis/cardkit/v1/cards/{card_id}/elements/"
                f"{STREAM_ELEMENT_ID}/content",
                {"content": text, "sequence": sequence},
            )
            return True
        except RuntimeError as exc:
            log(f"更新流式卡片失败 card={card_id}: {exc}")
            return False

    def close_stream_card(self, card_id: str, sequence: int) -> bool:
        settings = json.dumps({"config": {"streaming_mode": False}}, ensure_ascii=False)
        try:
            self._api(
                "PATCH",
                f"/open-apis/cardkit/v1/cards/{card_id}/settings",
                {"settings": settings, "sequence": sequence, "uuid": uuid.uuid4().hex},
            )
            return True
        except RuntimeError as exc:
            log(f"关闭流式模式失败 card={card_id}: {exc}")
            return False


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


def _receive_id_type(chat_id: str) -> str:
    """飞书 chat_id 以 oc_ 开头；其它情况按 open_id 处理。"""
    return "chat_id" if chat_id.startswith("oc_") else "open_id"


def _multipart_body(
    fields: dict[str, str], file_field: str, file_path: Path
) -> tuple[bytes, str]:
    """标准库构造 multipart/form-data，用于图片/文件上传与语音转写。"""
    boundary = f"----markbot{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    filename = file_path.name.replace('"', "_")
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
    )
    chunks.append(file_path.read_bytes())
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


_LOCAL_MEDIA_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def _extract_local_media(text: str) -> list[str]:
    """从回复的 Markdown 图片/链接里提取已存在的本地文件，作为出站媒体。"""
    found: list[str] = []
    for match in _LOCAL_MEDIA_RE.finditer(text or ""):
        raw = match.group(1).strip().strip("<>")
        if raw.startswith(("http://", "https://", "data:")):
            continue
        if raw.startswith("file://"):
            raw = urllib.parse.unquote(raw[len("file://") :])
        else:
            raw = urllib.parse.unquote(raw)
        candidates = [Path(raw).expanduser()]
        if not candidates[0].is_absolute():
            candidates = [
                Path.cwd() / candidates[0],
                markbot_home() / candidates[0],
            ]
        for path in candidates:
            try:
                if path.is_file():
                    resolved = str(path.resolve())
                    if resolved not in found:
                        found.append(resolved)
                    break
            except OSError:
                continue
    return found


def _transcription_endpoint(settings: Settings) -> str:
    base = settings.transcribe_api_base
    if not base:
        return ""
    if base.endswith("/audio/transcriptions"):
        return base
    return base.rstrip("/") + "/audio/transcriptions"


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
