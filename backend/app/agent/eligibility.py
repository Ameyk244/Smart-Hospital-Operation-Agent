"""The agent eligibility gate (concept 44).

Why it exists: a request the deterministic parser doesn't recognize isn't
automatically entitled to invoke the LLM — docs/ARCHITECTURE.md's diagram
draws this as a separate decision point on purpose. Kept deliberately small
for now (empty input, a hard length cap); this is the seam where future
policy (per-session rate limiting, blocklists, auth checks) would plug in
without touching the parser or the graph.

What calls it: `app/api/routes/chat.py`, only for requests the parser
already returned `matched=False` for.
"""

from dataclasses import dataclass

MAX_REQUEST_LENGTH = 2000


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason: str | None = None


def check_eligibility(text: str) -> EligibilityResult:
    stripped = text.strip()
    if not stripped:
        return EligibilityResult(eligible=False, reason="empty_request")
    if len(stripped) > MAX_REQUEST_LENGTH:
        return EligibilityResult(eligible=False, reason="request_too_long")
    return EligibilityResult(eligible=True)
