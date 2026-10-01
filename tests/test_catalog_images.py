"""Product images: the upload/confirm handshake, primaries, and deletion.

Every Supabase call is replaced by the ``fake_storage`` fixture, so nothing
here needs network access or a real bucket.
"""

import uuid

from sqlalchemy import text

from tests.catalog_helpers import (
    PRODUCTS,
    confirm_image,
    delete_image,
    product_id,
    upload_url,
)


class TestUploadTarget:
    def test_the_server_chooses_a_path_scoped_to_the_product(
        self, client, catalog_admin, fake_storage
    ):
        product = product_id(client, catalog_admin)

        response = upload_url(client, catalog_admin, product)

        assert response.status_code == 201
        body = response.json()
        assert body["storage_path"].startswith(f"products/{product}/images/")
        assert body["upload_url"].startswith("https://")

    def test_requesting_a_target_writes_no_row(
        self, client, catalog_admin, fake_storage, test_engine
    ):
        """No placeholder row: an abandoned upload leaves nothing behind."""
        product = product_id(client, catalog_admin)

        upload_url(client, catalog_admin, product)

        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM product_images WHERE product_id = :p"),
                {"p": product},
            ).scalar_one() == 0

    def test_an_unknown_product_gets_no_target(self, client, catalog_admin, fake_storage):
        response = upload_url(client, catalog_admin, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
        assert fake_storage.issued_paths == []


class TestConfirm:
    def test_a_path_belonging_to_another_product_is_refused_without_touching_storage(
        self, client, catalog_admin, fake_storage
    ):
        """The cheap check comes first, so a replay never reaches Supabase."""
        victim = product_id(client, catalog_admin, name="Victim Lamp")
        attacker = product_id(client, catalog_admin, name="Attacker Lamp")
        victim_path = upload_url(client, catalog_admin, victim).json()["storage_path"]
        fake_storage.simulate_upload(victim_path)

        response = confirm_image(client, catalog_admin, attacker, victim_path)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "IMAGE_UPLOAD_NOT_FOUND"
        assert fake_storage.existence_checks == []

    def test_an_arbitrary_path_is_refused_without_touching_storage(
        self, client, catalog_admin, fake_storage
    ):
        product = product_id(client, catalog_admin)

        response = confirm_image(client, catalog_admin, product, "etc/passwd")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "IMAGE_UPLOAD_NOT_FOUND"
        assert fake_storage.existence_checks == []

    def test_a_reserved_path_with_nothing_uploaded_yet_is_refused(
        self, client, catalog_admin, fake_storage, test_engine
    ):
        product = product_id(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]

        response = confirm_image(client, catalog_admin, product, path)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "IMAGE_UPLOAD_NOT_FOUND"
        # The path matched, so storage was consulted - and said no.
        assert fake_storage.existence_checks == [path]
        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM product_images")
            ).scalar_one() == 0

    def test_a_matching_path_with_an_uploaded_object_creates_exactly_one_row(
        self, client, catalog_admin, fake_storage, test_engine
    ):
        product = product_id(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)

        response = confirm_image(
            client, catalog_admin, product, path, alt_text="A brass desk lamp"
        )

        assert response.status_code == 201
        body = response.json()
        assert body["storage_path"] == path
        assert body["alt_text"] == "A brass desk lamp"
        assert body["url"].startswith("https://storage.test/signed/")
        assert path in fake_storage.delivery_calls
        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM product_images WHERE product_id = :p"),
                {"p": product},
            ).scalar_one() == 1

    def test_confirming_the_same_path_twice_is_refused(
        self, client, catalog_admin, fake_storage
    ):
        product = product_id(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        confirm_image(client, catalog_admin, product, path)

        response = confirm_image(client, catalog_admin, product, path)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "IMAGE_ALREADY_CONFIRMED"

    def test_an_unknown_product_is_reported(self, client, catalog_admin, fake_storage):
        response = confirm_image(
            client, catalog_admin, uuid.uuid4(), "products/x/images/y"
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


class TestPrimaryAndOrdering:
    def add_image(self, client, headers, fake_storage, product, **body) -> dict:
        path = upload_url(client, headers, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        return confirm_image(client, headers, product, path, **body).json()

    def test_a_second_primary_image_is_refused(self, client, catalog_admin, fake_storage):
        product = product_id(client, catalog_admin)
        self.add_image(client, catalog_admin, fake_storage, product, is_primary=True)

        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        response = confirm_image(client, catalog_admin, product, path, is_primary=True)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PRIMARY_IMAGE_ALREADY_SET"

    def test_many_secondary_images_are_fine_alongside_one_primary(
        self, client, catalog_admin, fake_storage
    ):
        product = product_id(client, catalog_admin)
        self.add_image(client, catalog_admin, fake_storage, product, is_primary=True)
        for _ in range(3):
            self.add_image(client, catalog_admin, fake_storage, product)

        body = client.get(f"{PRODUCTS}/{product}", headers=catalog_admin).json()

        assert len(body["images"]) == 4
        assert sum(image["is_primary"] for image in body["images"]) == 1

    def test_the_gallery_is_returned_in_sort_order(
        self, client, catalog_admin, fake_storage
    ):
        product = product_id(client, catalog_admin)
        self.add_image(client, catalog_admin, fake_storage, product, sort_order=2, alt_text="third")
        self.add_image(client, catalog_admin, fake_storage, product, sort_order=0, alt_text="first")
        self.add_image(client, catalog_admin, fake_storage, product, sort_order=1, alt_text="second")

        body = client.get(f"{PRODUCTS}/{product}", headers=catalog_admin).json()

        assert [image["alt_text"] for image in body["images"]] == ["first", "second", "third"]


class TestDeliveryUrls:
    def test_a_signed_url_is_generated_once_per_image_in_a_response(
        self, client, catalog_admin, fake_storage
    ):
        product = product_id(client, catalog_admin)
        paths = []
        for _ in range(3):
            path = upload_url(client, catalog_admin, product).json()["storage_path"]
            fake_storage.simulate_upload(path)
            confirm_image(client, catalog_admin, product, path)
            paths.append(path)
        fake_storage.delivery_calls.clear()

        body = client.get(f"{PRODUCTS}/{product}", headers=catalog_admin).json()

        assert sorted(fake_storage.delivery_calls) == sorted(paths)
        # Opaque and time-limited in reality, so only the fact that the
        # generated value reaches the response is asserted.
        assert all(image["url"].startswith("https://storage.test/signed/") for image in body["images"])

    def test_a_fresh_url_is_generated_on_every_request(
        self, client, catalog_admin, fake_storage
    ):
        """Nothing is cached: a stored URL would expire and start failing."""
        product = product_id(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        confirm_image(client, catalog_admin, product, path)
        fake_storage.delivery_calls.clear()

        first = client.get(f"{PRODUCTS}/{product}", headers=catalog_admin).json()
        second = client.get(f"{PRODUCTS}/{product}", headers=catalog_admin).json()

        assert fake_storage.delivery_calls == [path, path]
        assert first["images"][0]["url"] != second["images"][0]["url"]

    def test_no_url_is_stored_in_the_database(
        self, client, catalog_admin, fake_storage, test_engine
    ):
        product = product_id(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        confirm_image(client, catalog_admin, product, path)

        with test_engine.connect() as connection:
            columns = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'product_images'"
                )
            ).scalars().all()

        assert "url" not in columns
        assert "storage_path" in columns


class TestDeletion:
    def test_the_file_is_removed_before_the_row(
        self, client, catalog_admin, fake_storage, test_engine
    ):
        product = product_id(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        image = confirm_image(client, catalog_admin, product, path).json()["id"]

        response = delete_image(client, catalog_admin, product, image)

        assert response.status_code == 204
        assert fake_storage.deleted == [path]
        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM product_images WHERE id = :i"), {"i": image}
            ).scalar_one() == 0

    def test_deleting_through_another_products_url_is_refused(
        self, client, catalog_admin, fake_storage, test_engine
    ):
        owner = product_id(client, catalog_admin, name="Owner Lamp")
        other = product_id(client, catalog_admin, name="Other Lamp")
        path = upload_url(client, catalog_admin, owner).json()["storage_path"]
        fake_storage.simulate_upload(path)
        image = confirm_image(client, catalog_admin, owner, path).json()["id"]

        response = delete_image(client, catalog_admin, other, image)

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "IMAGE_NOT_FOUND"
        # Nothing was touched in storage or in the database.
        assert fake_storage.deleted == []
        with test_engine.connect() as connection:
            assert connection.execute(
                text("SELECT count(*) FROM product_images WHERE id = :i"), {"i": image}
            ).scalar_one() == 1

    def test_an_unknown_image_is_reported(self, client, catalog_admin, fake_storage):
        product = product_id(client, catalog_admin)

        response = delete_image(client, catalog_admin, product, uuid.uuid4())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "IMAGE_NOT_FOUND"


class TestAuditTrail:
    def test_image_events_are_recorded(
        self, client, catalog_admin, fake_storage, test_engine
    ):
        product = product_id(client, catalog_admin)
        path = upload_url(client, catalog_admin, product).json()["storage_path"]
        fake_storage.simulate_upload(path)
        image = confirm_image(client, catalog_admin, product, path).json()["id"]
        delete_image(client, catalog_admin, product, image)

        with test_engine.connect() as connection:
            events = connection.execute(text("SELECT event_type FROM audit_logs")).scalars().all()

        assert {"image.uploaded", "image.deleted"} <= set(events)
