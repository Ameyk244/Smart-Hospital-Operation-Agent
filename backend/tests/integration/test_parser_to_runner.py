"""Parser → CommandRunner tests (concept 54): the deterministic path
end-to-end, from raw text to a database-backed result, with no agent and no
LLM involved anywhere in the call chain.
"""

import pytest

from app.execution.commands import CommandRunner
from app.parser.parser import parse

pytestmark = pytest.mark.integration


async def test_show_patient_end_to_end(seeded_session):
    outcome = parse("show patient a")
    assert outcome.matched is True
    result = await CommandRunner(seeded_session).execute(outcome.command)
    assert result.success is True
    assert len(result.data) > 0


async def test_list_delayed_appointments_end_to_end(seeded_session):
    outcome = parse("list delayed appointments mri")
    result = await CommandRunner(seeded_session).execute(outcome.command)
    assert result.success is True
    assert all(a["status"] == "DELAYED" and a["appointment_type"] == "MRI" for a in result.data)


async def test_unrecognized_text_never_reaches_the_runner(seeded_session):
    outcome = parse("reschedule the earliest delayed MRI to any open scanner")
    assert outcome.matched is False
    assert outcome.command is None
    # By construction there is nothing to execute — this is exactly the
    # boundary app/agent/eligibility.py (later phase) hands off from.
