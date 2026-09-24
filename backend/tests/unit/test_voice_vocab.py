"""Unit tests for the speech-to-text vocabulary correction. Pure string
logic, no STT, no database. Cases are the real mishearings from the spoken
test pass, plus the near-misses each rule must leave alone."""

import pytest

from app.parser.parser import parse
from app.voice.vocab import correct_transcript

OBSERVED_MISHEARINGS = [
    ("which MI scanners are available.", "which MRI scanners are available."),
    ("Remember that I prefer MI scanner 1.", "Remember that I prefer MRI scanner 1."),
    ("How many scanners are free for MRR?", "How many scanners are free for MRI?"),
    ("For Get My Scanner Preference.", "Forget My Scanner Preference."),
    ("Show scanner aid status.", "Show scanner eight status."),
    ("is scanner too available.", "is scanner two available."),
    ("Move the Pointment 99999 to Scan 999", "Move the appointment 99999 to Scan 999"),
    ("list appointments for pointments", "list appointments for appointments"),
]

SPELLED_OUT_ACRONYMS = [
    ("list scanners M R I", "list scanners MRI"),
    ("list scanners M.R.I. available", "list scanners MRI available"),
    ("list C T scanners", "list CT scanners"),
    ("list C.T. scanners", "list CT scanners"),
    ("list x-ray scanners", "list xray scanners"),
    ("list X ray scanners", "list xray scanners"),
]

MUST_BE_LEFT_ALONE = [
    "why is scanner for unavailable?",  # 'four' vs 'for' is ambiguous: not guessed
    "which scanners are for CT",  # real English 'for'
    "is the scanner too busy",  # 'too' with no status word after it
    "show patient Mi Lee",  # a name, not the acronym
    "find the patient named Mi Chen",
    "list scanners MRI available",  # already right
    "forget my scanner preference",  # already right
    "the aid workers",  # 'aid' not after 'scanner'
    "what time is it, Mr. Ian",
    "show scanner 8 status",
]


@pytest.mark.parametrize(("heard", "expected"), OBSERVED_MISHEARINGS + SPELLED_OUT_ACRONYMS)
def test_known_mishearings_are_corrected(heard, expected):
    assert correct_transcript(heard) == expected


@pytest.mark.parametrize("text", MUST_BE_LEFT_ALONE)
def test_ambiguous_or_correct_text_is_left_alone(text):
    assert correct_transcript(text) == text


def test_correction_is_idempotent():
    for heard, _ in OBSERVED_MISHEARINGS + SPELLED_OUT_ACRONYMS:
        once = correct_transcript(heard)
        assert correct_transcript(once) == once


def test_mi_scanners_now_reach_the_parser_as_mri_scanners():
    """The point of the whole module: the misheard command used to miss the
    parser (and, via Jev, lose its modality filter). Corrected, it is an
    exact deterministic MRI query."""
    misheard = parse("List scanners MI available.")
    assert misheard.matched is False or misheard.command.args.get("type") != "MRI"

    fixed = parse(correct_transcript("List scanners MI available."))
    assert fixed.matched is True
    assert fixed.command.name == "list_scanners"
    assert fixed.command.args == {"type": "MRI", "status": "AVAILABLE"}
