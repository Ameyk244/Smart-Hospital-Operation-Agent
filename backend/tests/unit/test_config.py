import pytest
from pydantic import ValidationError

from app.config import Settings


def test_render_postgres_url_uses_asyncpg_driver() -> None:
    settings = Settings(database_url="postgresql://user:pass@host:5432/database")

    assert settings.database_url == "postgresql+asyncpg://user:pass@host:5432/database"


def test_asyncpg_database_url_is_preserved() -> None:
    url = "postgresql+asyncpg://user:pass@host:5432/database"

    assert Settings(database_url=url).database_url == url


def test_cors_origins_are_normalized() -> None:
    settings = Settings(cors_origins="http://localhost:5173/, https://example.com ")

    assert settings.allowed_cors_origins == [
        "http://localhost:5173",
        "https://example.com",
    ]


def test_in_process_runtime_needs_no_harness_token() -> None:
    assert Settings(agent_runtime="in_process", harness_token=None).harness_token is None


def test_harness_runtime_without_token_fails() -> None:
    with pytest.raises(ValidationError, match="HARNESS_TOKEN"):
        Settings(agent_runtime="harness", harness_token=None)


def test_harness_runtime_rejects_a_31_character_token() -> None:
    with pytest.raises(ValidationError, match="HARNESS_TOKEN"):
        Settings(agent_runtime="harness", harness_token="t" * 31)


def test_harness_runtime_accepts_a_32_character_token() -> None:
    assert Settings(agent_runtime="harness", harness_token="t" * 32).agent_runtime == "harness"


def test_harness_token_error_never_contains_the_token() -> None:
    token = "short-secret-value"

    with pytest.raises(ValidationError) as excinfo:
        Settings(agent_runtime="harness", harness_token=token)

    assert token not in str(excinfo.value)


@pytest.mark.parametrize("url", ["localhost:8891", "http://", "ftp://host", "http://host/path", "http://ho st"])
def test_harness_url_must_be_an_http_origin(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(harness_url=url)


@pytest.mark.parametrize(
    "overrides",
    [{"harness_max_tokens": 0}, {"harness_max_tokens": -1}, {"harness_turn_timeout_seconds": 0}, {"harness_turn_timeout_seconds": 3601}],
)
def test_harness_limits_must_be_positive_and_the_deadline_at_most_an_hour(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)

