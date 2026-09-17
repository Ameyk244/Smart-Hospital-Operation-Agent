"""Grounding-by-rejection policy (concepts 22, 23).

Why it exists: this is the code-level enforcement docs/ARCHITECTURE.md §6
promises — "only IDs a search/read tool actually exposed to *this session*
may be passed into a write tool", checked here, not requested via a prompt
instruction. `GroundingRepository` (the persistence layer) has no opinion on
when to expose or require — this module is that policy, and it is the only
caller of the repository (see that module's own docstring).

What calls it: specific handlers in `app/agent/tools/` expose the typed
codes they return and require the typed codes they consume. The graph's
tool node catches `GroundingRejectedError` and returns it to the model as a
rejected `ToolMessage`.

Fails: `require_grounded` raises `GroundingRejectedError`, which the
tool_node turns into a `ToolMessage` describing the rejection (so the model
sees *why* and can search again) rather than letting the write proceed.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.grounding_repository import GroundingRepository


class GroundingRejectedError(Exception):
    def __init__(self, entity_type: str, code: str) -> None:
        self.entity_type = entity_type
        self.code = code
        super().__init__(
            f"{code!r} was never exposed to this session as a {entity_type} "
            "(no prior search/read tool returned it) — refusing to act on it."
        )


class GroundingRegistry:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = GroundingRepository(session)

    async def expose(self, session_id: str, entity_type: str, codes: list[str]) -> None:
        await self._repo.expose(session_id, entity_type, codes)

    async def require_grounded(self, session_id: str, entity_type: str, code: str) -> None:
        if not await self._repo.is_grounded(session_id, entity_type, code):
            raise GroundingRejectedError(entity_type, code)
