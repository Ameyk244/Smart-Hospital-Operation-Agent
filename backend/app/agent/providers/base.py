"""The LLM provider abstraction (concept 47).

Why it exists: `app/agent/graph.py` should never import `ChatAnthropic` or
`ChatOpenAI` directly. It asks for "the configured chat model" and gets a
LangChain `BaseChatModel` back — swapping providers is a config change
(`LLM_PROVIDER=openrouter` in `.env`), not a code change. This is
deliberately thin: LangChain's `BaseChatModel` is already the real
cross-vendor abstraction (concept 2); this module's only job is picking
*which* one to construct from `app/config.py`'s settings.

What calls it: `app/agent/providers/factory.py` returns the concrete
provider; `app/agent/graph.py` calls `.get_chat_model()` on whatever it
gets, without knowing which vendor it is.
"""

from typing import Protocol

from langchain_core.language_models import BaseChatModel


class LLMProvider(Protocol):
    def get_chat_model(self) -> BaseChatModel: ...
