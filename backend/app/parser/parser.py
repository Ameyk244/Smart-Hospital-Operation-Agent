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
_BED_STATUS_WORDS = {"available": "AVAILABLE", "occupied": "OCCUPIED", "cleaning": "CLEANING"}
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


def _build_list_wards(_match: re.Match) -> Command:
    return Command("list_wards")


def _build_list_beds(match: re.Match) -> Command:
    tail = (match.group("tail") or "").strip().lower()
    for word, status in _BED_STATUS_WORDS.items():
        if word in tail:
            return Command("list_beds", {"status": status})
    return Command("list_beds")


def _build_list_emergency_queue(_match: re.Match) -> Command:
    return Command("list_emergency_queue")


def _build_patient_journey(match: re.Match) -> Command:
    return Command("patient_journey", {"query": match.group("name").strip()})


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


def _build_watch(condition: str) -> Callable[[re.Match], Command]:
    def build(match: re.Match) -> Command:
        args: dict[str, str | int] = {"condition": condition, "threshold": int(match.group("n"))}
        if ward := match.groupdict().get("ward"):
            args["ward_code"] = ward.upper()
        return Command("create_watch", args)

    return build


def _build_list_watches(_match: re.Match) -> Command:
    return Command("list_watches")


def _build_stop_watch(match: re.Match) -> Command:
    return Command("stop_watch", {"task_code": match.group("code").upper()})


def _build_list_alerts(_match: re.Match) -> Command:
    return Command("list_alerts")


def _build_acknowledge_alert(match: re.Match) -> Command:
    return Command("acknowledge_alert", {"alert_id": int(match.group("id"))})


def _build_acknowledge_all_alerts(_match: re.Match) -> Command:
    return Command("acknowledge_all_alerts")


# What may follow `list scanners` / `list delayed appointments`. Anything else
# in the tail (a misheard "MI", a typo, "banana", "please") means this is not
# an exact command, so the parser misses and the request goes to Jev or the
# agent -- instead of running with the unknown word silently ignored and
# returning a confident answer to a question that was not asked. Each word is a
# whole word, so "connected" no longer reads as "ct".
_MODALITY_TAIL = r"(?:mri|ct|xray)"
_STATUS_TAIL = r"(?:available|in[\s-]use|maintenance)"
# "that are" is natural filler ("list scanners that are available"); it carries
# no filter, so allowing it cannot change what is returned.
_SCANNER_TAIL = rf"(?:\s+(?:{_MODALITY_TAIL}|{_STATUS_TAIL}|that|are))*"
_APPOINTMENT_TAIL = rf"(?:\s+{_MODALITY_TAIL})*"
_BED_TAIL = r"(?:\s+(?:available|occupied|cleaning))?"

# Order matters: first match wins, so more specific patterns come first.
_GRAMMAR: list[tuple[re.Pattern, Callable[[re.Match], Command]]] = [
    (re.compile(r"^list\s+departments$", re.IGNORECASE), _build_list_departments),
    (
        re.compile(rf"^list\s+scanners(?P<tail>{_SCANNER_TAIL})$", re.IGNORECASE),
        _build_list_scanners,
    ),
    (re.compile(r"^list\s+wards$", re.IGNORECASE), _build_list_wards),
    (re.compile(rf"^list\s+beds(?P<tail>{_BED_TAIL})$", re.IGNORECASE), _build_list_beds),
    (
        re.compile(r"^(?:show|list)\s+emergency\s+queue$", re.IGNORECASE),
        _build_list_emergency_queue,
    ),
    # Before "show patient <name>", or "show patient journey X" would search for
    # a patient named "journey X".
    (
        re.compile(r"^show\s+(?:patient\s+)?journey\s+(?P<name>.+)$", re.IGNORECASE),
        _build_patient_journey,
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
        re.compile(
            rf"^list\s+delayed\s+appointments(?P<tail>{_APPOINTMENT_TAIL})$", re.IGNORECASE
        ),
        _build_list_delayed_appointments,
    ),
    # Watches and alerts (docs/SCHEDULER.md). Always matched; the commands say
    # so themselves when the scheduler is off.
    (
        re.compile(r"^watch\s+queue\s+wait\s+over\s+(?P<n>\d{1,6})\s+minutes?$", re.IGNORECASE),
        _build_watch("QUEUE_WAIT_OVER"),
    ),
    (
        re.compile(
            r"^watch\s+free\s+beds\s+under\s+(?P<n>\d{1,6})(?:\s+in\s+(?P<ward>WRD-\d+))?$",
            re.IGNORECASE,
        ),
        _build_watch("FREE_BEDS_UNDER"),
    ),
    (
        re.compile(r"^watch\s+delayed\s+appointments\s+over\s+(?P<n>\d{1,6})$", re.IGNORECASE),
        _build_watch("DELAYED_APPOINTMENTS_OVER"),
    ),
    (re.compile(r"^list\s+watches$", re.IGNORECASE), _build_list_watches),
    (re.compile(r"^stop\s+watch\s+(?P<code>TSK-\d+)$", re.IGNORECASE), _build_stop_watch),
    (re.compile(r"^list\s+alerts$", re.IGNORECASE), _build_list_alerts),
    (re.compile(r"^acknowledge\s+alert\s+(?P<id>\d{1,9})$", re.IGNORECASE), _build_acknowledge_alert),
    (re.compile(r"^acknowledge\s+all\s+alerts$", re.IGNORECASE), _build_acknowledge_all_alerts),
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
