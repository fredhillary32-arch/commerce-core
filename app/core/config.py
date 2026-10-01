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

    # No default: an application that signs access tokens with a value baked
    # into source control is not deployable, so a missing secret must fail
    # loudly at startup rather than silently work in development.
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    # Product image files live in Supabase Storage, never in PostgreSQL. Both
    # of these get the same treatment as jwt_secret_key: no default, so an
    # installation missing them fails at startup instead of failing later on
    # the first upload.
    supabase_url: str
    supabase_service_key: str

    # Not secrets, so they carry defaults. The bucket is private; read access
    # is granted only through short-lived signed URLs valid for this long.
    supabase_product_image_bucket: str = "product-images"
    supabase_signed_url_expiry_seconds: int = 3600

    def model_post_init(self, __context: object) -> None:
        _reject_forbidden_database(self.database_url)
        _reject_forbidden_database(self.test_database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
