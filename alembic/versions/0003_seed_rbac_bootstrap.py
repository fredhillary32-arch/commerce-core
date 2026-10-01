"""seed rbac bootstrap

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17

Seeds the minimum authorization state needed for a human to become the first
RBAC administrator: an `admin` role, the `rbac.manage` permission, and the
grant between them. It deliberately creates no user and assigns the role to
nobody - the first administrator is appointed out-of-band with
`python -m app.cli assign-admin <user_id>`, which also writes an audit entry.

Downgrade is only meaningful in a fresh environment. Once a real user holds
the admin role, deleting it hits the RESTRICT foreign key on `user_roles`;
that failure is the intended protection, not a defect to work around.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ADMIN_ROLE_NAME = "admin"
RBAC_MANAGE_PERMISSION = "rbac.manage"


def seed_rbac_bootstrap(connection: Connection) -> None:
    """Insert the bootstrap role, permission and grant, skipping what exists.

    Idempotent: every statement defers to the unique/primary-key constraints
    already on the tables, so running it twice leaves exactly one of each row.
    """
    connection.execute(
        sa.text(
            "INSERT INTO roles (name, description) VALUES (:name, :description) "
            "ON CONFLICT (name) DO NOTHING"
        ),
        {"name": ADMIN_ROLE_NAME, "description": "Full RBAC administration."},
    )
    connection.execute(
        sa.text(
            "INSERT INTO permissions (code, description) VALUES (:code, :description) "
            "ON CONFLICT (code) DO NOTHING"
        ),
        {
            "code": RBAC_MANAGE_PERMISSION,
            "description": "Administer roles, permissions and user authorization.",
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.name = :name AND p.code = :code "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        ),
        {"name": ADMIN_ROLE_NAME, "code": RBAC_MANAGE_PERMISSION},
    )


def upgrade() -> None:
    seed_rbac_bootstrap(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM role_permissions "
            "WHERE role_id = (SELECT id FROM roles WHERE name = :name) "
            "AND permission_id = (SELECT id FROM permissions WHERE code = :code)"
        ),
        {"name": ADMIN_ROLE_NAME, "code": RBAC_MANAGE_PERMISSION},
    )
    connection.execute(sa.text("DELETE FROM roles WHERE name = :name"), {"name": ADMIN_ROLE_NAME})
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code = :code"), {"code": RBAC_MANAGE_PERMISSION}
    )
