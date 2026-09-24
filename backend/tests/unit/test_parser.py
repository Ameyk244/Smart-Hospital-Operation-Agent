"""Unit tests for the deterministic parser (concept 54's "parser" half).
No database, no LLM — pure grammar-matching logic."""

import pytest

from app.parser.parser import parse


def test_list_departments():
    outcome = parse("list departments")
    assert outcome.matched is True
    assert outcome.command.name == "list_departments"
    assert outcome.command.args == {}


def test_list_scanners_no_filters():
    outcome = parse("list scanners")
    assert outcome.matched is True
    assert outcome.command.name == "list_scanners"
    assert outcome.command.args == {}


def test_list_scanners_with_modality():
    outcome = parse("list scanners mri")
    assert outcome.command.args == {"type": "MRI"}


def test_list_scanners_with_status():
    outcome = parse("list scanners that are available")
    assert outcome.command.args == {"status": "AVAILABLE"}


def test_list_scanners_with_modality_and_status():
    outcome = parse("list scanners ct available")
    assert outcome.command.args == {"type": "CT", "status": "AVAILABLE"}


def test_show_patient():
    outcome = parse("show patient John Doe")
    assert outcome.matched is True
    assert outcome.command.name == "search_patients"
    assert outcome.command.args == {"query": "John Doe"}


def test_show_next_appointment():
    outcome = parse("show next appointment")
    assert outcome.matched is True
    assert outcome.command.name == "show_next_appointment"


def test_show_the_next_appointment_variant():
    outcome = parse("show the next appointment")
    assert outcome.matched is True
    assert outcome.command.name == "show_next_appointment"


def test_list_delayed_appointments():
    outcome = parse("list delayed appointments")
    assert outcome.matched is True
    assert outcome.command.name == "list_delayed_appointments"
    assert outcome.command.args == {}


def test_list_delayed_appointments_with_modality():
    outcome = parse("list delayed appointments mri")
    assert outcome.command.args == {"appointment_type": "MRI"}


def test_case_insensitive():
    outcome = parse("LIST DEPARTMENTS")
    assert outcome.matched is True
    assert outcome.command.name == "list_departments"


def test_leading_trailing_whitespace_ignored():
    outcome = parse("   list departments   ")
    assert outcome.matched is True


def test_unrecognized_text_is_unmatched():
    outcome = parse("find the first delayed CT appointment and move it to an available scanner")
    assert outcome.matched is False
    assert outcome.command is None


def test_empty_string_is_unmatched():
    outcome = parse("")
    assert outcome.matched is False


def test_unmatched_preserves_raw_text():
    outcome = parse("do something clever")
    assert outcome.raw_text == "do something clever"


# --- Trailing punctuation / whitespace ------------------------------------
# Found by running spoken commands through the real pipeline: Whisper adds a
# trailing "." or "?" to most transcripts (9 of 16 in that run), and typed
# input ends in one just as often. Most grammar rules are `$`-anchored, so
# those inputs used to miss the parser and detour through a paid Jev call --
# and "show patient David Davis." matched but searched for the literal
# string "David Davis." and found nobody.

TRAILING_PUNCTUATION = [".", "?", "!", "...", " .", "?!", ";", ",", ":", "…", " . ", "\n"]


@pytest.mark.parametrize("suffix", TRAILING_PUNCTUATION)
@pytest.mark.parametrize(
    "text, command, args",
    [
        ("list departments", "list_departments", {}),
        ("show next appointment", "show_next_appointment", {}),
        ("show the next appointment", "show_next_appointment", {}),
        ("list scanners", "list_scanners", {}),
        ("list scanners ct available", "list_scanners", {"type": "CT", "status": "AVAILABLE"}),
        ("list delayed appointments", "list_delayed_appointments", {}),
        ("list delayed appointments mri", "list_delayed_appointments", {"appointment_type": "MRI"}),
        ("show patient David Davis", "search_patients", {"query": "David Davis"}),
    ],
)
def test_trailing_punctuation_does_not_change_the_parsed_command(text, command, args, suffix):
    outcome = parse(text + suffix)
    assert outcome.matched is True
    assert outcome.command.name == command
    assert outcome.command.args == args


def test_patient_query_never_captures_the_trailing_period():
    """The confidently-wrong case: this used to yield query "David Davis."."""
    outcome = parse("show patient David Davis.")
    assert outcome.command.args == {"query": "David Davis"}


@pytest.mark.parametrize(
    "text",
    [
        "show patient  David   Davis",
        "  show   patient David    Davis  ?  ",
        "show\tpatient\tDavid Davis.",
    ],
)
def test_internal_whitespace_runs_collapse_so_names_still_match(text):
    assert parse(text).command.args == {"query": "David Davis"}


def test_only_trailing_punctuation_is_removed_not_punctuation_inside_a_name():
    outcome = parse("show patient O'Neil-Smith.")
    assert outcome.command.args == {"query": "O'Neil-Smith"}


def test_raw_text_still_reports_what_was_actually_sent():
    outcome = parse("list departments.")
    assert outcome.raw_text == "list departments."


def test_normalization_did_not_make_the_grammar_fuzzy():
    """Punctuation is forgiven; extra words still aren't. Fuzzy phrasing is
    Jev's and the agent's job, not the parser's (see the module docstring)."""
    assert parse("list departments please.").matched is False
    assert parse("please list departments.").matched is False
    assert parse("show me the next appointment.").matched is False
    assert parse("do something clever.").matched is False


@pytest.mark.parametrize("text", ["...", "?", "!?", " . ", "…"])
def test_punctuation_only_input_is_unmatched_and_does_not_crash(text):
    assert parse(text).matched is False
