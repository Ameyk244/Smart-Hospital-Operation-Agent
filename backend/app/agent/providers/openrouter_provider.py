"""OpenRouter provider — the documented alternative to Anthropic (see
app/config.py LLM_PROVIDER). OpenRouter exposes an OpenAI-compatible API, so
this reuses `langchain_openai.ChatOpenAI` pointed at OpenRouter's base URL
rather than needing a separate LangChain integration package.
"""

from langchain_openai import ChatOpenAI

from app.config import Settings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.openrouter_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is not set. "
                "Get one at https://openrouter.ai/keys and put it in backend/.env."
            )
        self._settings = settings

    def get_chat_model(self) -> ChatOpenAI:
        return ChatOpenAI(
            model=self._settings.openrouter_model,
            api_key=self._settings.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
            timeout=self._settings.llm_timeout_seconds,
            max_tokens=2048,
        )
