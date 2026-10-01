"""The two adversarial gaps recorded in `phase-2-auth-rbac-as-built.md` §10.

Both are written against **existing, locked Phase 2 code**. They assert what
that code actually guarantees, read from the source, not what a refresh
mechanism might be assumed to do.

**Cross-user refresh family.** The existing "other sessions" test is
same-user, different family. Missing was the case where the token belongs to
somebody else entirely. The behaviour to pin down is *isolation*: whatever
one account's token causes must never reach another account's sessions.

Read from `app/modules/auth/service.py`, the design is:

* ``POST /auth/refresh`` takes **no access token**. Possession of the raw
  refresh token is the whole credential, and the account is resolved from
  ``stored.user_id``. So the endpoint can never issue tokens *for the
  presenter* - it issues tokens for the token's owner, whoever asks.
* Reuse revokes ``stored.family_id`` via ``repository.revoke_family``, which
  is scoped to one family. Not the user's other families, and certainly not
  another user's.
* ``POST /auth/logout`` *is* authenticated and **does** check ownership
  (``stored.user_id != user.id``), answering with the same
  ``INVALID_REFRESH_TOKEN`` it gives for a token that does not exist.

**Concurrent password change.** The auth suite contains no thread-based
test at all; the existing rotation-lock test is two sequential sessions
with a lock timeout. What the as-built design guarantees is stated in §10
and in ``change_password`` itself: every session is revoked across all
families, and ``authorization_version`` is **deliberately** left alone.

> ⚠️ That last point contradicts a common assumption, and it is asserted
> here explicitly rather than glossed over: because
> ``authorization_version`` is not bumped, an access token minted before a
> password change **stays valid until it expires** (15 minutes by default).
> Only refresh tokens are killed immediately. See
> ``TestWhatAPasswordChangeDoesNotDo``.
"""

import threading
import uuid

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.security import decode_access_token, hash_refresh_token
from app.db.models.user import User
from app.modules.audit.service import ClientInfo
from app.modules.auth import service
from tests.test_auth_accounts import PASSWORD, auth_header, login, register
from tests.test_refresh_tokens import refresh

JOIN_TIMEOUT = 30.0


def account(client, email: str) -> dict:
    """A registered, logged-in account. Returns its first token pair."""
    register(client, email=email)
    return login(client, email=email).json()


def live_tokens_of(engine, email: str) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM refresh_tokens rt JOIN users u ON u.id = rt.user_id "
                "WHERE u.email = :e AND rt.revoked_at IS NULL"
            ),
            {"e": email},
        ).scalar_one()


def user_id_of(engine, email: str) -> uuid.UUID:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": email}
        ).scalar_one()


# --------------------------------------------------------------------------
# Cross-user refresh-token families
# --------------------------------------------------------------------------


class TestAnotherAccountsRefreshToken:
    """Whatever A's token causes must never reach B's sessions."""

    def test_refreshing_someone_elses_token_issues_tokens_for_them_not_you(
        self, client, test_engine
    ):
        """The endpoint is unauthenticated: the token names the account.

        This is the property that matters. A refresh endpoint that resolved
        the account from an access token *and* accepted an arbitrary refresh
        token would let a holder of somebody's leaked token mint tokens
        under their own identity. This one cannot: the user comes from
        ``stored.user_id``.
        """
        alice = account(client, "alice@example.com")
        account(client, "bob@example.com")

        response = refresh(client, alice["refresh_token"])

        assert response.status_code == 200
        issued_for = decode_access_token(response.json()["access_token"]).user_id
        assert issued_for == user_id_of(test_engine, "alice@example.com")
        assert issued_for != user_id_of(test_engine, "bob@example.com")

    def test_it_leaves_the_other_accounts_sessions_untouched(self, client, test_engine):
        alice = account(client, "alice2@example.com")
        account(client, "bob2@example.com")
        login(client, email="bob2@example.com")
        assert live_tokens_of(test_engine, "bob2@example.com") == 2

        refresh(client, alice["refresh_token"])

        assert live_tokens_of(test_engine, "bob2@example.com") == 2

    def test_reusing_someone_elses_rotated_token_revokes_only_their_family(
        self, client, test_engine
    ):
        """The adversarial case: can one account's reuse kill another's sessions?

        Reuse is the most destructive thing a refresh token can trigger -
        it revokes a whole family. If that revocation were scoped to the
        user rather than the family, or worse not scoped at all, anyone
        holding a leaked token could log other people out.
        """
        alice_first = account(client, "alice3@example.com")
        alice_second = login(client, email="alice3@example.com").json()
        bob = account(client, "bob3@example.com")
        login(client, email="bob3@example.com")

        # Rotate, then replay: the replay is what triggers reuse detection.
        refresh(client, alice_first["refresh_token"])
        replayed = refresh(client, alice_first["refresh_token"])

        assert replayed.status_code == 401
        assert replayed.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"

        # Alice's own second session, in a different family, survives.
        assert refresh(client, alice_second["refresh_token"]).status_code == 200
        # Bob is entirely unaffected: both of his sessions still live.
        assert live_tokens_of(test_engine, "bob3@example.com") == 2
        assert refresh(client, bob["refresh_token"]).status_code == 200

    def test_logging_out_with_someone_elses_token_is_refused(self, client, test_engine):
        alice = account(client, "alice4@example.com")
        bob = account(client, "bob4@example.com")

        response = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": alice["refresh_token"]},
            headers=auth_header(bob["access_token"]),
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"

    def test_the_refused_logout_revokes_nothing(self, client, test_engine):
        alice = account(client, "alice5@example.com")
        bob = account(client, "bob5@example.com")

        client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": alice["refresh_token"]},
            headers=auth_header(bob["access_token"]),
        )

        assert live_tokens_of(test_engine, "alice5@example.com") == 1
        assert refresh(client, alice["refresh_token"]).status_code == 200

    def test_it_does_not_disclose_whether_the_token_exists(self, client):
        """Another account's token and a token that never existed answer alike.

        A distinguishable response would turn the logout endpoint into an
        oracle for whether a captured token string is live.
        """
        bob = account(client, "bob6@example.com")
        alice = account(client, "alice6@example.com")

        someone_elses = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": alice["refresh_token"]},
            headers=auth_header(bob["access_token"]),
        )
        nonexistent = client.post(
            "/api/v1/auth/logout",
            json={"refresh_token": "this-token-was-never-issued"},
            headers=auth_header(bob["access_token"]),
        )

        assert someone_elses.status_code == nonexistent.status_code
        assert someone_elses.json() == nonexistent.json()

    def test_the_ownership_check_is_what_refuses_it(self):
        """A direct reading, so the check cannot quietly disappear.

        The behavioural tests above are the real proof; this one makes a
        deletion fail loudly and specifically rather than as a subtle
        change in an authorization test somewhere else.
        """
        import inspect

        source = inspect.getsource(service.logout)

        assert "stored.user_id != user.id" in source


# --------------------------------------------------------------------------
# Concurrent password change
# --------------------------------------------------------------------------


class TestConcurrentPasswordChange:
    FIRST = "first-new-passphrase-entirely"
    SECOND = "second-new-passphrase-entirely"

    def change_on_its_own_session(self, session_factory, user_id, new_password, outcomes, name):
        session = session_factory()
        try:
            user = session.get(User, user_id)
            service.change_password(
                session,
                user=user,
                current_password=PASSWORD,
                new_password=new_password,
                client=ClientInfo(),
            )
            outcomes[name] = "ok"
        except Exception as error:  # noqa: BLE001 - the outcome is the assertion
            outcomes[name] = error
        finally:
            session.close()

    def race(self, client, test_engine, email: str):
        """Two simultaneous password changes on two real connections."""
        register(client, email=email)
        sessions = [login(client, email=email).json() for _ in range(3)]
        user_id = user_id_of(test_engine, email)
        session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

        outcomes: dict[str, object] = {}
        threads = [
            threading.Thread(
                target=self.change_on_its_own_session,
                args=(session_factory, user_id, password, outcomes, name),
            )
            for name, password in (("first", self.FIRST), ("second", self.SECOND))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)

        assert all(not thread.is_alive() for thread in threads), "a thread never finished"
        return sessions, outcomes

    def test_exactly_one_new_password_wins(self, client, test_engine):
        """The final state is one coherent password, never a blend of two."""
        self.race(client, test_engine, "racer@example.com")

        accepted = [
            candidate
            for candidate in (self.FIRST, self.SECOND)
            if login(client, email="racer@example.com", password=candidate).status_code == 200
        ]

        assert len(accepted) == 1, f"{len(accepted)} of the two new passwords work"

    def test_the_old_password_stops_working(self, client, test_engine):
        self.race(client, test_engine, "racer2@example.com")

        assert login(client, email="racer2@example.com", password=PASSWORD).status_code == 401

    def test_every_pre_change_refresh_token_is_revoked(self, client, test_engine):
        """The guarantee §10 records: every session, across all families."""
        sessions, _ = self.race(client, test_engine, "racer3@example.com")

        assert live_tokens_of(test_engine, "racer3@example.com") == 0
        for session in sessions:
            assert refresh(client, session["refresh_token"]).status_code == 401

    def test_no_session_survives_in_a_half_revoked_state(self, client, test_engine):
        """Both changes revoke everything, so neither can leave a gap.

        The hazard a race would produce is a token issued or spared by one
        transaction while the other was revoking - a live session under a
        password nobody holds.
        """
        self.race(client, test_engine, "racer4@example.com")

        with test_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT count(*) FROM refresh_tokens rt JOIN users u ON u.id = rt.user_id "
                    "WHERE u.email = :e AND rt.revoked_at IS NULL"
                ),
                {"e": "racer4@example.com"},
            ).scalar_one()

        assert rows == 0

    def test_authorization_version_is_untouched_by_either_change(self, client, test_engine):
        """Deliberate: a password change alters credentials, not privileges."""
        self.race(client, test_engine, "racer5@example.com")

        with test_engine.connect() as connection:
            version = connection.execute(
                text("SELECT authorization_version FROM users WHERE email = :e"),
                {"e": "racer5@example.com"},
            ).scalar_one()

        assert version == 1

    def test_neither_change_reports_a_failure(self, client, test_engine):
        """Both supplied the correct current password, so both are legitimate."""
        _, outcomes = self.race(client, test_engine, "racer6@example.com")

        failures = [out for out in outcomes.values() if isinstance(out, Exception)]
        assert not failures, f"a concurrent password change errored: {failures}"


class TestWhatAPasswordChangeDoesNotDo:
    """The exposure this design accepts, asserted so it cannot be forgotten.

    ``change_password`` deliberately does not bump ``authorization_version``:
    *"Authorization is unchanged, so authorization_version deliberately
    stays put."* ``get_current_user`` compares that version per request, so
    an access token minted **before** the change keeps working until it
    expires.

    This is recorded rather than fixed. Changing it would alter locked
    Phase 2 behaviour, and whether a password change should immediately end
    every access token is a security-policy decision, not an implementer's.
    See the Phase 6 report.
    """

    def test_an_access_token_from_before_the_change_still_works(self, client):
        register(client, email="lingering@example.com")
        tokens = login(client, email="lingering@example.com").json()

        changed = client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": PASSWORD,
                "new_password": "a-brand-new-passphrase-here",
            },
            headers=auth_header(tokens["access_token"]),
        )
        assert changed.status_code == 204

        still_valid = client.get(
            "/api/v1/users/me", headers=auth_header(tokens["access_token"])
        )

        assert still_valid.status_code == 200, (
            "the access token was rejected - authorization_version must now be "
            "bumped on a password change, so §10 and this test are out of date"
        )

    def test_but_the_refresh_token_is_dead_immediately(self, client):
        """So the exposure is bounded by the access-token lifetime, not open-ended."""
        register(client, email="lingering2@example.com")
        tokens = login(client, email="lingering2@example.com").json()

        client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": PASSWORD,
                "new_password": "a-brand-new-passphrase-here",
            },
            headers=auth_header(tokens["access_token"]),
        )

        assert refresh(client, tokens["refresh_token"]).status_code == 401


def test_the_reuse_revocation_is_scoped_to_one_family(client, test_engine):
    """A direct reading of the scoping the isolation tests depend on."""
    import inspect

    source = inspect.getsource(service.refresh)

    assert "revoke_family(db, stored.family_id" in source
    assert "revoke_all_for_user" not in source
