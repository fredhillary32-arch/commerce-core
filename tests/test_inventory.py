"""Provisioning, the available calculation, and inventory authorization."""

import uuid

import pytest
from sqlalchemy import text

from tests.catalog_helpers import activate_product_with_variant
from tests.inventory_helpers import (
    INVENTORY,
    list_reservations,
    list_transactions,
    provision,
    read_inventory,
    restock,
)


@pytest.fixture()
def inventory_admin(authorize) -> dict[str, str]:
    return authorize("inventory.read", "inventory.adjust", email="stock-admin@example.com")


@pytest.fixture()
def inventory_reader(authorize) -> dict[str, str]:
    return authorize("inventory.read", email="stock-reader@example.com")


@pytest.fixture()
def variant(client, catalog_admin) -> str:
    _, created = activate_product_with_variant(client, catalog_admin)
    return created


class TestProvisioning:
    def test_a_variant_is_provisioned_with_everything_at_zero(
        self, client, inventory_admin, variant
    ):
        """Zero, not a guess: how much is in a warehouse is not knowable here.

        Stock enters through an adjustment, which writes a ledger entry, so
        the reconciliation invariant holds from the very first row.
        """
        response = provision(client, inventory_admin, variant)

        assert response.status_code == 201
        body = response.json()
        assert body["variant_id"] == variant
        assert body["quantity_on_hand"] == 0
        assert body["quantity_reserved"] == 0
        assert body["quantity_damaged"] == 0
        assert body["quantity_quarantined"] == 0
        assert body["available"] == 0

    def test_provisioning_twice_is_refused(self, client, inventory_admin, variant):
        provision(client, inventory_admin, variant)

        response = provision(client, inventory_admin, variant)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "INVENTORY_ALREADY_EXISTS"

    def test_provisioning_an_unknown_variant_is_refused(self, client, inventory_admin):
        response = provision(client, inventory_admin, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "VARIANT_NOT_FOUND"

    def test_a_variant_created_after_phase_4_has_no_inventory_until_provisioned(
        self, client, inventory_admin, variant
    ):
        """Inventory is deliberately not hooked into variant creation.

        That is locked Phase 3 code, and provisioning is a merchandising
        step: a variant cannot be sold until it is both ACTIVE and stocked.
        """
        response = read_inventory(client, inventory_admin, variant)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INVENTORY_NOT_FOUND"

    def test_provisioning_is_audited(self, client, inventory_admin, variant, test_engine):
        provision(client, inventory_admin, variant)

        with test_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM audit_logs WHERE event_type = 'inventory.provisioned'")
                ).scalar_one()
                == 1
            )


class TestMigrationBackfill:
    def test_every_variant_that_predates_the_migration_has_a_row(self, test_engine):
        """Re-running the backfill must find nothing left to do.

        The per-test reset deletes catalogue rows, so this asserts the
        property the migration guarantees rather than counting rows it left.
        """
        with test_engine.begin() as connection:
            unprovisioned = connection.execute(
                text(
                    "SELECT count(*) FROM product_variants v "
                    "WHERE NOT EXISTS (SELECT 1 FROM inventory i WHERE i.variant_id = v.id)"
                )
            ).scalar_one()

        assert unprovisioned == 0

    def test_the_backfill_is_idempotent(self, test_engine, client, catalog_admin):
        import importlib.util
        from pathlib import Path

        _, variant = activate_product_with_variant(client, catalog_admin)
        path = (
            Path(__file__).resolve().parent.parent
            / "alembic"
            / "versions"
            / "0006_phase_4_cart_and_inventory_schema.py"
        )
        spec = importlib.util.spec_from_file_location("phase_4_schema", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        with test_engine.begin() as connection:
            first = migration.backfill_inventory_for_existing_variants(connection)
            second = migration.backfill_inventory_for_existing_variants(connection)

        assert (first, second) == (1, 0)
        assert variant is not None


class TestAvailableCalculation:
    """available = quantity_on_hand - quantity_reserved, and nothing else.

    The buckets are disjoint: damaged and quarantined units are not part of
    on-hand, so they are not subtracted a second time. Moving stock into
    either bucket reduces what is available by reducing on-hand itself.
    """

    def set_quantities(self, test_engine, variant, **quantities):
        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE inventory SET quantity_on_hand = :on_hand, "
                    "quantity_reserved = :reserved, quantity_damaged = :damaged, "
                    "quantity_quarantined = :quarantined "
                    "WHERE variant_id = :v"
                ),
                {"v": variant, **quantities},
            )

    @pytest.mark.parametrize(
        "quantities,expected",
        [
            ({"on_hand": 0, "reserved": 0, "damaged": 0, "quarantined": 0}, 0),
            ({"on_hand": 10, "reserved": 0, "damaged": 0, "quarantined": 0}, 10),
            ({"on_hand": 10, "reserved": 4, "damaged": 0, "quarantined": 0}, 6),
            ({"on_hand": 10, "reserved": 4, "damaged": 3, "quarantined": 2}, 6),
            ({"on_hand": 10, "reserved": 10, "damaged": 7, "quarantined": 7}, 0),
        ],
    )
    def test_available_across_all_four_quantities(
        self, client, inventory_admin, variant, test_engine, quantities, expected
    ):
        provision(client, inventory_admin, variant)
        self.set_quantities(test_engine, variant, **quantities)

        body = read_inventory(client, inventory_admin, variant).json()

        assert body["available"] == expected

    def test_the_formula_lives_in_exactly_one_place(self):
        """A second copy of this arithmetic is how the two drift apart."""
        from pathlib import Path

        app_directory = Path(__file__).resolve().parent.parent / "app"
        sources = [
            path.read_text(encoding="utf-8")
            for path in app_directory.rglob("*.py")
        ]
        definitions = sum(
            source.count("quantity_on_hand - self.quantity_reserved")
            + source.count("quantity_on_hand - inventory.quantity_reserved")
            for source in sources
        )

        assert definitions == 1


class TestInventoryAuthorization:
    def endpoints(self, variant):
        return [
            ("get", f"{INVENTORY}/{variant}", None, "inventory.read"),
            ("get", f"{INVENTORY}/{variant}/transactions", None, "inventory.read"),
            ("get", f"{INVENTORY}/{variant}/reservations", None, "inventory.read"),
            ("post", INVENTORY, {"variant_id": str(variant)}, "inventory.adjust"),
            (
                "post",
                f"{INVENTORY}/{variant}/adjustments",
                {
                    "transaction_type": "RESTOCK",
                    "quantity": 1,
                    "to_bucket": "ON_HAND",
                    "reason": "x",
                },
                "inventory.adjust",
            ),
        ]

    def call(self, client, method, path, body, headers):
        keywords = {"json": body} if body is not None else {}
        return getattr(client, method)(path, headers=headers, **keywords)

    def test_an_anonymous_caller_is_refused_everywhere(self, client, variant):
        for method, path, body, _ in self.endpoints(variant):
            response = self.call(client, method, path, body, {})

            assert response.status_code == 401, path
            assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"

    def test_an_ordinary_authenticated_customer_is_refused_everywhere(
        self, client, authorize, variant
    ):
        """A registered shopper has zero roles and must not touch stock."""
        customer = authorize(email="shopper@example.com")

        for method, path, body, _ in self.endpoints(variant):
            response = self.call(client, method, path, body, customer)

            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_inventory_read_does_not_imply_inventory_adjust(
        self, client, inventory_reader, variant
    ):
        for method, path, body, permission in self.endpoints(variant):
            if permission != "inventory.adjust":
                continue

            response = self.call(client, method, path, body, inventory_reader)

            assert response.status_code == 403, path
            assert response.json()["error"]["code"] == "PERMISSION_DENIED"

    def test_inventory_adjust_does_not_imply_inventory_read(
        self, client, authorize, variant
    ):
        adjuster = authorize("inventory.adjust", email="stock-writer@example.com")

        for method, path, body, permission in self.endpoints(variant):
            if permission != "inventory.read":
                continue

            response = self.call(client, method, path, body, adjuster)

            assert response.status_code == 403, path

    def test_the_right_permission_gets_past_the_guard(self, client, authorize, variant):
        for index, (method, path, body, permission) in enumerate(self.endpoints(variant)):
            headers = authorize(permission, email=f"holder-{index}@example.com")

            response = self.call(client, method, path, body, headers)

            # Past authorization; the inventory row does not exist, so the
            # request then fails on its own merits rather than on who asks.
            assert response.status_code != 403, path
            assert response.status_code in (200, 201, 404), path

    def test_a_user_level_deny_beats_the_role_grant(self, client, test_engine, authorize, variant):
        email = "denied-stock@example.com"
        headers = authorize("inventory.read", email=email)
        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_permission_overrides (user_id, permission_id, effect) "
                    "SELECT u.id, p.id, 'DENY' FROM users u, permissions p "
                    "WHERE u.email = :e AND p.code = 'inventory.read'"
                ),
                {"e": email},
            )

        assert read_inventory(client, headers, variant).status_code == 403


class TestInventoryReads:
    def test_reading_inventory_reports_every_bucket(self, client, inventory_admin, variant):
        provision(client, inventory_admin, variant)
        restock(client, inventory_admin, variant, 12)

        body = read_inventory(client, inventory_admin, variant).json()

        assert body["quantity_on_hand"] == 12
        assert body["available"] == 12
        assert body["variant_id"] == variant

    def test_reading_an_unknown_variants_inventory_is_refused(self, client, inventory_admin):
        response = read_inventory(client, inventory_admin, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INVENTORY_NOT_FOUND"

    def test_the_transaction_list_starts_empty_and_grows(
        self, client, inventory_admin, variant
    ):
        provision(client, inventory_admin, variant)
        assert list_transactions(client, inventory_admin, variant).json() == []

        restock(client, inventory_admin, variant, 4)

        transactions = list_transactions(client, inventory_admin, variant).json()
        assert len(transactions) == 1
        assert transactions[0]["transaction_type"] == "RESTOCK"

    def test_the_reservation_list_starts_empty(self, client, inventory_admin, variant):
        provision(client, inventory_admin, variant)

        assert list_reservations(client, inventory_admin, variant).json() == []

    def test_reads_are_not_audited(self, client, inventory_admin, variant, test_engine):
        provision(client, inventory_admin, variant)
        read_inventory(client, inventory_admin, variant)
        list_transactions(client, inventory_admin, variant)
        list_reservations(client, inventory_admin, variant)

        with test_engine.connect() as connection:
            events = (
                connection.execute(
                    text("SELECT event_type FROM audit_logs WHERE event_type LIKE 'inventory.%'")
                )
                .scalars()
                .all()
            )

        assert events == ["inventory.provisioned"]
