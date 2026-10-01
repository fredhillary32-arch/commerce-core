"""Cart behaviour: get-or-create, item rules, and availability flagging."""

import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from tests.cart_helpers import (
    add_item,
    clear_cart,
    get_cart,
    items_of,
    only_item,
    remove_item,
    update_item,
)
from tests.catalog_helpers import (
    activate_product_with_variant,
    create_variant,
    patch_product,
    patch_variant,
    product_id,
)


@pytest.fixture()
def customer(authorize) -> dict[str, str]:
    """A plain registered user holding no permissions at all.

    This is the whole point of the cart's authorization model: a brand-new
    customer, who by the Phase 2 rule has zero roles, must be able to use
    their own cart immediately.
    """
    return authorize(email="customer@example.com")


@pytest.fixture()
def other_customer(authorize) -> dict[str, str]:
    return authorize(email="someone-else@example.com")


@pytest.fixture()
def sellable_variant(client, catalog_admin) -> str:
    """An ACTIVE variant on an ACTIVE product - something you may buy."""
    _, variant = activate_product_with_variant(client, catalog_admin)
    return variant


class TestGetOrCreate:
    def test_a_first_request_creates_an_active_cart(self, client, customer):
        response = get_cart(client, customer)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ACTIVE"
        assert body["items"] == []

    def test_a_second_request_returns_the_same_cart(self, client, customer):
        first = get_cart(client, customer).json()["id"]
        second = get_cart(client, customer).json()["id"]

        assert first == second

    def test_a_user_never_ends_up_with_two_active_carts(self, client, customer, test_engine):
        for _ in range(4):
            get_cart(client, customer)

        with test_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM carts WHERE status = 'ACTIVE'")
                ).scalar_one()
                == 1
            )

    def test_anonymous_callers_are_refused(self, client):
        response = get_cart(client, {})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHORIZATION_REQUIRED"

    def test_two_users_get_two_different_carts(self, client, customer, other_customer):
        mine = get_cart(client, customer).json()["id"]
        theirs = get_cart(client, other_customer).json()["id"]

        assert mine != theirs


class TestGetOrCreateRace:
    """Two concurrent first requests for the same user.

    Both transactions SELECT nothing and both attempt an INSERT; the partial
    unique index rejects one of them. The service has to recognise that and
    re-read, rather than letting an IntegrityError surface as a 500.
    """

    def test_concurrent_first_requests_resolve_to_one_cart(
        self, client, test_engine, authorize
    ):
        from app.db.models.user import User
        from app.modules.cart import service as cart_service

        authorize(email="racer@example.com")
        session_factory = sessionmaker(bind=test_engine, autoflush=False, future=True)

        with test_engine.connect() as connection:
            user_id = connection.execute(
                text("SELECT id FROM users WHERE email = 'racer@example.com'")
            ).scalar_one()

        barrier = threading.Barrier(2)
        results: list[object] = []

        def attempt() -> None:
            session = session_factory()
            try:
                user = session.get(User, user_id)
                barrier.wait(timeout=10)
                results.append(cart_service.get_or_create_active_cart(session, user).id)
            except Exception as error:  # noqa: BLE001 - recorded, asserted below
                results.append(error)
            finally:
                session.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert len(results) == 2
        assert all(isinstance(result, uuid.UUID) for result in results), results
        assert results[0] == results[1]

        with test_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM carts WHERE user_id = :u AND status = 'ACTIVE'"),
                    {"u": user_id},
                ).scalar_one()
                == 1
            )


class TestOwnership:
    def test_a_user_cannot_touch_another_users_item(
        self, client, customer, other_customer, sellable_variant
    ):
        theirs = add_item(client, other_customer, sellable_variant)
        item = only_item(theirs)["id"]

        assert update_item(client, customer, item, 5).status_code == 404
        assert remove_item(client, customer, item).status_code == 404

    def test_the_refusal_says_the_item_is_missing_not_forbidden(
        self, client, customer, other_customer, sellable_variant
    ):
        """From this cart's perspective the item does not exist.

        Answering 403 would confirm that the id is real and belongs to
        somebody, which is more than the caller is entitled to know.
        """
        item = only_item(add_item(client, other_customer, sellable_variant))["id"]

        response = remove_item(client, customer, item)

        assert response.json()["error"]["code"] == "CART_ITEM_NOT_FOUND"

    def test_clearing_a_cart_leaves_other_carts_alone(
        self, client, customer, other_customer, sellable_variant
    ):
        add_item(client, customer, sellable_variant)
        add_item(client, other_customer, sellable_variant)

        clear_cart(client, customer)

        assert items_of(get_cart(client, customer)) == []
        assert len(items_of(get_cart(client, other_customer))) == 1

    def test_the_cart_is_never_addressed_by_a_client_supplied_id(self, client):
        """There is no route that takes a cart id, so none can be forged."""
        paths = client.app.openapi()["paths"]

        assert not any("{cart_id}" in path for path in paths)
        assert not any("{user_id}" in path for path in paths if path.startswith("/api/v1/cart"))


class TestAddingItems:
    def test_an_item_is_added_with_its_quantity(self, client, customer, sellable_variant):
        response = add_item(client, customer, sellable_variant, quantity=3)

        assert response.status_code == 201
        item = only_item(response)
        assert item["variant_id"] == sellable_variant
        assert item["quantity"] == 3

    def test_adding_the_same_variant_increments_rather_than_duplicating(
        self, client, customer, sellable_variant
    ):
        add_item(client, customer, sellable_variant, quantity=2)
        response = add_item(client, customer, sellable_variant, quantity=3)

        assert only_item(response)["quantity"] == 5

    def test_the_item_carries_the_variants_current_price(
        self, client, customer, sellable_variant
    ):
        item = only_item(add_item(client, customer, sellable_variant))

        assert item["unit_price"] == "24.99"
        assert item["currency"] == "GBP"

    def test_adding_an_unknown_variant_is_refused(self, client, customer):
        response = add_item(client, customer, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "VARIANT_NOT_FOUND"

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_a_non_positive_quantity_is_refused(
        self, client, customer, sellable_variant, quantity
    ):
        response = add_item(client, customer, sellable_variant, quantity=quantity)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CART_ITEM_QUANTITY"

    def test_adding_a_draft_variant_is_refused(self, client, customer, catalog_admin):
        product = product_id(client, catalog_admin)
        variant = create_variant(
            client, catalog_admin, product, sku="DRAFT-1", price="9.99", currency="GBP"
        ).json()["id"]

        response = add_item(client, customer, variant)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VARIANT_NOT_AVAILABLE"

    def test_adding_a_discontinued_variant_is_refused(
        self, client, customer, catalog_admin, sellable_variant
    ):
        patch_variant(client, catalog_admin, sellable_variant, status="DISCONTINUED")

        response = add_item(client, customer, sellable_variant)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VARIANT_NOT_AVAILABLE"

    def test_adding_an_active_variant_of_an_archived_product_is_refused(
        self, client, customer, catalog_admin
    ):
        product, variant = activate_product_with_variant(client, catalog_admin)
        patch_product(client, catalog_admin, product, status="ARCHIVED")

        response = add_item(client, customer, variant)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VARIANT_NOT_AVAILABLE"

    def test_adding_an_item_does_not_reserve_inventory(
        self, client, customer, sellable_variant, test_engine
    ):
        """Cart state and reservation state are separate concerns."""
        add_item(client, customer, sellable_variant, quantity=2)

        with test_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM inventory_reservations")
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT coalesce(sum(quantity_reserved), 0) FROM inventory")
                ).scalar_one()
                == 0
            )


class TestUpdatingItems:
    def test_a_quantity_can_be_raised_and_lowered(self, client, customer, sellable_variant):
        item = only_item(add_item(client, customer, sellable_variant, quantity=2))["id"]

        assert only_item(update_item(client, customer, item, 7))["quantity"] == 7
        assert only_item(update_item(client, customer, item, 1))["quantity"] == 1

    def test_setting_the_quantity_to_zero_removes_the_item(
        self, client, customer, sellable_variant
    ):
        item = only_item(add_item(client, customer, sellable_variant))["id"]

        response = update_item(client, customer, item, 0)

        assert response.status_code == 200
        assert items_of(response) == []

    def test_a_negative_quantity_is_refused(self, client, customer, sellable_variant):
        item = only_item(add_item(client, customer, sellable_variant))["id"]

        response = update_item(client, customer, item, -1)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CART_ITEM_QUANTITY"

    def test_updating_an_unknown_item_is_refused(self, client, customer):
        response = update_item(client, customer, uuid.uuid4(), 3)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CART_ITEM_NOT_FOUND"


class TestRemovingItems:
    def test_an_item_is_removed(self, client, customer, sellable_variant):
        item = only_item(add_item(client, customer, sellable_variant))["id"]

        assert remove_item(client, customer, item).status_code == 204
        assert items_of(get_cart(client, customer)) == []

    def test_removing_an_unknown_item_is_refused(self, client, customer):
        response = remove_item(client, customer, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CART_ITEM_NOT_FOUND"

    def test_clearing_empties_the_cart_but_keeps_it_active(
        self, client, customer, catalog_admin, sellable_variant
    ):
        _, second = activate_product_with_variant(
            client, catalog_admin, name="Second Lamp", sku="LAMP-002"
        )
        add_item(client, customer, sellable_variant)
        add_item(client, customer, second)

        assert clear_cart(client, customer).status_code == 204

        after = get_cart(client, customer)
        assert items_of(after) == []
        assert after.json()["status"] == "ACTIVE"

    def test_clearing_an_empty_cart_is_not_an_error(self, client, customer):
        assert clear_cart(client, customer).status_code == 204


class TestAvailabilityFlagging:
    """An item already in the cart is never silently deleted.

    Deleting it would destroy the customer's cart without their consent;
    presenting it as purchasable would be a lie. It stays, flagged.
    """

    def test_a_sellable_item_is_flagged_available(self, client, customer, sellable_variant):
        item = only_item(add_item(client, customer, sellable_variant))

        assert item["available"] is True
        assert item["unavailable_reason"] is None

    def test_an_item_whose_variant_is_discontinued_stays_and_is_flagged(
        self, client, customer, catalog_admin, sellable_variant
    ):
        add_item(client, customer, sellable_variant, quantity=2)

        patch_variant(client, catalog_admin, sellable_variant, status="DISCONTINUED")

        item = only_item(get_cart(client, customer))
        assert item["quantity"] == 2
        assert item["available"] is False
        assert item["unavailable_reason"] == "VARIANT_NOT_ACTIVE"

    def test_an_item_whose_product_is_archived_stays_and_is_flagged(
        self, client, customer, catalog_admin
    ):
        product, variant = activate_product_with_variant(client, catalog_admin)
        add_item(client, customer, variant)

        patch_product(client, catalog_admin, product, status="ARCHIVED")

        item = only_item(get_cart(client, customer))
        assert item["available"] is False
        assert item["unavailable_reason"] == "PRODUCT_NOT_ACTIVE"

    def test_the_row_really_is_still_there(
        self, client, customer, catalog_admin, sellable_variant, test_engine
    ):
        add_item(client, customer, sellable_variant)
        patch_variant(client, catalog_admin, sellable_variant, status="DISCONTINUED")

        with test_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM cart_items")).scalar_one() == 1

    def test_an_unavailable_item_can_still_be_removed_by_the_customer(
        self, client, customer, catalog_admin, sellable_variant
    ):
        item = only_item(add_item(client, customer, sellable_variant))["id"]
        patch_variant(client, catalog_admin, sellable_variant, status="DISCONTINUED")

        assert remove_item(client, customer, item).status_code == 204


class TestCartAudit:
    def audit_types(self, test_engine) -> list[str]:
        with test_engine.connect() as connection:
            return (
                connection.execute(
                    text(
                        "SELECT event_type FROM audit_logs WHERE event_type LIKE 'cart.%' "
                        "ORDER BY created_at"
                    )
                )
                .scalars()
                .all()
            )

    def test_adding_updating_and_removing_are_recorded(
        self, client, customer, sellable_variant, test_engine
    ):
        item = only_item(add_item(client, customer, sellable_variant))["id"]
        update_item(client, customer, item, 4)
        remove_item(client, customer, item)

        assert self.audit_types(test_engine) == [
            "cart.item_added",
            "cart.item_updated",
            "cart.item_removed",
        ]

    def test_the_audit_row_names_the_acting_customer(
        self, client, customer, sellable_variant, test_engine
    ):
        add_item(client, customer, sellable_variant)

        with test_engine.connect() as connection:
            actor = connection.execute(
                text(
                    "SELECT u.email FROM audit_logs a JOIN users u ON u.id = a.actor_user_id "
                    "WHERE a.event_type = 'cart.item_added'"
                )
            ).scalar_one()

        assert actor == "customer@example.com"

    def test_reading_a_cart_is_not_audited(self, client, customer, test_engine):
        get_cart(client, customer)
        get_cart(client, customer)

        assert self.audit_types(test_engine) == []


class TestConcurrentDuplicateAdd:
    """Two requests adding the same variant to one cart at the same time.

    Both read "no such line yet" and both insert, so
    uq_cart_items_cart_id_variant_id rejects one of them. The locked
    contract says a duplicate add *increments*, so the loser must recover
    and increment rather than surfacing the constraint violation as a 500.

    This is the same structural shape as the adjust_inventory bug: a
    repository helper flushes internally, and the surrounding code has to
    cover it.
    """

    def test_the_loser_of_the_race_increments_instead_of_failing(
        self, client, customer, sellable_variant, monkeypatch, test_engine
    ):
        from app.modules.cart import repository as cart_repository

        add_item(client, customer, sellable_variant, quantity=2)

        # Simulate the losing request: it read the cart before the winner
        # committed, so it still believes there is no line for this variant.
        original = cart_repository.get_item_by_variant
        seen: list[int] = []

        def blind_once(db, *, cart_id, variant_id):
            seen.append(1)
            if len(seen) == 1:
                return None
            return original(db, cart_id=cart_id, variant_id=variant_id)

        monkeypatch.setattr(cart_repository, "get_item_by_variant", blind_once)

        response = add_item(client, customer, sellable_variant, quantity=3)

        assert response.status_code == 201, response.json()
        assert only_item(response)["quantity"] == 5

        with test_engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM cart_items")).scalar_one() == 1

    def test_an_unrelated_integrity_error_is_not_swallowed(
        self, client, customer, sellable_variant, db_session, monkeypatch, test_engine
    ):
        """Only the duplicate-line violation is recovered from.

        Anything else propagates, rather than being retried into a second
        identical failure or quietly turned into a success. Asserted at the
        service layer: the invariant is about what the service re-raises,
        not about how the HTTP layer renders it.
        """
        from sqlalchemy.exc import IntegrityError

        from app.db.models.user import User
        from app.modules.cart import repository as cart_repository
        from app.modules.cart import service as cart_service
        from app.modules.audit.service import ClientInfo

        get_cart(client, customer)
        db_session.rollback()
        user = db_session.execute(
            text("SELECT id FROM users WHERE email = 'customer@example.com'")
        ).scalar_one()
        actor = db_session.get(User, user)

        class Diagnostic:
            constraint_name = "some_entirely_different_constraint"

        class Original:
            diag = Diagnostic()

        attempts: list[int] = []

        def always_fails(db, *, cart_id, variant_id, quantity):
            attempts.append(1)
            raise IntegrityError("stmt", {}, Original())

        monkeypatch.setattr(cart_repository, "add_item", always_fails)

        with pytest.raises(IntegrityError):
            cart_service.add_item(
                db_session,
                user=actor,
                variant_id=uuid.UUID(sellable_variant),
                quantity=1,
                client=ClientInfo(),
            )

        assert attempts == [1], "an unrelated violation must not be retried"
