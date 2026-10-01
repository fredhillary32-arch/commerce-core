"""Every SQLAlchemy model, imported so ``Base.metadata`` is complete.

Alembic's ``env.py`` imports ``app.db.base``; importing this package from
there is what makes autogenerate and the test-schema helpers see all tables.
"""

from app.db.models.address import Address
from app.db.models.audit import AuditLog
from app.db.models.cart import Cart, CartItem
from app.db.models.catalog import (
    Category,
    Product,
    ProductCategory,
    ProductImage,
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    VariantOptionValue,
)
from app.db.models.inventory import (
    Inventory,
    InventoryAdjustment,
    InventoryReservation,
    InventoryTransaction,
)
from app.db.models.order import Order, OrderItem
from app.db.models.rbac import (
    Permission,
    Role,
    RolePermission,
    UserPermissionOverride,
    UserRole,
)
from app.db.models.refresh_token import RefreshToken
from app.db.models.shipping import ShippingRule
from app.db.models.tax import TaxRate
from app.db.models.user import User

__all__ = [
    "Address",
    "AuditLog",
    "Cart",
    "CartItem",
    "Category",
    "Inventory",
    "InventoryAdjustment",
    "InventoryReservation",
    "InventoryTransaction",
    "Order",
    "OrderItem",
    "Permission",
    "Product",
    "ProductCategory",
    "ProductImage",
    "ProductOption",
    "ProductOptionValue",
    "ProductVariant",
    "RefreshToken",
    "Role",
    "RolePermission",
    "ShippingRule",
    "TaxRate",
    "User",
    "UserPermissionOverride",
    "UserRole",
    "VariantOptionValue",
]
