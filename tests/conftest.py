import os
import subprocess
import sys
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.session import get_db
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_alembic(*args: str) -> subprocess.CompletedProcess:
    """Run Alembic against the dedicated test database, never the dev one."""
    env = os.environ.copy()
    env["DATABASE_URL"] = get_settings().test_database_url
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture(scope="session")
def test_engine() -> Generator[Engine, None, None]:
    settings = get_settings()
    engine = create_engine(settings.test_database_url, pool_pre_ping=True, future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database() -> None:
    result = run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr


# Permission codes the seed migrations create. They survive the per-test
# reset below, so every test starts from a freshly migrated system rather
# than having to re-seed what Alembic already owns.
SEEDED_PERMISSIONS = (
    "rbac.manage",
    "catalog.read",
    "catalog.write",
    "images.write",
    "inventory.read",
    "inventory.adjust",
    "orders.read",
    "orders.manage",
    "tax.manage",
    "shipping.manage",
)

# Child-before-parent, so the plain DELETEs below never trip a foreign key.
# The Phase 4 tables come first: inventory and cart items hold RESTRICT
# foreign keys onto product_variants precisely so that stock records cannot
# be deleted out from under them, and the same rule applies to this reset.
DOMAIN_TABLES = (
    # Phase 5 first: order_items holds a RESTRICT foreign key onto
    # product_variants, and orders hold RESTRICT keys onto both users and
    # carts, so an order that outlived the reset would block everything
    # below it.
    "order_items",
    "orders",
    "addresses",
    "tax_rates",
    "shipping_rules",
    "inventory_adjustments",
    "inventory_transactions",
    "inventory_reservations",
    "inventory",
    "cart_items",
    "carts",
    "variant_option_values",
    "product_variants",
    "product_option_values",
    "product_options",
    "product_images",
    "product_categories",
    "products",
    "categories",
)


@pytest.fixture(autouse=True)
def clean_database(test_engine: Engine, migrated_test_database: None) -> None:
    """Reset to the state the seed migrations leave behind.

    Deleting rather than truncating lets the seeded `admin` role, the
    permission codes and the admin/rbac.manage grant survive, so every test
    starts from a freshly bootstrapped - but not yet administered - system.
    """
    seeded = ", ".join(f"'{code}'" for code in SEEDED_PERMISSIONS)
    with test_engine.begin() as connection:
        for table in DOMAIN_TABLES:
            connection.execute(text(f"DELETE FROM {table}"))
        connection.execute(text("DELETE FROM user_roles"))
        connection.execute(text("DELETE FROM user_permission_overrides"))
        connection.execute(text("DELETE FROM audit_logs"))
        connection.execute(text("DELETE FROM refresh_tokens"))
        connection.execute(text("DELETE FROM users"))
        connection.execute(
            text(
                "DELETE FROM role_permissions WHERE role_id <> "
                "(SELECT id FROM roles WHERE name = 'admin') OR permission_id <> "
                "(SELECT id FROM permissions WHERE code = 'rbac.manage')"
            )
        )
        connection.execute(text("DELETE FROM roles WHERE name <> 'admin'"))
        connection.execute(text(f"DELETE FROM permissions WHERE code NOT IN ({seeded})"))


@pytest.fixture()
def db_session(test_engine: Engine) -> Generator[Session, None, None]:
    testing_session_local = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True)
    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client_with_working_db(app, test_engine: Engine):
    from fastapi.testclient import TestClient

    testing_session_local = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True)

    def _override_get_db() -> Generator[Session, None, None]:
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def client(client_with_working_db):
    return client_with_working_db


@pytest.fixture()
def client_with_unreachable_db(app):
    from fastapi.testclient import TestClient

    # Nothing listens here; a short connect_timeout keeps the test fast.
    broken_engine = create_engine(
        "postgresql+psycopg://postgres:postgres@localhost:1/commerce_platform",
        connect_args={"connect_timeout": 1},
    )
    broken_session_local = sessionmaker(bind=broken_engine, autoflush=False, autocommit=False, future=True)

    def _override_get_db() -> Generator[Session, None, None]:
        db = broken_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    broken_engine.dispose()


# --------------------------------------------------------------------------
# Catalogue fixtures
# --------------------------------------------------------------------------

CATALOGUE_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture()
def authorize(client, test_engine):
    """Register a user holding exactly the named permissions, return its headers.

    Permissions are attached through a role created on the spot, which is how
    a real deployment grants them: migration 0005 registers the codes but
    deliberately grants them to nobody.
    """
    created: list[str] = []

    def _authorize(*permission_codes: str, email: str | None = None) -> dict[str, str]:
        address = email or f"staff{len(created)}@example.com"
        created.append(address)
        client.post(
            "/api/v1/auth/register", json={"email": address, "password": CATALOGUE_PASSWORD}
        )
        with test_engine.begin() as connection:
            user_id = connection.execute(
                text("SELECT id FROM users WHERE email = :e"), {"e": address}
            ).scalar_one()
            role_id = connection.execute(
                text("INSERT INTO roles (name) VALUES (:n) RETURNING id"),
                {"n": f"role-for-{address}"},
            ).scalar_one()
            for code in permission_codes:
                connection.execute(
                    text(
                        "INSERT INTO role_permissions (role_id, permission_id) "
                        "SELECT :r, id FROM permissions WHERE code = :c"
                    ),
                    {"r": role_id, "c": code},
                )
            connection.execute(
                text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
                {"u": user_id, "r": role_id},
            )
            # Mirrors what the RBAC service does, so a token minted before the
            # grant is retired rather than silently under-privileged.
            connection.execute(
                text(
                    "UPDATE users SET authorization_version = authorization_version + 1 "
                    "WHERE id = :u"
                ),
                {"u": user_id},
            )
        tokens = client.post(
            "/api/v1/auth/login", json={"email": address, "password": CATALOGUE_PASSWORD}
        ).json()
        return {"Authorization": f"Bearer {tokens['access_token']}"}

    return _authorize


@pytest.fixture()
def catalog_admin(authorize) -> dict[str, str]:
    """Headers for a user holding every catalogue permission."""
    return authorize("catalog.read", "catalog.write", "images.write")


class FakeStorage:
    """Stand-in for app.core.storage, recording what the service asked for.

    No test reaches Supabase. Uploads are simulated by ``simulate_upload``,
    which is what makes "the object is not there yet" an easy state to test.
    """

    def __init__(self) -> None:
        self.objects: set[str] = set()
        self.issued_paths: list[str] = []
        self.existence_checks: list[str] = []
        self.deleted: list[str] = []
        self.delivery_calls: list[str] = []

    def image_prefix(self, product_id) -> str:
        return f"products/{product_id}/images/"

    def create_upload_target(self, product_id) -> tuple[str, str]:
        path = f"{self.image_prefix(product_id)}{uuid.uuid4()}"
        self.issued_paths.append(path)
        return path, f"https://storage.test/upload/{path}?token=fake-upload-token"

    def simulate_upload(self, storage_path: str) -> None:
        """Pretend the client completed the direct upload to Supabase."""
        self.objects.add(storage_path)

    def object_exists(self, storage_path: str) -> bool:
        self.existence_checks.append(storage_path)
        return storage_path in self.objects

    def delete_object(self, storage_path: str) -> None:
        self.deleted.append(storage_path)
        self.objects.discard(storage_path)

    def get_delivery_url(self, storage_path: str) -> str:
        self.delivery_calls.append(storage_path)
        # Opaque on purpose: a real signed URL carries a token and an expiry,
        # so no test may depend on its shape.
        return f"https://storage.test/signed/{uuid.uuid4()}"


@pytest.fixture()
def fake_storage(monkeypatch) -> FakeStorage:
    from app.core import storage

    fake = FakeStorage()
    for name in ("create_upload_target", "object_exists", "delete_object", "get_delivery_url"):
        monkeypatch.setattr(storage, name, getattr(fake, name))
    return fake
