"""Checkout guarantees PostgreSQL enforces, tested against PostgreSQL.

Raw SQL rather than the API, for the same reason as the Phase 3 and Phase 4
schema tests: the point is that the rule holds even if a service-layer
check is later removed or bypassed. Money in particular is only as safe as
the constraints underneath it - a service that computed a total wrongly
must be refused by the database, not merely reviewed.

Two of these matter more than the rest. ``uq_orders_cart_id`` is the
idempotency guarantee, and ``ck_orders_totals_reconcile`` is what makes it
impossible to persist an order whose numbers do not add up.
"""

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.test_phase3_schema import insert_product, insert_variant
from tests.test_phase4_schema import insert_cart, insert_user

PHASE_5_TABLES = {"addresses", "tax_rates", "shipping_rules", "orders", "order_items"}

# Not Phase 5's, and their absence is asserted rather than assumed: shipments
# belong to Phase 7 and would be easy to add "while we are here".
NOT_YET = {"shipments", "shipment_events", "coupons", "payments"}


def insert_order(connection, *, user_id, cart_id, **overrides) -> uuid.UUID:
    values = {
        "reference": f"ORD-{uuid.uuid4().hex[:12].upper()}",
        "user_id": user_id,
        "cart_id": cart_id,
        "status": "PENDING_PAYMENT",
        "currency": "GBP",
        "subtotal_amount": "100.00",
        "discount_total": "0.00",
        "tax_total": "20.00",
        "shipping_total": "5.00",
        "total_amount": "125.00",
    }
    values.update(overrides)
    return connection.execute(
        text(
            "INSERT INTO orders (reference, user_id, cart_id, status, currency, "
            "subtotal_amount, discount_total, tax_total, shipping_total, total_amount, "
            "shipping_address, billing_address) VALUES "
            "(:reference, :user_id, :cart_id, :status, :currency, :subtotal_amount, "
            ":discount_total, :tax_total, :shipping_total, :total_amount, '{}', '{}') "
            "RETURNING id"
        ),
        values,
    ).scalar_one()


def insert_order_item(connection, *, order_id, variant_id, **overrides) -> uuid.UUID:
    values = {
        "order_id": order_id,
        "variant_id": variant_id,
        "product_id": str(uuid.uuid4()),
        "product_name": "Snapshot",
        "sku": f"SKU-{uuid.uuid4().hex[:8]}",
        "quantity": 2,
        "unit_price": "10.00",
        "currency": "GBP",
        "discount_amount": "0.00",
        "tax_rate": "0.2000",
        "tax_amount": "4.00",
        "line_subtotal": "20.00",
        "line_total": "24.00",
    }
    values.update(overrides)
    return connection.execute(
        text(
            "INSERT INTO order_items (order_id, variant_id, product_id, product_name, "
            "sku, quantity, unit_price, currency, discount_amount, tax_rate, tax_amount, "
            "line_subtotal, line_total) VALUES (:order_id, :variant_id, :product_id, "
            ":product_name, :sku, :quantity, :unit_price, :currency, :discount_amount, "
            ":tax_rate, :tax_amount, :line_subtotal, :line_total) RETURNING id"
        ),
        values,
    ).scalar_one()


def buyer_with_cart(connection) -> tuple[uuid.UUID, uuid.UUID]:
    user = insert_user(connection)
    return user, insert_cart(connection, user)


def constraint_names(connection, table: str) -> set[str]:
    return set(
        connection.execute(
            text(
                "SELECT con.conname FROM pg_constraint con "
                "JOIN pg_class rel ON rel.oid = con.conrelid "
                "JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace "
                "WHERE nsp.nspname = 'public' AND rel.relname = :t"
            ),
            {"t": table},
        ).scalars()
    )


class TestTablesExist:
    def test_all_five_phase_5_tables_are_present(self, test_engine):
        assert PHASE_5_TABLES <= set(inspect(test_engine).get_table_names())

    def test_no_later_phase_table_was_created_early(self, test_engine):
        present = set(inspect(test_engine).get_table_names())

        assert not (NOT_YET & present), "a later phase's table was created in Phase 5"


class TestOrderIdentity:
    def test_a_cart_can_produce_only_one_order(self, test_engine):
        """The idempotency guarantee, enforced where it cannot be bypassed."""
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            insert_order(connection, user_id=user, cart_id=cart)
            with pytest.raises(IntegrityError) as raised:
                insert_order(connection, user_id=user, cart_id=cart)

        assert "uq_orders_cart_id" in str(raised.value)

    def test_a_reference_cannot_be_reused(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            insert_order(connection, user_id=user, cart_id=cart, reference="ORD-AAAA")
            _, other_cart = buyer_with_cart(connection)
            with pytest.raises(IntegrityError) as raised:
                insert_order(
                    connection, user_id=user, cart_id=other_cart, reference="ORD-AAAA"
                )

        assert "uq_orders_reference" in str(raised.value)

    def test_an_unknown_status_is_refused(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            with pytest.raises(IntegrityError):
                insert_order(connection, user_id=user, cart_id=cart, status="SHIPPED")


class TestOrderArithmetic:
    def test_a_total_that_does_not_reconcile_is_refused(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            with pytest.raises(IntegrityError) as raised:
                insert_order(
                    connection, user_id=user, cart_id=cart, total_amount="999.00"
                )

        assert "ck_orders_totals_reconcile" in str(raised.value)

    def test_a_discount_is_subtracted_by_the_constraint_too(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(
                connection,
                user_id=user,
                cart_id=cart,
                discount_total="10.00",
                total_amount="115.00",
            )

        assert order is not None

    @pytest.mark.parametrize(
        "column",
        [
            "subtotal_amount",
            "discount_total",
            "tax_total",
            "shipping_total",
        ],
    )
    def test_no_money_column_may_go_negative(self, test_engine, column):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            with pytest.raises(IntegrityError):
                insert_order(
                    connection,
                    user_id=user,
                    cart_id=cart,
                    **{column: "-1.00"},
                    total_amount="0.00",
                )


class TestOrderLineArithmetic:
    def test_a_line_subtotal_must_be_price_times_quantity(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(connection, user_id=user, cart_id=cart)
            variant = insert_variant(connection, insert_product(connection))
            with pytest.raises(IntegrityError) as raised:
                insert_order_item(
                    connection,
                    order_id=order,
                    variant_id=variant,
                    line_subtotal="99.00",
                    line_total="103.00",
                )

        assert "ck_order_items_line_subtotal_matches" in str(raised.value)

    def test_a_line_total_must_include_its_tax(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(connection, user_id=user, cart_id=cart)
            variant = insert_variant(connection, insert_product(connection))
            with pytest.raises(IntegrityError) as raised:
                insert_order_item(
                    connection, order_id=order, variant_id=variant, line_total="20.00"
                )

        assert "ck_order_items_line_total_matches" in str(raised.value)

    def test_a_zero_quantity_line_is_refused(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(connection, user_id=user, cart_id=cart)
            variant = insert_variant(connection, insert_product(connection))
            with pytest.raises(IntegrityError):
                insert_order_item(
                    connection,
                    order_id=order,
                    variant_id=variant,
                    quantity=0,
                    line_subtotal="0.00",
                    line_total="4.00",
                )

    def test_a_free_line_is_refused(self, test_engine):
        """An order line at zero price is a bug, not a gift."""
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(connection, user_id=user, cart_id=cart)
            variant = insert_variant(connection, insert_product(connection))
            with pytest.raises(IntegrityError):
                insert_order_item(
                    connection,
                    order_id=order,
                    variant_id=variant,
                    unit_price="0.00",
                    line_subtotal="0.00",
                    line_total="4.00",
                )

    def test_one_line_per_variant(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(connection, user_id=user, cart_id=cart)
            variant = insert_variant(connection, insert_product(connection))
            insert_order_item(connection, order_id=order, variant_id=variant)
            with pytest.raises(IntegrityError) as raised:
                insert_order_item(connection, order_id=order, variant_id=variant)

        assert "uq_order_items_order_id_variant_id" in str(raised.value)


class TestHistoricalStability:
    def test_an_ordered_variant_cannot_be_deleted(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(connection, user_id=user, cart_id=cart)
            variant = insert_variant(connection, insert_product(connection))
            insert_order_item(connection, order_id=order, variant_id=variant)
            with pytest.raises(IntegrityError):
                connection.execute(
                    text("DELETE FROM product_variants WHERE id = :v"), {"v": variant}
                )

    def test_deleting_a_product_cannot_cascade_an_ordered_variant_away(self, test_engine):
        """products -> variants is CASCADE; variants -> order_items is not."""
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(connection, user_id=user, cart_id=cart)
            product = insert_product(connection)
            variant = insert_variant(connection, product)
            insert_order_item(connection, order_id=order, variant_id=variant)
            with pytest.raises(IntegrityError):
                connection.execute(
                    text("DELETE FROM products WHERE id = :p"), {"p": product}
                )

    def test_a_customer_who_has_ordered_cannot_be_deleted(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            insert_order(connection, user_id=user, cart_id=cart)
            with pytest.raises(IntegrityError):
                connection.execute(text("DELETE FROM users WHERE id = :u"), {"u": user})

    def test_the_cart_holding_an_orders_reservations_cannot_be_deleted(self, test_engine):
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            insert_order(connection, user_id=user, cart_id=cart)
            with pytest.raises(IntegrityError):
                connection.execute(text("DELETE FROM carts WHERE id = :c"), {"c": cart})

    def test_deleting_an_order_takes_its_lines_with_it(self, test_engine):
        """Lines belong to their order; nothing else points at them."""
        with test_engine.begin() as connection:
            user, cart = buyer_with_cart(connection)
            order = insert_order(connection, user_id=user, cart_id=cart)
            variant = insert_variant(connection, insert_product(connection))
            insert_order_item(connection, order_id=order, variant_id=variant)

            connection.execute(text("DELETE FROM orders WHERE id = :o"), {"o": order})

            remaining = connection.execute(
                text("SELECT count(*) FROM order_items WHERE order_id = :o"), {"o": order}
            ).scalar_one()
        assert remaining == 0


class TestTaxAndShippingConstraints:
    def test_a_rate_above_one_is_refused(self, test_engine):
        with test_engine.begin() as connection:
            with pytest.raises(IntegrityError) as raised:
                connection.execute(
                    text(
                        "INSERT INTO tax_rates (name, rate, country_code) "
                        "VALUES ('Absurd', 1.5, 'GB')"
                    )
                )

        assert "ck_tax_rates_rate_range" in str(raised.value)

    def test_two_active_rates_for_one_jurisdiction_are_refused(self, test_engine):
        with test_engine.begin() as connection:
            insert = text(
                "INSERT INTO tax_rates (name, rate, country_code, is_active) "
                "VALUES (:n, 0.2, 'GB', true)"
            )
            connection.execute(insert, {"n": "first"})
            with pytest.raises(IntegrityError) as raised:
                connection.execute(insert, {"n": "second"})

        assert "uq_tax_rates_active_jurisdiction" in str(raised.value)

    def test_a_country_rate_and_a_region_rate_coexist(self, test_engine):
        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tax_rates (name, rate, country_code) "
                    "VALUES ('country', 0.2, 'GB')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO tax_rates (name, rate, country_code, region) "
                    "VALUES ('region', 0.1, 'GB', 'Scotland')"
                )
            )

    def test_a_retired_rate_does_not_block_its_replacement(self, test_engine):
        """The index is partial on is_active, which is what makes this work."""
        with test_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tax_rates (name, rate, country_code, is_active) "
                    "VALUES ('old', 0.2, 'GB', false)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO tax_rates (name, rate, country_code, is_active) "
                    "VALUES ('new', 0.175, 'GB', true)"
                )
            )

    def test_an_inverted_shipping_band_is_refused(self, test_engine):
        with test_engine.begin() as connection:
            with pytest.raises(IntegrityError) as raised:
                connection.execute(
                    text(
                        "INSERT INTO shipping_rules (name, country_code, "
                        "min_order_subtotal, max_order_subtotal, cost, currency) "
                        "VALUES ('backwards', 'GB', 50, 10, 4.99, 'GBP')"
                    )
                )

        assert "ck_shipping_rules_band_valid" in str(raised.value)

    def test_a_negative_shipping_cost_is_refused(self, test_engine):
        with test_engine.begin() as connection:
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO shipping_rules (name, country_code, cost, currency) "
                        "VALUES ('paying you', 'GB', -1, 'GBP')"
                    )
                )


class TestTheDeferredPhase4Constraint:
    """Ridden along in 0008 rather than given a migration of its own."""

    def test_one_adjustment_per_ledger_entry(self, test_engine):
        assert (
            "uq_inventory_adjustments_transaction_id"
            in constraint_names_for(test_engine, "inventory_adjustments")
        )

    def test_the_redundant_index_was_dropped(self, test_engine):
        indexes = {
            index["name"] for index in inspect(test_engine).get_indexes("inventory_adjustments")
        }

        assert "ix_inventory_adjustments_transaction_id" not in indexes


def constraint_names_for(engine, table: str) -> set[str]:
    with engine.connect() as connection:
        return constraint_names(connection, table)


class TestNamedConstraintsArePresent:
    @pytest.mark.parametrize(
        "table,expected",
        [
            (
                "orders",
                {
                    "uq_orders_reference",
                    "uq_orders_cart_id",
                    "ck_orders_status_valid",
                    "ck_orders_totals_reconcile",
                    "fk_orders_user_id_users",
                    "fk_orders_cart_id_carts",
                },
            ),
            (
                "order_items",
                {
                    "ck_order_items_quantity_positive",
                    "ck_order_items_unit_price_positive",
                    "ck_order_items_line_subtotal_matches",
                    "ck_order_items_line_total_matches",
                    "uq_order_items_order_id_variant_id",
                    "fk_order_items_order_id_orders",
                    "fk_order_items_variant_id_product_variants",
                },
            ),
            (
                "tax_rates",
                {"ck_tax_rates_rate_range"},
            ),
            (
                "shipping_rules",
                {"ck_shipping_rules_band_valid", "ck_shipping_rules_cost_non_negative"},
            ),
        ],
    )
    def test_the_constraint_is_named_as_the_service_expects(
        self, test_engine, table, expected
    ):
        """Names are load-bearing: the services map them to error codes."""
        assert expected <= constraint_names_for(test_engine, table)

    def test_the_partial_indexes_exist(self, test_engine):
        inspector = inspect(test_engine)

        assert "uq_addresses_one_default_per_user" in {
            index["name"] for index in inspector.get_indexes("addresses")
        }
        assert "uq_tax_rates_active_jurisdiction" in {
            index["name"] for index in inspector.get_indexes("tax_rates")
        }
