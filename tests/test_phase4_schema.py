"""Cart and inventory guarantees PostgreSQL enforces, tested against PostgreSQL.

Raw SQL rather than the API, for the same reason as the Phase 3 schema
tests: the point is that the constraint holds even if a service-layer check
is later removed. The reservation counters in particular are only as safe as
the constraints underneath them.
"""

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.test_phase3_schema import insert_product, insert_variant

PHASE_4_TABLES = {
    "carts",
    "cart_items",
    "inventory",
    "inventory_reservations",
    "inventory_transactions",
    "inventory_adjustments",
}


def insert_user(connection, email=None) -> uuid.UUID:
    return connection.execute(
        text("INSERT INTO users (email, password_hash) VALUES (:e, 'x') RETURNING id"),
        {"e": email or f"schema-{uuid.uuid4()}@example.com"},
    ).scalar_one()


def insert_cart(connection, user_id, status="ACTIVE") -> uuid.UUID:
    return connection.execute(
        text("INSERT INTO carts (user_id, status) VALUES (:u, :s) RETURNING id"),
        {"u": user_id, "s": status},
    ).scalar_one()


def insert_inventory(connection, variant_id, **quantities) -> uuid.UUID:
    return connection.execute(
        text(
            "INSERT INTO inventory (variant_id, quantity_on_hand, quantity_reserved, "
            "quantity_damaged, quantity_quarantined) "
            "VALUES (:v, :on_hand, :reserved, :damaged, :quarantined) RETURNING id"
        ),
        {
            "v": variant_id,
            "on_hand": quantities.get("on_hand", 0),
            "reserved": quantities.get("reserved", 0),
            "damaged": quantities.get("damaged", 0),
            "quarantined": quantities.get("quarantined", 0),
        },
    ).scalar_one()


def stocked_variant(connection, **quantities) -> tuple[uuid.UUID, uuid.UUID]:
    """A variant and its inventory row - the pair most of these tests need."""
    variant = insert_variant(connection, insert_product(connection))
    return variant, insert_inventory(connection, variant, **quantities)


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
    def test_all_six_phase_4_tables_are_present(self, test_engine):
        assert PHASE_4_TABLES <= set(inspect(test_engine).get_table_names())


class TestCarts:
    def test_one_active_cart_per_user_is_enforced_by_a_partial_unique_index(self, test_engine):
        with test_engine.begin() as connection:
            user = insert_user(connection)
            insert_cart(connection, user)

        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                insert_cart(connection, user)

    def test_a_user_may_hold_many_non_active_carts(self, test_engine):
        """The index is partial, so cart history never blocks a new cart."""
        with test_engine.begin() as connection:
            user = insert_user(connection)
            insert_cart(connection, user, status="CONVERTED")
            insert_cart(connection, user, status="CONVERTED")
            insert_cart(connection, user, status="ABANDONED")
            insert_cart(connection, user, status="ACTIVE")

    def test_an_unknown_status_is_rejected(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                insert_cart(connection, insert_user(connection), status="PENDING")

    def test_a_cart_requires_a_real_user(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                insert_cart(connection, uuid.uuid4())


class TestCartItems:
    def test_a_variant_appears_at_most_once_in_a_cart(self, test_engine):
        with test_engine.begin() as connection:
            cart = insert_cart(connection, insert_user(connection))
            variant = insert_variant(connection, insert_product(connection))
            connection.execute(
                text("INSERT INTO cart_items (cart_id, variant_id, quantity) VALUES (:c, :v, 1)"),
                {"c": cart, "v": variant},
            )

        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO cart_items (cart_id, variant_id, quantity) VALUES (:c, :v, 2)"
                    ),
                    {"c": cart, "v": variant},
                )

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_a_non_positive_quantity_is_rejected(self, test_engine, quantity):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                cart = insert_cart(connection, insert_user(connection))
                variant = insert_variant(connection, insert_product(connection))
                connection.execute(
                    text(
                        "INSERT INTO cart_items (cart_id, variant_id, quantity) VALUES (:c, :v, :q)"
                    ),
                    {"c": cart, "v": variant, "q": quantity},
                )

    def test_deleting_a_cart_deletes_its_items(self, test_engine):
        with test_engine.begin() as connection:
            cart = insert_cart(connection, insert_user(connection))
            variant = insert_variant(connection, insert_product(connection))
            connection.execute(
                text("INSERT INTO cart_items (cart_id, variant_id, quantity) VALUES (:c, :v, 1)"),
                {"c": cart, "v": variant},
            )
            connection.execute(text("DELETE FROM carts WHERE id = :c"), {"c": cart})

            assert (
                connection.execute(
                    text("SELECT count(*) FROM cart_items WHERE cart_id = :c"), {"c": cart}
                ).scalar_one()
                == 0
            )


class TestInventory:
    def test_a_variant_has_at_most_one_inventory_row(self, test_engine):
        with test_engine.begin() as connection:
            variant, _ = stocked_variant(connection)

        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                insert_inventory(connection, variant)

    @pytest.mark.parametrize(
        "column",
        ["quantity_on_hand", "quantity_reserved", "quantity_damaged", "quantity_quarantined"],
    )
    def test_no_quantity_may_go_negative(self, test_engine, column):
        with test_engine.begin() as connection:
            _, inventory = stocked_variant(connection, on_hand=10)

        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                connection.execute(
                    text(f"UPDATE inventory SET {column} = -1 WHERE id = :i"), {"i": inventory}
                )

    def test_reserved_may_not_exceed_on_hand(self, test_engine):
        with test_engine.begin() as connection:
            _, inventory = stocked_variant(connection, on_hand=10, reserved=10)

        with pytest.raises(IntegrityError) as raised:
            with test_engine.begin() as connection:
                connection.execute(
                    text("UPDATE inventory SET quantity_reserved = 11 WHERE id = :i"),
                    {"i": inventory},
                )

        assert "ck_inventory_quantities_balance" in str(raised.value)

    def test_damaged_and_quarantined_sit_alongside_on_hand_not_inside_it(self, test_engine):
        """The buckets are disjoint, so holding damaged stock never caps on-hand."""
        with test_engine.begin() as connection:
            _, inventory = stocked_variant(
                connection, on_hand=5, reserved=5, damaged=40, quarantined=40
            )

            assert inventory is not None

    def test_the_balance_constraint_is_named_for_the_service_to_map(self, test_engine):
        with test_engine.connect() as connection:
            assert "ck_inventory_quantities_balance" in constraint_names(connection, "inventory")

    def test_inventory_requires_a_real_variant(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                insert_inventory(connection, uuid.uuid4())

    def test_a_variant_holding_inventory_cannot_be_deleted(self, test_engine):
        """RESTRICT, not CASCADE: stock records are never collateral damage."""
        with test_engine.begin() as connection:
            variant, _ = stocked_variant(connection, on_hand=3)

        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM product_variants WHERE id = :v"), {"v": variant}
                )


class TestReservations:
    def reserve(self, connection, inventory, cart, *, quantity=1, status="ACTIVE"):
        return connection.execute(
            text(
                "INSERT INTO inventory_reservations "
                "(inventory_id, cart_id, quantity, status, expires_at) "
                "VALUES (:i, :c, :q, :s, now() + interval '1 hour') RETURNING id"
            ),
            {"i": inventory, "c": cart, "q": quantity, "s": status},
        ).scalar_one()

    def test_a_reservation_records_inventory_cart_quantity_and_expiry(self, test_engine):
        with test_engine.begin() as connection:
            _, inventory = stocked_variant(connection, on_hand=5)
            cart = insert_cart(connection, insert_user(connection))
            reservation = self.reserve(connection, inventory, cart, quantity=2)

            assert (
                connection.execute(
                    text("SELECT quantity FROM inventory_reservations WHERE id = :r"),
                    {"r": reservation},
                ).scalar_one()
                == 2
            )

    @pytest.mark.parametrize("status", ["ACTIVE", "CONVERTED", "EXPIRED", "RELEASED"])
    def test_every_specified_status_is_accepted(self, test_engine, status):
        with test_engine.begin() as connection:
            _, inventory = stocked_variant(connection, on_hand=5)
            cart = insert_cart(connection, insert_user(connection))
            self.reserve(connection, inventory, cart, status=status)

    def test_an_unknown_status_is_rejected(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                _, inventory = stocked_variant(connection, on_hand=5)
                cart = insert_cart(connection, insert_user(connection))
                self.reserve(connection, inventory, cart, status="PENDING")

    def test_a_non_positive_quantity_is_rejected(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                _, inventory = stocked_variant(connection, on_hand=5)
                cart = insert_cart(connection, insert_user(connection))
                self.reserve(connection, inventory, cart, quantity=0)

    def test_the_sweep_index_exists(self, test_engine):
        """(inventory_id, status) is what makes expiring past-due rows cheap."""
        with test_engine.connect() as connection:
            definitions = (
                connection.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
                        "AND tablename = 'inventory_reservations'"
                    )
                )
                .scalars()
                .all()
            )

        assert any(
            "inventory_id" in definition and "status" in definition for definition in definitions
        )


class TestTransactions:
    def write(self, connection, inventory, **columns):
        return connection.execute(
            text(
                "INSERT INTO inventory_transactions "
                "(inventory_id, transaction_type, quantity, from_bucket, to_bucket) "
                "VALUES (:i, :t, :q, :f, :to) RETURNING id"
            ),
            {
                "i": inventory,
                "t": columns.get("transaction_type", "RESTOCK"),
                "q": columns.get("quantity", 1),
                "f": columns.get("from_bucket"),
                "to": columns.get("to_bucket", "ON_HAND"),
            },
        ).scalar_one()

    @pytest.mark.parametrize(
        "transaction_type",
        ["PURCHASE", "RESTOCK", "RETURN", "ADJUSTMENT", "DISPOSAL", "TRANSFER"],
    )
    def test_every_specified_type_is_accepted(self, test_engine, transaction_type):
        with test_engine.begin() as connection:
            _, inventory = stocked_variant(connection)
            self.write(connection, inventory, transaction_type=transaction_type)

    @pytest.mark.parametrize("transaction_type", ["SALE", "RESERVATION", "RELEASE"])
    def test_reservation_lifecycle_types_do_not_exist(self, test_engine, transaction_type):
        """Reservations are their own history; they never enter the ledger."""
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                _, inventory = stocked_variant(connection)
                self.write(connection, inventory, transaction_type=transaction_type)

    def test_quantity_is_always_a_positive_magnitude(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                _, inventory = stocked_variant(connection)
                self.write(connection, inventory, quantity=-5)

    def test_a_row_with_no_direction_at_all_is_rejected(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                _, inventory = stocked_variant(connection)
                self.write(connection, inventory, from_bucket=None, to_bucket=None)

    def test_an_unknown_bucket_is_rejected(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                _, inventory = stocked_variant(connection)
                self.write(connection, inventory, to_bucket="SHELF")

    def test_inbound_outbound_and_transfer_are_all_representable(self, test_engine):
        with test_engine.begin() as connection:
            _, inventory = stocked_variant(connection)
            self.write(connection, inventory, from_bucket=None, to_bucket="ON_HAND")
            self.write(connection, inventory, from_bucket="ON_HAND", to_bucket=None)
            self.write(connection, inventory, from_bucket="ON_HAND", to_bucket="DAMAGED")


class TestAdjustments:
    def test_an_adjustment_names_its_transaction_its_actor_and_a_reason(self, test_engine):
        with test_engine.begin() as connection:
            _, inventory = stocked_variant(connection)
            user = insert_user(connection)
            transaction = connection.execute(
                text(
                    "INSERT INTO inventory_transactions "
                    "(inventory_id, transaction_type, quantity, to_bucket) "
                    "VALUES (:i, 'RESTOCK', 5, 'ON_HAND') RETURNING id"
                ),
                {"i": inventory},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO inventory_adjustments "
                    "(inventory_id, transaction_id, actor_user_id, reason) "
                    "VALUES (:i, :t, :u, 'Stock count correction')"
                ),
                {"i": inventory, "t": transaction, "u": user},
            )

    def test_an_adjustment_cannot_be_anonymous(self, test_engine):
        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                _, inventory = stocked_variant(connection)
                transaction = connection.execute(
                    text(
                        "INSERT INTO inventory_transactions "
                        "(inventory_id, transaction_type, quantity, to_bucket) "
                        "VALUES (:i, 'RESTOCK', 5, 'ON_HAND') RETURNING id"
                    ),
                    {"i": inventory},
                ).scalar_one()
                connection.execute(
                    text(
                        "INSERT INTO inventory_adjustments "
                        "(inventory_id, transaction_id, actor_user_id, reason) "
                        "VALUES (:i, :t, NULL, 'No actor')"
                    ),
                    {"i": inventory, "t": transaction},
                )
