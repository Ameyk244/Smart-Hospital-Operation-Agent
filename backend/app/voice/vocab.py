"""Domain-vocabulary correction for speech-to-text output.

Why it exists: a small Whisper model turns domain acronyms into other words
("MRI" -> "MI"), and everything downstream trusts the transcript. The real
cost was found in a spoken test pass: "which MI scanners are available" was
routed by Jev with the modality filter silently dropped, so it answered with
five scanners including CT and X-ray, confidently and without a hedge.
The same class of error stored "MI scanner 1" as a saved preference and
turned "forget my scanner preference" into "For Get My ...", so nothing was
forgotten. One shared correction, run once right after transcription and
before the parser, domain gate, Jev or any memory tool sees the text, fixes
all three rather than each symptom separately.

Deliberately a short, explicit list of anchored regex rules -- not fuzzy
matching, not a model, not a general spell-checker. Each rule corrects a
mishearing actually observed (or its obvious sibling) and is narrow enough
that the corrected phrase could not have been a legitimate different word in
this domain. Ambiguous cases are left alone on purpose: "four" heard as "for"
("why is scanner for unavailable") cannot be told from real English ("which
scanners are for CT"), so it is not corrected; a wrong guess would be a new
confident-wrong-answer bug.

Voice only. Typed text is what the user meant; rewriting it would be a
surprise with nothing to gain, since typing does not produce these errors.

What calls it: `app/api/routes/voice.py`, once per utterance.
"""

import re
from collections.abc import Callable

_PATIENT_CONTEXT = re.compile(r"\b(?:patient|named|name)\s+$", re.IGNORECASE)
_STATUS_AFTER = r"(?=\s+(?:available|unavailable|free|status|in use|maintenance|down)\b)"


def _match_case(original: str, replacement: str) -> str:
    return replacement.capitalize() if original[:1].isupper() else replacement


def _acronym_unless_name(match: re.Match[str]) -> str:
    """"MI"/"MRR" -> "MRI", but not in "show patient Mi Lee" -- a name."""
    if _PATIENT_CONTEXT.search(match.string[: match.start()]):
        return match.group(0)
    return "MRI"


# (pattern, replacement) applied in order. A callable replacement gets the match.
_Rule = tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]
_RULES: tuple[_Rule, ...] = (
    # Spelled-out or dotted acronyms: "M R I", "M.R.I.", "C T", "C.T.".
    (re.compile(r"\bm(?:\.\s*r\.\s*i\b\.?|\s+r\s+i\b)", re.IGNORECASE), "MRI"),
    (re.compile(r"\bc(?:\.\s*t\b\.?|\s+t\b)", re.IGNORECASE), "CT"),
    # Observed: "MRI" -> "MI" ("which MI scanners", "prefer MI scanner 1"),
    # and "MRR" ("free for MRR").
    (re.compile(r"\b(?:mi|mrr)\b", re.IGNORECASE), _acronym_unless_name),
    # The parser's grammar word is "xray"; "X-ray"/"x ray" never matched it.
    (re.compile(r"\bx[\s-]?ray\b", re.IGNORECASE), "xray"),
    # Observed: "forget my scanner preference" -> "For Get My Scanner ...".
    (re.compile(r"\bfor get\b", re.IGNORECASE), lambda m: _match_case(m.group(0), "forget")),
    # Observed: "appointment" -> "Pointment".
    (re.compile(r"\bpointment(s?)\b", re.IGNORECASE), lambda m: "appointment" + m.group(1)),
    # Observed: "scanner eight" -> "scanner aid"; only right after "scanner".
    (re.compile(r"\b(scanners?)\s+(?:aid|ate)\b", re.IGNORECASE), r"\1 eight"),
    # Observed: "scanner two available" -> "scanner too available"; only when a
    # status word follows, so "is the scanner too busy" is left as spoken.
    (re.compile(r"\b(scanner)\s+too\b" + _STATUS_AFTER, re.IGNORECASE), r"\1 two"),
)


def correct_transcript(text: str) -> str:
    """Apply the domain-vocabulary corrections to one transcript."""
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
