"""seed catalogue permissions

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-18

Registers the three catalogue permission codes so an RBAC administrator can
grant them. It deliberately grants them to no role: which staff role should
be able to publish products is a business decision, not something a
migration should assume, and the seeded `admin` role exists to administer
authorization rather than to hold every permission by default.

Idempotent in the same way as 0003 - each insert defers to the unique
constraint on permissions.code.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CATALOGUE_PERMISSIONS = (
    (
        "catalog.read",
        "View catalogue data that is not publicly visible, through admin endpoints.",
    ),
    (
        "catalog.write",
        "Create and change products, options, variants, categories and their statuses.",
    ),
    ("images.write", "Upload and delete product image metadata."),
)


def seed_catalogue_permissions(connection: Connection) -> None:
    """Insert the catalogue permission codes, skipping any already present."""
    for code, description in CATALOGUE_PERMISSIONS:
        connection.execute(
            sa.text(
                "INSERT INTO permissions (code, description) VALUES (:code, :description) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            {"code": code, "description": description},
        )


def upgrade() -> None:
    seed_catalogue_permissions(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    for code, _ in CATALOGUE_PERMISSIONS:
        connection.execute(
            sa.text("DELETE FROM permissions WHERE code = :code"), {"code": code}
        )
