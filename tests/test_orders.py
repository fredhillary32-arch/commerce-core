"""Orders after checkout: reading them, ending them, and why they never change.

The property most worth protecting here is **historical stability**. An
order is a snapshot, so renaming the product, repricing the variant,
discontinuing it, amending the tax rate or deleting the saved address must
all leave the order reading back exactly as it did the day it was placed.
Each of those is asserted separately, because each is a different mechanism
- a frozen column, a JSONB copy, or a RESTRICT foreign key.

The state machine is the other half. ``CONFIRMED`` is terminal in this
phase: cancelling a paid order implies a refund (Phase 6) and a restock
(Phase 7), and a cancel path that could do neither would be worse than not
having one.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.exceptions import BusinessRuleError, NotFoundError
from app.modules.orders import service as orders
from tests.cart_helpers import add_item, get_cart
from tests.catalog_helpers import patch_product, patch_variant
from tests.checkout_fixtures import (  # noqa: F401  (fixtures)
    Shop,
    sellable,
    shop,
    shop_admin,
)
from tests.inventory_helpers import read_inventory
from tests.order_helpers import (
    ADMIN_ORDERS,
    ORDERS,
    TAX_RATES,
    address_id,
    cancel_order,
    checkout,
    create_tax_rate,
    delete_address,
    error_code,
    get_order,
    list_orders,
)


@pytest.fixture()
def placed(client, shop):
    """One PENDING_PAYMENT order, with its shop still to hand."""
    return checkout(client, shop.customer, shop.address).json()


class TestReadingOwnOrders:
    def test_an_order_reads_back(self, client, shop, placed):
        response = get_order(client, shop.customer, placed["id"])

        assert response.status_code == 200
        assert response.json()["reference"] == placed["reference"]

    def test_orders_are_listed_most_recent_first(self, client, shop, placed):
        add_item(client, shop.customer, shop.variant, quantity=1)
        second = checkout(client, shop.customer, shop.address).json()

        listed = list_orders(client, shop.customer).json()

        assert [row["id"] for row in listed] == [second["id"], placed["id"]]

    def test_a_listing_row_omits_the_lines(self, client, shop, placed):
        row = list_orders(client, shop.customer).json()[0]

        assert "items" not in row
        assert row["total_amount"] == "64.97"

    def test_a_listing_is_paged(self, client, shop, placed):
        add_item(client, shop.customer, shop.variant, quantity=1)
        checkout(client, shop.customer, shop.address)

        assert len(list_orders(client, shop.customer, limit=1).json()) == 1

    def test_another_customers_order_reports_not_found(self, client, shop, placed, authorize):
        stranger = authorize(email="nosy@example.com")

        response = get_order(client, stranger, placed["id"])

        assert response.status_code == 404
        assert error_code(response) == "ORDER_NOT_FOUND"

    def test_an_unknown_order_reports_not_found(self, client, shop):
        response = get_order(client, shop.customer, uuid.uuid4())

        assert response.status_code == 404

    def test_reading_requires_authentication(self, client):
        assert client.get(ORDERS).status_code == 401
        assert client.get(f"{ORDERS}/{uuid.uuid4()}").status_code == 401


class TestSnapshotDurability:
    """Everything an order shows is frozen. None of it is read back."""

    def test_renaming_the_product_leaves_the_order_alone(self, client, shop, placed):
        patch_product(client, shop.catalog, shop.product, name="Renamed Entirely")

        line = get_order(client, shop.customer, placed["id"]).json()["items"][0]

        assert line["product_name"] == "Aurora Desk Lamp"

    def test_repricing_the_variant_leaves_the_order_alone(self, client, shop, placed):
        patch_variant(client, shop.catalog, shop.variant, price="99.99")

        body = get_order(client, shop.customer, placed["id"]).json()

        assert body["items"][0]["unit_price"] == "24.99"
        assert body["total_amount"] == "64.97"

    def test_discontinuing_the_variant_leaves_the_order_readable(
        self, client, shop, placed
    ):
        patch_variant(client, shop.catalog, shop.variant, status="DISCONTINUED")

        response = get_order(client, shop.customer, placed["id"])

        assert response.status_code == 200
        assert response.json()["items"][0]["sku"] == "LAMP-001"

    def test_amending_the_tax_rate_leaves_the_order_alone(self, client, shop, placed):
        rate = client.get(TAX_RATES, headers=shop.catalog).json()[0]["id"]
        client.patch(f"{TAX_RATES}/{rate}", json={"rate": "0.0500"}, headers=shop.catalog)

        body = get_order(client, shop.customer, placed["id"]).json()

        assert body["tax_total"] == "10.00"
        assert body["items"][0]["tax_rate"] == "0.2000"

    def test_editing_the_saved_address_leaves_the_order_alone(self, client, shop, placed):
        client.patch(
            f"/api/v1/addresses/{shop.address}",
            json={"recipient_name": "Someone Else"},
            headers=shop.customer,
        )

        body = get_order(client, shop.customer, placed["id"]).json()

        assert body["shipping_address"]["recipient_name"] == "Ada Lovelace"

    def test_deleting_the_saved_address_leaves_the_order_alone(self, client, shop, placed):
        assert delete_address(client, shop.customer, shop.address).status_code == 204

        body = get_order(client, shop.customer, placed["id"]).json()

        assert body["shipping_address"]["recipient_name"] == "Ada Lovelace"

    def test_the_ordered_variant_cannot_be_deleted(self, client, shop, placed, test_engine):
        """RESTRICT is what keeps an order's line resolvable to a variant."""
        from sqlalchemy.exc import IntegrityError

        with test_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text("DELETE FROM product_variants WHERE id = :v"), {"v": shop.variant}
                )

    def test_the_buyer_cannot_be_deleted(self, client, shop, placed, test_engine):
        from sqlalchemy.exc import IntegrityError

        with test_engine.begin() as connection:
            user_id = connection.execute(
                text("SELECT user_id FROM orders WHERE id = :o"), {"o": placed["id"]}
            ).scalar_one()
            with pytest.raises(IntegrityError):
                connection.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


class TestCancellation:
    def test_an_unpaid_order_can_be_cancelled(self, client, shop, placed):
        response = cancel_order(client, shop.customer, placed["id"])

        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"
        assert response.json()["cancelled_at"] is not None

    def test_the_stock_goes_back(self, client, shop, placed):
        cancel_order(client, shop.customer, placed["id"])

        inventory = read_inventory(client, shop.inventory, shop.variant).json()
        assert inventory["quantity_reserved"] == 0
        assert inventory["available"] == 10

    def test_the_reservations_are_released_not_converted(self, client, shop, placed, db_session):
        cancel_order(client, shop.customer, placed["id"])

        statuses = db_session.execute(
            text("SELECT DISTINCT status FROM inventory_reservations")
        ).scalars().all()
        assert statuses == ["RELEASED"]

    def test_no_stock_is_permanently_deducted(self, client, shop, placed):
        cancel_order(client, shop.customer, placed["id"])

        assert read_inventory(client, shop.inventory, shop.variant).json()[
            "quantity_on_hand"
        ] == 10

    def test_the_basket_comes_back(self, client, shop, placed):
        """Rebuilding a cart from scratch after a failed payment is poor UX."""
        cancel_order(client, shop.customer, placed["id"])

        cart = get_cart(client, shop.customer).json()
        assert cart["status"] == "ACTIVE"
        assert len(cart["items"]) == 1
        assert cart["items"][0]["quantity"] == 2

    def test_the_restored_lines_merge_into_a_basket_already_started(
        self, client, shop, placed
    ):
        add_item(client, shop.customer, shop.variant, quantity=3)

        cancel_order(client, shop.customer, placed["id"])

        cart = get_cart(client, shop.customer).json()
        assert len(cart["items"]) == 1
        assert cart["items"][0]["quantity"] == 5

    def test_a_line_that_is_no_longer_sellable_comes_back_flagged(
        self, client, shop, placed
    ):
        """Flagged by the cart's existing read logic, not special-cased here."""
        patch_variant(client, shop.catalog, shop.variant, status="DISCONTINUED")

        cancel_order(client, shop.customer, placed["id"])

        line = get_cart(client, shop.customer).json()["items"][0]
        assert line["available"] is False
        assert line["unavailable_reason"] == "VARIANT_NOT_ACTIVE"

    def test_the_converted_cart_stays_converted(self, client, shop, placed, db_session):
        """So the order keeps its route to its reservations, and its uniqueness."""
        cancel_order(client, shop.customer, placed["id"])

        cart_id = db_session.execute(
            text("SELECT cart_id FROM orders WHERE id = :o"), {"o": placed["id"]}
        ).scalar_one()
        assert db_session.execute(
            text("SELECT status FROM carts WHERE id = :c"), {"c": cart_id}
        ).scalar_one() == "CONVERTED"

    def test_cancelling_twice_is_refused(self, client, shop, placed):
        cancel_order(client, shop.customer, placed["id"])

        response = cancel_order(client, shop.customer, placed["id"])

        assert response.status_code == 422
        assert error_code(response) == "INVALID_ORDER_STATE"

    def test_the_stock_is_not_released_twice(self, client, shop, placed):
        cancel_order(client, shop.customer, placed["id"])
        cancel_order(client, shop.customer, placed["id"])

        assert read_inventory(client, shop.inventory, shop.variant).json()[
            "quantity_reserved"
        ] == 0

    def test_another_customer_cannot_cancel_an_order(self, client, shop, placed, authorize):
        stranger = authorize(email="vandal@example.com")

        response = cancel_order(client, stranger, placed["id"])

        assert response.status_code == 404
        assert error_code(response) == "ORDER_NOT_FOUND"
        assert get_order(client, shop.customer, placed["id"]).json()["status"] == (
            "PENDING_PAYMENT"
        )

    def test_cancellation_is_audited(self, client, shop, placed, db_session):
        cancel_order(client, shop.customer, placed["id"])

        assert db_session.execute(
            text("SELECT count(*) FROM audit_logs WHERE event_type = 'order.cancelled'")
        ).scalar_one() == 1


class TestConfirmation:
    """Phase 6's entry point. Service layer only - there is no route."""

    def test_no_http_route_confirms_an_order(self, client):
        """Phase 6 is the only caller, so Phase 5 invents no endpoint for it.

        Scoped to order paths deliberately: the catalogue has its own
        unrelated ``/images/confirm``, and a bare suffix check would pass
        or fail for the wrong reason.
        """
        paths = client.app.openapi()["paths"]
        order_paths = [path for path in paths if "orders" in path or "checkout" in path]

        assert order_paths, "the order routes are not registered"
        assert not [path for path in order_paths if path.endswith("/confirm")]

    def test_a_pending_order_is_confirmed(self, client, shop, placed, db_session):
        order = orders.confirm_order(db_session, order_id=uuid.UUID(placed["id"]))

        assert order.status == "CONFIRMED"
        assert order.confirmed_at is not None

    def test_the_stock_is_permanently_deducted(self, client, shop, placed, db_session):
        orders.confirm_order(db_session, order_id=uuid.UUID(placed["id"]))

        inventory = read_inventory(client, shop.inventory, shop.variant).json()
        assert inventory["quantity_on_hand"] == 8
        assert inventory["quantity_reserved"] == 0

    def test_a_purchase_is_written_to_the_ledger(self, client, shop, placed, db_session):
        orders.confirm_order(db_session, order_id=uuid.UUID(placed["id"]))

        rows = db_session.execute(
            text(
                "SELECT transaction_type, quantity, from_bucket "
                "FROM inventory_transactions WHERE transaction_type = 'PURCHASE'"
            )
        ).all()
        assert [(row.transaction_type, row.quantity, row.from_bucket) for row in rows] == [
            ("PURCHASE", 2, "ON_HAND")
        ]

    def test_confirming_a_cancelled_order_is_refused(self, client, shop, placed, db_session):
        cancel_order(client, shop.customer, placed["id"])

        with pytest.raises(BusinessRuleError) as raised:
            orders.confirm_order(db_session, order_id=uuid.UUID(placed["id"]))

        assert raised.value.code == "INVALID_ORDER_STATE"

    def test_confirming_twice_is_refused(self, client, shop, placed, db_session):
        orders.confirm_order(db_session, order_id=uuid.UUID(placed["id"]))

        with pytest.raises(BusinessRuleError) as raised:
            orders.confirm_order(db_session, order_id=uuid.UUID(placed["id"]))

        assert raised.value.code == "INVALID_ORDER_STATE"

    def test_a_confirmed_order_cannot_be_cancelled(self, client, shop, placed, db_session):
        """Terminal in Phase 5: no refund path and no restock path exist yet."""
        orders.confirm_order(db_session, order_id=uuid.UUID(placed["id"]))

        response = cancel_order(client, shop.customer, placed["id"])

        assert response.status_code == 422
        assert error_code(response) == "INVALID_ORDER_STATE"

    def test_an_unknown_order_is_reported(self, db_session):
        with pytest.raises(NotFoundError) as raised:
            orders.confirm_order(db_session, order_id=uuid.uuid4())

        assert raised.value.code == "ORDER_NOT_FOUND"

    def test_a_lapsed_hold_refuses_confirmation(self, client, shop, placed, db_session):
        """Phase 5 will not confirm an order whose stock is no longer held."""
        db_session.execute(
            text("UPDATE inventory_reservations SET status = 'EXPIRED'")
        )
        db_session.execute(text("UPDATE inventory SET quantity_reserved = 0"))
        db_session.commit()

        with pytest.raises(BusinessRuleError) as raised:
            orders.confirm_order(db_session, order_id=uuid.UUID(placed["id"]))

        assert raised.value.code == "INSUFFICIENT_INVENTORY"


class TestTheStateMachine:
    def test_the_transitions_are_declared_in_one_place(self):
        assert orders.ORDER_TRANSITIONS == {
            "PENDING_PAYMENT": frozenset({"CONFIRMED", "CANCELLED"}),
            "CONFIRMED": frozenset(),
            "CANCELLED": frozenset(),
        }

    def test_the_database_admits_no_other_status(self, test_engine, placed):
        from sqlalchemy.exc import IntegrityError

        with test_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text("UPDATE orders SET status = 'SHIPPED' WHERE id = :o"),
                    {"o": placed["id"]},
                )


class TestAdministrativeAccess:
    def test_orders_read_sees_any_customers_order(self, client, shop, placed, authorize):
        admin = authorize("orders.read", email="support@example.com")

        response = client.get(f"{ADMIN_ORDERS}/{placed['id']}", headers=admin)

        assert response.status_code == 200
        assert response.json()["reference"] == placed["reference"]

    def test_orders_read_lists_every_order(self, client, shop, placed, authorize):
        admin = authorize("orders.read", email="support2@example.com")

        assert len(client.get(ADMIN_ORDERS, headers=admin).json()) == 1

    def test_orders_read_does_not_grant_cancellation(self, client, shop, placed, authorize):
        admin = authorize("orders.read", email="support3@example.com")

        response = client.post(f"{ADMIN_ORDERS}/{placed['id']}/cancel", headers=admin)

        assert response.status_code == 403
        assert error_code(response) == "PERMISSION_DENIED"

    def test_orders_manage_cancels_any_order(self, client, shop, placed, authorize):
        admin = authorize("orders.manage", email="manager@example.com")

        response = client.post(f"{ADMIN_ORDERS}/{placed['id']}/cancel", headers=admin)

        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"

    def test_an_administrative_cancellation_returns_the_basket_too(
        self, client, shop, placed, authorize
    ):
        admin = authorize("orders.manage", email="manager2@example.com")

        client.post(f"{ADMIN_ORDERS}/{placed['id']}/cancel", headers=admin)

        assert len(get_cart(client, shop.customer).json()["items"]) == 1

    def test_a_plain_customer_cannot_reach_the_admin_surface(self, client, shop, placed):
        assert client.get(ADMIN_ORDERS, headers=shop.customer).status_code == 403
        assert client.get(
            f"{ADMIN_ORDERS}/{placed['id']}", headers=shop.customer
        ).status_code == 403

    def test_inventory_adjust_grants_no_order_authority(self, client, placed, authorize):
        stock_keeper = authorize("inventory.adjust", email="warehouse@example.com")

        assert client.get(ADMIN_ORDERS, headers=stock_keeper).status_code == 403


class TestTotalsReconcile:
    def test_every_order_total_equals_the_sum_of_its_lines(self, client, shop, placed):
        body = get_order(client, shop.customer, placed["id"]).json()

        subtotal = sum(Decimal(line["line_subtotal"]) for line in body["items"])
        tax = sum(Decimal(line["tax_amount"]) for line in body["items"])
        assert Decimal(body["subtotal_amount"]) == subtotal
        assert Decimal(body["tax_total"]) == tax
        assert Decimal(body["total_amount"]) == (
            subtotal
            - Decimal(body["discount_total"])
            + tax
            + Decimal(body["shipping_total"])
        )
