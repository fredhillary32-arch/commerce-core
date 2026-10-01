"""The reservation lifecycle, exercised at the service layer.

There is deliberately no reservation endpoint: Phase 5 checkout is the
caller, and inventing an HTTP surface now would only mean replacing it then.
So these tests drive the service directly against a real database session,
which is also the only honest way to test behaviour that depends on row
locks and transaction boundaries.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.exceptions import AppException
from app.db.models.cart import Cart
from app.db.models.catalog import Product, ProductVariant
from app.db.models.inventory import Inventory
from app.db.models.user import User
from app.modules.inventory import service


@pytest.fixture()
def stock(db_session):
    """A variant with inventory, plus a cart to hold reservations for."""

    def _stock(on_hand: int = 10, **quantities) -> SimpleNamespace:
        product = Product(name="Aurora Desk Lamp", slug=f"lamp-{uuid.uuid4()}", status="ACTIVE")
        db_session.add(product)
        db_session.flush()

        variant = ProductVariant(
            product_id=product.id,
            sku=f"SKU-{uuid.uuid4()}",
            status="ACTIVE",
            price=Decimal("24.99"),
            currency="GBP",
        )
        db_session.add(variant)
        db_session.flush()

        inventory = Inventory(variant_id=variant.id, quantity_on_hand=on_hand, **quantities)
        db_session.add(inventory)

        user = User(email=f"holder-{uuid.uuid4()}@example.com", password_hash="x")
        db_session.add(user)
        db_session.flush()

        cart = Cart(user_id=user.id)
        db_session.add(cart)
        db_session.commit()

        return SimpleNamespace(
            product=product, variant=variant, inventory=inventory, cart=cart, user=user
        )

    return _stock


def error_code(raised) -> str:
    return raised.value.code


def quantities(db_session, inventory_id) -> tuple[int, int]:
    row = db_session.execute(
        text(
            "SELECT quantity_on_hand, quantity_reserved FROM inventory WHERE id = :i"
        ),
        {"i": inventory_id},
    ).one()
    return row.quantity_on_hand, row.quantity_reserved


def backdate(db_session, reservation_id) -> None:
    """Put a reservation's window in the past without waiting five minutes."""
    db_session.execute(
        text("UPDATE inventory_reservations SET expires_at = now() - interval '1 minute' WHERE id = :r"),
        {"r": reservation_id},
    )
    db_session.commit()


class TestCreating:
    def test_a_reservation_holds_stock_without_deducting_it(self, db_session, stock):
        held = stock(on_hand=10)

        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=3
        )

        assert reservation.status == "ACTIVE"
        assert reservation.quantity == 3
        assert quantities(db_session, held.inventory.id) == (10, 3)

    def test_the_window_is_five_minutes(self, db_session, stock):
        held = stock()
        before = datetime.now(timezone.utc)

        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )

        assert service.RESERVATION_WINDOW == timedelta(minutes=5)
        assert reservation.expires_at >= before + timedelta(minutes=4, seconds=30)
        assert reservation.expires_at <= datetime.now(timezone.utc) + timedelta(minutes=5)

    def test_reserving_exactly_all_available_stock_succeeds(self, db_session, stock):
        held = stock(on_hand=4)

        service.reserve(db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=4)

        assert quantities(db_session, held.inventory.id) == (4, 4)

    def test_reserving_more_than_is_available_is_refused(self, db_session, stock):
        held = stock(on_hand=4)

        with pytest.raises(AppException) as raised:
            service.reserve(
                db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=5
            )

        assert error_code(raised) == "INSUFFICIENT_INVENTORY"

    def test_a_refused_reservation_leaves_no_row_and_no_counter_drift(self, db_session, stock):
        held = stock(on_hand=4)

        with pytest.raises(AppException):
            service.reserve(
                db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=99
            )

        assert quantities(db_session, held.inventory.id) == (4, 0)
        assert (
            db_session.execute(text("SELECT count(*) FROM inventory_reservations")).scalar_one()
            == 0
        )

    def test_already_reserved_stock_is_not_available_again(self, db_session, stock):
        held = stock(on_hand=5)
        service.reserve(db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=5)

        with pytest.raises(AppException) as raised:
            service.reserve(
                db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
            )

        assert error_code(raised) == "INSUFFICIENT_INVENTORY"

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_a_non_positive_quantity_is_refused(self, db_session, stock, quantity):
        held = stock()

        with pytest.raises(AppException) as raised:
            service.reserve(
                db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=quantity
            )

        assert error_code(raised) == "INVALID_RESERVATION_QUANTITY"

    def test_reserving_against_a_variant_with_no_inventory_row_is_refused(
        self, db_session, stock
    ):
        held = stock()
        db_session.execute(
            text("DELETE FROM inventory WHERE id = :i"), {"i": held.inventory.id}
        )
        db_session.commit()

        with pytest.raises(AppException) as raised:
            service.reserve(
                db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
            )

        assert error_code(raised) == "INVENTORY_NOT_FOUND"

    def test_no_ledger_row_is_written_when_stock_is_merely_held(self, db_session, stock):
        held = stock()

        service.reserve(db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=2)

        assert (
            db_session.execute(text("SELECT count(*) FROM inventory_transactions")).scalar_one()
            == 0
        )


class TestConverting:
    def test_conversion_permanently_deducts_stock(self, db_session, stock):
        held = stock(on_hand=10)
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=3
        )

        converted = service.convert_reservation(db_session, reservation_id=reservation.id)

        assert converted.status == "CONVERTED"
        assert quantities(db_session, held.inventory.id) == (7, 0)

    def test_conversion_writes_exactly_one_outbound_purchase(self, db_session, stock):
        held = stock(on_hand=10)
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=3
        )

        service.convert_reservation(db_session, reservation_id=reservation.id)

        row = db_session.execute(
            text(
                "SELECT transaction_type, quantity, from_bucket, to_bucket, reference_id "
                "FROM inventory_transactions"
            )
        ).one()
        assert row.transaction_type == "PURCHASE"
        assert row.quantity == 3
        assert row.from_bucket == "ON_HAND"
        assert row.to_bucket is None
        assert row.reference_id == reservation.id

    def test_there_is_no_sale_type(self, db_session, stock):
        """PURCHASE *is* the customer purchase; a SALE type would duplicate it."""
        held = stock(on_hand=10)
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        service.convert_reservation(db_session, reservation_id=reservation.id)

        assert "SALE" not in service.TRANSACTION_TYPES
        assert (
            db_session.execute(
                text("SELECT count(*) FROM inventory_transactions WHERE transaction_type = 'SALE'")
            ).scalar_one()
            == 0
        )

    def test_a_converted_reservation_is_never_left_active(self, db_session, stock):
        held = stock()
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        service.convert_reservation(db_session, reservation_id=reservation.id)

        assert (
            db_session.execute(
                text("SELECT status FROM inventory_reservations WHERE id = :r"),
                {"r": reservation.id},
            ).scalar_one()
            == "CONVERTED"
        )

    def test_converting_twice_is_refused(self, db_session, stock):
        held = stock()
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        service.convert_reservation(db_session, reservation_id=reservation.id)

        with pytest.raises(AppException) as raised:
            service.convert_reservation(db_session, reservation_id=reservation.id)

        assert error_code(raised) == "INVALID_RESERVATION_STATE"

    def test_converting_an_unknown_reservation_is_refused(self, db_session):
        with pytest.raises(AppException) as raised:
            service.convert_reservation(db_session, reservation_id=uuid.uuid4())

        assert error_code(raised) == "RESERVATION_NOT_FOUND"


class TestReleasing:
    def test_releasing_restores_available_stock(self, db_session, stock):
        held = stock(on_hand=10)
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=4
        )

        released = service.release_reservation(db_session, reservation_id=reservation.id)

        assert released.status == "RELEASED"
        assert quantities(db_session, held.inventory.id) == (10, 0)

    def test_releasing_writes_no_ledger_row(self, db_session, stock):
        """Nothing physical moved, so nothing belongs in the ledger."""
        held = stock()
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )

        service.release_reservation(db_session, reservation_id=reservation.id)

        assert (
            db_session.execute(text("SELECT count(*) FROM inventory_transactions")).scalar_one()
            == 0
        )

    def test_releasing_twice_is_refused(self, db_session, stock):
        held = stock()
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        service.release_reservation(db_session, reservation_id=reservation.id)

        with pytest.raises(AppException) as raised:
            service.release_reservation(db_session, reservation_id=reservation.id)

        assert error_code(raised) == "INVALID_RESERVATION_STATE"

    def test_released_stock_can_be_reserved_again(self, db_session, stock):
        held = stock(on_hand=2)
        first = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=2
        )
        service.release_reservation(db_session, reservation_id=first.id)

        second = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=2
        )

        assert second.status == "ACTIVE"


class TestTerminalStates:
    """All three non-ACTIVE states are terminal. No route back exists."""

    def reach(self, db_session, held, state):
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        if state == "CONVERTED":
            service.convert_reservation(db_session, reservation_id=reservation.id)
        elif state == "RELEASED":
            service.release_reservation(db_session, reservation_id=reservation.id)
        elif state == "EXPIRED":
            backdate(db_session, reservation.id)
            service.expire_due_reservations(db_session, held.inventory.id)
        return reservation

    @pytest.mark.parametrize("state", ["CONVERTED", "RELEASED", "EXPIRED"])
    @pytest.mark.parametrize("operation", ["convert_reservation", "release_reservation"])
    def test_no_further_transition_is_accepted(self, db_session, stock, state, operation):
        held = stock(on_hand=10)
        reservation = self.reach(db_session, held, state)

        with pytest.raises(AppException) as raised:
            getattr(service, operation)(db_session, reservation_id=reservation.id)

        assert error_code(raised) == "INVALID_RESERVATION_STATE"

    @pytest.mark.parametrize("state", ["CONVERTED", "RELEASED", "EXPIRED"])
    def test_the_record_survives_as_history(self, db_session, stock, state):
        """Terminal reservations are never deleted; they are the audit trail."""
        held = stock(on_hand=10)
        reservation = self.reach(db_session, held, state)

        stored = db_session.execute(
            text("SELECT status, quantity FROM inventory_reservations WHERE id = :r"),
            {"r": reservation.id},
        ).one()

        assert stored.status == state
        assert stored.quantity == 1

    def test_the_transition_table_says_all_three_are_terminal(self):
        assert service.RESERVATION_TRANSITIONS["CONVERTED"] == frozenset()
        assert service.RESERVATION_TRANSITIONS["RELEASED"] == frozenset()
        assert service.RESERVATION_TRANSITIONS["EXPIRED"] == frozenset()
        assert service.RESERVATION_TRANSITIONS["ACTIVE"] == frozenset(
            {"CONVERTED", "RELEASED", "EXPIRED"}
        )


class TestExpirySweep:
    """Nothing schedules expiry in Phase 4, so reservation does it.

    Without this, a reservation whose window elapsed would sit ACTIVE
    forever and strand its stock permanently.
    """

    def test_a_past_due_reservation_frees_its_stock_for_the_next_attempt(
        self, db_session, stock
    ):
        held = stock(on_hand=1)
        stale = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        backdate(db_session, stale.id)

        # Without the sweep this raises INSUFFICIENT_INVENTORY: the counter
        # still says 1 is reserved.
        fresh = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )

        assert fresh.status == "ACTIVE"
        assert quantities(db_session, held.inventory.id) == (1, 1)
        assert (
            db_session.execute(
                text("SELECT status FROM inventory_reservations WHERE id = :r"), {"r": stale.id}
            ).scalar_one()
            == "EXPIRED"
        )

    def test_the_sweep_and_the_new_reservation_share_one_transaction(
        self, db_session, stock
    ):
        """The counter is never stale at the moment a decision depends on it."""
        held = stock(on_hand=2)
        stale = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=2
        )
        backdate(db_session, stale.id)

        service.reserve(db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=2)

        # 2 released by the sweep and 2 taken by the new hold, both visible.
        assert quantities(db_session, held.inventory.id) == (2, 2)

    def test_a_reservation_that_is_not_yet_due_is_untouched(self, db_session, stock):
        held = stock(on_hand=5)
        live = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=2
        )

        service.reserve(db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1)

        assert (
            db_session.execute(
                text("SELECT status FROM inventory_reservations WHERE id = :r"), {"r": live.id}
            ).scalar_one()
            == "ACTIVE"
        )
        assert quantities(db_session, held.inventory.id) == (5, 3)

    def test_the_standalone_method_expires_due_rows_when_called_directly(
        self, db_session, stock
    ):
        """Exposed so Phase 8 can drive expiry proactively, not only lazily."""
        held = stock(on_hand=6)
        first = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=2
        )
        second = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=3
        )
        backdate(db_session, first.id)
        backdate(db_session, second.id)

        expired = service.expire_due_reservations(db_session, held.inventory.id)

        assert expired == 2
        assert quantities(db_session, held.inventory.id) == (6, 0)

    def test_the_standalone_method_reports_nothing_to_do(self, db_session, stock):
        held = stock(on_hand=6)
        service.reserve(db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=2)

        assert service.expire_due_reservations(db_session, held.inventory.id) == 0

    def test_expiry_writes_no_ledger_row(self, db_session, stock):
        held = stock(on_hand=3)
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        backdate(db_session, reservation.id)

        service.expire_due_reservations(db_session, held.inventory.id)

        assert (
            db_session.execute(text("SELECT count(*) FROM inventory_transactions")).scalar_one()
            == 0
        )

    def test_expiring_against_an_unknown_inventory_row_is_refused(self, db_session):
        with pytest.raises(AppException) as raised:
            service.expire_due_reservations(db_session, uuid.uuid4())

        assert error_code(raised) == "INVENTORY_NOT_FOUND"


class TestReservationAudit:
    def events(self, db_session) -> list[str]:
        return list(
            db_session.execute(
                text(
                    "SELECT event_type FROM audit_logs WHERE event_type LIKE 'inventory.%' "
                    "ORDER BY created_at, event_type"
                )
            ).scalars()
        )

    def test_holding_and_converting_are_both_recorded(self, db_session, stock):
        held = stock()
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        service.convert_reservation(db_session, reservation_id=reservation.id)

        assert self.events(db_session) == ["inventory.reserved", "inventory.converted"]

    def test_releasing_is_recorded(self, db_session, stock):
        held = stock()
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        service.release_reservation(db_session, reservation_id=reservation.id)

        assert self.events(db_session) == ["inventory.reserved", "inventory.released"]

    def test_expiry_is_recorded(self, db_session, stock):
        held = stock()
        reservation = service.reserve(
            db_session, variant_id=held.variant.id, cart_id=held.cart.id, quantity=1
        )
        backdate(db_session, reservation.id)

        service.expire_due_reservations(db_session, held.inventory.id)

        assert self.events(db_session) == ["inventory.reserved", "inventory.expired"]
