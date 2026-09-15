from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

_FORBIDDEN_DATABASE_NAME = "ecommerce"


def _reject_forbidden_database(url: str) -> str:
    database_name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    if database_name == _FORBIDDEN_DATABASE_NAME:
        raise ValueError(
            "Refusing to configure the application against the pre-existing "
            f"'{_FORBIDDEN_DATABASE_NAME}' database. Point this at "
            "'commerce_platform' (or a 'commerce_platform'-derived name) instead."
        )
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "commerce-platform-api"
    app_env: str = "development"
    debug: bool = False

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/commerce_platform"
    test_database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/commerce_platform_test"

    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"

    def model_post_init(self, __context: object) -> None:
        _reject_forbidden_database(self.database_url)
        _reject_forbidden_database(self.test_database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
