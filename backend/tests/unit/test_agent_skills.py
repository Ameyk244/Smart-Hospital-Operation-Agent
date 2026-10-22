import pytest

from app.agent.skills import _DIR, MAX_SKILLS, SKILL_KEYWORDS, select_skills


@pytest.mark.parametrize("name", SKILL_KEYWORDS)
def test_every_skill_has_a_non_empty_file(name):
    assert (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def test_every_skill_file_is_in_the_table():
    assert {p.stem for p in _DIR.glob("*.md")} == set(SKILL_KEYWORDS)


def test_matching_text_selects_the_skill():
    assert "search_queue" in select_skills("admit the waiting patient to a bed")
    assert "Shift handover" in select_skills("give me the shift handover")


def test_watch_requests_select_the_watches_skill_only_when_the_scheduler_is_on():
    assert "create_watch" in select_skills("alert me when the wait is long", scheduler_enabled=True)
    assert select_skills("alert me when the wait is long") == ""


def test_watches_rank_directly_after_app_help():
    assert list(SKILL_KEYWORDS)[:2] == ["app_help", "watches"]


def test_text_matching_nothing_selects_nothing():
    assert select_skills("list scanners mri available") == ""


def test_keywords_match_whole_words_only():
    assert select_skills("embedded rebedding") == ""


def test_selection_is_capped():
    text = "handover: assign a bed, reschedule, check the queue and the journey"
    assert len(select_skills(text, scheduler_enabled=True).split("\n\n")) == MAX_SKILLS
