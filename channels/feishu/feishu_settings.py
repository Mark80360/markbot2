"""飞书渠道配置：读取 config.toml + secrets.toml 的 [channels.feishu] 等小节。

从 channel.py 拆出（ADR 0005）；入口仍是 channel.py。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from feishu_common import (
    FEISHU_BASE,
    LARK_BASE,
    _deep_merge,
    _register_channel_secrets,
    _secret_ref,
    _string_list,
    default_config_path,
    markbot_home,
)


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
    streaming: bool = True
    transcribe_enabled: bool = False
    transcribe_provider: str = "groq"
    transcribe_model: str = ""
    transcribe_api_base: str = ""
    transcribe_api_key: str = ""
    transcribe_language: str = ""
    transcribe_max_upload_mb: int = 25
    # 并行处理消息的线程数；同一 chat_id 的消息总是落到同一线程，严格按顺序处理
    workers: int = 4
    # 异步任务（ADR 0010）：提交后按事件长轮询；等待超过 markbot_timeout_s 后不再占着
    # 工作线程，任务在后台继续，完成后由服务端主动推送结果。服务端不支持时自动回退 /chat。
    async_tasks: bool = True
    # 工作线程最多等一个任务多久就放手转后台（秒）；0 = 只看 markbot_timeout_s。
    # 同一聊天的消息由同一个工作线程按顺序处理：等太久，期间发来的新消息全部排在后面
    # （2026-11-23：等了 608 秒，两条消息排队 3 分钟）。
    task_wait_s: float = 120.0

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
    # 密钥文件与 config.toml 同目录，叠加在其上（与 markbot 内核的加载顺序一致）
    secrets_file = path.parent / "secrets.toml"
    if secrets_file.exists():
        data = _deep_merge(data, tomllib.loads(secrets_file.read_text(encoding="utf-8")))
    channels = data.get("channels") or {}
    section = channels.get("feishu") or {}
    secrets = {k: _secret_ref(v) for k, v in (data.get("secrets") or {}).items()}

    app_id = str(section.get("app_id") or "").strip()
    app_secret = str(
        _secret_ref(section.get("app_secret")) or secrets.get("FEISHU_APP_SECRET") or ""
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
    if mode == "webhook" and not str(section.get("verification_token") or "").strip():
        # 没有校验令牌时，任何能访问回调端口的人都能伪造飞书事件驱动机器人
        raise SystemExit(
            f"{path}: webhook 模式必须配置 [channels.feishu].verification_token"
            "（或改用 mode = \"longconn\"，无需公网端口）"
        )

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

    # 语音转写：顶层 [transcription]，密钥优先 [secrets].TRANSCRIPTION_API_KEY，
    # 其次环境变量（GROQ_API_KEY / OPENAI_API_KEY）。
    transcription = data.get("transcription")
    if not isinstance(transcription, dict):
        transcription = {}
    transcribe_provider = str(transcription.get("provider") or "groq").strip().lower()
    transcribe_api_base = str(
        transcription.get("api_base")
        or {
            "groq": "https://api.groq.com/openai/v1",
            "openai": "https://api.openai.com/v1",
        }.get(transcribe_provider, "")
    ).rstrip("/")
    transcribe_model = str(
        transcription.get("model")
        or {"groq": "whisper-large-v3", "openai": "whisper-1"}.get(
            transcribe_provider, "whisper-1"
        )
    ).strip()
    transcribe_api_key = str(
        _secret_ref(transcription.get("api_key"))
        or secrets.get("TRANSCRIPTION_API_KEY")
        or os.environ.get("TRANSCRIPTION_API_KEY")
        or os.environ.get(
            "GROQ_API_KEY" if transcribe_provider == "groq" else "OPENAI_API_KEY"
        )
        or ""
    ).strip()

    _register_channel_secrets(
        app_secret,
        transcribe_api_key,
        str(section.get("verification_token") or ""),
        *(str(value) for value in secrets.values()),
    )
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
        streaming=bool(section.get("streaming", True)),
        transcribe_enabled=bool(transcription.get("enabled", True))
        and bool(transcribe_api_key)
        and bool(transcribe_api_base),
        transcribe_provider=transcribe_provider,
        transcribe_model=transcribe_model,
        transcribe_api_base=transcribe_api_base,
        transcribe_api_key=transcribe_api_key,
        transcribe_language=str(transcription.get("language") or "").strip(),
        transcribe_max_upload_mb=int(transcription.get("max_upload_mb") or 25),
        workers=max(1, min(16, int(section.get("workers") or 4))),
        async_tasks=bool(section.get("async_tasks", True)),
        task_wait_s=float(section.get("task_wait_s", 120) or 0),
    )
