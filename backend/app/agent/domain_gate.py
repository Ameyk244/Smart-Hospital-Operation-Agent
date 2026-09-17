"""The domain-relevance gate (extends concept 44's agent eligibility gate).

Why it exists: `app/agent/eligibility.py`'s `check_eligibility` used to ask
only one question — "did the parser fail to match, and is this input
otherwise well-formed (non-empty, under the length cap)?" It never asked
whether the request has anything to do with this hospital system at all. A
clearly off-topic request ("write me a poem", "what's 15 * 37", "book me a
flight") that happens to be non-empty and under the length cap would sail
past eligibility and trigger a real LLM call just to be told "I can't help
with that" — API spend for zero chance of a useful answer.

This gate is a small, synchronous, keyword/intent screen — deliberately NOT
an LLM call — that runs on the hot path *before* `check_eligibility` and
before any LLM client is constructed. It is intentionally conservative: the
cost of a false positive (blocking something that should have reached the
agent) is much higher than a false negative (letting something mildly
off-topic through, which the agent can still decline conversationally). So
this only rejects requests that share *no* vocabulary at all with the
system's real domain surface — anything ambiguous-but-plausibly-hospital-
related passes.

The vocabulary below is pulled from the system's actual domain surface, not
invented: `app/parser/parser.py`'s grammar (department/scanner/patient/
appointment nouns, MRI/CT/XRAY modalities, AVAILABLE/IN_USE/MAINTENANCE
statuses, delayed appointments), the agent's own tool descriptions and
`args_schema` fields in `app/agent/tools/*.py` (search/reschedule/
availability/preference verbs and nouns), and the seeded department names
and staff roles in `app/seed/seed_data.py`.

Why a separate component from the regex parser, not folded into it: the
parser's job is to *structure* known commands into a `Command` object it can
execute deterministically; this gate's job is only to *screen relevance* —
it never produces a `Command` and is not trying to understand the request,
just to notice when it shares nothing with the domain at all. Conflating the
two would turn the parser into an ever-growing pseudo-NLU layer (exactly
what its own docstring says not to do) while also making this gate's "stay
conservative" property harder to reason about independently.

What calls it: `app/api/routes/chat.py`, for every request the parser
already returned `matched=False` for, before `check_eligibility` runs.
"""

import re
from dataclasses import dataclass

_WORD_RE = re.compile(r"[a-z]+")

# Real domain vocabulary, derived from (not invented alongside) the actual
# command grammar and tool surface:
#   - app/parser/parser.py: department/scanner/patient/appointment nouns,
#     _MODALITY_WORDS (mri/ct/xray), _STATUS_WORDS (available/in use/
#     maintenance), "delayed appointments", "next appointment"
#   - app/agent/tools/search_tools.py: status enum (SCHEDULED, DELAYED,
#     COMPLETED, CANCELLED), appointment_type, patient/department/scanner
#   - app/agent/tools/action_tools.py: reschedule, scanner_code, new_start
#   - app/agent/tools/observation_tools.py: scanner availability
#   - app/agent/tools/command_tools.py: list/show/search/find verbs
#   - app/agent/tools/memory_tools.py: remember/forget/prefer(ence)
#   - app/seed/seed_data.py: department names (Radiology, Cardiology,
#     Orthopedics, Emergency) and staff roles (Radiologist, Technologist,
#     Nurse, Physician)
_DOMAIN_VOCABULARY: frozenset[str] = frozenset(
    {
        # Core entities / nouns
        "patient",
        "patients",
        "appointment",
        "appointments",
        "scanner",
        "scanners",
        "department",
        "departments",
        "room",
        "rooms",
        "staff",
        "session",
        "hospital",
        "modality",
        "modalities",
        # Modalities (parser's _MODALITY_WORDS + tool arg descriptions)
        "mri",
        "ct",
        "xray",
        # Statuses (parser's _STATUS_WORDS + search_appointments status enum)
        "available",
        "unavailable",
        "maintenance",
        "scheduled",
        "delayed",
        "delay",
        "completed",
        "complete",
        "cancelled",
        "cancel",
        "busy",
        # Actions/verbs across the parser grammar and tool descriptions
        "list",
        "show",
        "search",
        "find",
        "reschedule",
        "move",
        "assign",
        "remember",
        "forget",
        "prefer",
        "prefers",
        "preferred",
        "preference",
        "preferences",
        "check",
        "free",
        "schedule",
        "scheduling",
        "availability",
        # Seeded department names (app/seed/seed_data.py DEPARTMENTS)
        "radiology",
        "cardiology",
        "orthopedics",
        "orthopaedics",
        "emergency",
        # Seeded staff roles (app/seed/seed_data.py STAFF_ROLES)
        "radiologist",
        "technologist",
        "nurse",
        "physician",
    }
)


@dataclass(frozen=True)
class DomainGateResult:
    in_domain: bool
    reason: str | None = None


def check_domain_gate(text: str) -> DomainGateResult:
    """Cheap, synchronous relevance screen — no LLM call. Blank/whitespace
    input is deliberately let through here (`in_domain=True`): that's
    `check_eligibility`'s job to reject as `empty_request`, not this gate's;
    keeping that one concern in one place matches how the parser/eligibility
    split already works."""
    stripped = text.strip()
    if not stripped:
        return DomainGateResult(in_domain=True)

    # Hyphens stripped (not turned into spaces) so a term like "x-ray" folds
    # into "xray" and still matches the vocabulary, instead of splitting into
    # two unrelated words ("x", "ray").
    words = set(_WORD_RE.findall(stripped.lower().replace("-", "")))
    if words & _DOMAIN_VOCABULARY:
        return DomainGateResult(in_domain=True)
    return DomainGateResult(
        in_domain=False,
        reason="off_topic",
    )
