"""Registration, login and the authenticated-identity endpoint."""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import text

from app.core.config import get_settings

PASSWORD = "correct-horse-battery-staple"


def register(client, email: str = "shopper@example.com", password: str = PASSWORD):
    return client.post("/api/v1/auth/register", json={"email": email, "password": password})


def login(client, email: str = "shopper@example.com", password: str = PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register_and_login(client, email: str = "shopper@example.com", password: str = PASSWORD) -> dict:
    register(client, email, password)
    return login(client, email, password).json()


def set_user_flag(engine, email: str, column: str, value) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE users SET {column} = :value WHERE email = :email"),
            {"value": value, "email": email},
        )


class TestRegistration:
    def test_registration_creates_an_account(self, client, test_engine):
        response = register(client)

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "shopper@example.com"
        assert body["is_active"] is True
        assert body["email_verified"] is False
        uuid.UUID(body["id"])

        with test_engine.connect() as connection:
            stored = connection.execute(
                text("SELECT password_hash, authorization_version FROM users WHERE email = :e"),
                {"e": "shopper@example.com"},
            ).one()
        assert stored.password_hash.startswith("$argon2id$")
        assert stored.password_hash != PASSWORD
        assert stored.authorization_version == 1

    def test_registration_normalizes_the_email_to_lowercase(self, client, test_engine):
        response = register(client, email="Mixed.Case@Example.COM")

        assert response.status_code == 201
        assert response.json()["email"] == "mixed.case@example.com"

        with test_engine.connect() as connection:
            emails = connection.execute(text("SELECT email FROM users")).scalars().all()
        assert emails == ["mixed.case@example.com"]

    def test_registration_never_returns_the_password_hash(self, client):
        body = register(client).json()

        assert "password_hash" not in body
        assert "password" not in body

    def test_duplicate_email_is_rejected_regardless_of_case(self, client):
        assert register(client, email="dupe@example.com").status_code == 201

        response = register(client, email="DUPE@example.com")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_EMAIL"

    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "not-an-email", "password": PASSWORD},
            {"email": "someone@example.com", "password": "short"},
            {"email": "someone@example.com"},
            {"password": PASSWORD},
        ],
    )
    def test_invalid_registration_input_is_rejected(self, client, payload):
        response = client.post("/api/v1/auth/register", json=payload)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestLogin:
    def test_successful_login_returns_an_access_and_refresh_token(self, client, test_engine):
        register(client)

        response = login(client)

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"] and body["refresh_token"]

        settings = get_settings()
        payload = jwt.decode(
            body["access_token"], settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        assert payload["auth_version"] == 1

        with test_engine.connect() as connection:
            stored = connection.execute(
                text("SELECT token_hash, family_id, revoked_at, rotated_at FROM refresh_tokens")
            ).all()
        assert len(stored) == 1
        assert stored[0].token_hash != body["refresh_token"]
        assert stored[0].revoked_at is None and stored[0].rotated_at is None

    def test_login_accepts_a_differently_cased_email(self, client):
        register(client, email="case@example.com")

        assert login(client, email="CASE@Example.com").status_code == 200

    def test_wrong_password_is_rejected(self, client):
        register(client)

        response = login(client, password="not-the-right-password")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    def test_unknown_account_is_rejected_identically_to_a_wrong_password(self, client):
        register(client)

        wrong_password = login(client, password="not-the-right-password")
        unknown_account = login(client, email="ghost@example.com")

        assert unknown_account.status_code == wrong_password.status_code == 401
        assert unknown_account.json() == wrong_password.json()

    def test_disabled_account_cannot_log_in(self, client, test_engine):
        register(client)
        set_user_flag(test_engine, "shopper@example.com", "is_active", False)

        response = login(client)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"

    def test_disabled_account_is_issued_no_tokens(self, client, test_engine):
        register(client)
        set_user_flag(test_engine, "shopper@example.com", "is_active", False)

        login(client)

        with test_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM refresh_tokens")).scalar() == 0

    def test_unverified_email_does_not_block_login(self, client):
        """Nothing in Phase 2 can set email_verified, so it must not gate login."""
        register(client)

        assert login(client).status_code == 200


class TestCurrentUser:
    def test_returns_the_authenticated_account(self, client):
        tokens = register_and_login(client)

        response = client.get("/api/v1/users/me", headers=auth_header(tokens["access_token"]))

        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "shopper@example.com"
        assert "password_hash" not in body

    def test_requires_a_token(self, client):
        response = client.get("/api/v1/users/me")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"

    def test_rejects_a_malformed_token(self, client):
        response = client.get("/api/v1/users/me", headers=auth_header("not-a-jwt"))

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_ACCESS_TOKEN"

    def test_rejects_a_token_signed_with_another_key(self, client):
        settings = get_settings()
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int((datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()),
                "jti": uuid.uuid4().hex,
                "auth_version": 1,
            },
            "an-attacker-chosen-signing-key-of-sufficient-length",
            algorithm=settings.jwt_algorithm,
        )

        response = client.get("/api/v1/users/me", headers=auth_header(forged))

        assert response.status_code == 401

    def test_rejects_a_token_for_a_user_that_no_longer_exists(self, client):
        from app.core.security import create_access_token

        orphan = create_access_token(user_id=uuid.uuid4(), auth_version=1)

        response = client.get("/api/v1/users/me", headers=auth_header(orphan))

        assert response.status_code == 401

    def test_rejects_a_token_once_the_account_is_disabled(self, client, test_engine):
        tokens = register_and_login(client)
        set_user_flag(test_engine, "shopper@example.com", "is_active", False)

        response = client.get("/api/v1/users/me", headers=auth_header(tokens["access_token"]))

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"

    def test_rejects_a_token_with_a_stale_authorization_version(self, client, test_engine):
        tokens = register_and_login(client)
        with test_engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET authorization_version = authorization_version + 1")
            )

        response = client.get("/api/v1/users/me", headers=auth_header(tokens["access_token"]))

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "STALE_AUTHORIZATION"
