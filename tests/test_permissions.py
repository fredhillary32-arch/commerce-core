"""Authorization precedence: user DENY > user ALLOW > role grant > default deny."""

import pytest

from app.core.permissions import has_permission
from app.db.models.rbac import Permission, Role, RolePermission, UserPermissionOverride, UserRole
from app.db.models.user import User


@pytest.fixture()
def user(db_session) -> User:
    user = User(email="resolver@example.com", password_hash="x")
    db_session.add(user)
    db_session.commit()
    return user


def make_role(db_session, name: str, *permission_codes: str) -> Role:
    role = Role(name=name)
    db_session.add(role)
    db_session.flush()
    for code in permission_codes:
        permission = get_or_create_permission(db_session, code)
        db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.commit()
    return role


def get_or_create_permission(db_session, code: str) -> Permission:
    existing = db_session.query(Permission).filter_by(code=code).one_or_none()
    if existing is not None:
        return existing
    permission = Permission(code=code)
    db_session.add(permission)
    db_session.flush()
    return permission


def grant_role(db_session, user: User, role: Role) -> None:
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    db_session.commit()


def set_override(db_session, user: User, code: str, effect: str) -> None:
    permission = get_or_create_permission(db_session, code)
    existing = (
        db_session.query(UserPermissionOverride)
        .filter_by(user_id=user.id, permission_id=permission.id)
        .one_or_none()
    )
    if existing is None:
        db_session.add(
            UserPermissionOverride(
                user_id=user.id, permission_id=permission.id, effect=effect
            )
        )
    else:
        existing.effect = effect
    db_session.commit()


def test_a_user_with_no_roles_is_denied(db_session, user):
    get_or_create_permission(db_session, "orders.read")
    db_session.commit()

    assert has_permission(db_session, user.id, "orders.read") is False


def test_an_unknown_permission_code_is_denied(db_session, user):
    assert has_permission(db_session, user.id, "nothing.defined") is False


def test_a_role_grant_allows_the_permission(db_session, user):
    grant_role(db_session, user, make_role(db_session, "support-agent", "orders.read"))

    assert has_permission(db_session, user.id, "orders.read") is True


def test_one_permission_never_implies_another(db_session, user):
    grant_role(db_session, user, make_role(db_session, "viewer", "inventory.read"))

    assert has_permission(db_session, user.id, "inventory.read") is True
    assert has_permission(db_session, user.id, "inventory.adjust") is False


def test_multiple_roles_combine_their_grants(db_session, user):
    grant_role(
        db_session,
        user,
        make_role(db_session, "support-agent", "orders.read", "orders.update"),
    )
    grant_role(
        db_session,
        user,
        make_role(db_session, "warehouse-manager", "inventory.read", "inventory.adjust"),
    )

    for code in ("orders.read", "orders.update", "inventory.read", "inventory.adjust"):
        assert has_permission(db_session, user.id, code) is True


def test_a_user_deny_overrides_a_role_grant(db_session, user):
    grant_role(
        db_session,
        user,
        make_role(db_session, "warehouse-manager", "inventory.read", "inventory.adjust"),
    )

    set_override(db_session, user, "inventory.adjust", "DENY")

    assert has_permission(db_session, user.id, "inventory.adjust") is False
    assert has_permission(db_session, user.id, "inventory.read") is True


def test_a_user_allow_grants_a_permission_no_role_provides(db_session, user):
    set_override(db_session, user, "orders.refund", "ALLOW")

    assert has_permission(db_session, user.id, "orders.refund") is True


def test_changing_an_override_from_deny_to_allow_updates_the_same_row(db_session, user):
    grant_role(db_session, user, make_role(db_session, "warehouse", "inventory.adjust"))
    set_override(db_session, user, "inventory.adjust", "DENY")
    assert has_permission(db_session, user.id, "inventory.adjust") is False

    set_override(db_session, user, "inventory.adjust", "ALLOW")

    assert has_permission(db_session, user.id, "inventory.adjust") is True
    assert db_session.query(UserPermissionOverride).count() == 1


def test_one_users_override_does_not_affect_another(db_session, user):
    other = User(email="other@example.com", password_hash="x")
    db_session.add(other)
    db_session.commit()
    role = make_role(db_session, "warehouse", "inventory.adjust")
    grant_role(db_session, user, role)
    db_session.add(UserRole(user_id=other.id, role_id=role.id))
    db_session.commit()

    set_override(db_session, user, "inventory.adjust", "DENY")

    assert has_permission(db_session, user.id, "inventory.adjust") is False
    assert has_permission(db_session, other.id, "inventory.adjust") is True
