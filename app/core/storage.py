"""Supabase Storage access for product image files.

Image bytes never enter PostgreSQL; the database stores only the
``storage_path`` of an object in a *private* Supabase bucket. Because the
bucket is private there is no permanent public URL to construct or cache, so
delivery goes through a short-lived signed URL generated fresh on every read.

Deliberately not a provider-agnostic storage abstraction: this is one
concrete integration with exactly the four operations the catalogue needs.
"""

import uuid
from collections.abc import Callable
from functools import lru_cache
from typing import TypeVar

from storage3._sync.file_api import SyncBucketProxy
from supabase import Client, create_client

from app.core.config import get_settings
from app.core.exceptions import InfrastructureError

T = TypeVar("T")


@lru_cache
def _bucket() -> SyncBucketProxy:
    """The configured private bucket, built once per process.

    Authenticated with the service-role key, which stays server-side: clients
    only ever receive the signed URLs produced below.
    """
    settings = get_settings()
    client: Client = create_client(settings.supabase_url, settings.supabase_service_key)
    return client.storage.from_(settings.supabase_product_image_bucket)


def _call(operation: str, action: Callable[[SyncBucketProxy], T]) -> T:
    """Run one bucket operation, reporting failures as infrastructure errors.

    Without this the Supabase SDK's own exception would fall through to the
    catch-all handler as an opaque 500; a storage outage is a 503.
    """
    try:
        return action(_bucket())
    except Exception as exc:  # noqa: BLE001 - any SDK/transport failure is a 503
        raise InfrastructureError(
            f"Object storage is unavailable ({operation}).", code="STORAGE_UNAVAILABLE"
        ) from exc


def image_prefix(product_id: uuid.UUID) -> str:
    """The only directory under which a product's images may ever live."""
    return f"products/{product_id}/images/"


def create_upload_target(product_id: uuid.UUID) -> tuple[str, str]:
    """Reserve a path for a new image and return ``(storage_path, upload_url)``.

    The server picks the path; the client never supplies or influences it. The
    signed URL Supabase returns is bound to this exact path, which is what
    makes the prefix check at confirm time sufficient to stop an object
    uploaded for one product being claimed by another.
    """
    storage_path = f"{image_prefix(product_id)}{uuid.uuid4()}"
    target = _call(
        "create upload target", lambda bucket: bucket.create_signed_upload_url(storage_path)
    )
    return storage_path, target["signed_url"]


def object_exists(storage_path: str) -> bool:
    """Whether an object has actually been uploaded to ``storage_path``."""
    directory, _, name = storage_path.rpartition("/")
    entries = _call(
        "check object", lambda bucket: bucket.list(directory, {"search": name, "limit": 1})
    )
    # `search` is a prefix match, so the name is compared exactly here.
    return any(entry.get("name") == name for entry in entries)


def delete_object(storage_path: str) -> None:
    _call("delete object", lambda bucket: bucket.remove([storage_path]))


def get_delivery_url(storage_path: str) -> str:
    """Mint a short-lived signed read URL for a private object.

    Called afresh for every response that needs it. The result expires after
    ``SUPABASE_SIGNED_URL_EXPIRY_SECONDS`` and must not be stored or cached.
    """
    expiry = get_settings().supabase_signed_url_expiry_seconds
    signed = _call(
        "create delivery url", lambda bucket: bucket.create_signed_url(storage_path, expiry)
    )
    return signed["signedURL"]
