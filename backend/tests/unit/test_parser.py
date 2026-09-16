"""Unit tests for the deterministic parser (concept 54's "parser" half).
No database, no LLM — pure grammar-matching logic."""

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
