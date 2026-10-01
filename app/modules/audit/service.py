"""Append security-relevant events to audit_logs.

Events never carry credentials, password hashes or raw tokens - only
identifiers and short, non-sensitive context.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.db.models.audit import AuditLog

LOGIN_SUCCEEDED = "auth.login.succeeded"
LOGIN_FAILED = "auth.login.failed"
LOGOUT = "auth.logout"
PASSWORD_CHANGED = "auth.password.changed"
REFRESH_TOKEN_REUSED = "auth.refresh_token.reused"
ACCOUNT_REGISTERED = "account.registered"
ACCOUNT_ENABLED = "account.enabled"
ACCOUNT_DISABLED = "account.disabled"
ROLE_ASSIGNED = "rbac.role.assigned"
ROLE_REMOVED = "rbac.role.removed"
ROLE_CREATED = "rbac.role.created"
ROLE_PERMISSION_GRANTED = "rbac.role.permission_granted"
PERMISSION_OVERRIDE_SET = "rbac.override.set"
PERMISSION_OVERRIDE_REMOVED = "rbac.override.removed"
ADMIN_BOOTSTRAPPED = "rbac.bootstrap.admin_assigned"

# Catalogue events. Deliberately only meaningful state changes - renaming a
# product or fixing its description is not worth an audit row, but publishing
# it, withdrawing it or touching its imagery is.
PRODUCT_CREATED = "product.created"
PRODUCT_PUBLISHED = "product.published"
PRODUCT_ARCHIVED = "product.archived"
PRODUCT_DISCONTINUED = "product.discontinued"
VARIANT_CREATED = "variant.created"
VARIANT_DISCONTINUED = "variant.discontinued"
CATEGORY_CREATED = "category.created"
CATEGORY_ARCHIVED = "category.archived"
CATEGORY_PARENT_CHANGED = "category.parent_changed"
IMAGE_UPLOADED = "image.uploaded"
IMAGE_DELETED = "image.deleted"

# Cart events. Only the three mutations - reading a cart is not auditable
# activity, and auditing it would bury the changes under the browsing.
CART_ITEM_ADDED = "cart.item_added"
CART_ITEM_UPDATED = "cart.item_updated"
CART_ITEM_REMOVED = "cart.item_removed"

# Inventory events. Stock is the thing most worth being able to reconstruct
# after the fact: every movement of it, and every hold placed on it, leaves
# a row here in addition to the ledger or the reservation record itself.
INVENTORY_PROVISIONED = "inventory.provisioned"
INVENTORY_ADJUSTED = "inventory.adjusted"
INVENTORY_RESERVED = "inventory.reserved"
INVENTORY_RELEASED = "inventory.released"
INVENTORY_EXPIRED = "inventory.expired"
INVENTORY_CONVERTED = "inventory.converted"

# Order events. As elsewhere, only state changes: reading an order is not
# auditable activity. Reservation events are already emitted by the
# inventory service when checkout, confirmation and cancellation move
# stock, and are deliberately not duplicated here.
ORDER_PLACED = "order.placed"
ORDER_CONFIRMED = "order.confirmed"
ORDER_CANCELLED = "order.cancelled"
ADDRESS_CREATED = "address.created"
ADDRESS_DELETED = "address.deleted"
TAX_RATE_CHANGED = "tax_rate.changed"
SHIPPING_RULE_CHANGED = "shipping_rule.changed"


@dataclass(frozen=True)
class ClientInfo:
    """Where a request came from, for audit records and refresh-token rows."""

    ip_address: str | None = None
    user_agent: str | None = None

    @classmethod
    def from_request(cls, request: Request) -> "ClientInfo":
        return cls(
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )


def record_event(
    db: Session,
    *,
    event_type: str,
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    client: ClientInfo | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the caller's transaction; the caller commits."""
    client = client or ClientInfo()
    db.add(
        AuditLog(
            event_type=event_type,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            ip_address=client.ip_address,
            user_agent=client.user_agent,
            details=details or {},
        )
    )
