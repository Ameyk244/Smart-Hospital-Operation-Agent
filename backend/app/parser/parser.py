"""The deterministic command parser (concepts 14, 16, 17).

Why it exists: this is the fast, no-LLM entrance to the trusted execution
path (docs/ARCHITECTURE.md §1). A fixed, small grammar of regexes is matched
against the raw request text; a match produces a `Command` handed straight
to `CommandRunner` — no model call, no grounding step (grounding exists to
constrain what an LLM can reference; a regex match is not choosing anything
the way a model choosing an ID is). Anything that doesn't match becomes
`ParseOutcome(matched=False, ...)`, which is exactly the signal
`app/agent/eligibility.py` (added in a later phase) uses to decide whether
to invoke the agent at all.

What calls it: `app/api/routes/chat.py` (or wherever the command endpoint
lives) tries this first, on every request, before ever considering the
agent.

Design note: intentionally a short, explicit list of (pattern, builder)
pairs, not a general NLU layer — growing this into something that "sort of"
understands arbitrary phrasing would blur the line the whole architecture
depends on (docs/ARCHITECTURE.md §1): known commands are deterministic,
everything else is the agent's job.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.execution.commands import Command

_MODALITY_WORDS = {"mri", "ct", "xray"}
_STATUS_WORDS = {
    "available": "AVAILABLE",
    "in use": "IN_USE",
    "in-use": "IN_USE",
    "maintenance": "MAINTENANCE",
}


@dataclass(frozen=True)
class ParseOutcome:
    matched: bool
    command: Command | None
    raw_text: str


def _build_list_departments(_match: re.Match) -> Command:
    return Command("list_departments")


def _build_list_scanners(match: re.Match) -> Command:
    tail = (match.group("tail") or "").strip().lower()
    args: dict[str, str] = {}
    for word in _MODALITY_WORDS:
        if word in tail:
            args["type"] = word.upper()
            break
    for phrase, status in _STATUS_WORDS.items():
        if phrase in tail:
            args["status"] = status
            break
    return Command("list_scanners", args)


def _build_search_patients(match: re.Match) -> Command:
    return Command("search_patients", {"query": match.group("name").strip()})


def _build_show_next_appointment(_match: re.Match) -> Command:
    return Command("show_next_appointment")


def _build_list_delayed_appointments(match: re.Match) -> Command:
    tail = (match.group("tail") or "").strip().lower()
    args: dict[str, str] = {}
    for word in _MODALITY_WORDS:
        if word in tail:
            args["appointment_type"] = word.upper()
            break
    return Command("list_delayed_appointments", args)


# Order matters: first match wins, so more specific patterns come first.
_GRAMMAR: list[tuple[re.Pattern, Callable[[re.Match], Command]]] = [
    (re.compile(r"^list\s+departments$", re.IGNORECASE), _build_list_departments),
    (
        re.compile(r"^list\s+scanners(?P<tail>.*)$", re.IGNORECASE),
        _build_list_scanners,
    ),
    (
        re.compile(r"^show\s+patient\s+(?P<name>.+)$", re.IGNORECASE),
        _build_search_patients,
    ),
    (
        re.compile(r"^show\s+(?:the\s+)?next\s+appointment$", re.IGNORECASE),
        _build_show_next_appointment,
    ),
    (
        re.compile(r"^list\s+delayed\s+appointments(?P<tail>.*)$", re.IGNORECASE),
        _build_list_delayed_appointments,
    ),
]


_WHITESPACE_RUN = re.compile(r"\s+")
# Sentence-final punctuation and any whitespace around it. Deliberately only
# ever removed from the *end*: an apostrophe or hyphen inside a name
# ("O'Neil", "Mary-Jane") is real content, a trailing "." or "?" never is.
_TRAILING_PUNCTUATION = re.compile(r"[\s.?!,;:…]+$")


def _normalize(text: str) -> str:
    """Whitespace runs collapse to one space; trailing sentence punctuation
    and surrounding whitespace go.

    Why this lives here and nowhere else: this function is the single choke
    point every request shape passes through -- typed chat, spoken chat
    (Whisper habitually appends a "." or "?" to a transcript), the
    `/api/commands` endpoint, and the agent's `execute_command`
    decomposition -- so one fix covers all of them instead of each entrance
    growing its own cleanup.

    Why it matters (not cosmetic): most grammar rules are `$`-anchored, so
    "list departments." simply missed and detoured through a paid Jev call.
    Worse, `show patient <name>` has no anchor to trip on: "show patient
    David Davis." matched, captured the query "David Davis." (period
    included), and the patient search returned zero rows -- a confidently
    wrong "No patients matched" under a DETERMINISTIC badge, with no error
    anywhere. Collapsing internal whitespace fixes the same class of bug for
    "show patient  David   Davis", whose captured name would otherwise never
    substring-match the stored "David Davis".
    """
    collapsed = _WHITESPACE_RUN.sub(" ", text).strip()
    return _TRAILING_PUNCTUATION.sub("", collapsed)


def parse(text: str) -> ParseOutcome:
    stripped = _normalize(text)
    for pattern, builder in _GRAMMAR:
        match = pattern.match(stripped)
        if match:
            return ParseOutcome(matched=True, command=builder(match), raw_text=text)
    return ParseOutcome(matched=False, command=None, raw_text=text)
