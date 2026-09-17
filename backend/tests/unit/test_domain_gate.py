"""Unit tests for the domain-relevance gate (extends concept 44). No
database, no LLM — pure keyword-matching logic, same spirit as
test_parser.py."""

import pytest

from app.agent.domain_gate import check_domain_gate

OFF_TOPIC_REQUESTS = [
    "what's the weather like today",
    "will it rain tomorrow in the city",
    "write me a poem about the ocean",
    "write a haiku about spring",
    "what is 15 * 37",
    "what's the square root of 144",
    "can you book me a flight to Chicago tomorrow",
    "book a flight to Paris for next week",
    "how are you",
    "tell me a joke",
]

AMBIGUOUS_BUT_IN_DOMAIN_REQUESTS = [
    "how many delayed MRI appointments are there",
    "can you free up a scanner",
    "what do I prefer",
    "is the radiology department busy",
    "who's working in cardiology today",
    "any scanners under maintenance right now",
    "find the first delayed CT appointment and move it to an available scanner",
]


@pytest.mark.parametrize("text", OFF_TOPIC_REQUESTS)
def test_off_topic_requests_are_rejected(text):
    result = check_domain_gate(text)
    assert result.in_domain is False
    assert result.reason is not None


@pytest.mark.parametrize("text", AMBIGUOUS_BUT_IN_DOMAIN_REQUESTS)
def test_ambiguous_but_hospital_related_requests_pass(text):
    result = check_domain_gate(text)
    assert result.in_domain is True


def test_case_insensitive_matching():
    result = check_domain_gate("SHOW ME THE SCANNERS")
    assert result.in_domain is True


def test_hyphenated_modality_matches():
    result = check_domain_gate("any x-ray appointments delayed today")
    assert result.in_domain is True


def test_blank_input_defers_to_eligibility_gate():
    # Empty/whitespace-only input is check_eligibility's concern
    # (empty_request), not this gate's — must not be flagged off_topic here.
    result = check_domain_gate("   ")
    assert result.in_domain is True
