"""渠道核心：消息队列与处理、聊天内确认、命令（/allow /deny）、调用 MarkBot、主动通知。

从 channel.py 拆出（ADR 0005）；入口仍是 channel.py。
"""

from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from feishu_common import (
    AUTH_HEADER,
    CARD_TEXT_LIMIT,
    CONFIRM_TTL_S,
    STREAM_EDIT_INTERVAL,
    _auth_token,
    _preview,
    log,
)
from feishu_settings import (
    Settings,
)
from feishu_store import (
    Store,
)
from feishu_parse import (
    _extract_post_attachments,
    _extract_post_content,
    _extract_share_card_content,
    _is_bot_mentioned,
    _resolve_mentions,
    _strip_leading_bot_mention,
    _strip_mention_keys,
)
from feishu_client import (
    FeishuClient,
    MSG_TYPE_MAP,
    _ALL_WORDS,
    _NO_WORDS,
    _YES_WORDS,
    _extract_local_media,
    _failure_text,
    _multipart_body,
    _safe_filename,
    _transcription_endpoint,
)


def _verify_token(settings: Settings, data: dict[str, Any]) -> bool:
    expected = settings.verification_token
    if not expected:
        return True
    header = data.get("header") or {}
    token = str(header.get("token") or data.get("token") or "")
    return token == expected


class _StreamCard:
    """把模型增量文本渐进写入一张 CardKit 卡片；失败时由调用方回退普通发送。"""

    def __init__(
        self,
        client: FeishuClient,
        chat_id: str,
        reply_to: str | None,
        reply_in_thread: bool,
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._reply_to = reply_to
        self._reply_in_thread = reply_in_thread
        self._text = ""
        self._pushed = ""
        self._card_id: str | None = None
        self._sequence = 1
        self._last_edit = 0.0
        self._failed = False

    def feed(self, delta: str) -> None:
        """收到一段增量文本：必要时创建卡片，并按节流更新内容。"""
        if self._failed:
            return
        self._text += delta
        if not self._text.strip() or len(self._text) > CARD_TEXT_LIMIT:
            # 超长内容交给普通发送分片，避免卡片体积超限
            return
        now = time.monotonic()
        if self._card_id is None:
            card_id = self._client.create_stream_card(
                self._chat_id, self._reply_to, self._reply_in_thread
            )
            if not card_id:
                self._failed = True
                return
            self._card_id = card_id
            if self._client.update_stream_card(card_id, self._text, self._sequence):
                self._last_edit = now
                self._pushed = self._text
            else:
                self._failed = True
            return
        if now - self._last_edit < STREAM_EDIT_INTERVAL:
            return
        self._sequence += 1
        if self._client.update_stream_card(self._card_id, self._text, self._sequence):
            self._last_edit = now
            self._pushed = self._text
        else:
            self._failed = True

    def finish(self, text: str) -> bool:
        """把卡片定稿为最终答复并关闭流式模式；返回是否已展示答复。"""
        if self._card_id is None or self._failed:
            return False
        final = (text or "").strip() or self._text.strip()
        if len(final) > CARD_TEXT_LIMIT:
            self._sequence += 1
            self._client.close_stream_card(self._card_id, self._sequence)
            return False
        ok = True
        if final and final != self._pushed:
            self._sequence += 1
            ok = self._client.update_stream_card(
                self._card_id, final, self._sequence
            )
            if ok:
                self._pushed = final
        self._sequence += 1
        closed = self._client.close_stream_card(self._card_id, self._sequence)
        if ok and closed:
            log(
                f"流式卡片完成 card={self._card_id} chat={self._chat_id} "
                f"长度={len(self._pushed)}"
            )
        return ok and closed


class Gateway:
    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._client = FeishuClient(settings)
        # 按 chat_id 分片的队列：不同聊天并行处理（服务端会话间本就并发），同一聊天
        # 永远落到同一分片、严格串行——确认状态（_pending / _auto_approve）按 chat_id
        # 记录，同一聊天并行两个回合会互相覆盖
        shards = max(1, int(getattr(settings, "workers", 1) or 1))
        self.queues: list[queue.Queue[dict[str, Any]]] = [
            queue.Queue(maxsize=max(1, 1000 // shards)) for _ in range(shards)
        ]
        # 兼容旧引用：第一个分片
        self.queue = self.queues[0]
        # chat_id -> (token, 过期时间, 发起人的 open_id)
        self._pending: dict[str, tuple[str, float, str]] = {}
        # 本轮问答内已选择“不再重复确认”的 chat_id，回合结束即清除
        self._auto_approve: set[str] = set()
        # chat_id -> 当前回合的回复目标（消息 id, 是否在话题内）。
        # 确认提示不是由回合线程直接发送的，需要靠它回到用户发起对话的那个话题。
        self._turn_targets: dict[str, tuple[str | None, bool]] = {}
        self._pending_lock = threading.Lock()
        self.auth_token = _auth_token()

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
        reply_to, reply_in_thread = self._turn_target(chat_id)
        self._reply(
            chat_id,
            f"【需要授权】{payload.get('title') or '执行命令'}\n"
            f"{payload.get('message') or ''}\n\n"
            f"回复 1 允许，回复 2 拒绝，回复 3 本轮不再确认"
            f"（{int(CONFIRM_TTL_S // 60)} 分钟内有效）",
            reply_to=reply_to,
            reply_in_thread=reply_in_thread,
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
        reply_to, reply_in_thread = self._turn_target(chat_id)
        if time.monotonic() > expire_at:
            self._reply(
                chat_id,
                "确认已过期，请重新发起请求。",
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
            )
            return True
        if approve_all:
            with self._pending_lock:
                self._auto_approve.add(chat_id)
        self._send_confirm(token, approved)
        if approve_all:
            self._reply(
                chat_id,
                "已允许，本轮后续操作不再询问。",
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
            )
        else:
            self._reply(
                chat_id,
                "已允许，继续执行…" if approved else "已拒绝执行。",
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
            )
        return True

    def _remember_turn_target(
        self, chat_id: str, reply_to: str | None, reply_in_thread: bool
    ) -> None:
        """记录本回合的回复目标，供确认提示回到同一话题。"""
        with self._pending_lock:
            if reply_to:
                self._turn_targets[chat_id] = (reply_to, reply_in_thread)
            else:
                self._turn_targets.pop(chat_id, None)

    def _turn_target(self, chat_id: str) -> tuple[str | None, bool]:
        with self._pending_lock:
            return self._turn_targets.get(chat_id, (None, False))

    def _clear_auto_approve(self, chat_id: str) -> None:
        with self._pending_lock:
            self._auto_approve.discard(chat_id)

    def _send_confirm(self, token: str, approved: bool) -> None:
        url = self._settings.markbot_url.rstrip("/") + "/confirm"
        body = json.dumps({"token": token, "approved": approved}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", AUTH_HEADER: self.auth_token},
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
        shard = self._shard_of(data)
        target = self.queues[shard]
        try:
            target.put_nowait(data)
        except queue.Full:
            log(
                f"队列已满（分片 {shard} 当前 {target.qsize()}），"
                f"丢弃事件 event_id={event_id or '-'}"
            )
            return False
        log(
            f"事件已入队 event_id={event_id or '-'} 分片={shard} "
            f"队列长度={target.qsize()}/{self.queue_size()}"
        )
        return True

    def _shard_of(self, data: dict[str, Any]) -> int:
        """同一 chat_id 总是映射到同一分片（crc32 稳定，不受 PYTHONHASHSEED 影响）。"""
        if len(self.queues) == 1:
            return 0
        message = (data.get("event") or {}).get("message") or {}
        key = str(message.get("chat_id") or "")
        if not key:
            key = str((data.get("header") or {}).get("event_id") or "")
        return zlib.crc32(key.encode("utf-8")) % len(self.queues)

    def queue_size(self) -> int:
        return sum(q.qsize() for q in self.queues)

    def worker(self, index: int = 0) -> None:
        source = self.queues[index % len(self.queues)]
        while True:
            data = source.get()
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
                source.task_done()

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

        # 记录本回合回复目标：确认提示由服务端回调触发，必须回到同一个话题
        self._remember_turn_target(chat_id, reply_to, reply_in_thread)

        log(f"收到消息 chat={chat_id} 长度={len(content)} 内容={_preview(content)}")
        reaction_id = self._client.add_reaction(message_id, self._settings.react_emoji)
        stream = (
            _StreamCard(self._client, chat_id, reply_to, reply_in_thread)
            if self._settings.streaming
            else None
        )
        started = time.monotonic()
        try:
            result = self._ask_markbot(
                content,
                self._store.get_session(chat_key),
                chat_key=chat_key,
                chat_id=chat_id,
                sender_id=sender_id,
                on_delta=stream.feed if stream is not None else None,
            )
        except Exception as exc:  # noqa: BLE001 - 失败要落日志并告知用户，不能静默
            elapsed = time.monotonic() - started
            log(
                f"处理失败 chat={chat_id} 耗时={elapsed:.1f}s "
                f"错误={type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            )
            failure = _failure_text(exc, elapsed)
            if not (stream is not None and stream.finish(failure)):
                self._reply(chat_id, failure)
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
        media = [
            str(item) for item in (result.get("media") or []) if str(item).strip()
        ]
        for path in _extract_local_media(answer):
            if path not in media:
                media.append(path)
        streamed = stream is not None and stream.finish(answer)
        self._reply(
            chat_id,
            "" if streamed else answer,
            reply_to=reply_to,
            reply_in_thread=reply_in_thread,
            media=media,
        )

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
        note = f"[{msg_type}: {target}]"
        if msg_type == "audio":
            transcript = self._transcribe_audio(target)
            if transcript:
                log(f"语音转写完成 {target.name}: {_preview(transcript, 80)}")
                note += f"\n[transcription: {transcript}]"
        return str(target), note

    def _transcribe_audio(self, path: Path) -> str:
        """调用 OpenAI 兼容的 /audio/transcriptions 接口；失败返回空串。"""
        settings = self._settings
        endpoint = _transcription_endpoint(settings)
        if not settings.transcribe_enabled or not endpoint:
            return ""
        try:
            size = path.stat().st_size
        except OSError:
            return ""
        limit = max(1, settings.transcribe_max_upload_mb) * 1024 * 1024
        if size > limit:
            log(
                f"语音文件超过转写上限 {settings.transcribe_max_upload_mb}MB，跳过: {path}"
            )
            return ""
        fields = {"model": settings.transcribe_model}
        if settings.transcribe_language:
            fields["language"] = settings.transcribe_language
        try:
            body, content_type = _multipart_body(fields, "file", path)
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {settings.transcribe_api_key}",
                    "Content-Type": content_type,
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            log(f"语音转写失败 HTTP {exc.code}: {_preview(detail, 200)}")
            return ""
        except (OSError, ValueError) as exc:
            log(f"语音转写失败: {exc}")
            return ""
        try:
            payload = json.loads(raw)
        except ValueError:
            log(f"语音转写响应不是合法 JSON: {_preview(raw, 200)}")
            return ""
        text = payload.get("text") if isinstance(payload, dict) else ""
        return text.strip() if isinstance(text, str) else ""

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
        # 话题群里的消息自带 thread_id：无论在哪个话题里交互，回复都留在该话题内
        if message.get("thread_id"):
            return message_id, True
        if self._settings.reply_to_message:
            return message_id, chat_type in ("group", "topic")
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
        on_delta: Any = None,
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
        if on_delta is not None:
            payload["stream"] = True
        url = self._settings.markbot_url.rstrip("/") + "/chat"
        log(
            f"请求 MarkBot {url} 长度={len(prompt)} "
            f"session_id={session_id or '新建'} 超时={self._settings.markbot_timeout_s}s "
            f"流式={'是' if on_delta is not None else '否'}"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", AUTH_HEADER: self.auth_token},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(
                request, timeout=self._settings.markbot_timeout_s
            ) as response:
                if on_delta is not None:
                    return self._consume_stream(response, on_delta, started)
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

    def _consume_stream(
        self, response: Any, on_delta: Any, started: float
    ) -> dict[str, Any]:
        """逐行读取 NDJSON：delta 交给 on_delta，done/error 结束本轮。"""
        result: dict[str, Any] | None = None
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "delta":
                text = event.get("text")
                if isinstance(text, str) and text:
                    on_delta(text)
            elif kind == "done":
                result = {
                    "text": event.get("text") or "",
                    "stopped_reason": event.get("stopped_reason"),
                    "error": event.get("error"),
                    "session_id": event.get("session_id"),
                }
            elif kind == "error":
                raise RuntimeError(
                    f"MarkBot 流式处理失败: {event.get('error') or 'unknown'}"
                )
        if result is None:
            raise RuntimeError("MarkBot 流式响应缺少 done 事件")
        log(f"MarkBot 流式响应完成 耗时={time.monotonic() - started:.1f}s")
        return result

    def _reply(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
        media: list[str] | None = None,
    ) -> None:
        try:
            self._client.send_answer(
                chat_id,
                text,
                media=media,
                reply_to=reply_to,
                reply_in_thread=reply_in_thread,
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
