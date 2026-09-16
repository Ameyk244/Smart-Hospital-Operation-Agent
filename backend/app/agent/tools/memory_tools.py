"""Explicit memory tools (concepts 37, 38, 41).

Why it exists: the *only* way `Preference` rows are ever written or removed
— nothing in the agent loop infers or auto-saves a preference. This is a
deliberate architectural choice from docs/ARCHITECTURE.md §7: persistent
memory is explicit-only, never inferred, so it can't silently accumulate
wrong guesses about what a user wants.

Unlike the search/action tools, these do not route through `CommandRunner`
— preferences aren't a hospital-operations command, they're a distinct
memory concern with its own repository (docs/ARCHITECTURE.md §7's "three
separate memories, never merged"). Routing them through the hospital
command grammar would blur that separation for no benefit.
"""

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.base import ToolSpec, register_tool
from app.db.repositories.preference_repository import PreferenceRepository


class RememberPreferenceArgs(BaseModel):
    key: str = Field(
        ..., description="A short identifier for this preference, e.g. 'preferred_scanner_type'"
    )
    value: str = Field(..., description="The value to remember for this key")


class ForgetPreferenceArgs(BaseModel):
    key: str = Field(..., description="The preference key to forget")


class ListPreferencesArgs(BaseModel):
    pass


async def handle_remember_preference(
    session: AsyncSession, session_id: str, args: RememberPreferenceArgs
) -> dict:
    pref = await PreferenceRepository(session).remember(session_id, args.key, args.value)
    return {"key": pref.key, "value": pref.value, "remembered": True}


async def handle_forget_preference(
    session: AsyncSession, session_id: str, args: ForgetPreferenceArgs
) -> dict:
    removed = await PreferenceRepository(session).forget(session_id, args.key)
    return {"key": args.key, "removed": removed}


async def handle_list_preferences(
    session: AsyncSession, session_id: str, _args: ListPreferencesArgs
) -> list[dict]:
    prefs = await PreferenceRepository(session).list_for_session(session_id)
    return [{"key": p.key, "value": p.value} for p in prefs]


REMEMBER_PREFERENCE_TOOL = register_tool(
    ToolSpec(
        name="remember_preference",
        description=(
            "Remember an explicit user preference for this session, e.g. a preferred "
            "scanner type or workflow style. Only call this when the user explicitly "
            "asks you to remember something — never infer and save a preference on "
            "your own."
        ),
        args_schema=RememberPreferenceArgs,
        handler=handle_remember_preference,
        is_write=True,
    )
)

FORGET_PREFERENCE_TOOL = register_tool(
    ToolSpec(
        name="forget_preference",
        description="Remove a previously remembered preference by its key.",
        args_schema=ForgetPreferenceArgs,
        handler=handle_forget_preference,
        is_write=True,
    )
)

LIST_PREFERENCES_TOOL = register_tool(
    ToolSpec(
        name="list_preferences",
        description="List every preference currently remembered for this session.",
        args_schema=ListPreferencesArgs,
        handler=handle_list_preferences,
        is_write=False,
    )
)
