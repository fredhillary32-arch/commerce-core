"""Products: slugs, the status lifecycle, and when a product may be deleted."""

import uuid

import pytest
from sqlalchemy import text

from tests.catalog_helpers import (
    PRODUCTS,
    assign_category,
    create_category,
    create_product,
    create_variant,
    patch_product,
    patch_variant,
    product_id,
)


class TestCreation:
    def test_a_product_starts_as_a_draft(self, client, catalog_admin):
        response = create_product(client, catalog_admin, name="Aurora Desk Lamp")

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "DRAFT"
        assert body["variants"] == []
        assert body["options"] == []
        uuid.UUID(body["id"])

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Aurora Desk Lamp", "aurora-desk-lamp"),
            ("  Brass & Glass Lamp!  ", "brass-glass-lamp"),
            ("Model 2000 / XL", "model-2000-xl"),
            ("Already-Hyphenated", "already-hyphenated"),
        ],
    )
    def test_the_slug_is_derived_from_the_name(self, client, catalog_admin, name, expected):
        response = create_product(client, catalog_admin, name=name)

        assert response.json()["slug"] == expected

    def test_an_explicit_slug_is_kept_verbatim(self, client, catalog_admin):
        response = create_product(
            client, catalog_admin, name="Aurora Desk Lamp", slug="lamp-2024"
        )

        assert response.json()["slug"] == "lamp-2024"

    def test_a_duplicate_slug_is_refused_rather_than_suffixed(self, client, catalog_admin):
        create_product(client, catalog_admin, name="Aurora Desk Lamp")

        response = create_product(client, catalog_admin, name="Aurora Desk Lamp")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_SLUG"

    def test_a_name_with_nothing_sluggable_in_it_is_reported(self, client, catalog_admin):
        response = create_product(client, catalog_admin, name="!!!")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_SLUG"

    def test_an_unknown_product_is_reported_with_its_own_code(self, client, catalog_admin):
        response = client.get(f"{PRODUCTS}/{uuid.uuid4()}", headers=catalog_admin)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


class TestStatusTransitions:
    def test_publishing_requires_an_active_variant(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        response = patch_product(client, catalog_admin, product, status="ACTIVE")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "PRODUCT_HAS_NO_ACTIVE_VARIANT"

    def test_a_draft_variant_does_not_count_towards_publishing(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        create_variant(client, catalog_admin, product, price="9.99", currency="GBP")

        response = patch_product(client, catalog_admin, product, status="ACTIVE")

        assert response.json()["error"]["code"] == "PRODUCT_HAS_NO_ACTIVE_VARIANT"

    def test_a_product_with_an_active_variant_can_be_published(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        variant = create_variant(
            client, catalog_admin, product, price="24.99", currency="GBP"
        ).json()["id"]
        patch_variant(client, catalog_admin, variant, status="ACTIVE")

        response = patch_product(client, catalog_admin, product, status="ACTIVE")

        assert response.status_code == 200
        assert response.json()["status"] == "ACTIVE"

    @pytest.mark.parametrize("target", ["ARCHIVED", "DISCONTINUED"])
    def test_a_published_product_can_be_withdrawn(self, client, catalog_admin, target):
        from tests.catalog_helpers import activate_product_with_variant

        product, _ = activate_product_with_variant(client, catalog_admin)

        response = patch_product(client, catalog_admin, product, status=target)

        assert response.status_code == 200
        assert response.json()["status"] == target

    def test_a_draft_cannot_jump_straight_to_discontinued(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        response = patch_product(client, catalog_admin, product, status="DISCONTINUED")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"

    def test_discontinued_is_terminal(self, client, catalog_admin):
        from tests.catalog_helpers import activate_product_with_variant

        product, _ = activate_product_with_variant(client, catalog_admin)
        patch_product(client, catalog_admin, product, status="DISCONTINUED")

        for target in ("ACTIVE", "ARCHIVED", "DRAFT"):
            response = patch_product(client, catalog_admin, product, status=target)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "INVALID_STATUS_TRANSITION"

    def test_an_archived_product_can_be_republished(self, client, catalog_admin):
        from tests.catalog_helpers import activate_product_with_variant

        product, _ = activate_product_with_variant(client, catalog_admin)
        patch_product(client, catalog_admin, product, status="ARCHIVED")

        response = patch_product(client, catalog_admin, product, status="ACTIVE")

        assert response.status_code == 200
        assert response.json()["status"] == "ACTIVE"

    def test_republishing_still_needs_an_active_variant(self, client, catalog_admin):
        from tests.catalog_helpers import activate_product_with_variant

        product, variant = activate_product_with_variant(client, catalog_admin)
        patch_product(client, catalog_admin, product, status="ARCHIVED")
        patch_variant(client, catalog_admin, variant, status="DISCONTINUED")

        response = patch_product(client, catalog_admin, product, status="ACTIVE")

        assert response.json()["error"]["code"] == "PRODUCT_HAS_NO_ACTIVE_VARIANT"

    def test_an_unknown_status_never_reaches_the_service(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        response = patch_product(client, catalog_admin, product, status="PUBLISHED")

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_editing_fields_without_a_status_leaves_the_status_alone(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        response = patch_product(
            client, catalog_admin, product, name="Renamed", description="Now with a description."
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"
        assert response.json()["status"] == "DRAFT"

    def test_renaming_onto_another_products_slug_is_refused(self, client, catalog_admin):
        create_product(client, catalog_admin, name="First", slug="taken")
        product = product_id(client, catalog_admin, name="Second")

        response = patch_product(client, catalog_admin, product, slug="taken")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_SLUG"


class TestDeletion:
    def test_an_untouched_draft_can_be_deleted(self, client, catalog_admin, test_engine):
        product = product_id(client, catalog_admin)

        response = client.delete(f"{PRODUCTS}/{product}", headers=catalog_admin)

        assert response.status_code == 204
        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM products WHERE id = :p"), {"p": product}
            ).scalar_one() == 0

    def test_deletion_is_blocked_once_a_variant_exists(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        create_variant(client, catalog_admin, product)

        response = client.delete(f"{PRODUCTS}/{product}", headers=catalog_admin)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_HAS_DEPENDENTS"

    def test_deletion_is_blocked_once_a_category_is_assigned(self, client, catalog_admin):
        product = product_id(client, catalog_admin)
        category = create_category(client, catalog_admin).json()["id"]
        assign_category(client, catalog_admin, product, category)

        response = client.delete(f"{PRODUCTS}/{product}", headers=catalog_admin)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_HAS_DEPENDENTS"

    def test_deletion_is_blocked_once_an_image_exists(
        self, client, catalog_admin, fake_storage
    ):
        from tests.catalog_helpers import confirm_image, upload_url

        product = product_id(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        confirm_image(client, catalog_admin, product, path)

        response = client.delete(f"{PRODUCTS}/{product}", headers=catalog_admin)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_HAS_DEPENDENTS"

    def test_a_published_product_is_never_deleted(self, client, catalog_admin):
        from tests.catalog_helpers import activate_product_with_variant

        product, _ = activate_product_with_variant(client, catalog_admin)

        response = client.delete(f"{PRODUCTS}/{product}", headers=catalog_admin)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRODUCT_HAS_DEPENDENTS"


class TestAuditTrail:
    def test_the_meaningful_state_changes_are_recorded(
        self, client, catalog_admin, test_engine
    ):
        from tests.catalog_helpers import activate_product_with_variant

        product, _ = activate_product_with_variant(client, catalog_admin)
        patch_product(client, catalog_admin, product, status="ARCHIVED")
        patch_product(client, catalog_admin, product, status="DISCONTINUED")

        with test_engine.connect() as connection:
            events = connection.execute(text("SELECT event_type FROM audit_logs")).scalars().all()

        assert {
            "product.created",
            "product.published",
            "product.archived",
            "product.discontinued",
        } <= set(events)

    def test_a_plain_rename_is_not_worth_an_audit_row(self, client, catalog_admin, test_engine):
        product = product_id(client, catalog_admin)
        patch_product(client, catalog_admin, product, name="Renamed")

        with test_engine.connect() as connection:
            events = connection.execute(text("SELECT event_type FROM audit_logs")).scalars().all()

        assert [event for event in events if event.startswith("product.")] == ["product.created"]
