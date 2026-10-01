"""The 0007 seed migration that registers the inventory permission codes."""

import importlib.util
from pathlib import Path

from sqlalchemy import text

SEED_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0007_seed_inventory_permissions.py"
)

INVENTORY_CODES = ("inventory.read", "inventory.adjust")


def load_seed_migration():
    spec = importlib.util.spec_from_file_location("seed_inventory_permissions", SEED_MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def permission_counts(connection) -> dict[str, int]:
    return {
        code: connection.execute(
            text("SELECT count(*) FROM permissions WHERE code = :c"), {"c": code}
        ).scalar_one()
        for code in INVENTORY_CODES
    }


def test_both_permission_codes_are_registered(test_engine):
    with test_engine.connect() as connection:
        assert permission_counts(connection) == dict.fromkeys(INVENTORY_CODES, 1)


def test_each_code_carries_a_description(test_engine):
    with test_engine.connect() as connection:
        descriptions = (
            connection.execute(
                text(
                    "SELECT description FROM permissions WHERE code IN "
                    "('inventory.read', 'inventory.adjust')"
                )
            )
            .scalars()
            .all()
        )

    assert all(description for description in descriptions)


def test_rerunning_the_seed_creates_no_duplicates(test_engine):
    seed = load_seed_migration()

    with test_engine.begin() as connection:
        seed.seed_inventory_permissions(connection)
        seed.seed_inventory_permissions(connection)

    with test_engine.connect() as connection:
        assert permission_counts(connection) == dict.fromkeys(INVENTORY_CODES, 1)


def test_the_seed_grants_them_to_nobody(test_engine):
    """Who may write stock off is an operator's decision, not a migration's."""
    with test_engine.connect() as connection:
        grants = connection.execute(
            text(
                "SELECT count(*) FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                "WHERE p.code IN ('inventory.read', 'inventory.adjust')"
            )
        ).scalar_one()
        overrides = connection.execute(
            text(
                "SELECT count(*) FROM user_permission_overrides upo "
                "JOIN permissions p ON p.id = upo.permission_id "
                "WHERE p.code IN ('inventory.read', 'inventory.adjust')"
            )
        ).scalar_one()

    assert (grants, overrides) == (0, 0)


def test_there_is_no_inventory_write_code(test_engine):
    """Reading stock and changing it are deliberately separate privileges."""
    with test_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM permissions WHERE code = 'inventory.write'")
            ).scalar_one()
            == 0
        )


def test_no_cart_permission_code_was_created(test_engine):
    """Phase 2 locked "new users start with zero roles".

    A cart.* code would therefore 403 every newly registered customer on
    their own cart, so cart endpoints authorize by ownership instead.
    """
    with test_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM permissions WHERE code LIKE 'cart.%'")
            ).scalar_one()
            == 0
        )


def test_the_codes_the_application_asks_for_are_the_codes_that_were_seeded():
    from app.core.permissions import INVENTORY_ADJUST, INVENTORY_READ

    assert {INVENTORY_READ, INVENTORY_ADJUST} == set(INVENTORY_CODES)
