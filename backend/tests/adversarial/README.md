Adversarial tests (fabricated IDs, malformed args, unknown tools, mutation-
via-decomposition attempts, oversized client input) are **not** collected
in this directory — they live alongside the feature they're attacking
(`tests/integration/test_agent_loop.py`, `test_command_tools.py`,
`test_observation_tools.py`, `test_live_agent.py`, `tests/e2e/
test_chat_api.py`), tagged with `@pytest.mark.adversarial`, so each one sits
next to the happy-path tests for the same tool/endpoint rather than being
separated from the context that makes it meaningful.

Run just the adversarial ones with:

```
pytest -m adversarial
```

This directory is kept as a documented convention, not a dumping ground.
