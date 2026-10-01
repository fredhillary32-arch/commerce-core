"""The Supabase Storage wrapper, with the Supabase client itself stubbed.

No test in this suite reaches Supabase. What is checked here is the wrapper's
own behaviour: the paths it invents, the arguments it passes, and the fact
that a storage outage becomes a 503 rather than an opaque 500.
"""

import uuid

import pytest

from app.core import storage
from app.core.config import get_settings
from app.core.exceptions import InfrastructureError


class StubBucket:
    """Records calls and returns Supabase-shaped payloads."""

    def __init__(self, listing=None, failing=False):
        self.listing = listing if listing is not None else []
        self.failing = failing
        self.calls: list[tuple] = []

    def _record(self, *call):
        self.calls.append(call)
        if self.failing:
            raise RuntimeError("supabase is unreachable")

    def create_signed_upload_url(self, path):
        self._record("create_signed_upload_url", path)
        return {"signed_url": f"https://supabase.test/upload/{path}?token=abc", "path": path}

    def list(self, directory, options):
        self._record("list", directory, options)
        return self.listing

    def remove(self, paths):
        self._record("remove", tuple(paths))
        return []

    def create_signed_url(self, path, expires_in):
        self._record("create_signed_url", path, expires_in)
        return {"signedURL": f"https://supabase.test/signed/{path}?token=xyz"}


@pytest.fixture()
def bucket(monkeypatch):
    stub = StubBucket()
    monkeypatch.setattr(storage, "_bucket", lambda: stub)
    return stub


def test_the_prefix_is_scoped_to_one_product():
    product_id = uuid.uuid4()

    assert storage.image_prefix(product_id) == f"products/{product_id}/images/"


class TestUploadTarget:
    def test_the_server_invents_a_path_under_the_products_prefix(self, bucket):
        product_id = uuid.uuid4()

        path, url = storage.create_upload_target(product_id)

        assert path.startswith(f"products/{product_id}/images/")
        assert url.startswith("https://supabase.test/upload/")
        assert bucket.calls == [("create_signed_upload_url", path)]

    def test_two_targets_never_collide(self, bucket):
        product_id = uuid.uuid4()

        first, _ = storage.create_upload_target(product_id)
        second, _ = storage.create_upload_target(product_id)

        assert first != second


class TestObjectExists:
    def test_a_listed_object_with_the_exact_name_exists(self, monkeypatch):
        path = "products/abc/images/def"
        stub = StubBucket(listing=[{"name": "def"}])
        monkeypatch.setattr(storage, "_bucket", lambda: stub)

        assert storage.object_exists(path) is True
        assert stub.calls == [("list", "products/abc/images", {"search": "def", "limit": 1})]

    def test_an_empty_listing_means_nothing_was_uploaded(self, bucket):
        assert storage.object_exists("products/abc/images/def") is False

    def test_a_prefix_match_is_not_an_exact_match(self, monkeypatch):
        """`search` matches prefixes, so the name is compared exactly."""
        stub = StubBucket(listing=[{"name": "def-thumbnail"}])
        monkeypatch.setattr(storage, "_bucket", lambda: stub)

        assert storage.object_exists("products/abc/images/def") is False


class TestDeleteAndDeliver:
    def test_deletion_passes_the_single_path(self, bucket):
        storage.delete_object("products/abc/images/def")

        assert bucket.calls == [("remove", ("products/abc/images/def",))]

    def test_a_delivery_url_is_signed_with_the_configured_expiry(self, bucket):
        url = storage.get_delivery_url("products/abc/images/def")

        assert url.startswith("https://supabase.test/signed/")
        assert bucket.calls == [
            (
                "create_signed_url",
                "products/abc/images/def",
                get_settings().supabase_signed_url_expiry_seconds,
            )
        ]

    def test_the_expiry_is_configuration_not_a_hard_coded_constant(self, bucket, monkeypatch):
        get_settings.cache_clear()
        monkeypatch.setenv("SUPABASE_SIGNED_URL_EXPIRY_SECONDS", "90")
        try:
            storage.get_delivery_url("products/abc/images/def")
            assert bucket.calls[0][2] == 90
        finally:
            get_settings.cache_clear()


class TestFailureHandling:
    @pytest.mark.parametrize(
        "operation,arguments",
        [
            (storage.create_upload_target, (uuid.uuid4(),)),
            (storage.object_exists, ("products/abc/images/def",)),
            (storage.delete_object, ("products/abc/images/def",)),
            (storage.get_delivery_url, ("products/abc/images/def",)),
        ],
    )
    def test_a_storage_outage_is_a_503_not_a_500(self, monkeypatch, operation, arguments):
        monkeypatch.setattr(storage, "_bucket", lambda: StubBucket(failing=True))

        with pytest.raises(InfrastructureError) as raised:
            operation(*arguments)

        assert raised.value.status_code == 503
        assert raised.value.code == "STORAGE_UNAVAILABLE"


class TestConfiguration:
    def test_supabase_credentials_are_required_with_no_default(self, monkeypatch):
        """Same treatment as JWT_SECRET_KEY: a missing value fails at startup."""
        from pydantic import ValidationError

        from app.core.config import Settings

        fields = Settings.model_fields
        assert fields["supabase_url"].is_required()
        assert fields["supabase_service_key"].is_required()

        monkeypatch.delenv("SUPABASE_URL", raising=False)
        with pytest.raises(ValidationError, match="supabase_url"):
            Settings(_env_file=None, jwt_secret_key="x", supabase_service_key="y")

    def test_the_expiry_and_bucket_are_plain_configuration_with_defaults(self):
        from app.core.config import Settings

        assert Settings.model_fields["supabase_signed_url_expiry_seconds"].default == 3600
        assert Settings.model_fields["supabase_product_image_bucket"].default == "product-images"
