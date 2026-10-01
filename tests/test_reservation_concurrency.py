"""Two genuinely concurrent reservations for one unit of stock.

This is the single most important test in Phase 4, and it is written to be
hard to fool. In Phase 2 of this project a thread-based concurrency test
passed even after the row lock was removed - a false positive that reported
safety which did not exist. The difference here is that the two
transactions are *forced* to overlap: the first is held open inside the
critical section, immediately after the locking SELECT and before it writes
anything, while the second one runs. That ordering is what a missing
``FOR UPDATE`` cannot survive.

Removing ``.with_for_update()`` from
``repository.lock_inventory_by_variant`` must make this test fail. If it
still passes, it is proving nothing.
"""

import itertools
import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import AppException
from app.db.models.cart import Cart
from app.db.models.catalog import Product, ProductVariant
from app.db.models.inventory import Inventory
from app.db.models.user import User
from app.modules.inventory import repository as inventory_repository
from app.modules.inventory import service

# Long enough that a slow machine does not produce a spurious result,
# short enough that a genuine deadlock fails the run rather than hanging it.
BLOCKED_TIMEOUT = 3.0
JOIN_TIMEOUT = 30.0


def one_unit_in_stock(db_session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A variant with exactly one sellable unit, and a cart to hold it for."""
    product = Product(name="Last One", slug=f"last-{uuid.uuid4()}", status="ACTIVE")
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

    inventory = Inventory(variant_id=variant.id, quantity_on_hand=1)
    db_session.add(inventory)

    user = User(email=f"racer-{uuid.uuid4()}@example.com", password_hash="x")
    db_session.add(user)
    db_session.flush()

    cart = Cart(user_id=user.id)
    db_session.add(cart)
    db_session.commit()

    return variant.id, inventory.id, cart.id


@pytest.fixture()
def contended(db_session):
    return one_unit_in_stock(db_session)


def test_two_concurrent_reservations_yield_exactly_one_winner(
    test_engine, contended, monkeypatch
):
    """available = 1, two concurrent attempts to take it. One must lose.

    The two attempts run on two separate sessions over two separate
    connections against real PostgreSQL - not two sequential calls dressed
    up as concurrency.
    """
    variant_id, inventory_id, cart_id = contended
    session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

    holding_the_lock = threading.Event()
    release_the_holder = threading.Event()
    call_index = itertools.count()
    original_lock = inventory_repository.lock_inventory_by_variant

    def instrumented(db, lookup_variant_id):
        """Freeze the first caller inside the critical section.

        It has taken the row lock and has not yet written anything, which is
        precisely the window a check-then-act bug lives in.
        """
        index = next(call_index)
        locked = original_lock(db, lookup_variant_id)
        if index == 0:
            holding_the_lock.set()
            release_the_holder.wait(timeout=JOIN_TIMEOUT)
        return locked

    monkeypatch.setattr(inventory_repository, "lock_inventory_by_variant", instrumented)

    outcomes: dict[str, object] = {}

    def attempt(name: str) -> None:
        session = session_factory()
        try:
            outcomes[name] = service.reserve(
                session, variant_id=variant_id, cart_id=cart_id, quantity=1
            )
        except AppException as error:
            outcomes[name] = error
        finally:
            session.close()

    first = threading.Thread(target=attempt, args=("first",), name="first")
    first.start()
    assert holding_the_lock.wait(timeout=JOIN_TIMEOUT), "the first attempt never took the lock"

    second = threading.Thread(target=attempt, args=("second",), name="second")
    second.start()

    # With the lock in place the second attempt is blocked in PostgreSQL on
    # the very SELECT, so it cannot finish here. Without the lock it sails
    # past the stale read and finishes - which is the failure this test
    # exists to catch.
    second.join(timeout=BLOCKED_TIMEOUT)
    blocked_by_the_lock = second.is_alive()

    release_the_holder.set()
    first.join(timeout=JOIN_TIMEOUT)
    second.join(timeout=JOIN_TIMEOUT)

    assert not first.is_alive() and not second.is_alive(), "a thread never finished"
    assert blocked_by_the_lock, (
        "the second transaction read the inventory row while the first still "
        "held it open - the row was not locked with FOR UPDATE"
    )

    successes = [name for name, result in outcomes.items() if not isinstance(result, Exception)]
    failures = [result for result in outcomes.values() if isinstance(result, AppException)]

    assert len(successes) == 1, f"expected exactly one winner, got {outcomes}"
    assert len(failures) == 1, f"expected exactly one loser, got {outcomes}"
    assert failures[0].code == "INSUFFICIENT_INVENTORY"


def test_the_losing_attempt_leaves_the_counters_exactly_right(
    test_engine, contended, monkeypatch, db_session
):
    """One unit of stock can only ever be reserved once. Never twice."""
    variant_id, inventory_id, cart_id = contended
    session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

    holding_the_lock = threading.Event()
    release_the_holder = threading.Event()
    call_index = itertools.count()
    original_lock = inventory_repository.lock_inventory_by_variant

    def instrumented(db, lookup_variant_id):
        index = next(call_index)
        locked = original_lock(db, lookup_variant_id)
        if index == 0:
            holding_the_lock.set()
            release_the_holder.wait(timeout=JOIN_TIMEOUT)
        return locked

    monkeypatch.setattr(inventory_repository, "lock_inventory_by_variant", instrumented)

    def attempt() -> None:
        session = session_factory()
        try:
            service.reserve(session, variant_id=variant_id, cart_id=cart_id, quantity=1)
        except AppException:
            pass
        finally:
            session.close()

    first = threading.Thread(target=attempt)
    first.start()
    assert holding_the_lock.wait(timeout=JOIN_TIMEOUT)

    second = threading.Thread(target=attempt)
    second.start()
    second.join(timeout=BLOCKED_TIMEOUT)

    release_the_holder.set()
    first.join(timeout=JOIN_TIMEOUT)
    second.join(timeout=JOIN_TIMEOUT)

    db_session.commit()
    reserved = db_session.execute(
        text("SELECT quantity_reserved FROM inventory WHERE id = :i"), {"i": inventory_id}
    ).scalar_one()
    active = db_session.execute(
        text(
            "SELECT count(*) FROM inventory_reservations "
            "WHERE inventory_id = :i AND status = 'ACTIVE'"
        ),
        {"i": inventory_id},
    ).scalar_one()

    assert reserved == 1, f"quantity_reserved drifted to {reserved}"
    assert active == 1, f"{active} reservations were created for one unit of stock"


def test_the_reservation_path_actually_locks_the_row():
    """A direct reading of the source, so the lock cannot quietly vanish.

    The behavioural tests above are the real proof. This one exists so that
    deleting ``.with_for_update()`` fails loudly and specifically, rather
    than only as a timing-dependent symptom somewhere else.
    """
    import inspect

    source = inspect.getsource(inventory_repository.lock_inventory_by_variant)

    assert ".with_for_update()" in source
