"""Variants: SKUs, when pricing becomes mandatory, and the status lifecycle."""

import uuid

import pytest
from sqlalchemy import text

from tests.catalog_helpers import (
    PRODUCTS,
    VARIANTS,
    create_variant,
    patch_product,
    patch_variant,
    product_id,
)


class TestCreation:
    def test_a_variant_starts_as_an_unpriced_draft(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        response = create_variant(client, catalog_admin, product, sku="LAMP-001")

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "DRAFT"
        assert body["price"] is None
        assert body["currency"] is None
        assert body["option_values"] == []

    def test_a_draft_variant_may_be_priced_from_the_start(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        body = create_variant(
            client, catalog_admin, product, price="24.99", currency="GBP"
        ).json()

        assert body["price"] == "24.99"
        assert body["currency"] == "GBP"

    def test_a_sku_is_unique_across_the_whole_catalogue(self, client, catalog_admin):
        first = product_id(client, catalog_admin, name="First Lamp")
        second = product_id(client, catalog_admin, name="Second Lamp")
        create_variant(client, catalog_admin, first, sku="SHARED")

        response = create_variant(client, catalog_admin, second, sku="SHARED")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_SKU"

    def test_a_sku_is_compared_case_sensitively(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        create_variant(client, catalog_admin, product, sku="lamp-001")

        response = create_variant(client, catalog_admin, product, sku="LAMP-001")

        assert response.status_code == 201

    def test_renaming_a_sku_onto_an_existing_one_is_refused(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        create_variant(client, catalog_admin, product, sku="TAKEN")
        variant = create_variant(client, catalog_admin, product, sku="FREE").json()["id"]

        response = patch_variant(client, catalog_admin, variant, sku="TAKEN")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_SKU"

    def test_a_variant_cannot_be_created_under_a_product_that_does_not_exist(
        self, client, catalog_admin
    ):
        response = create_variant(client, catalog_admin, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"

    def test_an_unknown_variant_is_reported_with_its_own_code(self, client, catalog_admin):
        response = patch_variant(client, catalog_admin, uuid.uuid4(), sku="ANYTHING")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "VARIANT_NOT_FOUND"

    @pytest.mark.parametrize("price", ["0", "-5.00"])
    def test_a_non_positive_price_never_reaches_the_database(
        self, client, catalog_admin, price
    ):
        product = product_id(client, catalog_admin)

        response = create_variant(
            client, catalog_admin, product, price=price, currency="GBP"
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_many_unconfigured_drafts_coexist_on_one_product(
        self, client, catalog_admin, test_engine
    ):
        """No collision, because an unassigned variant has a null signature."""
        product = product_id(client, catalog_admin)

        for index in range(4):
            assert create_variant(
                client, catalog_admin, product, sku=f"LAMP-{index}"
            ).status_code == 201

        with test_engine.connect() as connection:
            signatures = connection.execute(
                text("SELECT variant_signature FROM product_variants WHERE product_id = :p"),
                {"p": product},
            ).scalars().all()
        assert signatures == [None] * 4


class TestPricingRules:
    def test_activation_requires_a_price(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        variant = create_variant(client, catalog_admin, product).json()["id"]

        response = patch_variant(client, catalog_admin, variant, status="ACTIVE")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VARIANT_PRICE_REQUIRED"

    def test_activation_requires_a_currency_too(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        variant = create_variant(client, catalog_admin, product, price="24.99").json()["id"]

        response = patch_variant(client, catalog_admin, variant, status="ACTIVE")

        assert response.json()["error"]["code"] == "VARIANT_PRICE_REQUIRED"

    def test_pricing_and_activating_in_one_request_is_accepted(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        variant = create_variant(client, catalog_admin, product).json()["id"]

        response = patch_variant(
            client, catalog_admin, variant, price="24.99", currency="GBP", status="ACTIVE"
        )

        assert response.status_code == 200
        assert response.json()["status"] == "ACTIVE"


class TestStatusTransitions:
    def test_a_priced_draft_can_be_activated(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        variant = create_variant(
            client, catalog_admin, product, price="24.99", currency="GBP"
        ).json()["id"]

        response = patch_variant(client, catalog_admin, variant, status="ACTIVE")

        assert response.json()["status"] == "ACTIVE"

    def test_a_draft_cannot_be_discontinued_without_ever_being_sellable(
        self, client, catalog_admin
    ):
        product = product_id(client, catalog_admin)
        variant = create_variant(client, catalog_admin, product).json()["id"]

        response = patch_variant(client, catalog_admin, variant, status="DISCONTINUED")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"

    def test_discontinued_has_no_outgoing_edge(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        variant = create_variant(
            client, catalog_admin, product, price="24.99", currency="GBP"
        ).json()["id"]
        patch_variant(client, catalog_admin, variant, status="ACTIVE")
        patch_variant(client, catalog_admin, variant, status="DISCONTINUED")

        for target in ("ACTIVE", "DRAFT"):
            response = patch_variant(client, catalog_admin, variant, status=target)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"

    def test_an_active_variant_cannot_go_back_to_draft(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        variant = create_variant(
            client, catalog_admin, product, price="24.99", currency="GBP"
        ).json()["id"]
        patch_variant(client, catalog_admin, variant, status="ACTIVE")

        response = patch_variant(client, catalog_admin, variant, status="DRAFT")

        assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"

    def test_discontinuing_the_last_active_variant_leaves_the_product_published(
        self, client, catalog_admin
    ):
        """Unpublishing is the merchant's call, never a side effect."""
        from tests.catalog_helpers import activate_product_with_variant

        product, variant = activate_product_with_variant(client, catalog_admin)

        patch_variant(client, catalog_admin, variant, status="DISCONTINUED")

        body = client.get(f"{PRODUCTS}/{product}", headers=catalog_admin).json()
        assert body["status"] == "ACTIVE"

    def test_variant_events_are_audited(self, client, catalog_admin, test_engine):
        from tests.catalog_helpers import activate_product_with_variant

        _, variant = activate_product_with_variant(client, catalog_admin)
        patch_variant(client, catalog_admin, variant, status="DISCONTINUED")

        with test_engine.connect() as connection:
            events = connection.execute(text("SELECT event_type FROM audit_logs")).scalars().all()

        assert {"variant.created", "variant.discontinued"} <= set(events)


class TestOwnership:
    def test_a_variant_is_listed_only_under_its_own_product(self, client, catalog_admin):
        first = product_id(client, catalog_admin, name="First Lamp")
        second = product_id(client, catalog_admin, name="Second Lamp")
        create_variant(client, catalog_admin, first, sku="FIRST-1")

        body = client.get(f"{PRODUCTS}/{second}", headers=catalog_admin).json()

        assert body["variants"] == []

    def test_a_variant_records_the_product_it_was_created_under(
        self, client, catalog_admin, test_engine
    ):
        product = product_id(client, catalog_admin)
        variant = create_variant(client, catalog_admin, product).json()["id"]

        with test_engine.connect() as connection:
            owner = connection.execute(
                text("SELECT product_id FROM product_variants WHERE id = :v"), {"v": variant}
            ).scalar_one()

        assert str(owner) == product
