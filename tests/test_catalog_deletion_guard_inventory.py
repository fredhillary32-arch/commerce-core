"""The Phase 4 extension to the Phase 3 product deletion guard.

Phase 3 deliberately deferred inventory dependents to this phase. Two things
enforce the rule now, at different levels:

* the service guard refuses the deletion and says inventory is why, and
* ``inventory.variant_id`` is RESTRICT rather than CASCADE, so PostgreSQL
  refuses to drop a variant that has stock records even if the guard above
  were removed.

The product-level guard already refuses any product that has variants at
all, so the inventory count added here mostly changes *what the operator is
told*. That is not cosmetic: "this product still has 1 stocked variant" is
an answer they can act on, and the count is what the database-level RESTRICT
would otherwise report as an opaque foreign-key error.
"""

import uuid

import pytest
from sqlalchemy import text

from app.modules.inventory import repository as inventory_repository
from tests.catalog_helpers import PRODUCTS, activate_product_with_variant, create_variant, product_id
from tests.inventory_helpers import adjust, provision


@pytest.fixture()
def inventory_admin(authorize) -> dict[str, str]:
    return authorize("inventory.read", "inventory.adjust", email="guard-admin@example.com")


def delete_product(client, headers, product):
    return client.delete(f"{PRODUCTS}/{product}", headers=headers)


def product_uuid(client, headers, name, sku) -> tuple[str, str]:
    return activate_product_with_variant(client, headers, name=name, sku=sku)


def draft_product_with_variant(client, headers, name, sku) -> tuple[str, str]:
    """A product still DRAFT, so deletion reaches the dependents check.

    Phase 3 refuses to delete anything that is not DRAFT before it ever
    looks at dependents, so a published product would never exercise the
    inventory count this phase added.
    """
    product = product_id(client, headers, name=name)
    variant = create_variant(client, headers, product, sku=sku).json()["id"]
    return product, variant


class TestTheServiceGuard:
    def test_a_product_whose_variant_holds_stock_is_not_deletable(
        self, client, catalog_admin, inventory_admin
    ):
        product, variant = draft_product_with_variant(client, catalog_admin, "Stocked Lamp", "STOCK-1")
        provision(client, inventory_admin, variant)
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=5,
            to_bucket="ON_HAND",
        )

        response = delete_product(client, catalog_admin, product)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_HAS_DEPENDENTS"

    def test_the_message_names_inventory_as_a_reason(
        self, client, catalog_admin, inventory_admin
    ):
        """An operator has to be able to tell stock from a stray image."""
        product, variant = draft_product_with_variant(client, catalog_admin, "Named Reason", "STOCK-2")
        provision(client, inventory_admin, variant)
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=1,
            to_bucket="ON_HAND",
        )

        message = delete_product(client, catalog_admin, product).json()["error"]["message"]

        assert "stocked variant" in message

    def test_the_product_really_is_still_there(
        self, client, catalog_admin, inventory_admin, test_engine
    ):
        product, variant = draft_product_with_variant(client, catalog_admin, "Survivor", "STOCK-3")
        provision(client, inventory_admin, variant)
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=2,
            to_bucket="ON_HAND",
        )

        delete_product(client, catalog_admin, product)

        with test_engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT count(*) FROM products WHERE id = :p"), {"p": product}
                ).scalar_one()
                == 1
            )


class TestWhatCountsAsAnInventoryDependent:
    """Non-zero stock, any ledger history, or a live hold. Nothing else."""

    def count(self, db_session, product) -> int:
        db_session.rollback()
        return inventory_repository.count_stocked_variants_of_product(
            db_session, uuid.UUID(str(product))
        )

    def test_a_provisioned_but_untouched_variant_does_not_count(
        self, client, catalog_admin, inventory_admin, db_session
    ):
        """An all-zero row with no history records nothing worth keeping."""
        product, variant = draft_product_with_variant(client, catalog_admin, "Empty Shelf", "EMPTY-1")
        provision(client, inventory_admin, variant)

        assert self.count(db_session, product) == 0

    def test_stock_on_hand_counts(self, client, catalog_admin, inventory_admin, db_session):
        product, variant = draft_product_with_variant(client, catalog_admin, "Has Stock", "HAS-1")
        provision(client, inventory_admin, variant)
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=3,
            to_bucket="ON_HAND",
        )

        assert self.count(db_session, product) == 1

    def test_damaged_stock_counts_too(self, client, catalog_admin, inventory_admin, db_session):
        product, variant = draft_product_with_variant(client, catalog_admin, "Has Damage", "DMG-1")
        provision(client, inventory_admin, variant)
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=3,
            to_bucket="ON_HAND",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="TRANSFER",
            quantity=3,
            from_bucket="ON_HAND",
            to_bucket="DAMAGED",
        )

        assert self.count(db_session, product) == 1

    def test_ledger_history_alone_counts_even_at_zero_stock(
        self, client, catalog_admin, inventory_admin, db_session
    ):
        """Stock that came and went still happened, and the ledger says so."""
        product, variant = draft_product_with_variant(client, catalog_admin, "Spent", "SPENT-1")
        provision(client, inventory_admin, variant)
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="RESTOCK",
            quantity=2,
            to_bucket="ON_HAND",
        )
        adjust(
            client,
            inventory_admin,
            variant,
            transaction_type="DISPOSAL",
            quantity=2,
            from_bucket="ON_HAND",
        )

        assert self.count(db_session, product) == 1

    def test_a_product_with_no_inventory_at_all_counts_zero(self, client, catalog_admin, db_session):
        product = product_id(client, catalog_admin, name="Never Stocked")
        create_variant(client, catalog_admin, product, sku="NEVER-1")

        assert self.count(db_session, product) == 0


class TestTheDatabaseLevelGuarantee:
    def test_a_variant_with_inventory_cannot_be_deleted_even_by_raw_sql(
        self, client, catalog_admin, inventory_admin, test_engine
    ):
        """RESTRICT is what holds if the service guard is ever bypassed."""
        from sqlalchemy.exc import IntegrityError

        _, variant = product_uuid(client, catalog_admin, "Protected", "PROT-1")
        provision(client, inventory_admin, variant)

        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM product_variants WHERE id = :v"), {"v": variant}
                )

    def test_deleting_the_product_cannot_cascade_stock_away(
        self, client, catalog_admin, inventory_admin, test_engine
    ):
        """products -> variants is CASCADE, but variants -> inventory is not.

        So even a raw product delete is stopped by the inventory row rather
        than quietly taking it with the variant.
        """
        from sqlalchemy.exc import IntegrityError

        product, variant = product_uuid(client, catalog_admin, "No Cascade", "CASC-1")
        provision(client, inventory_admin, variant)

        with pytest.raises(IntegrityError):
            with test_engine.begin() as connection:
                connection.execute(text("DELETE FROM products WHERE id = :p"), {"p": product})
