"""Unit tests for extract_entity_codes — no DB, no LLM."""

from app.agent.entity_codes import extract_entity_codes


def test_none_returns_empty_list():
    assert extract_entity_codes(None) == []


def test_flat_list_of_dicts():
    data = [{"code": "DEPT-RAD", "name": "Radiology"}, {"code": "DEPT-ER", "name": "Emergency"}]
    assert extract_entity_codes(data) == ["DEPT-RAD", "DEPT-ER"]


def test_single_dict():
    data = {"code": "APT-2001", "status": "DELAYED"}
    assert extract_entity_codes(data) == ["APT-2001"]


def test_finds_nested_codes_in_relations():
    # Shape of a real AppointmentOut: nested patient/scanner each with
    # their own `code` — all of them should be pulled out, not just the
    # appointment's own.
    data = {
        "code": "APT-2001",
        "patient": {"code": "PT-1001", "name": "Anthony Martin"},
        "scanner": {"code": "SCN-3", "type": "MRI"},
        "staff": None,
    }
    assert extract_entity_codes(data) == ["APT-2001", "PT-1001", "SCN-3"]


def test_deduplicates_while_preserving_first_occurrence_order():
    data = [
        {"code": "APT-2001", "scanner": {"code": "SCN-3"}},
        {"code": "APT-2004", "scanner": {"code": "SCN-3"}},
    ]
    assert extract_entity_codes(data) == ["APT-2001", "SCN-3", "APT-2004"]


def test_non_string_code_field_is_ignored():
    assert extract_entity_codes({"code": 123}) == []


def test_dict_without_code_field_is_skipped_but_children_still_walked():
    data = {"remembered": True, "preference": {"code": "SCN-1"}}
    assert extract_entity_codes(data) == ["SCN-1"]


def test_plain_scalar_returns_empty_list():
    assert extract_entity_codes("just a string") == []
    assert extract_entity_codes(42) == []
