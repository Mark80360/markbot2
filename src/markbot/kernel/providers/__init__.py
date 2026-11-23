"""Provider 工厂：按 config.toml 选择实现。"""

from __future__ import annotations

import logging

from markbot.kernel.config import Config, resolve_provider_kind, resolve_secret_ref
from markbot.kernel.providers.anthropic import AnthropicProvider
from markbot.kernel.providers.base import (
    ContentRejectedError,
    ModelReply,
    Provider,
    ProviderError,
    ToolCall,
    register_moderation_markers,
)
from markbot.kernel.providers.echo import EchoProvider, ScriptedProvider
from markbot.kernel.providers.fallback import FallbackProvider
from markbot.kernel.providers.openai_compat import OpenAICompatProvider


def build_provider(cfg: Config, secrets: dict[str, str] | None = None) -> Provider:
    """主模型 + 可选的 ``[[agent.fallbacks]]`` 备用模型（ADR 0010）。"""
    register_moderation_markers(list(getattr(cfg.agent, "moderation_markers", None) or []))
    primary = _build_primary(cfg, secrets)
    fallbacks = getattr(cfg.agent, "fallbacks", None) or []
    if not fallbacks or isinstance(primary, EchoProvider):
        return primary
    chain: list[tuple[str, Provider]] = [(f"{primary.name}:{cfg.agent.model}", primary)]
    for index, raw in enumerate(fallbacks):
        if not isinstance(raw, dict):
            continue
        try:
            provider, label = _build_fallback(cfg, secrets or cfg.secrets, raw)
        except ProviderError as exc:
            # 备用模型配置错误不影响主模型
            logging.getLogger("markbot.provider").warning(
                "备用模型 #%d 配置无效，已忽略: %s", index + 1, exc
            )
            continue
        chain.append((label, provider))
    if len(chain) == 1:
        return primary
    return FallbackProvider(
        chain,
        failure_threshold=int(getattr(cfg.agent, "circuit_failures", 3) or 3),
        cooldown_s=float(getattr(cfg.agent, "circuit_cooldown_s", 300) or 300),
    )


ROLES = ("fast", "vision", "coder")


def build_role_providers(cfg: Config, secrets: dict[str, str] | None = None) -> dict[str, Provider]:
    """``[agent.roles.<role>]`` -> provider（ADR 0011）。配置无效的角色记警告并跳过（回退主模型）。

    只写 ``model`` 时沿用主模型的 provider / base_url / api_key。
    """
    secrets = secrets or cfg.secrets
    out: dict[str, Provider] = {}
    roles = getattr(cfg.agent, "roles", None) or {}
    if resolve_provider_kind(cfg) in {"echo", "scripted"}:
        return out
    for role, raw in roles.items():
        if not isinstance(raw, dict) or not raw.get("model"):
            continue
        merged = {
            "provider": cfg.agent.provider,
            "base_url": cfg.agent.base_url,
            "api_key": cfg.agent.api_key,
        }
        merged.update({key: value for key, value in raw.items() if value not in (None, "")})
        if not merged.get("api_key") and not merged.get("api_key_secret"):
            # 主模型的 key 可能来自 [secrets]（与 _build_primary 的查找顺序一致）
            for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MARKBOT_API_KEY"):
                if secrets.get(name):
                    merged["api_key"] = secrets[name]
                    break
        try:
            provider, _ = _build_fallback(cfg, secrets, merged)
        except ProviderError as exc:
            logging.getLogger("markbot.provider").warning("模型角色 %s 配置无效，回退主模型: %s", role, exc)
            continue
        out[str(role)] = provider
    return out


def _build_fallback(
    cfg: Config, secrets: dict[str, str], raw: dict
) -> tuple[Provider, str]:
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ProviderError("备用模型缺少 model")
    kind = str(raw.get("provider") or "").strip() or (
        "anthropic" if model.lower().startswith(("claude", "anthropic")) else "openai_compat"
    )
    key = resolve_secret_ref(raw.get("api_key") or "")
    secret_name = str(raw.get("api_key_secret") or "").strip()
    if not key and secret_name:
        key = secrets.get(secret_name, "")
    agent = cfg.agent
    common = dict(
        api_key=key,
        model=model,
        max_tokens=int(raw.get("max_tokens") or agent.max_tokens),
        timeout_s=float(raw.get("request_timeout_s") or agent.request_timeout_s),
        max_retries=int(raw.get("max_retries", agent.max_retries)),
        retry_backoff_s=float(raw.get("retry_backoff_s") or agent.retry_backoff_s),
    )
    if not key:
        raise ProviderError(f"备用模型 {model} 缺少 api_key（api_key = \"env:NAME\" 或 api_key_secret）")
    if kind == "anthropic":
        provider: Provider = AnthropicProvider(
            base_url=str(raw.get("base_url") or "https://api.anthropic.com"), **common
        )
    elif kind in {"openai", "openai_compat"}:
        provider = OpenAICompatProvider(
            base_url=str(raw.get("base_url") or "https://api.openai.com/v1"), **common
        )
    else:
        raise ProviderError(f"未知 provider: {kind}")
    return provider, f"{kind}:{model}"


def _build_primary(cfg: Config, secrets: dict[str, str] | None = None) -> Provider:
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
        cache = str(getattr(cfg.agent, "prompt_cache", "auto") or "auto").strip().lower()
        prompt_cache = cache in {"true", "1", "yes", "on"} or (
            cache == "auto" and "api.anthropic.com" in base
        )
        return AnthropicProvider(
            prompt_cache=prompt_cache,
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
    "FallbackProvider",
    "ModelReply",
    "OpenAICompatProvider",
    "Provider",
    "ProviderError",
    "ScriptedProvider",
    "ToolCall",
    "build_provider",
    "build_role_providers",
]
