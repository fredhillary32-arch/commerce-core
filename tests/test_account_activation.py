"""Disabling and re-enabling an account through the management command.

There is no HTTP endpoint for this: suspending an account is an operator
action, and adding an API route for it would widen the RBAC surface that
Phase 2 deliberately keeps to role and permission administration.
"""

import uuid

import pytest
from sqlalchemy import text

from app.core.exceptions import NotFoundError
from app.db.models.user import User
from tests.test_auth_accounts import auth_header, login, register, register_and_login


@pytest.fixture()
def account(db_session) -> User:
    user = User(email="suspendable@example.com", password_hash="x")
    db_session.add(user)
    db_session.commit()
    return user


def audit_event_types(db_session) -> list[str]:
    return list(db_session.execute(text("SELECT event_type FROM audit_logs")).scalars())


def test_disabling_an_account_records_an_audit_event(db_session, account):
    from app.cli import set_account_active

    set_account_active(db_session, account.id, is_active=False)

    db_session.refresh(account)
    assert account.is_active is False
    assert audit_event_types(db_session) == ["account.disabled"]


def test_re_enabling_an_account_records_an_audit_event(db_session, account):
    from app.cli import set_account_active

    set_account_active(db_session, account.id, is_active=False)
    set_account_active(db_session, account.id, is_active=True)

    db_session.refresh(account)
    assert account.is_active is True
    assert audit_event_types(db_session) == ["account.disabled", "account.enabled"]


def test_setting_the_state_it_already_has_changes_nothing(db_session, account):
    from app.cli import set_account_active

    set_account_active(db_session, account.id, is_active=True)

    assert audit_event_types(db_session) == []


def test_disabling_an_account_revokes_every_session(client, db_session, test_engine):
    from app.cli import set_account_active

    register(client)
    login(client)
    login(client)
    user_id = db_session.execute(
        text("SELECT id FROM users WHERE email = 'shopper@example.com'")
    ).scalar_one()
    assert db_session.execute(text("SELECT count(*) FROM refresh_tokens")).scalar() == 2

    set_account_active(db_session, user_id, is_active=False)

    live = db_session.execute(
        text("SELECT count(*) FROM refresh_tokens WHERE revoked_at IS NULL")
    ).scalar()
    assert live == 0


def test_a_disabled_account_is_locked_out_of_everything(client, db_session):
    from app.cli import set_account_active

    tokens = register_and_login(client)
    user_id = db_session.execute(
        text("SELECT id FROM users WHERE email = 'shopper@example.com'")
    ).scalar_one()

    set_account_active(db_session, user_id, is_active=False)

    assert client.get("/api/v1/users/me", headers=auth_header(tokens["access_token"])).status_code == 401
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).status_code == 401
    assert login(client).status_code == 401


def test_it_refuses_an_unknown_user(db_session):
    from app.cli import set_account_active

    with pytest.raises(NotFoundError):
        set_account_active(db_session, uuid.uuid4(), is_active=False)
