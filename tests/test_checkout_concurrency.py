"""Two customers, real threads, real PostgreSQL, one unit of stock.

Phase 4 established the standard these tests have to meet: a thread-based
concurrency test that still passes when the protection is removed is a
false positive reporting safety that does not exist. So each test here is
paired with a verification that **removing the protection makes it fail**.

Two distinct hazards are covered.

*Last-unit contention* is inherited, not reimplemented. Checkout calls the
reservation service and performs no availability arithmetic of its own, so
Phase 4's ``FOR UPDATE`` does the work. What is new is that the loser must
leave no half-written order behind.

*Multi-row deadlock* is new in Phase 5. Checkout holds one inventory lock
per line until it commits, so two carts containing the same two variants in
opposite order could each hold what the other wants next. Sorting by
variant id gives every checkout the same global acquisition order, which
makes the cycle impossible. ``TestRemovingTheSortDeadlocks`` removes the
sort and observes the deadlock PostgreSQL then reports.
"""

import threading
import uuid

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import AppException
from app.db.models.user import User
from app.modules.audit.service import ClientInfo
from app.modules.inventory import service as inventory
from app.modules.orders import service as orders
from tests.cart_helpers import add_item
from tests.checkout_fixtures import configure_rules, sellable, shop_admin  # noqa: F401
from tests.order_helpers import address_id

# Long enough that a slow machine does not produce a spurious result, short
# enough that a genuine hang fails the run rather than stopping it.
INTERLEAVE_TIMEOUT = 3.0
JOIN_TIMEOUT = 30.0


def user_of(db_session, email: str) -> User:
    user_id = db_session.execute(
        text("SELECT id FROM users WHERE email = :e"), {"e": email}
    ).scalar_one()
    return db_session.get(User, user_id)


def run_checkout(session_factory, user_id, address_id_, outcomes, name):
    """One checkout on its own session over its own connection."""
    session = session_factory()
    try:
        user = session.get(User, user_id)
        order, created = orders.checkout(
            session,
            user=user,
            shipping_address_id=address_id_,
            billing_address_id=None,
            client=ClientInfo(),
        )
        outcomes[name] = (str(order.id), created)
    except Exception as error:  # noqa: BLE001 - the outcome is the assertion
        outcomes[name] = error
    finally:
        session.close()


class TestLastUnitContention:
    @pytest.fixture()
    def scarce(self, client, shop_admin, authorize, db_session):
        """One unit of stock, two customers, both with it in their basket."""
        _, variant = sellable(client, shop_admin, price="10.00", quantity=1)
        configure_rules(client, shop_admin)

        shoppers = []
        for email in ("racer-a@example.com", "racer-b@example.com"):
            headers = authorize(email=email)
            add_item(client, headers, variant, quantity=1)
            shoppers.append(
                (user_of(db_session, email).id, uuid.UUID(address_id(client, headers)))
            )
        return variant, shoppers

    def test_only_one_of_two_checkouts_can_take_the_last_unit(
        self, test_engine, scarce, monkeypatch, db_session
    ):
        variant, shoppers = scarce
        session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

        holding = threading.Event()
        release = threading.Event()
        calls = {"n": 0}
        real = inventory.reserve_locked

        def instrumented(db, **arguments):
            """Freeze the first checkout inside the critical section.

            It holds the inventory row lock and has committed nothing,
            which is exactly the window a check-then-act bug lives in.
            """
            index = calls["n"]
            calls["n"] += 1
            reservation = real(db, **arguments)
            if index == 0:
                holding.set()
                release.wait(timeout=JOIN_TIMEOUT)
            return reservation

        monkeypatch.setattr(inventory, "reserve_locked", instrumented)

        outcomes: dict[str, object] = {}
        first = threading.Thread(
            target=run_checkout,
            args=(session_factory, shoppers[0][0], shoppers[0][1], outcomes, "first"),
        )
        first.start()
        assert holding.wait(timeout=JOIN_TIMEOUT), "the first checkout never took the lock"

        second = threading.Thread(
            target=run_checkout,
            args=(session_factory, shoppers[1][0], shoppers[1][1], outcomes, "second"),
        )
        second.start()

        # With the lock held the second checkout is blocked inside
        # PostgreSQL on the locking SELECT and cannot finish here.
        second.join(timeout=INTERLEAVE_TIMEOUT)
        blocked = second.is_alive()

        release.set()
        first.join(timeout=JOIN_TIMEOUT)
        second.join(timeout=JOIN_TIMEOUT)

        assert not first.is_alive() and not second.is_alive(), "a thread never finished"
        assert blocked, (
            "the second checkout read the inventory row while the first still "
            "held it open - the row was not locked"
        )

        successes = [name for name, out in outcomes.items() if not isinstance(out, Exception)]
        failures = [out for out in outcomes.values() if isinstance(out, AppException)]
        assert len(successes) == 1, f"expected exactly one winner, got {outcomes}"
        assert len(failures) == 1, f"expected exactly one loser, got {outcomes}"
        assert failures[0].code == "INSUFFICIENT_INVENTORY"

    def test_the_loser_leaves_no_trace(self, test_engine, scarce, db_session):
        """One order, one reservation, one unit reserved. Never two."""
        variant, shoppers = scarce
        session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

        outcomes: dict[str, object] = {}
        threads = [
            threading.Thread(
                target=run_checkout,
                args=(session_factory, user_id, address, outcomes, str(index)),
            )
            for index, (user_id, address) in enumerate(shoppers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)

        db_session.rollback()
        assert db_session.execute(text("SELECT count(*) FROM orders")).scalar_one() == 1
        assert db_session.execute(
            text("SELECT count(*) FROM inventory_reservations WHERE status = 'ACTIVE'")
        ).scalar_one() == 1
        assert db_session.execute(
            text("SELECT quantity_reserved FROM inventory WHERE variant_id = :v"),
            {"v": variant},
        ).scalar_one() == 1


class TwoCarts:
    """Two customers holding the same two variants in opposite order."""

    def __init__(self, first, second):
        self.first = first
        self.second = second


@pytest.fixture()
def crossed(client, shop_admin, authorize, db_session) -> TwoCarts:
    _, x = sellable(client, shop_admin, name="X", sku="X-1", price="10.00", quantity=50)
    _, y = sellable(client, shop_admin, name="Y", sku="Y-1", price="10.00", quantity=50)
    configure_rules(client, shop_admin)

    shoppers = []
    # Opposite insertion order, so the *cart* order of the two lines differs
    # between the two customers. Without a global lock order that is enough
    # to form a cycle.
    for email, order in (("cross-a@example.com", (x, y)), ("cross-b@example.com", (y, x))):
        headers = authorize(email=email)
        for variant in order:
            add_item(client, headers, variant, quantity=1)
        shoppers.append(
            (user_of(db_session, email).id, uuid.UUID(address_id(client, headers)))
        )
    return TwoCarts(*shoppers)


def race_two_checkouts(test_engine, crossed, monkeypatch) -> dict[str, object]:
    """Force both checkouts to hold their first lock at the same time."""
    session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

    took_first = {"a": threading.Event(), "b": threading.Event()}
    seen: dict[str, bool] = {}
    real = inventory.reserve_locked

    def instrumented(db, **arguments):
        name = threading.current_thread().name
        reservation = real(db, **arguments)
        if not seen.get(name):
            seen[name] = True
            took_first[name].set()
            other = "b" if name == "a" else "a"
            # A timeout, not a barrier: when the acquisition order is
            # sorted the other thread is blocked in PostgreSQL and never
            # arrives here, which is the correct behaviour rather than a
            # hang to be waited out forever.
            took_first[other].wait(timeout=INTERLEAVE_TIMEOUT)
        return reservation

    monkeypatch.setattr(inventory, "reserve_locked", instrumented)

    outcomes: dict[str, object] = {}
    threads = [
        threading.Thread(
            target=run_checkout,
            args=(session_factory, user_id, address, outcomes, name),
            name=name,
        )
        for name, (user_id, address) in (("a", crossed.first), ("b", crossed.second))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)

    assert all(not thread.is_alive() for thread in threads), "a checkout never finished"
    return outcomes


def deadlocks_in(outcomes) -> list[Exception]:
    return [
        outcome
        for outcome in outcomes.values()
        if isinstance(outcome, Exception)
        and isinstance(getattr(outcome, "orig", None), psycopg.errors.DeadlockDetected)
    ]


class TestCrossedCartsDoNotDeadlock:
    def test_both_checkouts_succeed(self, test_engine, crossed, monkeypatch, db_session):
        outcomes = race_two_checkouts(test_engine, crossed, monkeypatch)

        assert not deadlocks_in(outcomes), f"a deadlock occurred: {outcomes}"
        failures = [out for out in outcomes.values() if isinstance(out, Exception)]
        assert not failures, f"a checkout failed: {failures}"

        db_session.rollback()
        assert db_session.execute(text("SELECT count(*) FROM orders")).scalar_one() == 2

    def test_the_acquisition_order_is_the_variant_id(self):
        """A direct reading, so the property cannot quietly vanish."""
        lines = [{"variant_id": uuid.UUID(int=2)}, {"variant_id": uuid.UUID(int=1)}]

        ordered = sorted(lines, key=orders._acquisition_order)

        assert ordered[0]["variant_id"] == uuid.UUID(int=1)


class TestRemovingTheSortDeadlocks:
    """The falsification. Without a global lock order, the cycle forms.

    This is the Phase 5 counterpart of Phase 4's lock-removal check. If it
    ever stops observing a deadlock, the test above it has stopped proving
    that sorting is what prevents one.
    """

    def test_unsorted_acquisition_produces_a_deadlock(
        self, test_engine, crossed, monkeypatch, db_session
    ):
        # Every line gets the same key, so `sorted` is a no-op and each
        # checkout locks in its own cart's insertion order - the exact
        # defect the real key removes.
        monkeypatch.setattr(orders, "_acquisition_order", lambda line: "")

        outcomes = race_two_checkouts(test_engine, crossed, monkeypatch)

        assert deadlocks_in(outcomes), (
            "removing the deterministic lock order did not produce a deadlock - "
            "the test above is not proving that sorting is what prevents one; "
            f"outcomes were {outcomes}"
        )


class TestConcurrentDoubleSubmit:
    """One cart, two simultaneous submits. One order, and both callers told so."""

    @pytest.fixture()
    def ready(self, client, shop_admin, authorize, db_session):
        _, variant = sellable(client, shop_admin, price="10.00", quantity=20)
        configure_rules(client, shop_admin)
        headers = authorize(email="double@example.com")
        add_item(client, headers, variant, quantity=2)
        return (
            user_of(db_session, "double@example.com").id,
            uuid.UUID(address_id(client, headers)),
            variant,
        )

    def test_a_double_submit_produces_exactly_one_order(
        self, test_engine, ready, db_session
    ):
        user_id, address, variant = ready
        session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

        outcomes: dict[str, object] = {}
        threads = [
            threading.Thread(
                target=run_checkout,
                args=(session_factory, user_id, address, outcomes, name),
            )
            for name in ("first", "second")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)

        assert all(not thread.is_alive() for thread in threads)
        assert not [out for out in outcomes.values() if isinstance(out, Exception)], outcomes

        db_session.rollback()
        assert db_session.execute(text("SELECT count(*) FROM orders")).scalar_one() == 1

        # Both callers were handed the same order, and exactly one of them
        # is told it created it.
        ids = {order_id for order_id, _ in outcomes.values()}
        assert len(ids) == 1, f"the two submits returned different orders: {outcomes}"
        assert sum(created for _, created in outcomes.values()) == 1

    def test_the_stock_is_reserved_once(self, test_engine, ready, db_session):
        user_id, address, variant = ready
        session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

        outcomes: dict[str, object] = {}
        threads = [
            threading.Thread(
                target=run_checkout,
                args=(session_factory, user_id, address, outcomes, name),
            )
            for name in ("first", "second")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=JOIN_TIMEOUT)

        db_session.rollback()
        assert db_session.execute(
            text("SELECT quantity_reserved FROM inventory WHERE variant_id = :v"),
            {"v": variant},
        ).scalar_one() == 2
