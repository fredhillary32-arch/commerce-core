"""Catalogue business rules: status lifecycles, uniqueness, visibility.

Two principles run through this module.

First, uniqueness is settled by PostgreSQL, not by a read-then-write in
Python. Every "is this already taken?" question is asked by attempting the
write and translating the resulting constraint violation, the same way
Phase 2 handles DUPLICATE_EMAIL. A pre-check would leave a window between
the check and the insert; the constraint has no such window.

Second, every status change goes through one transition table per entity
rather than scattered conditionals, so the set of legal moves is readable in
one place and a forbidden move fails the same way wherever it is attempted.
"""

import re
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import storage
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    constraint_name,
)
from app.db.models.catalog import (
    PRODUCT_ACTIVE,
    PRODUCT_ARCHIVED,
    PRODUCT_DISCONTINUED,
    PRODUCT_DRAFT,
    VARIANT_ACTIVE,
    VARIANT_DISCONTINUED,
    VARIANT_DRAFT,
    Category,
    Product,
    ProductImage,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    VariantOptionValue,
)
from app.db.models.user import User
from app.modules.audit import service as audit
from app.modules.audit.service import ClientInfo
from app.modules.catalog import repository

# The one place the catalogue looks outward. Phase 3 deferred inventory
# dependents to Phase 4, and the deletion guard is where they land.
from app.modules.inventory import repository as inventory_repository
from app.modules.orders import repository as orders_repository

# Which moves are legal, for products and for variants. Both tables encode
# the same conviction: withdrawing something from sale is easy, putting it
# back is not, and a DISCONTINUED thing never returns - a configuration that
# comes back to sale does so as a new record with a new identity.
PRODUCT_TRANSITIONS: dict[str, frozenset[str]] = {
    PRODUCT_DRAFT: frozenset({PRODUCT_ACTIVE, PRODUCT_ARCHIVED}),
    PRODUCT_ACTIVE: frozenset({PRODUCT_ARCHIVED, PRODUCT_DISCONTINUED}),
    # Archiving only hides a product, so re-publishing one is legitimate -
    # subject to the same active-variant requirement as the first publish.
    PRODUCT_ARCHIVED: frozenset({PRODUCT_ACTIVE, PRODUCT_DISCONTINUED}),
    PRODUCT_DISCONTINUED: frozenset(),
}

VARIANT_TRANSITIONS: dict[str, frozenset[str]] = {
    VARIANT_DRAFT: frozenset({VARIANT_ACTIVE}),
    VARIANT_ACTIVE: frozenset({VARIANT_DISCONTINUED}),
    VARIANT_DISCONTINUED: frozenset(),
}

# Statuses a customer may see. Archived and discontinued products keep their
# rows for history but are absent from every public endpoint.
PUBLIC_PRODUCT_STATUSES = (PRODUCT_ACTIVE,)

_SIGNATURE_DELIMITER = "|"
_SLUG_SEPARATOR_PATTERN = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Lower-case, punctuation and whitespace collapsed to single hyphens."""
    return _SLUG_SEPARATOR_PATTERN.sub("-", value.lower()).strip("-")


def _require_transition(
    current: str, target: str, transitions: dict[str, frozenset[str]], subject: str
) -> None:
    if target not in transitions.get(current, frozenset()):
        raise BusinessRuleError(
            f"A {subject} cannot move from {current} to {target}.",
            code="INVALID_STATUS_TRANSITION",
        )


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------


def create_product(
    db: Session,
    *,
    name: str,
    slug: str | None,
    description: str | None,
    actor: User,
    client: ClientInfo,
) -> Product:
    resolved_slug = slug if slug is not None else slugify(name)
    if not resolved_slug:
        raise BusinessRuleError(
            "A slug could not be derived from that name; supply one explicitly.",
            code="INVALID_SLUG",
        )

    try:
        product = repository.add_product(
            db, name=name, slug=resolved_slug, description=description
        )
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    audit.record_event(
        db,
        event_type=audit.PRODUCT_CREATED,
        actor_user_id=actor.id,
        client=client,
        details={"product_id": str(product.id), "slug": product.slug},
    )
    db.commit()
    db.refresh(product)
    return product


def update_product(
    db: Session,
    *,
    product_id: uuid.UUID,
    changes: dict[str, Any],
    actor: User,
    client: ClientInfo,
) -> Product:
    product = require_product(db, product_id)
    target_status = changes.pop("status", None)

    for field, value in changes.items():
        setattr(product, field, value)

    if target_status is not None and target_status != product.status:
        _transition_product(db, product, target_status, actor=actor, client=client)

    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.commit()
    db.refresh(product)
    return product


def _transition_product(
    db: Session, product: Product, target: str, *, actor: User, client: ClientInfo
) -> None:
    _require_transition(product.status, target, PRODUCT_TRANSITIONS, "product")

    if target == PRODUCT_ACTIVE and not repository.has_active_variant(db, product.id):
        # Checked here rather than at variant creation: what matters is the
        # state at the moment of publishing, and a product can lose its last
        # active variant long after the variant was made.
        raise BusinessRuleError(
            "A product needs at least one ACTIVE variant before it can be published.",
            code="PRODUCT_HAS_NO_ACTIVE_VARIANT",
        )

    product.status = target
    event = {
        PRODUCT_ACTIVE: audit.PRODUCT_PUBLISHED,
        PRODUCT_ARCHIVED: audit.PRODUCT_ARCHIVED,
        PRODUCT_DISCONTINUED: audit.PRODUCT_DISCONTINUED,
    }[target]
    audit.record_event(
        db,
        event_type=event,
        actor_user_id=actor.id,
        client=client,
        details={"product_id": str(product.id)},
    )


def delete_product(db: Session, *, product_id: uuid.UUID) -> None:
    """Physically remove a product that was never used for anything.

    Phase 4 added inventory to the dependents counted here. The count rarely
    changes the *decision* - any variant at all already blocks deletion, and
    inventory cannot exist without one - but it changes the answer the
    operator gets, which is the difference between "this has 1 stocked
    variant" and an opaque foreign-key error. The guarantee underneath is
    ``inventory.variant_id``, which is RESTRICT rather than CASCADE, so
    PostgreSQL refuses to drop a variant holding stock records even if this
    guard were removed.

    Phase 5 extended it a second time, with ordered variants. The same
    reasoning applies: ``order_items.variant_id`` is RESTRICT, so
    PostgreSQL already refuses; the count is what turns an opaque
    foreign-key error into "this has 1 ordered variant".
    """
    product = require_product(db, product_id)

    if product.status != PRODUCT_DRAFT:
        raise ConflictError(
            "Only a DRAFT product can be deleted; archive or discontinue it instead.",
            code="PRODUCT_HAS_DEPENDENTS",
        )

    dependents = {
        "variants": repository.count_variants(db, product.id),
        "categories": repository.count_category_assignments(db, product.id),
        "images": repository.count_images(db, product.id),
        # Non-zero stock in any bucket, any ledger history, or a live hold.
        # A provisioned-but-untouched variant records nothing worth keeping
        # and deliberately does not count.
        "stocked variants": inventory_repository.count_stocked_variants_of_product(
            db, product.id
        ),
        # A variant that has been sold is history, not inventory. Counted
        # separately from stock because the two say different things to an
        # operator: one can be written off, the other never can.
        "ordered variants": orders_repository.count_ordered_variants_of_product(
            db, product.id
        ),
    }
    if any(dependents.values()):
        raise ConflictError(
            "This product already has "
            + ", ".join(f"{count} {kind}" for kind, count in dependents.items() if count)
            + "; archive or discontinue it instead of deleting it.",
            code="PRODUCT_HAS_DEPENDENTS",
        )

    repository.delete_product(db, product)
    db.commit()


def require_product(db: Session, product_id: uuid.UUID) -> Product:
    product = repository.get_product(db, product_id)
    if product is None:
        raise NotFoundError("Product not found.", code="PRODUCT_NOT_FOUND")
    return product


# --------------------------------------------------------------------------
# Options and option values
# --------------------------------------------------------------------------


def create_option(db: Session, *, product_id: uuid.UUID, name: str) -> ProductOption:
    product = require_product(db, product_id)
    option = repository.add_option(db, product_id=product.id, name=name)
    db.commit()
    db.refresh(option)
    return option


def create_option_value(
    db: Session, *, product_id: uuid.UUID, option_id: uuid.UUID, value: str
) -> ProductOptionValue:
    product = require_product(db, product_id)
    option = _require_option_of_product(db, option_id, product.id)
    option_value = repository.add_option_value(db, option_id=option.id, value=value)
    db.commit()
    db.refresh(option_value)
    return option_value


def _require_option_of_product(
    db: Session, option_id: uuid.UUID, product_id: uuid.UUID
) -> ProductOption:
    option = repository.get_option(db, option_id)
    # An option belonging to a different product is reported as missing
    # rather than forbidden: from this product's perspective it does not
    # exist, and saying so leaks nothing about other products.
    if option is None or option.product_id != product_id:
        raise NotFoundError("Option not found for this product.", code="OPTION_NOT_FOUND")
    return option


# --------------------------------------------------------------------------
# Variants
# --------------------------------------------------------------------------


def create_variant(
    db: Session,
    *,
    product_id: uuid.UUID,
    sku: str,
    price: Decimal | None,
    currency: str | None,
    actor: User,
    client: ClientInfo,
) -> ProductVariant:
    product = require_product(db, product_id)

    try:
        variant = repository.add_variant(
            db, product_id=product.id, sku=sku, price=price, currency=currency
        )
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    audit.record_event(
        db,
        event_type=audit.VARIANT_CREATED,
        actor_user_id=actor.id,
        client=client,
        details={"product_id": str(product.id), "variant_id": str(variant.id), "sku": sku},
    )
    db.commit()
    db.refresh(variant)
    return variant


def update_variant(
    db: Session,
    *,
    variant_id: uuid.UUID,
    changes: dict[str, Any],
    actor: User,
    client: ClientInfo,
) -> ProductVariant:
    variant = require_variant(db, variant_id)
    target_status = changes.pop("status", None)

    for field, value in changes.items():
        setattr(variant, field, value)

    if target_status is not None and target_status != variant.status:
        _transition_variant(db, variant, target_status, actor=actor, client=client)

    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.commit()
    db.refresh(variant)
    return variant


def _transition_variant(
    db: Session, variant: ProductVariant, target: str, *, actor: User, client: ClientInfo
) -> None:
    _require_transition(variant.status, target, VARIANT_TRANSITIONS, "variant")

    if target == VARIANT_ACTIVE and (variant.price is None or variant.currency is None):
        # Both stay nullable while DRAFT so a variant can be configured
        # gradually; they become mandatory exactly when it becomes sellable.
        raise BusinessRuleError(
            "A variant needs both a price and a currency before it can be activated.",
            code="VARIANT_PRICE_REQUIRED",
        )

    variant.status = target

    if target == VARIANT_DISCONTINUED:
        # Discontinuing the last ACTIVE variant deliberately leaves the
        # product ACTIVE. Withdrawing a product from sale is a merchant's
        # decision, and silently unpublishing it here would hide that
        # decision inside an unrelated action.
        audit.record_event(
            db,
            event_type=audit.VARIANT_DISCONTINUED,
            actor_user_id=actor.id,
            client=client,
            details={"variant_id": str(variant.id), "product_id": str(variant.product_id)},
        )


def require_variant(db: Session, variant_id: uuid.UUID) -> ProductVariant:
    variant = repository.get_variant(db, variant_id)
    if variant is None:
        raise NotFoundError("Variant not found.", code="VARIANT_NOT_FOUND")
    return variant


# --------------------------------------------------------------------------
# Variant option assignments and the signature they maintain
# --------------------------------------------------------------------------


def compute_signature(assignments: list[VariantOptionValue]) -> str | None:
    """A deterministic fingerprint of a variant's option-value combination.

    Null for a variant with no assignments yet. PostgreSQL considers every
    null distinct under a unique constraint, which is exactly what lets any
    number of not-yet-configured draft variants sit on one product while two
    configured ones can never share a combination.
    """
    if not assignments:
        return None
    return _SIGNATURE_DELIMITER.join(
        f"{assignment.option_id}:{assignment.option_value_id}"
        for assignment in sorted(assignments, key=lambda item: str(item.option_id))
    )


def assign_option_value(
    db: Session,
    *,
    variant_id: uuid.UUID,
    option_id: uuid.UUID,
    option_value_id: uuid.UUID,
    actor: User,
    client: ClientInfo,
) -> ProductVariant:
    """Give a variant one option's value and re-fingerprint it, atomically.

    The assignment and the recomputed signature are written in the same
    transaction: a committed assignment with a stale signature would let a
    duplicate combination slip past the unique constraint.
    """
    variant = require_variant(db, variant_id)
    option = _require_option_of_product(db, option_id, variant.product_id)
    _require_value_of_option(db, option_value_id, option.id)

    try:
        repository.add_assignment(
            db, variant_id=variant.id, option_id=option.id, option_value_id=option_value_id
        )
        variant.variant_signature = compute_signature(
            repository.list_assignments(db, variant.id)
        )
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.commit()
    db.refresh(variant)
    return variant


def _require_value_of_option(
    db: Session, option_value_id: uuid.UUID, option_id: uuid.UUID
) -> ProductOptionValue:
    option_value = repository.get_option_value(db, option_value_id)
    if option_value is None or option_value.option_id != option_id:
        raise NotFoundError(
            "Option value not found for that option.", code="OPTION_VALUE_NOT_FOUND"
        )
    return option_value


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------


def create_category(
    db: Session,
    *,
    name: str,
    slug: str | None,
    parent_id: uuid.UUID | None,
    actor: User,
    client: ClientInfo,
) -> Category:
    resolved_slug = slug if slug is not None else slugify(name)
    if not resolved_slug:
        raise BusinessRuleError(
            "A slug could not be derived from that name; supply one explicitly.",
            code="INVALID_SLUG",
        )
    if parent_id is not None:
        require_category(db, parent_id)

    try:
        category = repository.add_category(
            db, name=name, slug=resolved_slug, parent_id=parent_id
        )
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    audit.record_event(
        db,
        event_type=audit.CATEGORY_CREATED,
        actor_user_id=actor.id,
        client=client,
        details={"category_id": str(category.id), "slug": category.slug},
    )
    db.commit()
    db.refresh(category)
    return category


def update_category(
    db: Session,
    *,
    category_id: uuid.UUID,
    changes: dict[str, Any],
    actor: User,
    client: ClientInfo,
) -> Category:
    category = require_category(db, category_id)
    # Only fields the caller actually sent reach here, so the presence of the
    # key - not its value - is what distinguishes "move to the root" from
    # "leave the parent alone".
    parent_supplied = "parent_id" in changes
    new_parent_id = changes.pop("parent_id", None)
    new_is_active = changes.pop("is_active", None)

    for field, value in changes.items():
        setattr(category, field, value)

    if parent_supplied and new_parent_id != category.parent_id:
        _reparent(db, category, new_parent_id, actor=actor, client=client)

    if new_is_active is not None and new_is_active != category.is_active:
        category.is_active = new_is_active
        if not new_is_active:
            audit.record_event(
                db,
                event_type=audit.CATEGORY_ARCHIVED,
                actor_user_id=actor.id,
                client=client,
                details={"category_id": str(category.id)},
            )

    try:
        db.flush()
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.commit()
    db.refresh(category)
    return category


def _reparent(
    db: Session,
    category: Category,
    new_parent_id: uuid.UUID | None,
    *,
    actor: User,
    client: ClientInfo,
) -> None:
    """Move a category, refusing any move that would close a loop.

    No practical PostgreSQL constraint expresses "this edge must not create a
    cycle" over an adjacency list, so the ancestor chain is walked here. The
    walk also catches a category being made its own parent, which is simply
    the shortest possible cycle.
    """
    if new_parent_id is not None:
        ancestor_id: uuid.UUID | None = new_parent_id
        while ancestor_id is not None:
            if ancestor_id == category.id:
                raise BusinessRuleError(
                    "That parent is the category itself or one of its descendants.",
                    code="INVALID_CATEGORY_PARENT",
                )
            ancestor_id = require_category(db, ancestor_id).parent_id

    category.parent_id = new_parent_id
    audit.record_event(
        db,
        event_type=audit.CATEGORY_PARENT_CHANGED,
        actor_user_id=actor.id,
        client=client,
        details={
            "category_id": str(category.id),
            "parent_id": str(new_parent_id) if new_parent_id else None,
        },
    )


def delete_category(db: Session, *, category_id: uuid.UUID) -> None:
    """Remove a category that was never used. Anything else is deactivated."""
    category = require_category(db, category_id)

    children = repository.count_child_categories(db, category.id)
    products = repository.count_products_in_category(db, category.id)
    if children or products:
        raise ConflictError(
            "This category has child categories or assigned products; "
            "deactivate it instead of deleting it.",
            code="CATEGORY_HAS_DEPENDENTS",
        )

    repository.delete_category(db, category)
    db.commit()


def assign_product_to_category(
    db: Session, *, product_id: uuid.UUID, category_id: uuid.UUID
) -> None:
    product = require_product(db, product_id)
    category = require_category(db, category_id)

    try:
        repository.add_product_category(db, product_id=product.id, category_id=category.id)
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    db.commit()


def require_category(db: Session, category_id: uuid.UUID) -> Category:
    category = repository.get_category(db, category_id)
    if category is None:
        raise NotFoundError("Category not found.", code="CATEGORY_NOT_FOUND")
    return category


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


def create_image_upload_target(db: Session, *, product_id: uuid.UUID) -> tuple[str, str]:
    """Reserve a storage path for a product image. No database row yet.

    A row is only written once the object is known to exist, so a client that
    requests a target and never uploads leaves nothing behind.
    """
    product = require_product(db, product_id)
    return storage.create_upload_target(product.id)


def confirm_image(
    db: Session,
    *,
    product_id: uuid.UUID,
    storage_path: str,
    alt_text: str | None,
    sort_order: int,
    is_primary: bool,
    actor: User,
    client: ClientInfo,
) -> ProductImage:
    """Record an uploaded object, once it is proven to be this product's.

    The path check comes first and costs nothing: because the server invents
    every storage path and Supabase binds each signed upload URL to the exact
    path it was issued for, an object can only exist under this prefix if
    someone requested a target for *this* product. Confirming that the object
    is really there then closes the remaining case - a path that was reserved
    but never uploaded to.
    """
    product = require_product(db, product_id)

    if not storage_path.startswith(storage.image_prefix(product.id)):
        raise NotFoundError(
            "No uploaded image was found at that path for this product.",
            code="IMAGE_UPLOAD_NOT_FOUND",
        )

    if not storage.object_exists(storage_path):
        raise NotFoundError(
            "No uploaded image was found at that path for this product.",
            code="IMAGE_UPLOAD_NOT_FOUND",
        )

    try:
        image = repository.add_image(
            db,
            product_id=product.id,
            storage_path=storage_path,
            alt_text=alt_text,
            sort_order=sort_order,
            is_primary=is_primary,
        )
    except IntegrityError as error:
        db.rollback()
        raise _translate(error) from None

    audit.record_event(
        db,
        event_type=audit.IMAGE_UPLOADED,
        actor_user_id=actor.id,
        client=client,
        details={"product_id": str(product.id), "image_id": str(image.id)},
    )
    db.commit()
    db.refresh(image)
    return image


def delete_image(
    db: Session,
    *,
    product_id: uuid.UUID,
    image_id: uuid.UUID,
    actor: User,
    client: ClientInfo,
) -> None:
    """Delete the file, then the row - in that order, deliberately.

    If the row delete fails after the file is gone, the gallery shows a
    broken image until someone tidies up. The reverse ordering would leave an
    orphaned file that nothing references and nobody ever notices, which is
    the worse failure of the two.
    """
    require_product(db, product_id)
    image = repository.get_image(db, image_id)
    if image is None or image.product_id != product_id:
        raise NotFoundError("Image not found for this product.", code="IMAGE_NOT_FOUND")

    storage.delete_object(image.storage_path)
    repository.remove_image(db, image.id)
    audit.record_event(
        db,
        event_type=audit.IMAGE_DELETED,
        actor_user_id=actor.id,
        client=client,
        details={"product_id": str(product_id), "image_id": str(image_id)},
    )
    db.commit()


# --------------------------------------------------------------------------
# Read models
# --------------------------------------------------------------------------


def build_product_view(db: Session, product: Product, *, public: bool) -> dict[str, Any]:
    """Assemble a product and everything hanging off it.

    ``public`` narrows the result to what a customer may see - active
    variants and active categories only - and drops storage paths. The same
    assembly serves both so the two views cannot drift apart.
    """
    variants = repository.list_variants(
        db, product.id, statuses=(VARIANT_ACTIVE,) if public else None
    )
    assignments_by_variant = _assignments_by_variant(db, [variant.id for variant in variants])

    values_by_option: dict[uuid.UUID, list[ProductOptionValue]] = {}
    for option_value in repository.list_option_values_for_product(db, product.id):
        values_by_option.setdefault(option_value.option_id, []).append(option_value)

    view: dict[str, Any] = {
        "id": product.id,
        "name": product.name,
        "slug": product.slug,
        "description": product.description,
        "options": [
            {
                "id": option.id,
                "name": option.name,
                "values": values_by_option.get(option.id, []),
            }
            for option in repository.list_options(db, product.id)
        ],
        "variants": [
            _variant_view(variant, assignments_by_variant[variant.id]) for variant in variants
        ],
        "images": [
            image_view(image, public=public) for image in repository.list_images(db, product.id)
        ],
        "categories": repository.list_categories_for_product(db, product.id, active_only=public),
    }

    if not public:
        view |= {
            "status": product.status,
            "created_at": product.created_at,
            "updated_at": product.updated_at,
        }
    return view


def build_variant_view(db: Session, variant: ProductVariant) -> dict[str, Any]:
    """One variant with its option values, for the endpoints that return one."""
    return _variant_view(variant, _assignments_by_variant(db, [variant.id])[variant.id])


def _assignments_by_variant(
    db: Session, variant_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Option values for many variants in one query, keyed by variant."""
    grouped: dict[uuid.UUID, list[dict[str, Any]]] = {
        variant_id: [] for variant_id in variant_ids
    }
    for row in repository.list_assignment_details(db, variant_ids):
        grouped[row.variant_id].append(
            {
                "option_id": row.option_id,
                "option_name": row.option_name,
                "option_value_id": row.option_value_id,
                "value": row.value,
            }
        )
    return grouped


def _variant_view(
    variant: ProductVariant, option_values: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "id": variant.id,
        "sku": variant.sku,
        "status": variant.status,
        "price": variant.price,
        "currency": variant.currency,
        "option_values": option_values,
    }


def image_view(image: ProductImage, *, public: bool) -> dict[str, Any]:
    # Signed per response, never stored: every URL expires, so a cached one
    # would eventually start returning 403 to customers.
    view = {
        "id": image.id,
        "url": storage.get_delivery_url(image.storage_path),
        "alt_text": image.alt_text,
        "sort_order": image.sort_order,
        "is_primary": image.is_primary,
    }
    if not public:
        view["storage_path"] = image.storage_path
    return view


def list_public_products(db: Session, *, limit: int, offset: int) -> list[dict[str, Any]]:
    products = repository.list_products(
        db, statuses=PUBLIC_PRODUCT_STATUSES, limit=limit, offset=offset
    )
    summaries = []
    for product in products:
        primary = repository.get_primary_image(db, product.id)
        summaries.append(
            {
                "id": product.id,
                "name": product.name,
                "slug": product.slug,
                "description": product.description,
                "primary_image_url": (
                    storage.get_delivery_url(primary.storage_path) if primary else None
                ),
            }
        )
    return summaries


def get_public_product(db: Session, product_id: uuid.UUID) -> dict[str, Any]:
    product = repository.get_product(db, product_id)
    # A product that exists but is not published is reported as missing: a
    # public caller has no business distinguishing "no such product" from
    # "not published yet".
    if product is None or product.status not in PUBLIC_PRODUCT_STATUSES:
        raise NotFoundError("Product not found.", code="PRODUCT_NOT_FOUND")
    return build_product_view(db, product, public=True)


def list_public_categories(db: Session) -> list[Category]:
    return repository.list_categories(db, active_only=True)


def get_public_category(db: Session, category_id: uuid.UUID) -> dict[str, Any]:
    category = repository.get_category(db, category_id)
    if category is None or not category.is_active:
        raise NotFoundError("Category not found.", code="CATEGORY_NOT_FOUND")
    return {
        "id": category.id,
        "name": category.name,
        "slug": category.slug,
        "parent_id": category.parent_id,
        "product_ids": repository.list_active_product_ids_in_category(db, category.id),
    }


# --------------------------------------------------------------------------
# Constraint-violation translation
# --------------------------------------------------------------------------

# Every uniqueness rule in the catalogue is enforced by one of these, and the
# error a caller sees is decided by which one PostgreSQL named.
_CONFLICTS: dict[str, tuple[str, str]] = {
    "uq_products_slug": ("DUPLICATE_SLUG", "That product slug is already in use."),
    "uq_categories_slug": ("DUPLICATE_SLUG", "That category slug is already in use."),
    "uq_product_variants_sku": ("DUPLICATE_SKU", "That SKU is already in use."),
    "uq_product_variants_product_id_signature": (
        "DUPLICATE_VARIANT_COMBINATION",
        "This product already has a variant with that combination of option values.",
    ),
    "uq_variant_option_values_variant_id_option_id": (
        "OPTION_ALREADY_ASSIGNED",
        "This variant already has a value for that option.",
    ),
    "uq_product_images_one_primary_per_product": (
        "PRIMARY_IMAGE_ALREADY_SET",
        "This product already has a primary image.",
    ),
    "uq_product_images_storage_path": (
        "IMAGE_ALREADY_CONFIRMED",
        "An image has already been recorded for that storage path.",
    ),
    "pk_product_categories": (
        "CATEGORY_ALREADY_ASSIGNED",
        "This product is already assigned to that category.",
    ),
}


def _translate(error: IntegrityError) -> Exception:
    conflict = _CONFLICTS.get(constraint_name(error))
    if conflict is None:
        return error
    code, message = conflict
    return ConflictError(message, code=code)
