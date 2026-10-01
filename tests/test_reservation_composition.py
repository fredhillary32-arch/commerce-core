"""The composable halves of the reservation service, and the cart_id fix.

Phase 4's ``reserve``, ``convert_reservation`` and ``release_reservation``
each owned their transaction: every one ended in ``db.commit()`` and every
failure path called ``db.rollback()`` on the caller's session. Checkout
cannot use functions like that - calling ``reserve`` once per cart line
would produce N separately committed transactions, and a failure on line
three would roll away the order row the caller had already built.

Phase 5 splits each of them in two. The ``*_locked`` half does the work and
neither commits nor rolls back; the public function is a thin wrapper that
owns the transaction. These tests pin both halves:

* the wrappers still behave exactly as Phase 4's tests require - which the
  unmodified Phase 4 suite also proves;
* the inner halves leave the transaction open, so a caller can compose
  several of them and commit once;
* the inner halves raise **without** rolling back, so a caller's pending
  work survives long enough for the caller to decide what to do with it.

Also covered here is the ``cart_id`` gap Phase 4 recorded and left: the
service took the id on trust, the foreign key was absent from ``_CONFLICTS``
and a bad id therefore surfaced as a 500.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.exceptions import BusinessRuleError, NotFoundError
from app.db.models.cart import CART_CONVERTED, Cart
from app.db.models.catalog import Product, ProductVariant
from app.db.models.inventory import RESERVATION_ACTIVE, Inventory
from app.db.models.user import User
from app.modules.inventory import service


class Stocked:
    """A variant with stock, and an ACTIVE cart belonging to a real user."""

    def __init__(self, variant, inventory, cart, user):
        self.variant = variant
        self.inventory = inventory
        self.cart = cart
        self.user = user


@pytest.fixture()
def stocked(db_session) -> Stocked:
    product = Product(name="Composable", slug=f"comp-{uuid.uuid4()}", status="ACTIVE")
    db_session.add(product)
    db_session.flush()

    variant = ProductVariant(
        product_id=product.id,
        sku=f"SKU-{uuid.uuid4()}",
        status="ACTIVE",
        price=Decimal("10.00"),
        currency="GBP",
    )
    db_session.add(variant)
    db_session.flush()

    inventory = Inventory(variant_id=variant.id, quantity_on_hand=10)
    db_session.add(inventory)

    user = User(email=f"compose-{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(user)
    db_session.flush()

    cart = Cart(user_id=user.id)
    db_session.add(cart)
    db_session.commit()
    return Stocked(variant, inventory, cart, user)


def reserved_quantity(db_session, inventory_id) -> int:
    return db_session.execute(
        text("SELECT quantity_reserved FROM inventory WHERE id = :i"), {"i": inventory_id}
    ).scalar_one()


class TestTheInnerHalvesDoNotOwnTheTransaction:
    """What makes a single-transaction checkout possible at all."""

    def test_reserve_locked_leaves_the_work_uncommitted(self, stocked, db_session, test_engine):
        service.reserve_locked(
            db_session,
            variant_id=stocked.variant.id,
            cart_id=stocked.cart.id,
            quantity=2,
        )

        # A second connection cannot see uncommitted work. If reserve_locked
        # had committed, this would read 2.
        with test_engine.connect() as outside:
            visible = outside.execute(
                text("SELECT quantity_reserved FROM inventory WHERE id = :i"),
                {"i": stocked.inventory.id},
            ).scalar_one()

        assert visible == 0, "reserve_locked committed; checkout could not compose it"

        db_session.commit()
        assert reserved_quantity(db_session, stocked.inventory.id) == 2

    def test_several_reservations_commit_as_one_unit(self, stocked, db_session):
        """Three calls, one commit - the shape checkout actually uses."""
        for _ in range(3):
            service.reserve_locked(
                db_session,
                variant_id=stocked.variant.id,
                cart_id=stocked.cart.id,
                quantity=1,
            )
        db_session.commit()

        assert reserved_quantity(db_session, stocked.inventory.id) == 3

    def test_a_failing_inner_call_does_not_roll_back_the_caller(self, stocked, db_session):
        """The caller's pending work survives the failure, so it can decide.

        This is the behaviour Phase 5 needed and Phase 4 could not give: a
        ``db.rollback()`` inside the reservation path would silently discard
        the order row a caller had already built.
        """
        service.reserve_locked(
            db_session, variant_id=stocked.variant.id, cart_id=stocked.cart.id, quantity=2
        )

        with pytest.raises(BusinessRuleError) as raised:
            service.reserve_locked(
                db_session,
                variant_id=stocked.variant.id,
                cart_id=stocked.cart.id,
                quantity=999,
            )
        assert raised.value.code == "INSUFFICIENT_INVENTORY"

        # Still there, still uncommitted, still the caller's to keep or drop.
        db_session.commit()
        assert reserved_quantity(db_session, stocked.inventory.id) == 2

    def test_convert_locked_leaves_the_work_uncommitted(
        self, stocked, db_session, test_engine
    ):
        reservation = service.reserve(
            db_session, variant_id=stocked.variant.id, cart_id=stocked.cart.id, quantity=2
        )

        service.convert_reservation_locked(db_session, reservation_id=reservation.id)

        with test_engine.connect() as outside:
            on_hand = outside.execute(
                text("SELECT quantity_on_hand FROM inventory WHERE id = :i"),
                {"i": stocked.inventory.id},
            ).scalar_one()
        assert on_hand == 10, "convert_reservation_locked committed"

        db_session.commit()
        assert db_session.execute(
            text("SELECT quantity_on_hand FROM inventory WHERE id = :i"),
            {"i": stocked.inventory.id},
        ).scalar_one() == 8

    def test_release_locked_leaves_the_work_uncommitted(
        self, stocked, db_session, test_engine
    ):
        reservation = service.reserve(
            db_session, variant_id=stocked.variant.id, cart_id=stocked.cart.id, quantity=2
        )

        service.release_reservation_locked(db_session, reservation_id=reservation.id)

        with test_engine.connect() as outside:
            reserved = outside.execute(
                text("SELECT quantity_reserved FROM inventory WHERE id = :i"),
                {"i": stocked.inventory.id},
            ).scalar_one()
        assert reserved == 2, "release_reservation_locked committed"

        db_session.commit()
        assert reserved_quantity(db_session, stocked.inventory.id) == 0


class TestTheWrappersStillOwnTheirTransaction:
    """Phase 4's contract, unchanged. The whole Phase 4 suite also asserts it."""

    def test_reserve_commits(self, stocked, db_session, test_engine):
        service.reserve(
            db_session, variant_id=stocked.variant.id, cart_id=stocked.cart.id, quantity=2
        )

        with test_engine.connect() as outside:
            visible = outside.execute(
                text("SELECT quantity_reserved FROM inventory WHERE id = :i"),
                {"i": stocked.inventory.id},
            ).scalar_one()

        assert visible == 2

    def test_a_failed_reserve_leaves_nothing_behind(self, stocked, db_session):
        with pytest.raises(BusinessRuleError):
            service.reserve(
                db_session,
                variant_id=stocked.variant.id,
                cart_id=stocked.cart.id,
                quantity=999,
            )

        assert reserved_quantity(db_session, stocked.inventory.id) == 0
        assert db_session.execute(
            text("SELECT count(*) FROM inventory_reservations WHERE inventory_id = :i"),
            {"i": stocked.inventory.id},
        ).scalar_one() == 0

    def test_reserve_for_an_unprovisioned_variant_still_reports_it(self, stocked, db_session):
        product = Product(name="Bare", slug=f"bare-{uuid.uuid4()}", status="ACTIVE")
        db_session.add(product)
        db_session.flush()
        variant = ProductVariant(
            product_id=product.id, sku=f"SKU-{uuid.uuid4()}", status="ACTIVE"
        )
        db_session.add(variant)
        db_session.commit()

        with pytest.raises(NotFoundError) as raised:
            service.reserve(
                db_session, variant_id=variant.id, cart_id=stocked.cart.id, quantity=1
            )
        assert raised.value.code == "INVENTORY_NOT_FOUND"


class TestTheCartIsNoLongerTakenOnTrust:
    """Phase 4 recorded this gap and deliberately left it for its caller."""

    def test_an_unknown_cart_is_reported_not_crashed(self, stocked, db_session):
        with pytest.raises(NotFoundError) as raised:
            service.reserve(
                db_session,
                variant_id=stocked.variant.id,
                cart_id=uuid.uuid4(),
                quantity=1,
            )

        assert raised.value.code == "CART_NOT_FOUND"
        assert raised.value.status_code == 404

    def test_a_cart_that_is_no_longer_active_cannot_hold_new_stock(
        self, stocked, db_session
    ):
        """A CONVERTED cart has already become an order; it may not reserve more."""
        stocked.cart.status = CART_CONVERTED
        db_session.commit()

        with pytest.raises(BusinessRuleError) as raised:
            service.reserve(
                db_session,
                variant_id=stocked.variant.id,
                cart_id=stocked.cart.id,
                quantity=1,
            )

        assert raised.value.code == "CART_NOT_ACTIVE"
        assert raised.value.status_code == 422

    def test_nothing_is_reserved_when_the_cart_is_rejected(self, stocked, db_session):
        with pytest.raises(NotFoundError):
            service.reserve(
                db_session,
                variant_id=stocked.variant.id,
                cart_id=uuid.uuid4(),
                quantity=1,
            )

        assert reserved_quantity(db_session, stocked.inventory.id) == 0

    def test_the_foreign_key_is_mapped_as_a_backstop(self, stocked, db_session):
        """Even if the check-then-act window were lost, the FK is translated.

        The service check above closes the ordinary case. This asserts the
        second layer: the constraint itself is in ``_CONFLICTS``, so the
        violation cannot escape as an unmapped 500.
        """
        assert (
            "fk_inventory_reservations_cart_id_carts" in service._CONFLICTS
        ), "the cart foreign key is unmapped; a violation would be a 500"
        exception_class, code, _ = service._CONFLICTS[
            "fk_inventory_reservations_cart_id_carts"
        ]
        assert exception_class is NotFoundError
        assert code == "CART_NOT_FOUND"


class TestReservationsStayActiveUntilSomethingEndsThem:
    def test_a_composed_reservation_is_active_once_committed(self, stocked, db_session):
        reservation = service.reserve_locked(
            db_session, variant_id=stocked.variant.id, cart_id=stocked.cart.id, quantity=1
        )
        db_session.commit()

        assert db_session.execute(
            text("SELECT status FROM inventory_reservations WHERE id = :i"),
            {"i": reservation.id},
        ).scalar_one() == RESERVATION_ACTIVE
