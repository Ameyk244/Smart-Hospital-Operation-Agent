from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app.agent.graph import SYSTEM_PROMPT, build_system_message, run_agent
from app.agent.skills import select_skills
from app.config import Settings

BED_TEXT = "Admit the waiting patient to an available bed"


class _Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


async def test_without_user_text_the_message_is_the_plain_prompt(agent_session_factory):
    message = await build_system_message(agent_session_factory, "skills-plain")

    assert message.content == SYSTEM_PROMPT


async def test_user_text_appends_the_matching_guidance(agent_session_factory):
    message = await build_system_message(agent_session_factory, "skills-on", BED_TEXT)

    assert message.content == f"{SYSTEM_PROMPT}\n\nTask guidance for this request:\n{select_skills(BED_TEXT)}"


async def test_unmatched_user_text_appends_nothing(agent_session_factory):
    message = await build_system_message(agent_session_factory, "skills-none", "list scanners mri")

    assert message.content == SYSTEM_PROMPT


async def _system_message_for(factory, enabled: bool) -> str:
    final_state = await run_agent(
        session_id=f"skills-run-{enabled}",
        user_text=BED_TEXT,
        chat_model=_Model(responses=[AIMessage(content="ok")]),
        session_factory=factory,
        settings=Settings(
            database_url="postgresql+asyncpg://unused/unused", agent_skills_enabled=enabled
        ),
    )
    return final_state["messages"][0].content


async def test_in_process_turn_gets_guidance_only_when_enabled(agent_session_factory):
    assert await _system_message_for(agent_session_factory, False) == SYSTEM_PROMPT
    assert "Bed allocation" in await _system_message_for(agent_session_factory, True)
