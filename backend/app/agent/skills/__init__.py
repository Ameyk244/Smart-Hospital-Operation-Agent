"""Opt-in task guidance appended to the agent's system prompt when the user's
message calls for it (AGENT_SKILLS_ENABLED). Guidance only: no tool is added
and the safety rules stay in the core prompt and the backend gates."""

import re
from functools import cache
from pathlib import Path

MAX_SKILLS = 2

# Order is priority when more than MAX_SKILLS match.
SKILL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "app_help": ("how do i", "how does", "how can i", "what can you do", "trace panel"),
    "watches": ("watch", "watches", "watching", "alert", "alerts", "alert me", "notify me", "monitor", "acknowledge"),
    "shift_handover": ("handover", "hand over", "shift summary", "end of shift"),
    "bed_allocation": ("assign", "admit", "release bed", "discharge", "bed", "beds"),
    "scanner_reschedule": ("reschedule", "rebook", "move", "different scanner"),
    "er_queue": ("queue", "triage", "waiting patients"),
    "patient_journey": ("journey", "where is patient", "patient status"),
}

_DIR = Path(__file__).parent


@cache
def _load(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


def select_skills(user_text: str, scheduler_enabled: bool = False) -> str:
    words = " ".join(re.findall(r"[a-z]+", user_text.lower()))
    padded = f" {words} "
    matched = [
        name
        for name, keywords in SKILL_KEYWORDS.items()
        if (scheduler_enabled or name != "watches") and any(f" {k} " in padded for k in keywords)
    ]
    return "\n\n".join(_load(name) for name in matched[:MAX_SKILLS])
