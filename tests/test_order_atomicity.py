"""The single-commit boundary, verified by breaking things on purpose.

This is the most important test module in Phase 5, and it is written to be
hard to fool. The claim under test is that checkout is **one transaction**:
every reservation, the order, its lines and the cart's transition to
CONVERTED commit together or not at all, so a partially successful checkout
is not something to detect and clean up but something that cannot occur.

An atomicity claim that was never falsified is not evidence. So each test
here injects a failure at a specific step and asserts that *nothing*
survived it - no order, no order lines, no reservations, no change to
``quantity_reserved``, and a cart still ACTIVE with its items intact.

``TestTheTestsWouldNoticeABrokenBoundary`` is the falsification. It breaks
the boundary deliberately - by making the composable reservation half
commit, which is exactly what Phase 4's ``reserve`` did - and shows the
orphan state that then survives. If that class ever stops failing to find
orphans, the tests above it have stopped proving anything.
"""

import uuid

import pytest
from sqlalchemy import text

from app.db.models.user import User
from app.modules.inventory import service as inventory
from app.modules.orders import repository as orders_repository
from app.modules.orders import service as orders
from tests.cart_helpers import add_item
from tests.checkout_fixtures import (  # noqa: F401  (fixtures)
    Shop,
    sellable,
    shop,
    shop_admin,
)


class Injected(Exception):
    """A failure that is nothing to do with business rules.

    Deliberately not an ``AppException``: a rejection the service knows how
    to describe would exercise a path the service was designed for. This
    stands in for the ones it was not - a dropped connection, a bug, a
    deploy mid-request.
    """


@pytest.fixture()
def buyer(db_session, shop) -> User:
    """The customer's ORM row, so checkout can be driven at the service layer.

    Driven directly rather than over HTTP because these failures are
    injected *inside* the transaction, and the assertion is about what the
    database holds afterwards rather than about a status code.
    """
    return db_session.execute(
        text("SELECT id FROM users WHERE email = 'buyer@example.com'")
    ).scalar_one() and db_session.get(
        User,
        db_session.execute(
            text("SELECT id FROM users WHERE email = 'buyer@example.com'")
        ).scalar_one(),
    )


@pytest.fixture()
def two_lines(client, shop):
    """A second stocked line, so a failure can land part-way through."""
    _, second = sellable(client, shop.catalog, name="Beta", sku="BETA-1", price="10.00")
    add_item(client, shop.customer, second, quantity=3)
    return second


def attempt(db_session, shop, buyer):
    from app.modules.audit.service import ClientInfo

    return orders.checkout(
        db_session,
        user=buyer,
        shipping_address_id=uuid.UUID(shop.address),
        billing_address_id=None,
        client=ClientInfo(),
    )


def assert_nothing_survived(db_session) -> None:
    """No order, no lines, no holds, no counters moved, cart still live."""
    db_session.rollback()

    assert db_session.execute(text("SELECT count(*) FROM orders")).scalar_one() == 0
    assert db_session.execute(text("SELECT count(*) FROM order_items")).scalar_one() == 0
    assert (
        db_session.execute(text("SELECT count(*) FROM inventory_reservations")).scalar_one()
        == 0
    )
    reserved = db_session.execute(
        text("SELECT coalesce(sum(quantity_reserved), 0) FROM inventory")
    ).scalar_one()
    assert reserved == 0, f"quantity_reserved drifted to {reserved}"

    statuses = db_session.execute(text("SELECT status FROM carts")).scalars().all()
    assert statuses == ["ACTIVE"], statuses
    assert db_session.execute(text("SELECT count(*) FROM cart_items")).scalar_one() >= 1


class TestAFailureAtStepSevenLeavesNothing:
    """Reserving the lines."""

    def test_a_failure_on_the_second_reservation_undoes_the_first(
        self, db_session, shop, buyer, two_lines, monkeypatch
    ):
        calls = {"n": 0}
        real = inventory.reserve_locked

        def failing(db, **arguments):
            calls["n"] += 1
            if calls["n"] == 2:
                raise Injected("the second line failed to reserve")
            return real(db, **arguments)

        monkeypatch.setattr(inventory, "reserve_locked", failing)

        with pytest.raises(Injected):
            attempt(db_session, shop, buyer)

        assert calls["n"] == 2
        assert_nothing_survived(db_session)

    def test_a_failure_on_the_first_reservation_leaves_nothing(
        self, db_session, shop, buyer, monkeypatch
    ):
        monkeypatch.setattr(
            inventory,
            "reserve_locked",
            lambda db, **arguments: (_ for _ in ()).throw(Injected("no")),
        )

        with pytest.raises(Injected):
            attempt(db_session, shop, buyer)

        assert_nothing_survived(db_session)


class TestAFailureAtStepEightLeavesNothing:
    """Writing the order and its lines."""

    def test_a_failure_writing_the_order_header_undoes_the_reservations(
        self, db_session, shop, buyer, two_lines, monkeypatch
    ):
        monkeypatch.setattr(
            orders_repository,
            "add_order",
            lambda db, **fields: (_ for _ in ()).throw(Injected("no order")),
        )

        with pytest.raises(Injected):
            attempt(db_session, shop, buyer)

        # Both lines had already been reserved when this failed. Neither
        # hold may survive.
        assert_nothing_survived(db_session)

    def test_a_failure_writing_the_second_line_undoes_everything(
        self, db_session, shop, buyer, two_lines, monkeypatch
    ):
        calls = {"n": 0}
        real = orders_repository.add_item

        def failing(db, **fields):
            calls["n"] += 1
            if calls["n"] == 2:
                raise Injected("the second line failed to write")
            return real(db, **fields)

        monkeypatch.setattr(orders_repository, "add_item", failing)

        with pytest.raises(Injected):
            attempt(db_session, shop, buyer)

        assert calls["n"] == 2
        assert_nothing_survived(db_session)


class TestAFailureAtStepNineLeavesNothing:
    """Converting the cart, and the audit row that follows it."""

    def test_a_failure_after_the_cart_transition_undoes_all_of_it(
        self, db_session, shop, buyer, two_lines, monkeypatch
    ):
        """The audit write is the last thing before the commit.

        By the time it runs, both reservations exist, the order and its two
        lines exist, and the cart has already been marked CONVERTED. A
        failure here has the most to undo of any step.
        """
        from app.modules.audit import service as audit

        real = audit.record_event

        def failing(db, **arguments):
            if arguments.get("event_type") == audit.ORDER_PLACED:
                raise Injected("the order audit failed")
            return real(db, **arguments)

        monkeypatch.setattr(audit, "record_event", failing)

        with pytest.raises(Injected):
            attempt(db_session, shop, buyer)

        assert_nothing_survived(db_session)


class TestTheCommitIsTheOnlyBoundary:
    def test_the_successful_path_commits_exactly_once(
        self, db_session, shop, buyer, two_lines, monkeypatch
    ):
        """Two reservations, an order, two lines, a cart change - one commit.

        A second commit would mean a window in which some of that work was
        durable and the rest was not.
        """
        commits = {"n": 0}
        real = type(db_session).commit

        def counting(self):
            commits["n"] += 1
            return real(self)

        monkeypatch.setattr(type(db_session), "commit", counting)

        order, created = attempt(db_session, shop, buyer)

        assert created is True
        assert commits["n"] == 1, f"checkout committed {commits['n']} times"


class TestTheTestsWouldNoticeABrokenBoundary:
    """The falsification. Break the boundary; watch the orphans appear.

    This is the Phase 5 equivalent of Phase 4's lock-removal check. It
    re-creates the exact defect the D1-C split was made to remove - a
    reservation step that commits on its own - and shows that the failure
    then leaves stock held for an order that does not exist.

    If this class ever passes without finding orphans, the atomicity tests
    above are no longer testing anything.
    """

    def test_a_committing_reservation_step_strands_stock(
        self, db_session, shop, buyer, two_lines, monkeypatch
    ):
        real = inventory.reserve_locked
        calls = {"n": 0}

        def commits_like_phase_four(db, **arguments):
            """What ``reserve`` did before the split: work, then commit."""
            calls["n"] += 1
            if calls["n"] == 2:
                raise Injected("the second line failed to reserve")
            reservation = real(db, **arguments)
            db.commit()
            return reservation

        monkeypatch.setattr(inventory, "reserve_locked", commits_like_phase_four)

        with pytest.raises(Injected):
            attempt(db_session, shop, buyer)

        db_session.rollback()
        orphans = db_session.execute(
            text("SELECT count(*) FROM inventory_reservations WHERE status = 'ACTIVE'")
        ).scalar_one()
        stranded = db_session.execute(
            text("SELECT coalesce(sum(quantity_reserved), 0) FROM inventory")
        ).scalar_one()
        order_count = db_session.execute(text("SELECT count(*) FROM orders")).scalar_one()

        # This is the bug, reproduced: stock held, no order to hold it for.
        assert orphans == 1, (
            "breaking the commit boundary produced no orphan reservation - "
            "the atomicity tests above are proving nothing"
        )
        assert stranded > 0
        assert order_count == 0
