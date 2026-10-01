"""seed order permissions

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-27

Registers the four administrative permission codes Phase 5 introduces. As
in 0003, 0005 and 0007 they are granted to **no role**: who may look at
another customer's order, cancel one, or set a tax rate are business
decisions, not something a migration should assume.

Four codes rather than fewer, and none implying another:

* ``orders.read`` and ``orders.manage`` are separate because viewing an
  order and cancelling one are different powers - a support agent who may
  look up a customer's purchase should not thereby be able to void it.
* ``tax.manage`` and ``shipping.manage`` are separate because they are
  different jobs in a real organisation: tax is finance and compliance,
  shipping is operations. They are also named after resources that exist in
  this schema - ``tax_rates`` and ``shipping_rules`` - which a single
  ``pricing.manage`` would not have been.

There is deliberately no customer-facing code. Checkout, reading one's own
orders and managing one's own addresses take authentication only, and
ownership is the authorization - exactly as for the cart, and for the same
reason: Phase 2 locked "new users start with zero roles", so any
``orders.*`` code on a customer route would 403 every newly registered
buyer on their own order.

Idempotent in the same way as the earlier seeds - each insert defers to the
unique constraint on permissions.code.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORDER_PERMISSIONS = (
    (
        "orders.read",
        "View any customer's orders, not only one's own.",
    ),
    (
        "orders.manage",
        "Administratively cancel an order, releasing the stock it holds.",
    ),
    (
        "tax.manage",
        "Create and amend the tax rates applied to orders by destination.",
    ),
    (
        "shipping.manage",
        "Create and amend the rules that decide what delivery costs.",
    ),
)


def seed_order_permissions(connection: Connection) -> None:
    """Insert the Phase 5 permission codes, skipping any already present."""
    for code, description in ORDER_PERMISSIONS:
        connection.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "description": description},
        )


def upgrade() -> None:
    seed_order_permissions(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    for code, _ in ORDER_PERMISSIONS:
        connection.execute(
            sa.text("DELETE FROM permissions WHERE code = :code"), {"code": code}
        )
