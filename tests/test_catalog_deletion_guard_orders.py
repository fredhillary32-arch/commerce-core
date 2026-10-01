"""The product deletion guard, extended a second time for order history.

Phase 4 added stocked variants to the dependents ``delete_product`` counts.
Phase 5 adds ordered ones. The count rarely changes the *decision* - any
variant at all already blocks deletion - but it changes the answer the
operator gets, which is the difference between "this has 1 ordered variant"
and an opaque foreign-key error.

The guarantee underneath is ``order_items.variant_id``, which is RESTRICT
rather than CASCADE. The last two tests bypass the service entirely and
prove the database refuses on its own, which is what makes the guard a
courtesy rather than the only thing standing between an operator and
unreadable order history.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.catalog_helpers import PRODUCTS, create_variant, product_id
from tests.test_phase3_schema import insert_product, insert_variant
from tests.test_phase4_schema import insert_cart, insert_user
from tests.test_phase5_schema import insert_order, insert_order_item


def ordered_product(connection) -> tuple[uuid.UUID, uuid.UUID]:
    """A DRAFT product whose variant appears on an order."""
    user = insert_user(connection)
    cart = insert_cart(connection, user)
    order = insert_order(connection, user_id=user, cart_id=cart)
    product = insert_product(connection)
    variant = insert_variant(connection, product)
    insert_order_item(connection, order_id=order, variant_id=variant)
    return product, variant


class TestTheGuardCountsOrderedVariants:
    def test_a_product_whose_variant_was_ordered_cannot_be_deleted(
        self, client, catalog_admin, test_engine
    ):
        with test_engine.begin() as connection:
            product, _ = ordered_product(connection)

        response = client.delete(f"{PRODUCTS}/{product}", headers=catalog_admin)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_HAS_DEPENDENTS"

    def test_the_message_names_the_ordered_variants(
        self, client, catalog_admin, test_engine
    ):
        with test_engine.begin() as connection:
            product, _ = ordered_product(connection)

        message = client.delete(
            f"{PRODUCTS}/{product}", headers=catalog_admin
        ).json()["error"]["message"]

        assert "ordered variant" in message

    def test_a_product_that_was_never_ordered_still_deletes(self, client, catalog_admin):
        """The new count must not block a product with nothing behind it."""
        product = product_id(client, catalog_admin, name="Never Sold")

        assert client.delete(f"{PRODUCTS}/{product}", headers=catalog_admin).status_code == 204

    def test_the_variant_guard_still_fires_first(self, client, catalog_admin):
        """An unsold variant already blocks deletion; the order count adds to it."""
        product = product_id(client, catalog_admin, name="Has A Variant")
        create_variant(client, catalog_admin, product, sku=f"SKU-{uuid.uuid4().hex[:8]}")

        message = client.delete(
            f"{PRODUCTS}/{product}", headers=catalog_admin
        ).json()["error"]["message"]

        assert "1 variants" in message


class TestTheDatabaseRefusesWithoutTheGuard:
    """Raw SQL, so the service layer is not involved at all."""

    def test_an_ordered_variant_cannot_be_deleted_directly(self, test_engine):
        with test_engine.begin() as connection:
            _, variant = ordered_product(connection)
            with pytest.raises(IntegrityError) as raised:
                connection.execute(
                    text("DELETE FROM product_variants WHERE id = :v"), {"v": variant}
                )

        assert "fk_order_items_variant_id_product_variants" in str(raised.value)

    def test_the_product_cascade_is_stopped_at_the_ordered_variant(self, test_engine):
        """products -> variants is CASCADE, so this is the rule that matters."""
        with test_engine.begin() as connection:
            product, _ = ordered_product(connection)
            with pytest.raises(IntegrityError):
                connection.execute(
                    text("DELETE FROM products WHERE id = :p"), {"p": product}
                )
