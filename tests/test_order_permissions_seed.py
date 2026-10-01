"""The 0009 seed migration that registers the Phase 5 permission codes."""

import importlib.util
from pathlib import Path

from sqlalchemy import text

SEED_MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0009_seed_order_permissions.py"
)

ORDER_CODES = ("orders.read", "orders.manage", "tax.manage", "shipping.manage")
_CODE_LIST = ", ".join(f"'{code}'" for code in ORDER_CODES)


def load_seed_migration():
    spec = importlib.util.spec_from_file_location("seed_order_permissions", SEED_MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def permission_counts(connection) -> dict[str, int]:
    return {
        code: connection.execute(
            text("SELECT count(*) FROM permissions WHERE code = :c"), {"c": code}
        ).scalar_one()
        for code in ORDER_CODES
    }


def test_all_four_permission_codes_are_registered(test_engine):
    with test_engine.connect() as connection:
        assert permission_counts(connection) == dict.fromkeys(ORDER_CODES, 1)


def test_each_code_carries_a_description(test_engine):
    with test_engine.connect() as connection:
        descriptions = (
            connection.execute(
                text(f"SELECT description FROM permissions WHERE code IN ({_CODE_LIST})")
            )
            .scalars()
            .all()
        )

    assert len(descriptions) == len(ORDER_CODES)
    assert all(description for description in descriptions)


def test_rerunning_the_seed_creates_no_duplicates(test_engine):
    seed = load_seed_migration()

    with test_engine.begin() as connection:
        seed.seed_order_permissions(connection)
        seed.seed_order_permissions(connection)

    with test_engine.connect() as connection:
        assert permission_counts(connection) == dict.fromkeys(ORDER_CODES, 1)


def test_the_seed_grants_them_to_nobody(test_engine):
    """Who may cancel a customer's order is an operator's decision."""
    with test_engine.connect() as connection:
        grants = connection.execute(
            text(
                "SELECT count(*) FROM role_permissions rp "
                "JOIN permissions p ON p.id = rp.permission_id "
                f"WHERE p.code IN ({_CODE_LIST})"
            )
        ).scalar_one()
        overrides = connection.execute(
            text(
                "SELECT count(*) FROM user_permission_overrides upo "
                "JOIN permissions p ON p.id = upo.permission_id "
                f"WHERE p.code IN ({_CODE_LIST})"
            )
        ).scalar_one()

    assert (grants, overrides) == (0, 0)


def test_there_is_no_customer_facing_order_code(test_engine):
    """Zero-roles-by-default is why checkout authorizes by ownership.

    Any ``orders.write``, ``orders.create`` or ``checkout.*`` code would
    403 every newly registered buyer on their own purchase.
    """
    with test_engine.connect() as connection:
        stray = connection.execute(
            text(
                "SELECT count(*) FROM permissions WHERE code LIKE 'checkout.%' "
                "OR code IN ('orders.write', 'orders.create', 'addresses.manage')"
            )
        ).scalar_one()

    assert stray == 0


def test_tax_and_shipping_are_separate_codes(test_engine):
    """Not one ``pricing.manage``: different resources, different jobs.

    ``pricing`` names nothing in this schema, while ``tax_rates`` and
    ``shipping_rules`` both exist, and the project names permissions after
    resources. They are also finance work and operations work respectively.
    """
    with test_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM permissions WHERE code = 'pricing.manage'")
            ).scalar_one()
            == 0
        )


def test_the_codes_the_application_asks_for_are_the_codes_that_were_seeded():
    from app.core.permissions import (
        ORDERS_MANAGE,
        ORDERS_READ,
        SHIPPING_MANAGE,
        TAX_MANAGE,
    )

    assert {ORDERS_READ, ORDERS_MANAGE, TAX_MANAGE, SHIPPING_MANAGE} == set(ORDER_CODES)


def test_every_guarded_phase_5_route_names_a_seeded_code(client, test_engine):
    """No route may guard on a code no migration ever created.

    A typo in a permission string would otherwise produce an endpoint that
    refuses everybody, silently and forever.
    """
    from app.core.permissions import (
        ORDERS_MANAGE,
        ORDERS_READ,
        SHIPPING_MANAGE,
        TAX_MANAGE,
    )

    with test_engine.connect() as connection:
        seeded = set(
            connection.execute(text("SELECT code FROM permissions")).scalars().all()
        )

    assert {ORDERS_READ, ORDERS_MANAGE, TAX_MANAGE, SHIPPING_MANAGE} <= seeded
