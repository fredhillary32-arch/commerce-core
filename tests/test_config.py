import pytest

from app.core.config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("APP_NAME", "test-app")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/commerce_platform",
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/commerce_platform_test",
    )
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("API_V1_PREFIX", "/api/v1")

    settings = Settings(_env_file=None)

    assert settings.app_name == "test-app"
    assert settings.app_env == "test"
    assert settings.debug is False
    assert settings.log_level == "DEBUG"
    assert settings.api_v1_prefix == "/api/v1"


def test_settings_database_url_targets_commerce_platform(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/commerce_platform",
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/commerce_platform_test",
    )

    settings = Settings(_env_file=None)

    assert "commerce_platform" in settings.database_url
    assert "ecommerce" not in settings.database_url
    assert "ecommerce" not in settings.test_database_url


def test_settings_rejects_ecommerce_database_url(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/ecommerce",
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/commerce_platform_test",
    )

    with pytest.raises(ValueError, match="ecommerce"):
        Settings(_env_file=None)


def test_settings_rejects_ecommerce_test_database_url(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/commerce_platform",
    )
    monkeypatch.setenv(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/ecommerce",
    )

    with pytest.raises(ValueError, match="ecommerce"):
        Settings(_env_file=None)
