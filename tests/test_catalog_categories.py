"""Categories: the hierarchy, the cycle guard, and the lifecycle."""

import uuid

from sqlalchemy import text

from tests.catalog_helpers import (
    CATEGORIES,
    PUBLIC,
    assign_category,
    create_category,
    patch_category,
    product_id,
)


class TestCreation:
    def test_a_category_starts_active_at_the_root(self, client, catalog_admin):
        response = create_category(client, catalog_admin, name="Lighting")

        assert response.status_code == 201
        body = response.json()
        assert body["slug"] == "lighting"
        assert body["parent_id"] is None
        assert body["is_active"] is True

    def test_an_explicit_slug_is_kept(self, client, catalog_admin):
        response = create_category(client, catalog_admin, name="Lighting", slug="lamps")

        assert response.json()["slug"] == "lamps"

    def test_a_duplicate_slug_is_refused(self, client, catalog_admin):
        create_category(client, catalog_admin, name="Lighting")

        response = create_category(client, catalog_admin, name="Lighting")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "DUPLICATE_SLUG"

    def test_an_unknown_category_is_reported_with_its_own_code(self, client, catalog_admin):
        response = patch_category(client, catalog_admin, uuid.uuid4(), name="Anything")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CATEGORY_NOT_FOUND"


class TestHierarchy:
    def test_a_child_records_its_parent(self, client, catalog_admin):
        parent = create_category(client, catalog_admin, name="Lighting").json()["id"]

        response = create_category(
            client, catalog_admin, name="Desk Lamps", parent_id=parent
        )

        assert response.status_code == 201
        assert response.json()["parent_id"] == parent

    def test_a_deep_chain_is_one_table(self, client, catalog_admin, test_engine):
        first = create_category(client, catalog_admin, name="Home").json()["id"]
        second = create_category(
            client, catalog_admin, name="Lighting", parent_id=first
        ).json()["id"]
        third = create_category(
            client, catalog_admin, name="Desk Lamps", parent_id=second
        ).json()["id"]

        with test_engine.connect() as connection:
            parents = dict(
                connection.execute(text("SELECT id, parent_id FROM categories")).all()
            )

        assert str(parents[uuid.UUID(third)]) == second
        assert str(parents[uuid.UUID(second)]) == first
        assert parents[uuid.UUID(first)] is None

    def test_an_unknown_parent_is_reported_at_creation(self, client, catalog_admin):
        response = create_category(
            client, catalog_admin, name="Orphan", parent_id=str(uuid.uuid4())
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CATEGORY_NOT_FOUND"

    def test_a_category_can_be_moved_to_the_root(self, client, catalog_admin):
        parent = create_category(client, catalog_admin, name="Lighting").json()["id"]
        child = create_category(
            client, catalog_admin, name="Desk Lamps", parent_id=parent
        ).json()["id"]

        response = patch_category(client, catalog_admin, child, parent_id=None)

        assert response.status_code == 200
        assert response.json()["parent_id"] is None

    def test_a_patch_that_omits_the_parent_leaves_it_alone(self, client, catalog_admin):
        parent = create_category(client, catalog_admin, name="Lighting").json()["id"]
        child = create_category(
            client, catalog_admin, name="Desk Lamps", parent_id=parent
        ).json()["id"]

        response = patch_category(client, catalog_admin, child, name="Task Lamps")

        assert response.json()["parent_id"] == parent


class TestCycleGuard:
    def test_a_category_cannot_be_its_own_parent(self, client, catalog_admin):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]

        response = patch_category(client, catalog_admin, category, parent_id=category)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CATEGORY_PARENT"

    def test_a_parent_cannot_be_moved_under_its_own_child(self, client, catalog_admin):
        parent = create_category(client, catalog_admin, name="Lighting").json()["id"]
        child = create_category(
            client, catalog_admin, name="Desk Lamps", parent_id=parent
        ).json()["id"]

        response = patch_category(client, catalog_admin, parent, parent_id=child)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CATEGORY_PARENT"

    def test_a_longer_loop_is_caught_by_walking_the_whole_chain(self, client, catalog_admin):
        first = create_category(client, catalog_admin, name="Home").json()["id"]
        second = create_category(
            client, catalog_admin, name="Lighting", parent_id=first
        ).json()["id"]
        third = create_category(
            client, catalog_admin, name="Desk Lamps", parent_id=second
        ).json()["id"]

        response = patch_category(client, catalog_admin, first, parent_id=third)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_CATEGORY_PARENT"

    def test_a_rejected_move_changes_nothing(self, client, catalog_admin, test_engine):
        parent = create_category(client, catalog_admin, name="Lighting").json()["id"]
        child = create_category(
            client, catalog_admin, name="Desk Lamps", parent_id=parent
        ).json()["id"]

        patch_category(client, catalog_admin, parent, parent_id=child)

        with test_engine.connect() as connection:
            stored = connection.execute(
                text("SELECT parent_id FROM categories WHERE id = :c"), {"c": parent}
            ).scalar_one()
        assert stored is None


class TestLifecycle:
    def test_an_empty_never_used_category_can_be_deleted(
        self, client, catalog_admin, test_engine
    ):
        category = create_category(client, catalog_admin, name="Mistake").json()["id"]

        response = client.delete(f"{CATEGORIES}/{category}", headers=catalog_admin)

        assert response.status_code == 204
        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM categories WHERE id = :c"), {"c": category}
            ).scalar_one() == 0

    def test_deletion_is_blocked_once_a_product_is_assigned(self, client, catalog_admin):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]
        product = product_id(client, catalog_admin)
        assign_category(client, catalog_admin, product, category)

        response = client.delete(f"{CATEGORIES}/{category}", headers=catalog_admin)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CATEGORY_HAS_DEPENDENTS"

    def test_deletion_is_blocked_once_a_child_exists(self, client, catalog_admin):
        parent = create_category(client, catalog_admin, name="Lighting").json()["id"]
        create_category(client, catalog_admin, name="Desk Lamps", parent_id=parent)

        response = client.delete(f"{CATEGORIES}/{parent}", headers=catalog_admin)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CATEGORY_HAS_DEPENDENTS"

    def test_deactivating_hides_it_from_public_navigation(self, client, catalog_admin):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]

        patch_category(client, catalog_admin, category, is_active=False)

        listed = client.get(f"{PUBLIC}/categories").json()
        assert listed == []
        assert client.get(f"{PUBLIC}/categories/{category}").status_code == 404

    def test_deactivating_keeps_the_historical_product_relationships(
        self, client, catalog_admin, test_engine
    ):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]
        product = product_id(client, catalog_admin)
        assign_category(client, catalog_admin, product, category)

        patch_category(client, catalog_admin, category, is_active=False)

        with test_engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT count(*) FROM product_categories "
                    "WHERE product_id = :p AND category_id = :c"
                ),
                {"p": product, "c": category},
            ).scalar_one() == 1

    def test_a_deactivated_category_can_be_restored(self, client, catalog_admin):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]
        patch_category(client, catalog_admin, category, is_active=False)

        response = patch_category(client, catalog_admin, category, is_active=True)

        assert response.json()["is_active"] is True
        assert client.get(f"{PUBLIC}/categories/{category}").status_code == 200


class TestProductAssignment:
    def test_a_product_is_assigned_to_a_category(self, client, catalog_admin, test_engine):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]
        product = product_id(client, catalog_admin)

        response = assign_category(client, catalog_admin, product, category)

        assert response.status_code == 204
        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM product_categories WHERE product_id = :p"),
                {"p": product},
            ).scalar_one() == 1

    def test_a_product_may_belong_to_several_categories(self, client, catalog_admin):
        first = create_category(client, catalog_admin, name="Lighting").json()["id"]
        second = create_category(client, catalog_admin, name="Office").json()["id"]
        product = product_id(client, catalog_admin)

        assign_category(client, catalog_admin, product, first)
        assign_category(client, catalog_admin, product, second)

        body = client.get(f"/api/v1/products/{product}", headers=catalog_admin).json()
        assert {category["name"] for category in body["categories"]} == {"Lighting", "Office"}

    def test_the_same_relationship_twice_is_refused(self, client, catalog_admin):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]
        product = product_id(client, catalog_admin)
        assign_category(client, catalog_admin, product, category)

        response = assign_category(client, catalog_admin, product, category)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CATEGORY_ALREADY_ASSIGNED"

    def test_an_unknown_category_is_reported(self, client, catalog_admin):
        product = product_id(client, catalog_admin)

        response = assign_category(client, catalog_admin, product, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CATEGORY_NOT_FOUND"

    def test_an_unknown_product_is_reported(self, client, catalog_admin):
        category = create_category(client, catalog_admin, name="Lighting").json()["id"]

        response = assign_category(client, catalog_admin, uuid.uuid4(), category)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


class TestAuditTrail:
    def test_category_events_are_recorded(self, client, catalog_admin, test_engine):
        parent = create_category(client, catalog_admin, name="Lighting").json()["id"]
        child = create_category(client, catalog_admin, name="Desk Lamps").json()["id"]
        patch_category(client, catalog_admin, child, parent_id=parent)
        patch_category(client, catalog_admin, child, is_active=False)

        with test_engine.connect() as connection:
            events = connection.execute(text("SELECT event_type FROM audit_logs")).scalars().all()

        assert {
            "category.created",
            "category.parent_changed",
            "category.archived",
        } <= set(events)
