"""RBAC administration endpoints and the rbac.manage guard protecting them."""

import uuid

import pytest
from sqlalchemy import text

from tests.test_auth_accounts import PASSWORD, auth_header, login, register, register_and_login


@pytest.fixture()
def admin_headers(client, test_engine) -> dict[str, str]:
    """A bootstrapped administrator, appointed the documented way."""
    from app.cli import assign_admin
    from app.db.models.user import User
    from sqlalchemy.orm import Session

    register(client, email="admin@example.com")
    with Session(test_engine) as session:
        user = session.query(User).filter_by(email="admin@example.com").one()
        assign_admin(session, user.id)

    # Login must come after the assignment: the appointment bumps the
    # authorization version, which retires any token issued before it.
    tokens = login(client, email="admin@example.com").json()
    return auth_header(tokens["access_token"])


def user_id_for(test_engine, email: str) -> uuid.UUID:
    with test_engine.connect() as connection:
        return connection.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": email}
        ).scalar_one()


def create_permission(test_engine, code: str) -> None:
    with test_engine.begin() as connection:
        connection.execute(
            text("INSERT INTO permissions (code) VALUES (:c) ON CONFLICT DO NOTHING"), {"c": code}
        )


class TestAccessControl:
    def test_an_anonymous_caller_is_refused(self, client):
        response = client.post("/api/v1/roles", json={"name": "support-agent"})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"

    def test_an_ordinary_authenticated_user_is_refused(self, client):
        tokens = register_and_login(client)

        response = client.post(
            "/api/v1/roles",
            json={"name": "support-agent"},
            headers=auth_header(tokens["access_token"]),
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_the_endpoint_becomes_reachable_after_bootstrap_assignment(
        self, client, test_engine, admin_headers
    ):
        response = client.post(
            "/api/v1/roles", json={"name": "support-agent"}, headers=admin_headers
        )

        assert response.status_code == 201

    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("post", "/api/v1/roles", {"name": "x"}),
            ("get", "/api/v1/roles", None),
            ("post", f"/api/v1/roles/{uuid.uuid4()}/permissions", {"permission_code": "x.y"}),
            ("post", f"/api/v1/users/{uuid.uuid4()}/roles", {"role_name": "admin"}),
            (
                "post",
                f"/api/v1/users/{uuid.uuid4()}/permission-overrides",
                {"permission_code": "x.y", "effect": "DENY"},
            ),
        ],
    )
    def test_every_rbac_endpoint_requires_rbac_manage(self, client, method, path, body):
        tokens = register_and_login(client)

        keyword_arguments = {"json": body} if body is not None else {}
        response = getattr(client, method)(
            path, headers=auth_header(tokens["access_token"]), **keyword_arguments
        )

        assert response.status_code == 403


class TestRoleAdministration:
    def test_a_role_can_be_created_and_listed(self, client, admin_headers):
        created = client.post(
            "/api/v1/roles",
            json={"name": "support-agent", "description": "Handles order queries."},
            headers=admin_headers,
        )

        assert created.status_code == 201
        assert created.json()["name"] == "support-agent"

        listed = client.get("/api/v1/roles", headers=admin_headers)
        assert listed.status_code == 200
        assert {role["name"] for role in listed.json()} == {"admin", "support-agent"}

    def test_a_duplicate_role_name_is_rejected(self, client, admin_headers):
        client.post("/api/v1/roles", json={"name": "support-agent"}, headers=admin_headers)

        response = client.post(
            "/api/v1/roles", json={"name": "support-agent"}, headers=admin_headers
        )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ROLE_ALREADY_EXISTS"

    def test_a_permission_can_be_granted_to_a_role(self, client, test_engine, admin_headers):
        create_permission(test_engine, "orders.read")
        role_id = client.post(
            "/api/v1/roles", json={"name": "support-agent"}, headers=admin_headers
        ).json()["id"]

        response = client.post(
            f"/api/v1/roles/{role_id}/permissions",
            json={"permission_code": "orders.read"},
            headers=admin_headers,
        )

        assert response.status_code == 204
        with test_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM role_permissions rp "
                    "JOIN permissions p ON p.id = rp.permission_id "
                    "WHERE rp.role_id = :r AND p.code = 'orders.read'"
                ),
                {"r": role_id},
            ).scalar() == 1

    def test_granting_to_an_unknown_role_is_reported(self, client, test_engine, admin_headers):
        create_permission(test_engine, "orders.read")

        response = client.post(
            f"/api/v1/roles/{uuid.uuid4()}/permissions",
            json={"permission_code": "orders.read"},
            headers=admin_headers,
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ROLE_NOT_FOUND"

    def test_granting_an_unknown_permission_is_reported(self, client, admin_headers):
        role_id = client.post(
            "/api/v1/roles", json={"name": "support-agent"}, headers=admin_headers
        ).json()["id"]

        response = client.post(
            f"/api/v1/roles/{role_id}/permissions",
            json={"permission_code": "nothing.defined"},
            headers=admin_headers,
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PERMISSION_NOT_FOUND"


class TestUserAuthorizationAdministration:
    def test_assigning_a_role_takes_effect_and_retires_existing_tokens(
        self, client, test_engine, admin_headers
    ):
        create_permission(test_engine, "orders.read")
        role_id = client.post(
            "/api/v1/roles", json={"name": "support-agent"}, headers=admin_headers
        ).json()["id"]
        client.post(
            f"/api/v1/roles/{role_id}/permissions",
            json={"permission_code": "orders.read"},
            headers=admin_headers,
        )
        agent = register_and_login(client, email="agent@example.com")
        agent_id = user_id_for(test_engine, "agent@example.com")

        response = client.post(
            f"/api/v1/users/{agent_id}/roles",
            json={"role_name": "support-agent"},
            headers=admin_headers,
        )

        assert response.status_code == 204
        from app.core.permissions import has_permission
        from sqlalchemy.orm import Session

        with Session(test_engine) as session:
            assert has_permission(session, agent_id, "orders.read") is True

        stale = client.get("/api/v1/users/me", headers=auth_header(agent["access_token"]))
        assert stale.status_code == 401
        assert stale.json()["error"]["code"] == "STALE_AUTHORIZATION"
        assert login(client, email="agent@example.com").status_code == 200

    def test_assigning_a_role_twice_is_rejected(self, client, test_engine, admin_headers):
        client.post("/api/v1/roles", json={"name": "support-agent"}, headers=admin_headers)
        register(client, email="agent@example.com")
        agent_id = user_id_for(test_engine, "agent@example.com")
        body = {"role_name": "support-agent"}
        client.post(f"/api/v1/users/{agent_id}/roles", json=body, headers=admin_headers)

        response = client.post(
            f"/api/v1/users/{agent_id}/roles", json=body, headers=admin_headers
        )

        assert response.status_code == 409

    def test_assigning_an_unknown_role_is_reported(self, client, test_engine, admin_headers):
        register(client, email="agent@example.com")
        agent_id = user_id_for(test_engine, "agent@example.com")

        response = client.post(
            f"/api/v1/users/{agent_id}/roles",
            json={"role_name": "no-such-role"},
            headers=admin_headers,
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ROLE_NOT_FOUND"

    def test_assigning_to_an_unknown_user_is_reported(self, client, admin_headers):
        response = client.post(
            f"/api/v1/users/{uuid.uuid4()}/roles",
            json={"role_name": "admin"},
            headers=admin_headers,
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_an_override_is_created_then_updated_in_place(
        self, client, test_engine, admin_headers
    ):
        create_permission(test_engine, "inventory.adjust")
        register(client, email="picker@example.com")
        picker_id = user_id_for(test_engine, "picker@example.com")
        path = f"/api/v1/users/{picker_id}/permission-overrides"

        assert client.post(
            path, json={"permission_code": "inventory.adjust", "effect": "DENY"},
            headers=admin_headers,
        ).status_code == 204
        assert client.post(
            path, json={"permission_code": "inventory.adjust", "effect": "ALLOW"},
            headers=admin_headers,
        ).status_code == 204

        with test_engine.connect() as connection:
            rows = connection.execute(
                text("SELECT effect FROM user_permission_overrides WHERE user_id = :u"),
                {"u": picker_id},
            ).scalars().all()
        assert rows == ["ALLOW"]

    def test_each_override_change_bumps_the_authorization_version(
        self, client, test_engine, admin_headers
    ):
        create_permission(test_engine, "inventory.adjust")
        register(client, email="picker@example.com")
        picker_id = user_id_for(test_engine, "picker@example.com")
        path = f"/api/v1/users/{picker_id}/permission-overrides"

        client.post(
            path, json={"permission_code": "inventory.adjust", "effect": "DENY"},
            headers=admin_headers,
        )
        client.post(
            path, json={"permission_code": "inventory.adjust", "effect": "ALLOW"},
            headers=admin_headers,
        )

        with test_engine.connect() as connection:
            version = connection.execute(
                text("SELECT authorization_version FROM users WHERE id = :u"), {"u": picker_id}
            ).scalar()
        assert version == 3

    def test_an_invalid_override_effect_is_rejected(self, client, test_engine, admin_headers):
        create_permission(test_engine, "inventory.adjust")
        register(client, email="picker@example.com")
        picker_id = user_id_for(test_engine, "picker@example.com")

        response = client.post(
            f"/api/v1/users/{picker_id}/permission-overrides",
            json={"permission_code": "inventory.adjust", "effect": "MAYBE"},
            headers=admin_headers,
        )

        assert response.status_code == 422

    def test_an_override_for_an_unknown_permission_is_reported(
        self, client, test_engine, admin_headers
    ):
        register(client, email="picker@example.com")
        picker_id = user_id_for(test_engine, "picker@example.com")

        response = client.post(
            f"/api/v1/users/{picker_id}/permission-overrides",
            json={"permission_code": "nothing.defined", "effect": "DENY"},
            headers=admin_headers,
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PERMISSION_NOT_FOUND"


class TestAuthorizationAuditTrail:
    def test_rbac_changes_are_recorded(self, client, test_engine, admin_headers):
        create_permission(test_engine, "orders.read")
        role_id = client.post(
            "/api/v1/roles", json={"name": "support-agent"}, headers=admin_headers
        ).json()["id"]
        client.post(
            f"/api/v1/roles/{role_id}/permissions",
            json={"permission_code": "orders.read"},
            headers=admin_headers,
        )
        register(client, email="agent@example.com")
        agent_id = user_id_for(test_engine, "agent@example.com")
        client.post(
            f"/api/v1/users/{agent_id}/roles",
            json={"role_name": "support-agent"},
            headers=admin_headers,
        )
        client.post(
            f"/api/v1/users/{agent_id}/permission-overrides",
            json={"permission_code": "orders.read", "effect": "DENY"},
            headers=admin_headers,
        )

        with test_engine.connect() as connection:
            events = connection.execute(
                text("SELECT event_type FROM audit_logs")
            ).scalars().all()

        assert {
            "rbac.role.created",
            "rbac.role.permission_granted",
            "rbac.role.assigned",
            "rbac.override.set",
        } <= set(events)

    def test_authentication_events_are_recorded_without_secrets(self, client, test_engine):
        register(client)
        login(client)
        login(client, password="wrong-password-entirely")

        with test_engine.connect() as connection:
            rows = connection.execute(
                text("SELECT event_type, details::text FROM audit_logs")
            ).all()

        types = {row.event_type for row in rows}
        assert {"account.registered", "auth.login.succeeded", "auth.login.failed"} <= types
        for row in rows:
            assert PASSWORD not in row.details
            assert "wrong-password-entirely" not in row.details
