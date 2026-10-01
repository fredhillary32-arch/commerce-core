"""Manual stock changes: corrections, write-offs, receipts and bucket moves.

Every one of them goes through the same endpoint and writes the same pair of
rows - a ledger entry saying what moved, and an adjustment saying who moved
it and why. Neither exists without the other.
"""

import pytest
from sqlalchemy import text

from tests.catalog_helpers import activate_product_with_variant
from tests.inventory_helpers import adjust, list_transactions, provision, read_inventory


@pytest.fixture()
def inventory_admin(authorize) -> dict[str, str]:
    return authorize("inventory.read", "inventory.adjust", email="stock-admin@example.com")


@pytest.fixture()
def variant(client, catalog_admin, inventory_admin) -> str:
    _, created = activate_product_with_variant(client, catalog_admin)
    provision(client, inventory_admin, created)
    return created


def quantities(client, headers, variant) -> dict:
    return read_inventory(client, headers, variant).json()


def set_reserved(test_engine, variant, reserved) -> None:
    with test_engine.begin() as connection:
        connection.execute(
            text("UPDATE inventory SET quantity_reserved = :r WHERE variant_id = :v"),
            {"r": reserved, "v": variant},
        )


class TestTheWorkedExamples:
    """The three cases the specification spells out, verbatim."""

    def test_on_hand_20_plus_5_is_25(self, client, inventory_admin, variant):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=20,
            to_bucket="ON_HAND",
        )

        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="ADJUSTMENT",
            quantity=5,
            to_bucket="ON_HAND",
        )

        assert response.status_code == 201
        assert response.json()["quantity_on_hand"] == 25

    def test_on_hand_25_minus_2_is_23(self, client, inventory_admin, variant):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=25,
            to_bucket="ON_HAND",
        )

        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="ADJUSTMENT",
            quantity=2,
            from_bucket="ON_HAND",
        )

        assert response.status_code == 201
        assert response.json()["quantity_on_hand"] == 23

    def test_on_hand_10_reserved_8_minus_5_is_rejected(
        self, client, inventory_admin, variant, test_engine
    ):
        """PostgreSQL refuses it, not a Python pre-check.

        The constraint has no window between the check and the write; a
        pre-check would.
        """
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=10,
            to_bucket="ON_HAND",
        )
        set_reserved(test_engine, variant, 8)

        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="ADJUSTMENT",
            quantity=5,
            from_bucket="ON_HAND",
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INVENTORY_ADJUSTMENT"
        assert quantities(client, inventory_admin, variant)["quantity_on_hand"] == 10

    def test_the_rejected_adjustment_writes_no_ledger_row(
        self, client, inventory_admin, variant, test_engine
    ):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=10,
            to_bucket="ON_HAND",
        )
        set_reserved(test_engine, variant, 8)
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="ADJUSTMENT",
            quantity=5,
            from_bucket="ON_HAND",
        )

        transactions = list_transactions(client, inventory_admin, variant).json()

        assert [row["transaction_type"] for row in transactions] == ["RESTOCK"]


class TestValidation:
    def test_zero_is_rejected(self, client, inventory_admin, variant):
        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="ADJUSTMENT",
            quantity=0,
            to_bucket="ON_HAND",
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INVENTORY_ADJUSTMENT"

    def test_a_negative_quantity_is_rejected(self, client, inventory_admin, variant):
        """Quantity is a magnitude; the buckets carry the direction."""
        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="ADJUSTMENT",
            quantity=-5,
            from_bucket="ON_HAND",
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INVENTORY_ADJUSTMENT"

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_a_missing_reason_is_rejected(self, client, inventory_admin, variant, reason):
        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=5,
            to_bucket="ON_HAND",
            reason=reason,
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INVENTORY_ADJUSTMENT"

    def test_naming_no_bucket_at_all_is_rejected(self, client, inventory_admin, variant):
        response = adjust(
            client, inventory_admin, variant, transaction_type="ADJUSTMENT", quantity=5
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INVENTORY_ADJUSTMENT"

    def test_moving_a_bucket_into_itself_is_rejected(self, client, inventory_admin, variant):
        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=1,
            from_bucket="ON_HAND",
            to_bucket="ON_HAND",
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INVENTORY_ADJUSTMENT"

    def test_a_restock_may_not_be_outbound(self, client, inventory_admin, variant):
        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=5,
            from_bucket="ON_HAND",
        )

        assert response.status_code == 422

    def test_a_disposal_may_not_be_inbound(self, client, inventory_admin, variant):
        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="DISPOSAL",
            quantity=5,
            to_bucket="ON_HAND",
        )

        assert response.status_code == 422

    def test_a_transfer_must_name_both_ends(self, client, inventory_admin, variant):
        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=1,
            from_bucket="ON_HAND",
        )

        assert response.status_code == 422

    @pytest.mark.parametrize("transaction_type", ["PURCHASE", "RETURN"])
    def test_sales_and_returns_cannot_be_written_by_hand(
        self, client, inventory_admin, variant, transaction_type
    ):
        """Only a conversion records a purchase; Phase 7 owns returns."""
        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type=transaction_type,
            quantity=1,
            from_bucket="ON_HAND",
        )

        assert response.status_code == 422

    def test_adjusting_a_variant_with_no_inventory_row_is_refused(
        self, client, catalog_admin, inventory_admin
    ):
        _, unprovisioned = activate_product_with_variant(
            client, catalog_admin, name="Nothing Here", sku="NONE-1"
        )

        response = adjust(
            client,
            inventory_admin,
            unprovisioned,
            transaction_type="RESTOCK",
            quantity=1,
            to_bucket="ON_HAND",
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INVENTORY_NOT_FOUND"

    def test_taking_more_than_is_on_hand_is_refused(self, client, inventory_admin, variant):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=3,
            to_bucket="ON_HAND",
        )

        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="DISPOSAL",
            quantity=4,
            from_bucket="ON_HAND",
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INVENTORY_ADJUSTMENT"


class TestBucketTransfers:
    """The operation that makes the damaged and quarantined columns live.

    Shipping constrained columns that nothing can ever write would be the
    speculative infrastructure this project forbids.
    """

    def test_moving_stock_to_damaged_reduces_what_can_be_sold(
        self, client, inventory_admin, variant
    ):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=10,
            to_bucket="ON_HAND",
        )

        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=3,
            from_bucket="ON_HAND",
            to_bucket="DAMAGED",
        )

        body = response.json()
        assert body["quantity_on_hand"] == 7
        assert body["quantity_damaged"] == 3
        assert body["available"] == 7

    def test_a_transfer_leaves_total_physical_stock_unchanged(
        self, client, inventory_admin, variant
    ):
        """The buckets are disjoint, so the total is their sum."""
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=10,
            to_bucket="ON_HAND",
        )

        after = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=3,
            from_bucket="ON_HAND",
            to_bucket="DAMAGED",
        ).json()

        total = (
            after["quantity_on_hand"] + after["quantity_damaged"] + after["quantity_quarantined"]
        )
        assert total == 10

    def test_quarantined_stock_can_be_released_back_to_on_hand(
        self, client, inventory_admin, variant
    ):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=6,
            to_bucket="ON_HAND",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=6,
            from_bucket="ON_HAND",
            to_bucket="QUARANTINED",
        )

        after = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=4,
            from_bucket="QUARANTINED",
            to_bucket="ON_HAND",
        ).json()

        assert after["quantity_on_hand"] == 4
        assert after["quantity_quarantined"] == 2
        assert after["available"] == 4

    def test_damaged_stock_can_be_written_off(self, client, inventory_admin, variant):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=5,
            to_bucket="ON_HAND",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=5,
            from_bucket="ON_HAND",
            to_bucket="DAMAGED",
        )

        after = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="DISPOSAL",
            quantity=5,
            from_bucket="DAMAGED",
        ).json()

        assert after["quantity_damaged"] == 0
        assert after["quantity_on_hand"] == 0

    def test_transferring_more_than_the_source_holds_is_refused(
        self, client, inventory_admin, variant
    ):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=2,
            to_bucket="ON_HAND",
        )

        response = adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=3,
            from_bucket="ON_HAND",
            to_bucket="DAMAGED",
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INVENTORY_ADJUSTMENT"


class TestTheRecordsWritten:
    def test_a_transaction_row_is_written(self, client, inventory_admin, variant, test_engine):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=7,
            to_bucket="ON_HAND",
            reason="Pallet received from supplier",
        )

        with test_engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT transaction_type, quantity, from_bucket, to_bucket "
                    "FROM inventory_transactions"
                )
            ).one()

        assert row.transaction_type == "RESTOCK"
        assert row.quantity == 7
        assert row.from_bucket is None
        assert row.to_bucket == "ON_HAND"

    def test_an_adjustment_row_names_the_actor_and_the_reason(
        self, client, inventory_admin, variant, test_engine
    ):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=7,
            to_bucket="ON_HAND",
            reason="Pallet received from supplier",
        )

        with test_engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT a.reason, u.email, a.transaction_id IS NOT NULL AS linked "
                    "FROM inventory_adjustments a JOIN users u ON u.id = a.actor_user_id"
                )
            ).one()

        assert row.reason == "Pallet received from supplier"
        assert row.email == "stock-admin@example.com"
        assert row.linked is True

    def test_the_adjustment_points_at_the_transaction_it_caused(
        self, client, inventory_admin, variant, test_engine
    ):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=1,
            to_bucket="ON_HAND",
        )

        with test_engine.connect() as connection:
            matched = connection.execute(
                text(
                    "SELECT count(*) FROM inventory_adjustments a "
                    "JOIN inventory_transactions t ON t.id = a.transaction_id "
                    "WHERE a.inventory_id = t.inventory_id"
                )
            ).scalar_one()

        assert matched == 1

    def test_an_audit_event_is_written(self, client, inventory_admin, variant, test_engine):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=1,
            to_bucket="ON_HAND",
        )

        with test_engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM audit_logs WHERE event_type = 'inventory.adjusted'")
            ).scalar_one()

        assert count == 1

    def test_a_rejected_adjustment_writes_nothing_at_all(
        self, client, inventory_admin, variant, test_engine
    ):
        """No ledger row, no adjustment row, and no audit row either."""
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="DISPOSAL",
            quantity=99,
            from_bucket="ON_HAND",
        )

        with test_engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM inventory_transactions")).scalar_one()
                == 0
            )
            assert (
                connection.execute(text("SELECT count(*) FROM inventory_adjustments")).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM audit_logs WHERE event_type = 'inventory.adjusted'")
                ).scalar_one()
                == 0
            )
