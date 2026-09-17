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
this only rejects requests that have no plausible connection to the
system's real domain surface — anything ambiguous-but-plausibly-hospital-
related passes.

Presence-based matching had a real bypass (found and fixed this session):
the original version of this gate rejected a request only if *no* domain
vocabulary word appeared *anywhere* in the message — pure substring/word
presence, with no requirement that the domain word have anything to do with
the actual request. That let a message like `"What's 47 times 12? mri"`
sail through: the substantive request ("what's 47 times 12") is pure
off-topic arithmetic, complete on its own with a "?", and "mri" is a
disconnected word tacked on afterward — but because "mri" is in the
vocabulary, the whole message passed and would have triggered a real LLM
call to do arbitrary off-topic work. Same shape as `"Write me a poem.
patient"` or `"Book a flight to Chicago. appointment"` — a throwaway domain
word appended to an already-complete, unrelated sentence.

The fix moves from presence-based to intent-shape-based matching: instead of
asking "does a domain word appear anywhere in the message", this now asks
"does the request's primary clause combine a domain noun with a plausible
action/question word, structurally connected — not just co-occurring
somewhere in the message". Concretely: the vocabulary is split into two
sets — `_DOMAIN_NOUNS` (entities/statuses/modalities/department & role
names — the "what this is about" words) and `_ACTION_WORDS` (the
verbs/question words already implicit in the parser grammar and tool
surface — "what this is asking for" words) — and the message is split into
clause fragments on sentence punctuation (`.`/`?`/`!`) and on the
coordinating conjunctions "and"/"but". A request is in-domain only if at
least one fragment contains *both* a domain noun and an action word. This
keeps the same "no NLP parse, no ML" simplicity as before — it's still pure
regex/set-membership — while requiring the domain word to actually be part
of the clause doing the asking, not just present somewhere in the text. A
domain noun with zero action word anywhere (`"mri"` alone as its own
fragment) fails; an action word with zero domain noun anywhere continues to
fail outright, exactly as before.

The vocabulary below is pulled from the system's actual domain surface, not
invented: `app/parser/parser.py`'s grammar (department/scanner/patient/
appointment nouns, MRI/CT/XRAY modalities, AVAILABLE/IN_USE/MAINTENANCE
statuses, delayed appointments, and the list/show/search verbs of its own
grammar), the agent's own tool descriptions and `args_schema` fields in
`app/agent/tools/*.py` (search/reschedule/availability/preference verbs and
nouns), and the seeded department names and staff roles in
`app/seed/seed_data.py`.

Why a separate component from the regex parser, not folded into it: the
parser's job is to *structure* known commands into a `Command` object it can
execute deterministically; this gate's job is only to *screen relevance* —
it never produces a `Command` and is not trying to understand the request,
just to notice when it shares nothing with the domain at all. Conflating the
two would turn the parser into an ever-growing pseudo-NLU layer (exactly
what its own docstring says not to do) while also making this gate's "stay
conservative" property harder to reason about independently.

Defense in depth: this gate is not the only safeguard against the bundled-
off-topic-request pattern. `app/agent/graph.py`'s `SYSTEM_PROMPT` also
carries an explicit instruction for the agent to decline any non-hospital
sub-task that reaches it despite this gate — see that module's docstring.
This gate exists to save the API call in the common case; the prompt
instruction exists for whatever phrasing this gate's simple rules don't
catch.

What calls it: `app/api/routes/chat.py`, for every request the parser
already returned `matched=False` for, before `check_eligibility` runs.
"""

import re
from dataclasses import dataclass

_WORD_RE = re.compile(r"[a-z]+")

# Splits a message into clause-ish fragments: sentence-ending punctuation
# runs, or a standalone coordinating conjunction ("and"/"but"). Deliberately
# simple — this is not sentence-boundary detection, just enough structure to
# tell "two separate thoughts" apart from "one thought with several words".
_FRAGMENT_SPLIT_RE = re.compile(r"[.!?]+|\b(?:and|but)\b")

# Short replies that only make sense in light of a recent turn. These are
# intentionally narrow: context may admit a confirmation or a pronoun-based
# follow-up, but it must not turn an unrelated new request into an agent call.
_CONTEXT_FOLLOWUP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:yes|yep|yeah|sure|okay|ok|no|nope)(?:\s+please)?$"),
    re.compile(r"^(?:please\s+)?(?:go ahead|do it|proceed|confirm|cancel|stop)$"),
    re.compile(
        r"^(?:what|why|how)(?:\s+\w+){0,6}\s+"
        r"(?:happen|happened|change|changed|do|did|find|found)$"
    ),
    re.compile(r"^(?:why|how come)$"),
    re.compile(r"^(?:do|try|run)\s+(?:it|that|this)(?:\s+again)?$"),
    re.compile(r"^(?:explain|show|tell)\s+(?:it|that|this)(?:\s+again)?$"),
    re.compile(
        r"^(?:what|which)\s+(?:about\s+)?" r"(?:it|that|this|them|those|one|ones)$"
    ),
    re.compile(
        r"^(?:(?:tell|show)\s+me\s+(?:more\s+)?about\s+)?(?:the\s+)?"
        r"(?:first|second|third|last|next|previous)\s+one$"
    ),
    re.compile(r"^which\s+one\s+is\s+(?:first|second|third|last|next)$"),
)

# Domain nouns: the "what this is about" vocabulary — entities, statuses,
# modalities, seeded department names and staff roles. Derived from (not
# invented alongside) the actual command grammar and tool surface:
#   - app/parser/parser.py: department/scanner/patient/appointment nouns,
#     _MODALITY_WORDS (mri/ct/xray), _STATUS_WORDS (available/in use/
#     maintenance), "delayed appointments", "next appointment"
#   - app/agent/tools/search_tools.py: status enum (SCHEDULED, DELAYED,
#     COMPLETED, CANCELLED), appointment_type, patient/department/scanner
#   - app/agent/tools/action_tools.py: reschedule, scanner_code, new_start
#   - app/agent/tools/observation_tools.py: scanner availability
#   - app/agent/tools/memory_tools.py: preference/prefer(s)/preferred
#   - app/seed/seed_data.py: department names (Radiology, Cardiology,
#     Orthopedics, Emergency) and staff roles (Radiologist, Technologist,
#     Nurse, Physician)
_DOMAIN_NOUNS: frozenset[str] = frozenset(
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
        "availability",
        "status",
        "statuses",
        # Preference vocabulary (memory_tools.py) — subject matter, not an
        # action, even though it reads like a verb ("what do I prefer").
        "prefer",
        "prefers",
        "preferred",
        "preference",
        "preferences",
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

# Action/question words: the "what this is asking for" vocabulary. Pulled
# from the same grammar/tool surface as the nouns above (list/show/search/
# find/reschedule/move/assign/remember/forget/check are verbs the parser or
# the tool surface already uses) plus the ordinary question words a request
# phrased as a question needs (what/how/which/who/is/are/any/does/do/can/
# will/could/would/when/where) — not hospital-specific on their own, but
# only ever decisive here when paired with a genuine domain noun in the same
# clause fragment, so their genericness doesn't loosen the gate.
_ACTION_WORDS: frozenset[str] = frozenset(
    {
        # Verbs from the parser grammar / tool surface
        "list",
        "show",
        "search",
        "find",
        "reschedule",
        "move",
        "assign",
        "remember",
        "forget",
        "check",
        "free",
        "schedule",
        "scheduling",
        # Question words
        "what",
        "how",
        "which",
        "who",
        "is",
        "are",
        "any",
        "does",
        "do",
        "can",
        "will",
        "could",
        "would",
        "when",
        "where",
        "why",
        "choose",
        "chose",
        "change",
        "changed",
    }
)


@dataclass(frozen=True)
class DomainGateResult:
    in_domain: bool
    reason: str | None = None


def _is_context_followup(text: str) -> bool:
    normalized = " ".join(_WORD_RE.findall(text.lower()))
    return any(pattern.fullmatch(normalized) for pattern in _CONTEXT_FOLLOWUP_PATTERNS)


def check_domain_gate(
    text: str, *, prior_user_messages: list[str] | None = None
) -> DomainGateResult:
    """Cheap, synchronous relevance screen — no LLM call. Blank/whitespace
    input is deliberately let through here (`in_domain=True`): that's
    `check_eligibility`'s job to reject as `empty_request`, not this gate's;
    keeping that one concern in one place matches how the parser/eligibility
    split already works.

    Two-stage check, both against the same normalized text:
      1. If no domain noun appears anywhere in the message, reject
         immediately (`reason="off_topic"`) — identical to the old
         behavior, and still the common case (weather/poetry/math/flight
         requests share zero vocabulary with the domain).
      2. Otherwise, split the message into clause fragments and require at
         least one fragment to contain both a domain noun *and* an action/
         question word. A domain noun sitting alone in its own fragment,
         disconnected from the actual (off-topic) request, fails this stage
         (`reason="off_topic_disconnected"`) — this is what closes the
         `"What's 47 times 12? mri"`-style bypass.
    """
    stripped = text.strip()
    if not stripped:
        return DomainGateResult(in_domain=True)

    # Hyphens stripped (not turned into spaces) so a term like "x-ray" folds
    # into "xray" and still matches the vocabulary, instead of splitting into
    # two unrelated words ("x", "ray"). Other punctuation (.?!,) is left
    # alone here since fragment splitting below depends on some of it.
    normalized = stripped.lower().replace("-", "")

    all_words = set(_WORD_RE.findall(normalized))
    if not (all_words & _DOMAIN_NOUNS):
        # Context only admits narrow follow-up shapes, and only when a recent
        # user turn independently passes this gate. Assistant messages are
        # intentionally excluded by the caller because the canned rejection
        # response itself contains hospital vocabulary.
        if _is_context_followup(stripped):
            recent_messages = (prior_user_messages or [])[-6:]
            if any(check_domain_gate(message).in_domain for message in recent_messages):
                return DomainGateResult(in_domain=True)
        return DomainGateResult(in_domain=False, reason="off_topic")

    for fragment in _FRAGMENT_SPLIT_RE.split(normalized):
        words = set(_WORD_RE.findall(fragment))
        if (words & _DOMAIN_NOUNS) and (words & _ACTION_WORDS):
            return DomainGateResult(in_domain=True)

    return DomainGateResult(in_domain=False, reason="off_topic_disconnected")
