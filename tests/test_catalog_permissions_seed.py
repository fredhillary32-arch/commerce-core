"""The 0005 seed migration that registers the catalogue permission codes."""

import importlib.util
from pathlib import Path

from sqlalchemy import text

SEED_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0005_seed_catalogue_permissions.py"
)

CATALOGUE_CODES = ("catalog.read", "catalog.write", "images.write")


def load_seed_migration():
    spec = importlib.util.spec_from_file_location("seed_catalogue_permissions", SEED_MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def permission_counts(connection) -> dict[str, int]:
    return {
        code: connection.execute(
            text("SELECT count(*) FROM permissions WHERE code = :c"), {"c": code}
        ).scalar_one()
        for code in CATALOGUE_CODES
    }


def test_the_three_permission_codes_are_registered(test_engine):
    with test_engine.connect() as connection:
        assert permission_counts(connection) == dict.fromkeys(CATALOGUE_CODES, 1)


def test_each_code_carries_a_description(test_engine):
    with test_engine.connect() as connection:
        descriptions = connection.execute(
            text(
                "SELECT description FROM permissions WHERE code IN "
                "('catalog.read', 'catalog.write', 'images.write')"
            )
        ).scalars().all()

    assert all(description for description in descriptions)


def test_rerunning_the_seed_creates_no_duplicates(test_engine):
    seed = load_seed_migration()

    with test_engine.begin() as connection:
        seed.seed_catalogue_permissions(connection)
        seed.seed_catalogue_permissions(connection)

    with test_engine.connect() as connection:
        assert permission_counts(connection) == dict.fromkeys(CATALOGUE_CODES, 1)


def test_the_seed_grants_them_to_nobody(test_engine):
    """Deciding which staff may publish products is not a migration's job."""
    with test_engine.connect() as connection:
        grants = connection.execute(
            text(
                "SELECT count(*) FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE p.code IN ('catalog.read', 'catalog.write', 'images.write')"
            )
        ).scalar_one()
        overrides = connection.execute(
            text(
                "SELECT count(*) FROM user_permission_overrides upo "
                "JOIN permissions p ON p.id = upo.permission_id "
                "WHERE p.code IN ('catalog.read', 'catalog.write', 'images.write')"
            )
        ).scalar_one()

    assert (grants, overrides) == (0, 0)


def test_the_codes_the_application_asks_for_are_the_codes_that_were_seeded():
    from app.core.permissions import CATALOG_READ, CATALOG_WRITE, IMAGES_WRITE

    assert {CATALOG_READ, CATALOG_WRITE, IMAGES_WRITE} == set(CATALOGUE_CODES)
