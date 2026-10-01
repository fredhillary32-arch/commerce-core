"""Every admin catalogue endpoint is gated, and every public one is not."""

import uuid

import pytest

from tests.test_auth_accounts import auth_header, register_and_login

PRODUCT = uuid.uuid4()
OPTION = uuid.uuid4()
VARIANT = uuid.uuid4()
CATEGORY = uuid.uuid4()
IMAGE = uuid.uuid4()

# Every admin route from the Phase 3 specification, with the permission it
# requires. The two DELETE routes added alongside them are listed too, so a
# new endpoint cannot quietly arrive without a guard.
ADMIN_ENDPOINTS = [
    ("post", "/api/v1/products", {"name": "x"}, "catalog.write"),
    ("get", f"/api/v1/products/{PRODUCT}", None, "catalog.read"),
    ("patch", f"/api/v1/products/{PRODUCT}", {"name": "x"}, "catalog.write"),
    ("delete", f"/api/v1/products/{PRODUCT}", None, "catalog.write"),
    ("post", f"/api/v1/products/{PRODUCT}/options", {"name": "Size"}, "catalog.write"),
    (
        "post",
        f"/api/v1/products/{PRODUCT}/options/{OPTION}/values",
        {"value": "Large"},
        "catalog.write",
    ),
    ("post", f"/api/v1/products/{PRODUCT}/variants", {"sku": "X"}, "catalog.write"),
    ("patch", f"/api/v1/variants/{VARIANT}", {"sku": "X"}, "catalog.write"),
    (
        "post",
        f"/api/v1/variants/{VARIANT}/option-values",
        {"option_id": str(OPTION), "option_value_id": str(uuid.uuid4())},
        "catalog.write",
    ),
    ("post", "/api/v1/categories", {"name": "Lighting"}, "catalog.write"),
    ("patch", f"/api/v1/categories/{CATEGORY}", {"name": "Lighting"}, "catalog.write"),
    ("delete", f"/api/v1/categories/{CATEGORY}", None, "catalog.write"),
    (
        "post",
        f"/api/v1/products/{PRODUCT}/categories",
        {"category_id": str(CATEGORY)},
        "catalog.write",
    ),
    ("post", f"/api/v1/products/{PRODUCT}/images/upload-url", None, "images.write"),
    (
        "post",
        f"/api/v1/products/{PRODUCT}/images/confirm",
        {"storage_path": "products/x/images/y"},
        "images.write",
    ),
    ("delete", f"/api/v1/products/{PRODUCT}/images/{IMAGE}", None, "images.write"),
]

PUBLIC_ENDPOINTS = [
    "/api/v1/catalog/products",
    f"/api/v1/catalog/products/{PRODUCT}",
    "/api/v1/catalog/categories",
    f"/api/v1/catalog/categories/{CATEGORY}",
]


def call(client, method, path, body, headers=None):
    keywords = {"json": body} if body is not None else {}
    return getattr(client, method)(path, headers=headers or {}, **keywords)


@pytest.mark.parametrize("method,path,body,permission", ADMIN_ENDPOINTS)
class TestAdminEndpointsAreGated:
    def test_an_anonymous_caller_is_refused(self, client, method, path, body, permission):
        response = call(client, method, path, body)

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"

    def test_an_authenticated_user_without_the_permission_is_refused(
        self, client, method, path, body, permission
    ):
        tokens = register_and_login(client, email="nobody@example.com")

        response = call(client, method, path, body, auth_header(tokens["access_token"]))

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_holding_only_the_other_catalogue_permissions_is_not_enough(
        self, client, authorize, method, path, body, permission
    ):
        """catalog.read, catalog.write and images.write never imply each other."""
        others = [
            code
            for code in ("catalog.read", "catalog.write", "images.write")
            if code != permission
        ]

        response = call(client, method, path, body, authorize(*others))

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_the_required_permission_gets_past_the_guard(
        self, client, authorize, fake_storage, method, path, body, permission
    ):
        response = call(client, method, path, body, authorize(permission))

        # Past authorization; these ids are fabricated, so the request then
        # fails on its own merits rather than on who is asking.
        assert response.status_code != 403
        assert response.status_code in (200, 201, 204, 404, 409, 422)

    def test_a_user_level_deny_beats_the_role_grant(
        self, client, test_engine, authorize, method, path, body, permission
    ):
        from sqlalchemy import text

        email = "denied@example.com"
        headers = authorize(permission, email=email)
        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_permission_overrides (user_id, permission_id, effect) "
                    "SELECT u.id, p.id, 'DENY' FROM users u, permissions p "
                    "WHERE u.email = :e AND p.code = :c"
                ),
                {"e": email, "c": permission},
            )

        response = call(client, method, path, body, headers)

        assert response.status_code == 403


@pytest.mark.parametrize("path", PUBLIC_ENDPOINTS)
def test_public_storefront_endpoints_need_no_authentication(client, path):
    response = client.get(path)

    # 404 for the fabricated ids, but never 401 or 403.
    assert response.status_code in (200, 404)


def test_the_catalogue_permissions_are_granted_to_no_role_by_default(client, test_engine):
    """Migration 0005 registers the codes; who holds them is a human decision."""
    from sqlalchemy import text

    with test_engine.connect() as connection:
        granted = connection.execute(
            text(
                "SELECT count(*) FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE p.code IN ('catalog.read', 'catalog.write', 'images.write')"
            )
        ).scalar_one()

    assert granted == 0
