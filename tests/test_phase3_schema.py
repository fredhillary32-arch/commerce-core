"""Catalogue guarantees PostgreSQL enforces, tested against PostgreSQL itself.

These go through raw SQL rather than the API on purpose: the point is that
the constraint holds even if a future service-layer check is removed.
"""

import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models.catalog import PRODUCT_STATUSES, VARIANT_STATUSES
from app.modules.catalog.schemas import ProductStatus, VariantStatus

PHASE_3_TABLES = {
    "products",
    "product_options",
    "product_option_values",
    "product_variants",
    "variant_option_values",
    "categories",
    "product_categories",
    "product_images",
}


def insert_product(connection, name="Schema Product", slug=None, status="DRAFT") -> uuid.UUID:
    return connection.execute(
        text(
            "INSERT INTO products (name, slug, status) VALUES (:n, :s, :st) RETURNING id"
        ),
        {"n": name, "s": slug or f"schema-{uuid.uuid4()}", "st": status},
    ).scalar_one()


def insert_option(connection, product_id, name="Size") -> uuid.UUID:
    return connection.execute(
        text("INSERT INTO product_options (product_id, name) VALUES (:p, :n) RETURNING id"),
        {"p": product_id, "n": name},
    ).scalar_one()


def insert_option_value(connection, option_id, value="Large") -> uuid.UUID:
    return connection.execute(
        text("INSERT INTO product_option_values (option_id, value) VALUES (:o, :v) RETURNING id"),
        {"o": option_id, "v": value},
    ).scalar_one()


def insert_variant(connection, product_id, sku=None, signature=None, **columns) -> uuid.UUID:
    return connection.execute(
        text(
            "INSERT INTO product_variants (product_id, sku, variant_signature, status, price, currency) "
            "VALUES (:p, :sku, :sig, :status, :price, :currency) RETURNING id"
        ),
        {
            "p": product_id,
            "sku": sku or f"SKU-{uuid.uuid4()}",
            "sig": signature,
            "status": columns.get("status", "DRAFT"),
            "price": columns.get("price"),
            "currency": columns.get("currency"),
        },
    ).scalar_one()


def insert_category(connection, name="Schema Category", slug=None, parent_id=None) -> uuid.UUID:
    return connection.execute(
        text("INSERT INTO categories (name, slug, parent_id) VALUES (:n, :s, :p) RETURNING id"),
        {"n": name, "s": slug or f"cat-{uuid.uuid4()}", "p": parent_id},
    ).scalar_one()


def insert_image(connection, product_id, *, is_primary=False, path=None) -> uuid.UUID:
    return connection.execute(
        text(
            "INSERT INTO product_images (product_id, storage_path, is_primary) "
            "VALUES (:p, :path, :primary) RETURNING id"
        ),
        {
            "p": product_id,
            "path": path or f"products/{product_id}/images/{uuid.uuid4()}",
            "primary": is_primary,
        },
    ).scalar_one()


class TestTablesAndDefaults:
    def test_all_eight_phase_3_tables_exist(self, test_engine):
        assert PHASE_3_TABLES <= set(inspect(test_engine).get_table_names())

    def test_a_product_defaults_to_draft_with_generated_id_and_timestamps(self, test_engine):
        with test_engine.begin() as connection:
            row = connection.execute(
                text(
                    "INSERT INTO products (name, slug) VALUES ('Defaults', 'defaults') "
                    "RETURNING id, status, created_at, updated_at"
                )
            ).one()

        assert isinstance(row.id, uuid.UUID)
        assert row.status == "DRAFT"
        assert row.created_at.tzinfo is not None
        assert row.updated_at.tzinfo is not None

    def test_a_variant_defaults_to_draft_with_no_price_or_signature(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            row = connection.execute(
                text(
                    "INSERT INTO product_variants (product_id, sku) VALUES (:p, 'SKU-DEFAULTS') "
                    "RETURNING status, price, currency, variant_signature"
                ),
                {"p": product_id},
            ).one()

        assert row.status == "DRAFT"
        assert row.price is None
        assert row.currency is None
        assert row.variant_signature is None

    def test_an_image_defaults_to_non_primary_at_the_front_of_the_gallery(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            row = connection.execute(
                text(
                    "INSERT INTO product_images (product_id, storage_path) "
                    "VALUES (:p, 'products/x/images/y') RETURNING sort_order, is_primary"
                ),
                {"p": product_id},
            ).one()

        assert row.sort_order == 0
        assert row.is_primary is False

    def test_a_category_defaults_to_active_at_the_root(self, test_engine):
        with test_engine.begin() as connection:
            row = connection.execute(
                text(
                    "INSERT INTO categories (name, slug) VALUES ('Defaults', 'cat-defaults') "
                    "RETURNING parent_id, is_active"
                )
            ).one()

        assert row.parent_id is None
        assert row.is_active is True

    def test_money_keeps_two_decimal_places(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            variant_id = insert_variant(connection, product_id, price="19.99", currency="GBP")
            price = connection.execute(
                text("SELECT price FROM product_variants WHERE id = :v"), {"v": variant_id}
            ).scalar_one()

        assert str(price) == "19.99"


class TestCheckConstraints:
    def test_an_unknown_product_status_is_rejected(self, test_engine):
        with pytest.raises(IntegrityError, match="ck_products_status_valid"):
            with test_engine.begin() as connection:
                insert_product(connection, status="PUBLISHED")

    def test_an_unknown_variant_status_is_rejected(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)

        with pytest.raises(IntegrityError, match="ck_product_variants_status_valid"):
            with test_engine.begin() as connection:
                insert_variant(connection, product_id, status="ARCHIVED")

    @pytest.mark.parametrize("price", ["0", "-1.00"])
    def test_a_non_positive_price_is_rejected(self, test_engine, price):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)

        with pytest.raises(IntegrityError, match="ck_product_variants_price_positive"):
            with test_engine.begin() as connection:
                insert_variant(connection, product_id, price=price, currency="GBP")

    def test_the_price_check_still_permits_a_priceless_draft(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            variant_id = insert_variant(connection, product_id)

        assert variant_id is not None

    def test_the_status_literals_in_the_api_match_the_database_constraint(self):
        assert set(ProductStatus.__args__) == set(PRODUCT_STATUSES)
        assert set(VariantStatus.__args__) == set(VARIANT_STATUSES)


class TestUniqueness:
    def test_a_product_slug_is_unique(self, test_engine):
        with test_engine.begin() as connection:
            insert_product(connection, slug="duplicate-slug")

        with pytest.raises(IntegrityError, match="uq_products_slug"):
            with test_engine.begin() as connection:
                insert_product(connection, slug="duplicate-slug")

    def test_a_category_slug_is_unique(self, test_engine):
        with test_engine.begin() as connection:
            insert_category(connection, slug="duplicate-category")

        with pytest.raises(IntegrityError, match="uq_categories_slug"):
            with test_engine.begin() as connection:
                insert_category(connection, slug="duplicate-category")

    def test_a_sku_is_unique_across_every_product(self, test_engine):
        with test_engine.begin() as connection:
            first_product = insert_product(connection)
            second_product = insert_product(connection)
            insert_variant(connection, first_product, sku="SHARED-SKU")

        with pytest.raises(IntegrityError, match="uq_product_variants_sku"):
            with test_engine.begin() as connection:
                insert_variant(connection, second_product, sku="SHARED-SKU")

    def test_a_sku_differing_only_in_case_is_a_different_sku(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            insert_variant(connection, product_id, sku="abc-123")
            insert_variant(connection, product_id, sku="ABC-123")

        with test_engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM product_variants WHERE product_id = :p"),
                {"p": product_id},
            ).scalar_one()
        assert count == 2

    def test_two_variants_cannot_share_a_signature_within_a_product(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            insert_variant(connection, product_id, signature="option:value")

        with pytest.raises(IntegrityError, match="uq_product_variants_product_id_signature"):
            with test_engine.begin() as connection:
                insert_variant(connection, product_id, signature="option:value")

    def test_the_same_signature_on_two_products_is_fine(self, test_engine):
        with test_engine.begin() as connection:
            first_product = insert_product(connection)
            second_product = insert_product(connection)
            insert_variant(connection, first_product, signature="option:value")
            insert_variant(connection, second_product, signature="option:value")

        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM product_variants WHERE variant_signature = 'option:value'")
            ).scalar_one() == 2

    def test_many_unconfigured_variants_coexist_because_null_signatures_never_collide(
        self, test_engine
    ):
        """The reason variant_signature is nullable rather than defaulted."""
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            for index in range(5):
                insert_variant(connection, product_id, sku=f"EMPTY-{index}")

        with test_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM product_variants "
                    "WHERE product_id = :p AND variant_signature IS NULL"
                ),
                {"p": product_id},
            ).scalar_one() == 5

    def test_a_variant_may_hold_only_one_value_per_option(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            option_id = insert_option(connection, product_id)
            first_value = insert_option_value(connection, option_id, "Small")
            second_value = insert_option_value(connection, option_id, "Large")
            variant_id = insert_variant(connection, product_id)
            connection.execute(
                text(
                    "INSERT INTO variant_option_values (variant_id, option_id, option_value_id) "
                    "VALUES (:v, :o, :ov)"
                ),
                {"v": variant_id, "o": option_id, "ov": first_value},
            )

        with pytest.raises(IntegrityError, match="uq_variant_option_values_variant_id_option_id"):
            with test_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO variant_option_values (variant_id, option_id, option_value_id) "
                        "VALUES (:v, :o, :ov)"
                    ),
                    {"v": variant_id, "o": option_id, "ov": second_value},
                )

    def test_an_option_and_a_value_that_disagree_are_rejected_by_the_composite_key(
        self, test_engine
    ):
        """The denormalised option_id can never contradict option_value_id."""
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            size_option = insert_option(connection, product_id, "Size")
            colour_option = insert_option(connection, product_id, "Colour")
            colour_value = insert_option_value(connection, colour_option, "Red")
            variant_id = insert_variant(connection, product_id)

        with pytest.raises(IntegrityError, match="fk_variant_option_values_option_id_value_id"):
            with test_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO variant_option_values (variant_id, option_id, option_value_id) "
                        "VALUES (:v, :o, :ov)"
                    ),
                    # Claims to be a Size, but points at a Colour's value.
                    {"v": variant_id, "o": size_option, "ov": colour_value},
                )

    def test_a_product_may_have_only_one_primary_image(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            insert_image(connection, product_id, is_primary=True)

        with pytest.raises(IntegrityError, match="uq_product_images_one_primary_per_product"):
            with test_engine.begin() as connection:
                insert_image(connection, product_id, is_primary=True)

    def test_the_partial_index_still_allows_many_secondary_images(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            insert_image(connection, product_id, is_primary=True)
            for _ in range(3):
                insert_image(connection, product_id)

        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM product_images WHERE product_id = :p"),
                {"p": product_id},
            ).scalar_one() == 4

    def test_a_storage_path_is_unique(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            insert_image(connection, product_id, path="products/shared/images/one")

        with pytest.raises(IntegrityError, match="uq_product_images_storage_path"):
            with test_engine.begin() as connection:
                insert_image(connection, product_id, path="products/shared/images/one")

    def test_a_product_belongs_to_a_category_at_most_once(self, test_engine):
        with test_engine.begin() as connection:
            product_id = insert_product(connection)
            category_id = insert_category(connection)
            connection.execute(
                text("INSERT INTO product_categories (product_id, category_id) VALUES (:p, :c)"),
                {"p": product_id, "c": category_id},
            )

        with pytest.raises(IntegrityError, match="pk_product_categories"):
            with test_engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO product_categories (product_id, category_id) VALUES (:p, :c)"
                    ),
                    {"p": product_id, "c": category_id},
                )


class TestIndexes:
    @pytest.mark.parametrize(
        "table,columns",
        [
            ("product_options", ["product_id"]),
            ("product_option_values", ["option_id"]),
            ("product_variants", ["product_id"]),
            ("variant_option_values", ["variant_id"]),
            ("categories", ["parent_id"]),
            ("product_categories", ["category_id"]),
            ("product_images", ["product_id"]),
        ],
    )
    def test_foreign_keys_and_hot_lookups_are_indexed(self, test_engine, table, columns):
        indexed = [
            index["column_names"] for index in inspect(test_engine).get_indexes(table)
        ]
        unique = [
            constraint["column_names"]
            for constraint in inspect(test_engine).get_unique_constraints(table)
        ]
        primary = inspect(test_engine).get_pk_constraint(table)["constrained_columns"]
        assert columns in indexed or columns in unique or primary[: len(columns)] == columns

    @pytest.mark.parametrize(
        "table,column", [("products", "slug"), ("product_variants", "sku"), ("categories", "slug")]
    )
    def test_the_frequently_queried_natural_keys_are_indexed_by_their_uniqueness(
        self, test_engine, table, column
    ):
        unique = [
            constraint["column_names"]
            for constraint in inspect(test_engine).get_unique_constraints(table)
        ]
        assert [column] in unique
