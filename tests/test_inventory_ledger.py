"""The ledger reconciles against the quantity columns, bucket by bucket.

The invariant: for every bucket, the sum of quantities moved *into* it minus
the sum moved *out of* it equals that bucket's current column value, with a
TRANSFER counting as an outbound from one bucket and an inbound to the
other.

This is the reason reservation events are kept out of the ledger. Holding,
releasing and expiring stock move nothing physical, so recording them here
would break the equality on the very first abandoned cart.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.models.cart import Cart
from app.db.models.user import User
from app.modules.inventory import service
from tests.catalog_helpers import activate_product_with_variant
from tests.inventory_helpers import adjust, provision, read_inventory


@pytest.fixture()
def inventory_admin(authorize) -> dict[str, str]:
    return authorize("inventory.read", "inventory.adjust", email="ledger-admin@example.com")


@pytest.fixture()
def variant(client, catalog_admin, inventory_admin) -> str:
    _, created = activate_product_with_variant(client, catalog_admin)
    provision(client, inventory_admin, created)
    return created


@pytest.fixture()
def cart_id(db_session) -> uuid.UUID:
    user = User(email=f"ledger-{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(user)
    db_session.flush()
    cart = Cart(user_id=user.id)
    db_session.add(cart)
    db_session.commit()
    return cart.id


def assert_reconciled(db_session, variant) -> dict:
    """Every bucket's ledger total equals its column, with no exceptions."""
    db_session.rollback()
    inventory = service.require_inventory_for_variant(db_session, uuid.UUID(str(variant)))
    reconciliation = service.reconcile(db_session, inventory)

    for bucket, (from_ledger, from_column) in reconciliation.items():
        assert from_ledger == from_column, (
            f"{bucket}: the ledger says {from_ledger} but the column says {from_column}"
        )
    return reconciliation


class TestReconciliation:
    def test_it_holds_after_a_single_restock(self, client, inventory_admin, variant, db_session):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=40,
            to_bucket="ON_HAND",
        )

        assert assert_reconciled(db_session, variant)["ON_HAND"] == (40, 40)

    def test_it_holds_across_a_long_sequence_of_mixed_movements(
        self, client, inventory_admin, variant, db_session, cart_id
    ):
        """Receipts, corrections, write-offs, transfers and a real sale."""
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=100,
            to_bucket="ON_HAND",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=10,
            from_bucket="ON_HAND",
            to_bucket="DAMAGED",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=5,
            from_bucket="ON_HAND",
            to_bucket="QUARANTINED",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="DISPOSAL",
            quantity=4,
            from_bucket="DAMAGED",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="ADJUSTMENT",
            quantity=3,
            to_bucket="ON_HAND",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=2,
            from_bucket="QUARANTINED",
            to_bucket="ON_HAND",
        )

        # A genuine sale: hold, then convert on payment.
        db_session.rollback()
        variant_uuid = uuid.UUID(str(variant))
        sold = service.reserve(
            db_session, variant_id=variant_uuid, cart_id=cart_id, quantity=5
        )
        service.convert_reservation(db_session, reservation_id=sold.id)

        reconciliation = assert_reconciled(db_session, variant)

        assert reconciliation["ON_HAND"] == (85, 85)
        assert reconciliation["DAMAGED"] == (6, 6)
        assert reconciliation["QUARANTINED"] == (3, 3)

    def test_it_still_holds_after_holds_that_never_became_sales(
        self, client, inventory_admin, variant, db_session, cart_id
    ):
        """A released hold and an expired one leave the ledger untouched."""
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=20,
            to_bucket="ON_HAND",
        )

        db_session.rollback()
        variant_uuid = uuid.UUID(str(variant))
        inventory = service.require_inventory_for_variant(db_session, variant_uuid)

        released = service.reserve(
            db_session, variant_id=variant_uuid, cart_id=cart_id, quantity=3
        )
        service.release_reservation(db_session, reservation_id=released.id)

        expired = service.reserve(
            db_session, variant_id=variant_uuid, cart_id=cart_id, quantity=4
        )
        db_session.execute(
            text(
                "UPDATE inventory_reservations SET expires_at = now() - interval '1 minute' "
                "WHERE id = :r"
            ),
            {"r": expired.id},
        )
        db_session.commit()
        service.expire_due_reservations(db_session, inventory.id)

        reconciliation = assert_reconciled(db_session, variant)

        assert reconciliation["ON_HAND"] == (20, 20)
        assert read_inventory(client, inventory_admin, variant).json()["available"] == 20


class TestReservationsStayOutOfTheLedger:
    def test_no_reservation_lifecycle_row_is_ever_written(
        self, client, inventory_admin, variant, db_session, cart_id
    ):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=30,
            to_bucket="ON_HAND",
        )

        db_session.rollback()
        variant_uuid = uuid.UUID(str(variant))
        inventory = service.require_inventory_for_variant(db_session, variant_uuid)

        held = service.reserve(db_session, variant_id=variant_uuid, cart_id=cart_id, quantity=2)
        service.release_reservation(db_session, reservation_id=held.id)
        stale = service.reserve(db_session, variant_id=variant_uuid, cart_id=cart_id, quantity=2)
        db_session.execute(
            text(
                "UPDATE inventory_reservations SET expires_at = now() - interval '1 minute' "
                "WHERE id = :r"
            ),
            {"r": stale.id},
        )
        db_session.commit()
        service.expire_due_reservations(db_session, inventory.id)

        db_session.rollback()
        types = list(
            db_session.execute(
                text(
                    "SELECT DISTINCT transaction_type FROM inventory_transactions "
                    "WHERE inventory_id = :i"
                ),
                {"i": inventory.id},
            ).scalars()
        )

        assert types == ["RESTOCK"]

    def test_only_conversion_adds_to_the_ledger(
        self, client, inventory_admin, variant, db_session, cart_id
    ):
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=30,
            to_bucket="ON_HAND",
        )

        db_session.rollback()
        variant_uuid = uuid.UUID(str(variant))
        inventory = service.require_inventory_for_variant(db_session, variant_uuid)
        before = len(service.list_transactions(db_session, variant_uuid))

        sold = service.reserve(db_session, variant_id=variant_uuid, cart_id=cart_id, quantity=6)
        assert len(service.list_transactions(db_session, variant_uuid)) == before

        service.convert_reservation(db_session, reservation_id=sold.id)

        db_session.rollback()
        after = service.list_transactions(db_session, variant_uuid)
        assert len(after) == before + 1
        assert after[-1].transaction_type == "PURCHASE"
        assert after[-1].from_bucket == "ON_HAND"
        assert after[-1].to_bucket is None
        assert after[-1].reference_id == sold.id
        assert inventory is not None


class TestImmutability:
    def test_nothing_in_the_service_updates_or_deletes_a_ledger_row(self):
        """A correction is a new ADJUSTMENT, never rewritten history."""
        from pathlib import Path

        module = Path(service.__file__).read_text(encoding="utf-8")

        assert "delete(InventoryTransaction" not in module
        assert "db.delete(transaction" not in module

    def test_the_repository_offers_no_way_to_change_one(self):
        from pathlib import Path

        from app.modules.inventory import repository

        source = Path(repository.__file__).read_text(encoding="utf-8")

        assert "delete(InventoryTransaction" not in source
        assert "update(InventoryTransaction" not in source
