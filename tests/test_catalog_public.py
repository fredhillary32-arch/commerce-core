"""The public storefront: what a customer may and may not see."""

import uuid

import pytest

from tests.catalog_helpers import (
    PUBLIC,
    activate_product_with_variant,
    assign_category,
    confirm_image,
    create_category,
    create_option,
    create_option_value,
    create_variant,
    patch_category,
    patch_product,
    patch_variant,
    product_id,
    upload_url,
)


class TestVisibility:
    def test_a_draft_product_is_invisible(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        assert client.get(f"{PUBLIC}/products").json() == []
        assert client.get(f"{PUBLIC}/products/{product}").status_code == 404

    @pytest.mark.parametrize("status", ["ARCHIVED", "DISCONTINUED"])
    def test_a_withdrawn_product_disappears_but_keeps_its_data(
        self, client, catalog_admin, status
    ):
        product, _ = activate_product_with_variant(client, catalog_admin)
        assert len(client.get(f"{PUBLIC}/products").json()) == 1

        patch_product(client, catalog_admin, product, status=status)

        assert client.get(f"{PUBLIC}/products").json() == []
        assert client.get(f"{PUBLIC}/products/{product}").status_code == 404
        # The admin view still has it, with everything intact.
        admin = client.get(f"/api/v1/products/{product}", headers=catalog_admin).json()
        assert admin["status"] == status
        assert len(admin["variants"]) == 1

    def test_a_withdrawn_product_reports_the_same_code_as_one_that_never_existed(
        self, client, catalog_admin
    ):
        product, _ = activate_product_with_variant(client, catalog_admin)
        patch_product(client, catalog_admin, product, status="ARCHIVED")

        withdrawn = client.get(f"{PUBLIC}/products/{product}")
        missing = client.get(f"{PUBLIC}/products/{uuid.uuid4()}")

        assert withdrawn.status_code == missing.status_code == 404
        assert withdrawn.json() == missing.json()

    def test_only_active_variants_are_shown(self, client, catalog_admin):
        product, active = activate_product_with_variant(client, catalog_admin)
        create_variant(client, catalog_admin, product, sku="LAMP-DRAFT")
        retired = create_variant(
            client, catalog_admin, product, sku="LAMP-OLD", price="9.99", currency="GBP"
        ).json()["id"]
        patch_variant(client, catalog_admin, retired, status="ACTIVE")
        patch_variant(client, catalog_admin, retired, status="DISCONTINUED")

        body = client.get(f"{PUBLIC}/products/{product}").json()

        assert [variant["id"] for variant in body["variants"]] == [active]

    def test_inactive_categories_are_hidden_from_a_products_listing(
        self, client, catalog_admin
    ):
        product, _ = activate_product_with_variant(client, catalog_admin)
        shown = create_category(client, catalog_admin, name="Lighting").json()["id"]
        hidden = create_category(client, catalog_admin, name="Clearance").json()["id"]
        assign_category(client, catalog_admin, product, shown)
        assign_category(client, catalog_admin, product, hidden)
        patch_category(client, catalog_admin, hidden, is_active=False)

        body = client.get(f"{PUBLIC}/products/{product}").json()

        assert [category["name"] for category in body["categories"]] == ["Lighting"]

    def test_a_category_lists_only_published_products(self, client, catalog_admin):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]
        published, _ = activate_product_with_variant(client, catalog_admin)
        draft = product_id(client, catalog_admin, name="Unfinished Lamp")
        assign_category(client, catalog_admin, published, category)
        assign_category(client, catalog_admin, draft, category)

        body = client.get(f"{PUBLIC}/categories/{category}").json()

        assert body["product_ids"] == [published]


class TestContent:
    def test_a_product_carries_its_options_variants_and_pricing(
        self, client, catalog_admin
    ):
        product = product_id(client, catalog_admin)
        option = create_option(client, catalog_admin, product, name="Size").json()["id"]
        value = create_option_value(
            client, catalog_admin, product, option, "Large"
        ).json()["id"]
        variant = create_variant(
            client, catalog_admin, product, sku="LAMP-L", price="24.99", currency="GBP"
        ).json()["id"]
        from tests.catalog_helpers import assign_option_value

        assign_option_value(client, catalog_admin, variant, option, value)
        patch_variant(client, catalog_admin, variant, status="ACTIVE")
        patch_product(client, catalog_admin, product, status="ACTIVE")

        body = client.get(f"{PUBLIC}/products/{product}").json()

        assert body["options"] == [
            {"id": option, "name": "Size", "values": [{"id": value, "option_id": option, "value": "Large"}]}
        ]
        assert body["variants"][0]["price"] == "24.99"
        assert body["variants"][0]["currency"] == "GBP"
        assert body["variants"][0]["option_values"][0]["value"] == "Large"

    def test_storage_paths_are_never_exposed_publicly(
        self, client, catalog_admin, fake_storage
    ):
        product, _ = activate_product_with_variant(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        confirm_image(client, catalog_admin, product, path)

        body = client.get(f"{PUBLIC}/products/{product}").json()

        assert "storage_path" not in body["images"][0]
        assert body["images"][0]["url"].startswith("https://storage.test/signed/")

    def test_a_listing_signs_one_url_per_product_for_its_primary_image(
        self, client, catalog_admin, fake_storage
    ):
        product, _ = activate_product_with_variant(client, catalog_admin)
        primary = upload_url(client, catalog_admin, product).json()["storage_path"]
        secondary = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(primary)
        fake_storage.simulate_upload(secondary)
        confirm_image(client, catalog_admin, product, primary, is_primary=True)
        confirm_image(client, catalog_admin, product, secondary)
        fake_storage.delivery_calls.clear()

        body = client.get(f"{PUBLIC}/products").json()

        assert fake_storage.delivery_calls == [primary]
        assert body[0]["primary_image_url"].startswith("https://storage.test/signed/")

    def test_a_product_with_no_images_lists_without_a_url(self, client, catalog_admin):
        activate_product_with_variant(client, catalog_admin)

        body = client.get(f"{PUBLIC}/products").json()

        assert body[0]["primary_image_url"] is None


class TestCategories:
    def test_active_categories_are_listed_with_their_hierarchy(self, client, catalog_admin):
        parent = create_category(client, catalog_admin, name="Lighting").json()["id"]
        child = create_category(
            client, catalog_admin, name="Desk Lamps", parent_id=parent
        ).json()["id"]

        body = client.get(f"{PUBLIC}/categories").json()

        by_id = {category["id"]: category for category in body}
        assert by_id[child]["parent_id"] == parent
        assert by_id[parent]["parent_id"] is None

    def test_an_unknown_category_is_reported(self, client):
        response = client.get(f"{PUBLIC}/categories/{uuid.uuid4()}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CATEGORY_NOT_FOUND"


class TestPagination:
    def test_a_listing_is_bounded_and_pageable(self, client, catalog_admin):
        for index in range(3):
            activate_product_with_variant(
                client, catalog_admin, name=f"Lamp {index}", sku=f"LAMP-{index}"
            )

        first_page = client.get(f"{PUBLIC}/products", params={"limit": 2}).json()
        second_page = client.get(
            f"{PUBLIC}/products", params={"limit": 2, "offset": 2}
        ).json()

        assert len(first_page) == 2
        assert len(second_page) == 1
        assert not {product["id"] for product in first_page} & {
            product["id"] for product in second_page
        }

    def test_an_absurd_page_size_is_refused(self, client):
        response = client.get(f"{PUBLIC}/products", params={"limit": 5000})

        assert response.status_code == 422
