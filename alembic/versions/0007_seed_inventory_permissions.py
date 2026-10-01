"""seed inventory permissions

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-22

Registers the two inventory permission codes so an RBAC administrator can
grant them. As in 0003 and 0005, they are granted to no role: who may
correct a stock count is a business decision, not something a migration
should assume.

There is deliberately no ``inventory.write``. Reading stock levels and
changing them are different privileges - a shop-floor user who may see what
is in the warehouse should not thereby be able to write stock off - so
``inventory.read`` never implies ``inventory.adjust``.

Cart endpoints get no permission code at all. Phase 2 locked "new users
start with zero roles", so any ``cart.*`` code would 403 every newly
registered customer on their own cart until an administrator intervened.
Ownership is the authorization there instead.

Idempotent in the same way as 0003 and 0005 - each insert defers to the
unique constraint on permissions.code.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INVENTORY_PERMISSIONS = (
    (
        "inventory.read",
        "View stock levels, the stock ledger and reservations held against stock.",
    ),
    (
        "inventory.adjust",
        "Provision inventory for a variant and correct, write off or move stock between buckets.",
    ),
)


def seed_inventory_permissions(connection: Connection) -> None:
    """Insert the inventory permission codes, skipping any already present."""
    for code, description in INVENTORY_PERMISSIONS:
        connection.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "description": description},
        )


def upgrade() -> None:
    seed_inventory_permissions(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    for code, _ in INVENTORY_PERMISSIONS:
        connection.execute(
            sa.text("DELETE FROM permissions WHERE code = :code"), {"code": code}
        )
