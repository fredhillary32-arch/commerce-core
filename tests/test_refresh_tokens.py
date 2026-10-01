"""Refresh-token rotation, family scoping and reuse detection."""

from sqlalchemy import text

from app.core.security import hash_refresh_token
from tests.test_auth_accounts import (
    PASSWORD,
    auth_header,
    login,
    register,
    register_and_login,
    set_user_flag,
)


def refresh(client, token: str):
    return client.post("/api/v1/auth/refresh", json={"refresh_token": token})


def token_row(engine, raw_token: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT family_id, revoked_at, rotated_at, replaced_by_id "
                "FROM refresh_tokens WHERE token_hash = :h"
            ),
            {"h": hash_refresh_token(raw_token)},
        ).one()


def live_token_count(engine) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL")
        ).scalar()


class TestRotation:
    def test_refresh_returns_a_new_token_pair(self, client):
        tokens = register_and_login(client)

        response = refresh(client, tokens["refresh_token"])

        assert response.status_code == 200
        rotated = response.json()
        assert rotated["refresh_token"] != tokens["refresh_token"]
        assert rotated["access_token"]

    def test_rotation_keeps_the_family_and_links_the_replacement(self, client, test_engine):
        tokens = register_and_login(client)

        rotated = refresh(client, tokens["refresh_token"]).json()

        original = token_row(test_engine, tokens["refresh_token"])
        replacement = token_row(test_engine, rotated["refresh_token"])
        assert replacement.family_id == original.family_id
        assert original.rotated_at is not None
        assert original.replaced_by_id is not None

    def test_the_replacement_token_can_itself_be_refreshed(self, client):
        tokens = register_and_login(client)

        second = refresh(client, tokens["refresh_token"]).json()
        third = refresh(client, second["refresh_token"])

        assert third.status_code == 200
        assert third.json()["refresh_token"] not in (
            tokens["refresh_token"],
            second["refresh_token"],
        )

    def test_an_unknown_token_is_rejected(self, client):
        register_and_login(client)

        response = refresh(client, "a-token-that-was-never-issued")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"

    def test_an_expired_token_is_rejected(self, client, test_engine):
        tokens = register_and_login(client)
        with test_engine.begin() as connection:
            connection.execute(
                text("UPDATE refresh_tokens SET expires_at = now() - interval '1 hour'")
            )

        response = refresh(client, tokens["refresh_token"])

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"

    def test_a_revoked_token_is_rejected(self, client):
        tokens = register_and_login(client)
        client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers=auth_header(tokens["access_token"]),
        )

        response = refresh(client, tokens["refresh_token"])

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"

    def test_a_disabled_account_cannot_refresh(self, client, test_engine):
        tokens = register_and_login(client)
        set_user_flag(test_engine, "shopper@example.com", "is_active", False)

        response = refresh(client, tokens["refresh_token"])

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


class TestReuseDetection:
    def test_replaying_a_rotated_token_is_reported_as_reuse(self, client):
        tokens = register_and_login(client)
        refresh(client, tokens["refresh_token"])

        response = refresh(client, tokens["refresh_token"])

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"

    def test_reuse_issues_no_replacement_token(self, client, test_engine):
        tokens = register_and_login(client)
        refresh(client, tokens["refresh_token"])
        with test_engine.connect() as connection:
            before = connection.execute(text("SELECT count(*) FROM refresh_tokens")).scalar()

        refresh(client, tokens["refresh_token"])

        with test_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM refresh_tokens")).scalar() == before

    def test_reuse_revokes_the_whole_family(self, client, test_engine):
        tokens = register_and_login(client)
        second = refresh(client, tokens["refresh_token"]).json()
        third = refresh(client, second["refresh_token"]).json()

        refresh(client, tokens["refresh_token"])

        assert token_row(test_engine, third["refresh_token"]).revoked_at is not None
        assert refresh(client, third["refresh_token"]).status_code == 401

    def test_reuse_leaves_the_users_other_sessions_alone(self, client, test_engine):
        register(client)
        phone = login(client).json()
        laptop = login(client).json()
        assert token_row(test_engine, phone["refresh_token"]).family_id != token_row(
            test_engine, laptop["refresh_token"]
        ).family_id

        refresh(client, phone["refresh_token"])
        reuse = refresh(client, phone["refresh_token"])

        assert reuse.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"
        assert token_row(test_engine, laptop["refresh_token"]).revoked_at is None
        assert refresh(client, laptop["refresh_token"]).status_code == 200

    def test_reuse_is_audited_without_the_raw_token(self, client, test_engine):
        tokens = register_and_login(client)
        refresh(client, tokens["refresh_token"])

        refresh(client, tokens["refresh_token"])

        with test_engine.connect() as connection:
            events = connection.execute(
                text(
                    "SELECT details::text FROM audit_logs "
                    "WHERE event_type = 'auth.refresh_token.reused'"
                )
            ).scalars().all()
        assert len(events) == 1
        assert tokens["refresh_token"] not in events[0]


def test_a_refresh_token_is_row_locked_while_it_is_being_rotated(client, test_engine):
    """Without this lock two simultaneous refreshes could both rotate the
    same token and each mint a replacement, splitting one session in two."""
    import pytest
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from app.modules.auth.repository import lock_refresh_token

    tokens = register_and_login(client)
    digest = hash_refresh_token(tokens["refresh_token"])

    with Session(test_engine) as holder, Session(test_engine) as contender:
        assert lock_refresh_token(holder, digest) is not None

        contender.execute(text("SET LOCAL lock_timeout = '500ms'"))
        with pytest.raises(OperationalError):
            lock_refresh_token(contender, digest)

        contender.rollback()
        holder.rollback()


class TestConcurrentSessions:
    def test_logging_in_again_does_not_disturb_the_existing_session(self, client, test_engine):
        register(client)
        first = login(client).json()

        second = login(client).json()

        assert token_row(test_engine, first["refresh_token"]).revoked_at is None
        assert refresh(client, first["refresh_token"]).status_code == 200
        assert refresh(client, second["refresh_token"]).status_code == 200

    def test_logout_ends_only_the_session_it_was_given(self, client, test_engine):
        register(client)
        phone = login(client).json()
        laptop = login(client).json()

        client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": phone["refresh_token"]},
            headers=auth_header(phone["access_token"]),
        )

        assert token_row(test_engine, phone["refresh_token"]).revoked_at is not None
        assert refresh(client, laptop["refresh_token"]).status_code == 200

    def test_logout_requires_authentication(self, client):
        tokens = register_and_login(client)

        response = client.post(
            "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        )

        assert response.status_code == 401

    def test_logout_rejects_a_token_belonging_to_someone_else(self, client):
        victim = register_and_login(client, email="victim@example.com")
        attacker = register_and_login(client, email="attacker@example.com")

        response = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": victim["refresh_token"]},
            headers=auth_header(attacker["access_token"]),
        )

        assert response.status_code == 401
        assert refresh(client, victim["refresh_token"]).status_code == 200


class TestPasswordChange:
    NEW_PASSWORD = "an-entirely-different-passphrase"

    def _change(self, client, tokens, current=PASSWORD, new=NEW_PASSWORD):
        return client.post(
            "/api/v1/auth/change-password",
            json={"current_password": current, "new_password": new},
            headers=auth_header(tokens["access_token"]),
        )

    def test_requires_authentication(self, client):
        response = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": self.NEW_PASSWORD},
        )

        assert response.status_code == 401

    def test_wrong_current_password_is_rejected(self, client):
        tokens = register_and_login(client)

        response = self._change(client, tokens, current="not-my-password")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"
        assert login(client).status_code == 200

    def test_short_new_password_is_rejected(self, client):
        tokens = register_and_login(client)

        response = self._change(client, tokens, new="short")

        assert response.status_code == 422

    def test_the_new_password_replaces_the_old_one(self, client):
        tokens = register_and_login(client)

        assert self._change(client, tokens).status_code == 204

        assert login(client, password=self.NEW_PASSWORD).status_code == 200
        assert login(client, password=PASSWORD).status_code == 401

    def test_every_session_is_revoked_across_all_families(self, client, test_engine):
        register(client)
        phone = login(client).json()
        laptop = login(client).json()
        tablet = login(client).json()
        assert live_token_count(test_engine) == 3

        self._change(client, phone)

        assert live_token_count(test_engine) == 0
        for session in (phone, laptop, tablet):
            assert refresh(client, session["refresh_token"]).status_code == 401

    def test_authorization_version_is_untouched(self, client, test_engine):
        tokens = register_and_login(client)

        self._change(client, tokens)

        with test_engine.connect() as connection:
            version = connection.execute(
                text("SELECT authorization_version FROM users")
            ).scalar()
        assert version == 1
