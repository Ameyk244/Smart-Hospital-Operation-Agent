"""Unit tests for extract_text_content — no DB, no LLM.

Regression coverage for a real bug: `/api/chat` was returning the raw
Anthropic content-block list (including a `thinking` block's content and
signature) as the reply text whenever a model turn included one, instead of
just the answer — `str(list_of_blocks)` produces exactly the ugly
Python-repr-looking output that bug shipped. These fixtures are literal
shapes of what `AIMessage.content` looks like in that case; no live API
call needed to verify the fix.
"""

from app.agent.message_text import extract_text_content


def test_plain_string_content_passes_through():
    assert extract_text_content("hello") == "hello"


def test_extracts_text_block_and_discards_empty_thinking_block():
    content = [
        {"type": "thinking", "thinking": "", "signature": "abc123"},
        {"type": "text", "text": "Patient found: Name: David Davis"},
    ]
    assert extract_text_content(content) == "Patient found: Name: David Davis"


def test_extracts_text_block_and_discards_thinking_with_real_content():
    content = [
        {"type": "thinking", "thinking": "Let me search for that patient...", "signature": "xyz"},
        {"type": "text", "text": "There are 4 departments."},
    ]
    assert extract_text_content(content) == "There are 4 departments."


def test_concatenates_multiple_text_blocks():
    content = [{"type": "text", "text": "Part one. "}, {"type": "text", "text": "Part two."}]
    assert extract_text_content(content) == "Part one. Part two."


def test_ignores_non_text_block_types_other_than_thinking():
    content = [
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "text", "text": "Final answer."},
    ]
    assert extract_text_content(content) == "Final answer."


def test_empty_content_list_returns_empty_string():
    assert extract_text_content([]) == ""


def test_never_leaks_thinking_signature_into_the_result():
    content = [
        {"type": "thinking", "thinking": "internal reasoning", "signature": "sig-should-not-leak"},
        {"type": "text", "text": "clean answer"},
    ]
    result = extract_text_content(content)
    assert "sig-should-not-leak" not in result
    assert "internal reasoning" not in result
    assert "thinking" not in result
    assert result == "clean answer"
