"""Picks the configured provider. The one place `settings.llm_provider`
(a plain string from `.env`) turns into a concrete `LLMProvider`.
"""

from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.agent.providers.anthropic_provider import AnthropicProvider
from app.agent.providers.base import LLMProvider
from app.agent.providers.openrouter_provider import OpenRouterProvider
from app.config import Settings, get_settings


def get_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(settings)
    if settings.llm_provider == "openrouter":
        return OpenRouterProvider(settings)
    raise ValueError(f"Unknown LLM_PROVIDER {settings.llm_provider!r}")


@lru_cache
def get_default_chat_model() -> BaseChatModel:
    """Process-wide singleton chat model for the live app (tests construct
    their own provider/model directly instead, so they can swap in a fake).
    Constructed lazily on first use — a deterministic-only request never
    triggers this, so a missing API key doesn't break the parts of the app
    that don't need one."""
    return get_provider(get_settings()).get_chat_model()
