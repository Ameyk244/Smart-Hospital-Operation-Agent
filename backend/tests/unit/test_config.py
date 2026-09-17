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
