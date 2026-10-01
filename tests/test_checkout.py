"""Checkout: one transaction that either produces a whole order or nothing.

The single most important property asserted anywhere in Phase 5 is that a
**partially successful checkout is impossible** - not recovered from, not
compensated for, but structurally unable to occur, because every reservation
and the order itself commit together or not at all. That property is
verified from the failure side in ``test_order_atomicity.py``; here it is
verified from the success side, along with every rejection that stops a
checkout before it writes anything.

The money is the other thing worth reading carefully. Every amount is a
``Decimal`` rounded ``ROUND_HALF_UP`` **per line and then summed**, so a
customer adding up the printed lines gets the printed total exactly.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.cart_helpers import add_item, clear_cart, get_cart
from tests.catalog_helpers import patch_product, patch_variant
from tests.checkout_fixtures import (  # noqa: F401  (fixtures)
    Shop,
    configure_rules,
    sellable,
    shop,
    shop_admin,
)
from tests.inventory_helpers import read_inventory
from tests.order_helpers import (
    CHECKOUT,
    address_id,
    checkout,
    create_shipping_rule,
    create_tax_rate,
    error_code,
)


def orders_in(db_session) -> int:
    return db_session.execute(text("SELECT count(*) FROM orders")).scalar_one()


def active_reservations(db_session) -> int:
    return db_session.execute(
        text("SELECT count(*) FROM inventory_reservations WHERE status = 'ACTIVE'")
    ).scalar_one()


def nothing_persisted(db_session, client, shop_) -> None:
    """No order, no reservations, cart untouched - the rejection invariant."""
    db_session.commit()
    assert orders_in(db_session) == 0
    assert active_reservations(db_session) == 0
    assert get_cart(client, shop_.customer).json()["status"] == "ACTIVE"


class TestTheHappyPath:
    def test_an_order_is_created(self, client, shop):
        response = checkout(client, shop.customer, shop.address)

        assert response.status_code == 201, response.json()
        assert response.json()["status"] == "PENDING_PAYMENT"

    def test_the_line_is_snapshotted_from_the_catalogue(self, client, shop):
        body = checkout(client, shop.customer, shop.address).json()

        line = body["items"][0]
        assert line["product_name"] == "Aurora Desk Lamp"
        assert line["sku"] == "LAMP-001"
        assert line["unit_price"] == "24.99"
        assert line["quantity"] == 2

    def test_the_money_adds_up(self, client, shop):
        """24.99 x 2 = 49.98; 20% tax = 10.00; shipping 4.99; total 64.97."""
        body = checkout(client, shop.customer, shop.address).json()

        assert body["subtotal_amount"] == "49.98"
        assert body["tax_total"] == "10.00"
        assert body["shipping_total"] == "4.99"
        assert body["discount_total"] == "0.00"
        assert body["total_amount"] == "64.97"

    def test_the_line_totals_add_up_to_the_order_totals(self, client, shop):
        body = checkout(client, shop.customer, shop.address).json()

        lines = body["items"]
        assert sum(Decimal(line["line_subtotal"]) for line in lines) == Decimal(
            body["subtotal_amount"]
        )
        assert sum(Decimal(line["tax_amount"]) for line in lines) == Decimal(body["tax_total"])

    def test_stock_is_reserved_not_deducted(self, client, shop):
        """Until somebody has paid, the units are still the merchant's."""
        checkout(client, shop.customer, shop.address)

        inventory = read_inventory(client, shop.inventory, shop.variant).json()
        assert inventory["quantity_reserved"] == 2
        assert inventory["quantity_on_hand"] == 10
        assert inventory["available"] == 8

    def test_the_cart_becomes_converted(self, client, shop, db_session):
        checkout(client, shop.customer, shop.address)

        converted = db_session.execute(
            text("SELECT count(*) FROM carts WHERE status = 'CONVERTED'")
        ).scalar_one()
        assert converted == 1

    def test_the_order_carries_a_readable_reference(self, client, shop):
        reference = checkout(client, shop.customer, shop.address).json()["reference"]

        assert reference.startswith("ORD-")
        # No characters a customer could misread aloud. The fixed prefix is
        # excluded: "ORD" is read as a word, not spelled out.
        assert not set(reference.removeprefix("ORD-")) & set("01ILOU")

    def test_two_orders_get_different_references(self, client, shop, authorize, db_session):
        checkout(client, shop.customer, shop.address)

        other = authorize(email="second@example.com")
        add_item(client, other, shop.variant, quantity=1)
        second = checkout(client, other, address_id(client, other))

        assert second.status_code == 201
        distinct = db_session.execute(
            text("SELECT count(DISTINCT reference) FROM orders")
        ).scalar_one()
        assert distinct == 2

    def test_the_addresses_are_snapshotted(self, client, shop):
        body = checkout(client, shop.customer, shop.address).json()

        assert body["shipping_address"]["recipient_name"] == "Ada Lovelace"
        assert body["shipping_address"]["country_code"] == "GB"

    def test_the_billing_address_defaults_to_the_shipping_address(self, client, shop):
        body = checkout(client, shop.customer, shop.address).json()

        assert body["billing_address"] == body["shipping_address"]

    def test_a_separate_billing_address_is_used_when_given(self, client, shop):
        billing = address_id(client, shop.customer, recipient_name="Accounts Payable")

        body = checkout(client, shop.customer, shop.address, billing).json()

        assert body["billing_address"]["recipient_name"] == "Accounts Payable"
        assert body["shipping_address"]["recipient_name"] == "Ada Lovelace"

    def test_the_rules_that_decided_the_money_are_recorded(self, client, shop):
        body = checkout(client, shop.customer, shop.address).json()

        assert body["tax_rate_snapshot"]["rate"] == "0.2000"
        assert body["shipping_rule_snapshot"]["cost"] == "4.99"

    def test_the_hold_expiry_is_reported(self, client, shop):
        """So the API can say a hold lapsed, without a worker to sweep it."""
        body = checkout(client, shop.customer, shop.address).json()

        assert body["reservation_expires_at"] is not None

    def test_the_order_is_audited(self, client, shop, db_session):
        checkout(client, shop.customer, shop.address)

        placed = db_session.execute(
            text("SELECT count(*) FROM audit_logs WHERE event_type = 'order.placed'")
        ).scalar_one()
        assert placed == 1


class TestMultipleLines:
    def test_every_line_becomes_an_order_item(self, client, shop):
        _, second = sellable(
            client, shop.catalog, name="Beta Lamp", sku="LAMP-002", price="10.00"
        )
        add_item(client, shop.customer, second, quantity=3)

        body = checkout(client, shop.customer, shop.address).json()

        assert len(body["items"]) == 2
        assert body["subtotal_amount"] == "79.98"

    def test_each_line_is_reserved_separately(self, client, shop):
        _, second = sellable(
            client, shop.catalog, name="Beta Lamp", sku="LAMP-002", price="10.00"
        )
        add_item(client, shop.customer, second, quantity=3)

        checkout(client, shop.customer, shop.address)

        assert read_inventory(client, shop.inventory, shop.variant).json()[
            "quantity_reserved"
        ] == 2
        assert read_inventory(client, shop.inventory, second).json()["quantity_reserved"] == 3


class TestRejections:
    def test_an_empty_cart_is_refused(self, client, shop, db_session):
        clear_cart(client, shop.customer)

        response = checkout(client, shop.customer, shop.address)

        assert response.status_code == 422
        assert error_code(response) == "CART_EMPTY"
        assert orders_in(db_session) == 0

    def test_a_customer_with_no_cart_at_all_is_refused(self, client, authorize, shop):
        stranger = authorize(email="nocart@example.com")

        response = checkout(client, stranger, address_id(client, stranger))

        assert response.status_code == 404
        assert error_code(response) == "CART_NOT_FOUND"

    def test_a_variant_that_stopped_being_sellable_is_refused(
        self, client, shop, db_session
    ):
        patch_variant(client, shop.catalog, shop.variant, status="DISCONTINUED")

        response = checkout(client, shop.customer, shop.address)

        assert response.status_code == 422
        assert error_code(response) == "VARIANT_NOT_AVAILABLE"
        nothing_persisted(db_session, client, shop)

    def test_a_product_that_was_archived_is_refused(self, client, shop, db_session):
        patch_product(client, shop.catalog, shop.product, status="ARCHIVED")

        response = checkout(client, shop.customer, shop.address)

        assert response.status_code == 422
        assert error_code(response) == "VARIANT_NOT_AVAILABLE"
        nothing_persisted(db_session, client, shop)

    def test_an_unpriced_variant_is_refused(self, client, shop, db_session, test_engine):
        """Unpriced is unsellable, whatever the catalogue status says."""
        with test_engine.begin() as connection:
            connection.execute(
                text("UPDATE product_variants SET price = NULL WHERE id = :v"),
                {"v": shop.variant},
            )

        response = checkout(client, shop.customer, shop.address)

        assert response.status_code == 422
        assert error_code(response) == "VARIANT_NOT_AVAILABLE"

    def test_a_cart_mixing_currencies_is_refused(self, client, shop, db_session):
        _, second = sellable(
            client,
            shop.catalog,
            name="Dollar Lamp",
            sku="LAMP-USD",
            price="10.00",
            currency="USD",
        )
        add_item(client, shop.customer, second, quantity=1)

        response = checkout(client, shop.customer, shop.address)

        assert response.status_code == 422
        assert error_code(response) == "MIXED_CURRENCY_CART"
        nothing_persisted(db_session, client, shop)

    def test_insufficient_stock_fails_the_whole_checkout(self, client, shop, db_session):
        _, second = sellable(
            client, shop.catalog, name="Scarce", sku="LAMP-003", price="10.00", quantity=1
        )
        add_item(client, shop.customer, second, quantity=5)

        response = checkout(client, shop.customer, shop.address)

        assert response.status_code == 422
        assert error_code(response) == "INSUFFICIENT_INVENTORY"
        # Not just "the scarce line failed" - the whole order did.
        nothing_persisted(db_session, client, shop)

    def test_the_failure_message_names_the_line(self, client, shop):
        _, second = sellable(
            client, shop.catalog, name="Scarce", sku="LAMP-003", price="10.00", quantity=1
        )
        add_item(client, shop.customer, second, quantity=5)

        message = checkout(client, shop.customer, shop.address).json()["error"]["message"]

        assert "LAMP-003" in message

    def test_an_unknown_address_is_refused(self, client, shop, db_session):
        response = checkout(client, shop.customer, uuid.uuid4())

        assert response.status_code == 404
        assert error_code(response) == "ADDRESS_NOT_FOUND"
        nothing_persisted(db_session, client, shop)

    def test_another_customers_address_cannot_be_used(self, client, shop, authorize):
        stranger = authorize(email="stranger@example.com")
        theirs = address_id(client, stranger)

        response = checkout(client, shop.customer, theirs)

        assert response.status_code == 404
        assert error_code(response) == "ADDRESS_NOT_FOUND"

    def test_an_unknown_billing_address_is_refused(self, client, shop):
        response = checkout(client, shop.customer, shop.address, uuid.uuid4())

        assert response.status_code == 404
        assert error_code(response) == "ADDRESS_NOT_FOUND"

    def test_checkout_requires_authentication(self, client):
        response = client.post(CHECKOUT, json={"shipping_address_id": str(uuid.uuid4())})

        assert response.status_code == 401


class TestUnconfiguredDestinations:
    """Absence is a configuration gap, never a silent default."""

    def test_no_tax_rate_stops_the_sale(self, client, shop_admin, authorize, db_session):
        _, variant = sellable(client, shop_admin)
        create_shipping_rule(client, shop_admin)
        customer = authorize(email="untaxed@example.com")
        add_item(client, customer, variant, quantity=1)

        response = checkout(client, customer, address_id(client, customer))

        assert response.status_code == 422
        assert error_code(response) == "NO_TAX_RATE"
        assert orders_in(db_session) == 0

    def test_no_shipping_rule_stops_the_sale(self, client, shop_admin, authorize, db_session):
        _, variant = sellable(client, shop_admin)
        create_tax_rate(client, shop_admin)
        customer = authorize(email="unshipped@example.com")
        add_item(client, customer, variant, quantity=1)

        response = checkout(client, customer, address_id(client, customer))

        assert response.status_code == 422
        assert error_code(response) == "NO_SHIPPING_RULE"
        assert orders_in(db_session) == 0

    def test_a_deliberate_zero_rate_is_honoured(self, client, shop_admin, authorize):
        """Configured 0% is how "no tax applies here" is said out loud."""
        _, variant = sellable(client, shop_admin)
        create_tax_rate(client, shop_admin, rate="0.0000", country_code="JE")
        create_shipping_rule(client, shop_admin, country_code="JE", cost="0.00")
        customer = authorize(email="jersey@example.com")
        add_item(client, customer, variant, quantity=1)
        address = address_id(client, customer, country_code="JE", region=None)

        body = checkout(client, customer, address).json()

        assert body["tax_total"] == "0.00"
        assert body["total_amount"] == "24.99"

    def test_a_rule_in_the_wrong_currency_is_refused(
        self, client, shop_admin, authorize, db_session
    ):
        _, variant = sellable(client, shop_admin)
        create_tax_rate(client, shop_admin)
        create_shipping_rule(client, shop_admin, currency="USD")
        customer = authorize(email="mismatch@example.com")
        add_item(client, customer, variant, quantity=1)

        response = checkout(client, customer, address_id(client, customer))

        assert response.status_code == 422
        assert error_code(response) == "SHIPPING_RULE_CURRENCY_MISMATCH"
        assert orders_in(db_session) == 0


class TestRounding:
    def test_tax_is_rounded_half_up_not_to_even(self, client, shop_admin, authorize):
        """0.25 at 2% is exactly 0.005. Half-up gives 0.01; bankers' gives 0.00."""
        _, variant = sellable(client, shop_admin, price="0.25")
        create_tax_rate(client, shop_admin, rate="0.0200")
        create_shipping_rule(client, shop_admin, cost="0.00")
        customer = authorize(email="rounding@example.com")
        add_item(client, customer, variant, quantity=1)

        body = checkout(client, customer, address_id(client, customer)).json()

        assert body["items"][0]["tax_amount"] == "0.01"
        assert body["tax_total"] == "0.01"
        assert body["total_amount"] == "0.26"

    def test_each_line_is_rounded_before_the_lines_are_summed(
        self, client, shop_admin, authorize
    ):
        """So a customer adding up the printed lines gets the printed total."""
        _, first = sellable(client, shop_admin, name="A", sku="A-1", price="0.25")
        _, second = sellable(client, shop_admin, name="B", sku="B-1", price="0.25")
        create_tax_rate(client, shop_admin, rate="0.0200")
        create_shipping_rule(client, shop_admin, cost="0.00")
        customer = authorize(email="rounding2@example.com")
        add_item(client, customer, first, quantity=1)
        add_item(client, customer, second, quantity=1)

        body = checkout(client, customer, address_id(client, customer)).json()

        # Rounded per line: 0.01 + 0.01 = 0.02. Summed then rounded would
        # have given 0.01, and the printed lines would not add up.
        assert body["tax_total"] == "0.02"
        assert sum(Decimal(line["tax_amount"]) for line in body["items"]) == Decimal("0.02")

    def test_no_phase_5_module_uses_a_float(self):
        """Money is Decimal throughout. A float would round unpredictably."""
        import pathlib

        modules = pathlib.Path("app/modules")
        offenders = []
        for package in ("orders", "tax", "shipping", "addresses"):
            for path in (modules / package).glob("*.py"):
                if "float(" in path.read_text():
                    offenders.append(str(path))

        assert not offenders, f"float() used on money paths: {offenders}"


class TestIdempotency:
    def test_a_second_checkout_returns_the_same_order(self, client, shop, db_session):
        """The retry of a submit whose response was lost must not buy twice."""
        first = checkout(client, shop.customer, shop.address).json()

        second = checkout(client, shop.customer, shop.address)

        assert second.status_code == 200
        assert second.json()["id"] == first["id"]
        assert orders_in(db_session) == 1

    def test_the_retry_reserves_no_further_stock(self, client, shop):
        checkout(client, shop.customer, shop.address)
        checkout(client, shop.customer, shop.address)

        assert read_inventory(client, shop.inventory, shop.variant).json()[
            "quantity_reserved"
        ] == 2

    def test_shopping_again_after_checkout_starts_a_new_basket(self, client, shop):
        """Once the customer resumes shopping, a stale retry is not a retry."""
        checkout(client, shop.customer, shop.address)
        add_item(client, shop.customer, shop.variant, quantity=1)

        response = checkout(client, shop.customer, shop.address)

        assert response.status_code == 201

    def test_the_database_refuses_two_orders_for_one_cart(self, client, shop, test_engine):
        """The guarantee under the service's recovery, asserted directly."""
        from sqlalchemy.exc import IntegrityError

        checkout(client, shop.customer, shop.address)
        with test_engine.begin() as connection:
            existing = connection.execute(
                text("SELECT cart_id, user_id, currency FROM orders")
            ).one()
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO orders (reference, user_id, cart_id, currency, "
                        "subtotal_amount, total_amount, shipping_address, billing_address) "
                        "VALUES ('ORD-XXXX-XXXX-XXXX', :u, :c, :cur, 0, 0, '{}', '{}')"
                    ),
                    {"u": existing.user_id, "c": existing.cart_id, "cur": existing.currency},
                )
