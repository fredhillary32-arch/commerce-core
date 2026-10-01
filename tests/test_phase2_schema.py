"""Schema-level guarantees the application relies on but never enforces itself."""

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DataError, IntegrityError

PHASE_2_TABLES = {
    "users",
    "roles",
    "permissions",
    "user_roles",
    "role_permissions",
    "user_permission_overrides",
    "refresh_tokens",
    "audit_logs",
}


def _insert_user(connection, email: str = "schema@example.com") -> uuid.UUID:
    return connection.execute(
        text("INSERT INTO users (email, password_hash) VALUES (:email, 'x') RETURNING id"),
        {"email": email},
    ).scalar_one()


def _insert_role(connection, name: str = "schema-role") -> uuid.UUID:
    return connection.execute(
        text("INSERT INTO roles (name) VALUES (:name) RETURNING id"), {"name": name}
    ).scalar_one()


def _insert_permission(connection, code: str = "schema.read") -> uuid.UUID:
    return connection.execute(
        text("INSERT INTO permissions (code) VALUES (:code) RETURNING id"), {"code": code}
    ).scalar_one()


def test_all_phase_2_tables_exist(test_engine):
    existing = set(inspect(test_engine).get_table_names())
    assert PHASE_2_TABLES <= existing


def test_users_defaults_match_the_specification(test_engine):
    with test_engine.begin() as connection:
        row = connection.execute(
            text(
                "INSERT INTO users (email, password_hash) VALUES ('defaults@example.com', 'x') "
                "RETURNING email_verified, is_active, authorization_version, created_at, updated_at"
            )
        ).one()

    assert row.email_verified is False
    assert row.is_active is True
    assert row.authorization_version == 1
    assert row.created_at.tzinfo is not None
    assert row.updated_at.tzinfo is not None


def test_user_email_is_unique(test_engine):
    with test_engine.begin() as connection:
        _insert_user(connection, "duplicate@example.com")

    with pytest.raises(IntegrityError):
        with test_engine.begin() as connection:
            _insert_user(connection, "duplicate@example.com")


def test_role_name_and_permission_code_are_unique(test_engine):
    with test_engine.begin() as connection:
        _insert_role(connection, "unique-role")
        _insert_permission(connection, "unique.code")

    with pytest.raises(IntegrityError):
        with test_engine.begin() as connection:
            _insert_role(connection, "unique-role")

    with pytest.raises(IntegrityError):
        with test_engine.begin() as connection:
            _insert_permission(connection, "unique.code")


def test_user_roles_rejects_duplicate_assignment(test_engine):
    with test_engine.begin() as connection:
        user_id = _insert_user(connection)
        role_id = _insert_role(connection)
        connection.execute(
            text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
            {"u": user_id, "r": role_id},
        )

    with pytest.raises(IntegrityError):
        with test_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
                {"u": user_id, "r": role_id},
            )


def test_role_permissions_rejects_duplicate_grant(test_engine):
    with test_engine.begin() as connection:
        role_id = _insert_role(connection)
        permission_id = _insert_permission(connection)
        connection.execute(
            text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:r, :p)"),
            {"r": role_id, "p": permission_id},
        )

    with pytest.raises(IntegrityError):
        with test_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:r, :p)"),
                {"r": role_id, "p": permission_id},
            )


def test_user_roles_rejects_unknown_foreign_keys(test_engine):
    with pytest.raises(IntegrityError):
        with test_engine.begin() as connection:
            connection.execute(
                text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
                {"u": uuid.uuid4(), "r": uuid.uuid4()},
            )


def test_role_still_assigned_to_a_user_cannot_be_deleted(test_engine):
    with test_engine.begin() as connection:
        user_id = _insert_user(connection)
        role_id = _insert_role(connection)
        connection.execute(
            text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
            {"u": user_id, "r": role_id},
        )

    with pytest.raises(IntegrityError):
        with test_engine.begin() as connection:
            connection.execute(text("DELETE FROM roles WHERE id = :r"), {"r": role_id})


def test_permission_override_effect_is_constrained_to_allow_and_deny(test_engine):
    with test_engine.begin() as connection:
        user_id = _insert_user(connection)
        permission_id = _insert_permission(connection)
        for effect in ("ALLOW", "DENY"):
            connection.execute(
                text(
                    "INSERT INTO user_permission_overrides (user_id, permission_id, effect) "
                    "VALUES (:u, :p, :e) "
                    "ON CONFLICT (user_id, permission_id) DO UPDATE SET effect = EXCLUDED.effect"
                ),
                {"u": user_id, "p": permission_id, "e": effect},
            )

    with pytest.raises((IntegrityError, DataError)):
        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE user_permission_overrides SET effect = 'MAYBE' WHERE user_id = :u"
                ),
                {"u": user_id},
            )


def test_refresh_token_hash_is_unique(test_engine):
    with test_engine.begin() as connection:
        user_id = _insert_user(connection)
        connection.execute(
            text(
                "INSERT INTO refresh_tokens (user_id, token_hash, family_id, expires_at) "
                "VALUES (:u, 'hash-1', :f, now() + interval '1 day')"
            ),
            {"u": user_id, "f": uuid.uuid4()},
        )

    with pytest.raises(IntegrityError):
        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO refresh_tokens (user_id, token_hash, family_id, expires_at) "
                    "VALUES (:u, 'hash-1', :f, now() + interval '1 day')"
                ),
                {"u": user_id, "f": uuid.uuid4()},
            )


def test_expected_indexes_exist(test_engine):
    inspector = inspect(test_engine)

    def indexed_columns(table: str) -> set[tuple[str, ...]]:
        return {tuple(index["column_names"]) for index in inspector.get_indexes(table)}

    assert ("role_id",) in indexed_columns("user_roles")
    assert ("permission_id",) in indexed_columns("role_permissions")
    assert ("permission_id",) in indexed_columns("user_permission_overrides")
    assert ("user_id",) in indexed_columns("refresh_tokens")
    assert ("family_id",) in indexed_columns("refresh_tokens")


def test_composite_primary_keys_are_correct(test_engine):
    inspector = inspect(test_engine)

    assert inspector.get_pk_constraint("user_roles")["constrained_columns"] == ["user_id", "role_id"]
    assert inspector.get_pk_constraint("role_permissions")["constrained_columns"] == [
        "role_id",
        "permission_id",
    ]
    assert inspector.get_pk_constraint("user_permission_overrides")["constrained_columns"] == [
        "user_id",
        "permission_id",
    ]


def test_users_table_has_no_authorization_columns(test_engine):
    columns = {column["name"] for column in inspect(test_engine).get_columns("users")}
    assert not columns & {"role", "role_id", "is_admin", "permissions"}
