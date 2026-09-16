"""Anthropic provider (the default — see app/config.py LLM_PROVIDER).

Fails fast at construction if `ANTHROPIC_API_KEY` is missing, rather than
letting the first agent request fail deep inside a LangGraph node.
"""

from langchain_anthropic import ChatAnthropic

from app.config import Settings


class AnthropicProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set. "
                "Get one at https://console.anthropic.com/settings/keys and put it in backend/.env."
            )
        self._settings = settings

    def get_chat_model(self) -> ChatAnthropic:
        return ChatAnthropic(
            model=self._settings.anthropic_model,
            api_key=self._settings.anthropic_api_key,
            timeout=self._settings.llm_timeout_seconds,
            max_tokens=2048,
        )
