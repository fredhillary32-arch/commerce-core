"""baseline

Revision ID: 0001
Revises:
Create Date: 2026-09-16

Establishes Alembic version tracking for the commerce_platform database.
Intentionally creates no application tables - later phases add their own
migrations on top of this baseline.
"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
