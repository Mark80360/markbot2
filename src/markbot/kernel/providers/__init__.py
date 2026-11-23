"""Provider 工厂：按 config.toml 选择实现。"""

from __future__ import annotations

from markbot.kernel.config import Config, resolve_provider_kind
from markbot.kernel.providers.anthropic import AnthropicProvider
from markbot.kernel.providers.base import (
    ContentRejectedError,
    ModelReply,
    Provider,
    ProviderError,
    ToolCall,
)
from markbot.kernel.providers.echo import EchoProvider, ScriptedProvider
from markbot.kernel.providers.openai_compat import OpenAICompatProvider


def build_provider(cfg: Config, secrets: dict[str, str] | None = None) -> Provider:
    secrets = secrets or cfg.secrets
    model = cfg.agent.model
    kind = resolve_provider_kind(cfg)
    if kind in {"echo", "scripted"}:
        return EchoProvider()
    if kind == "anthropic":
        key = (
            cfg.agent.api_key
            or secrets.get("ANTHROPIC_API_KEY")
            or secrets.get("MARKBOT_API_KEY")
            or ""
        )
        if not key:
            raise ProviderError("缺少 api_key（[agent] api_key 或 [secrets] ANTHROPIC_API_KEY）")
        base = cfg.agent.base_url or "https://api.anthropic.com"
        return AnthropicProvider(
            api_key=key,
            model=model,
            base_url=base,
            max_tokens=cfg.agent.max_tokens,
            timeout_s=cfg.agent.request_timeout_s,
            max_retries=cfg.agent.max_retries,
            retry_backoff_s=cfg.agent.retry_backoff_s,
        )
    if kind in {"openai", "openai_compat"}:
        key = (
            cfg.agent.api_key
            or secrets.get("OPENAI_API_KEY")
            or secrets.get("MARKBOT_API_KEY")
            or ""
        )
        if not key:
            raise ProviderError("缺少 api_key（[agent] api_key 或 [secrets] OPENAI_API_KEY）")
        base = cfg.agent.base_url or "https://api.openai.com/v1"
        return OpenAICompatProvider(
            api_key=key,
            model=model,
            base_url=base,
            max_tokens=cfg.agent.max_tokens,
            timeout_s=cfg.agent.request_timeout_s,
            max_retries=cfg.agent.max_retries,
            retry_backoff_s=cfg.agent.retry_backoff_s,
        )
    raise ProviderError(f"未知 provider: {kind}")


__all__ = [
    "AnthropicProvider",
    "ContentRejectedError",
    "EchoProvider",
    "ModelReply",
    "OpenAICompatProvider",
    "Provider",
    "ProviderError",
    "ScriptedProvider",
    "ToolCall",
    "build_provider",
]
