"""Every cart database query. No business rules live here."""

import uuid

from sqlalchemy import Row, delete, select
from sqlalchemy.orm import Session

from app.db.models.cart import CART_ACTIVE, Cart, CartItem
from app.db.models.catalog import Product, ProductVariant


def get_active_cart(db: Session, user_id: uuid.UUID) -> Cart | None:
    return db.execute(
        select(Cart).where(Cart.user_id == user_id, Cart.status == CART_ACTIVE)
    ).scalar_one_or_none()


def get_cart(db: Session, cart_id: uuid.UUID) -> Cart | None:
    """One cart by id, whatever its status. No ownership check - callers do that."""
    return db.get(Cart, cart_id)


def lock_active_cart(db: Session, user_id: uuid.UUID) -> Cart | None:
    """The caller's live cart, locked for the rest of the transaction.

    Checkout takes this lock before it reads a single line, which closes two
    windows at once. A customer adding an item in another tab between the
    read and the cart's transition to CONVERTED would otherwise have that
    item silently dropped; and two concurrent checkouts of the same cart
    serialize here rather than racing to insert two orders.

    The second one is worth spelling out. Under READ COMMITTED the loser
    re-evaluates ``status = 'ACTIVE'`` against the committed row once the
    lock is released, finds the cart is now CONVERTED, and gets nothing
    back - which is exactly the state checkout treats as "you have already
    checked this basket out".
    """
    return db.execute(
        select(Cart)
        .where(Cart.user_id == user_id, Cart.status == CART_ACTIVE)
        .with_for_update()
    ).scalar_one_or_none()


def add_cart(db: Session, *, user_id: uuid.UUID) -> Cart:
    cart = Cart(user_id=user_id)
    db.add(cart)
    db.flush()
    return cart


def get_item(db: Session, item_id: uuid.UUID) -> CartItem | None:
    return db.get(CartItem, item_id)


def get_item_by_variant(
    db: Session, *, cart_id: uuid.UUID, variant_id: uuid.UUID
) -> CartItem | None:
    return db.execute(
        select(CartItem).where(CartItem.cart_id == cart_id, CartItem.variant_id == variant_id)
    ).scalar_one_or_none()


def add_item(db: Session, *, cart_id: uuid.UUID, variant_id: uuid.UUID, quantity: int) -> CartItem:
    item = CartItem(cart_id=cart_id, variant_id=variant_id, quantity=quantity)
    db.add(item)
    db.flush()
    return item


def remove_item(db: Session, item: CartItem) -> None:
    db.delete(item)


def clear_items(db: Session, cart_id: uuid.UUID) -> int:
    return db.execute(delete(CartItem).where(CartItem.cart_id == cart_id)).rowcount


def list_item_details(db: Session, cart_id: uuid.UUID) -> list[Row]:
    """Every item in one cart with the catalogue facts needed to display it.

    One join rather than a query per item: a cart response would otherwise
    cost two extra round trips for every line, and the statuses it reads are
    exactly what decides whether a line is still purchasable.
    """
    return list(
        db.execute(
            select(
                CartItem.id,
                CartItem.variant_id,
                CartItem.quantity,
                CartItem.created_at,
                CartItem.updated_at,
                ProductVariant.sku,
                ProductVariant.status.label("variant_status"),
                ProductVariant.price,
                ProductVariant.currency,
                Product.id.label("product_id"),
                Product.name.label("product_name"),
                Product.status.label("product_status"),
            )
            .join(ProductVariant, ProductVariant.id == CartItem.variant_id)
            .join(Product, Product.id == ProductVariant.product_id)
            .where(CartItem.cart_id == cart_id)
            .order_by(CartItem.created_at, CartItem.id)
        )
    )
