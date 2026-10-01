"""phase 4 cart and inventory schema

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-22

Creates the six cart and inventory tables, then backfills one zero-quantity
inventory row per existing variant. Four details are load-bearing:

* "one ACTIVE cart per user" is a *partial* unique index, the same shape as
  the catalogue's one-primary-image-per-product index, so a user keeps any
  number of CONVERTED or ABANDONED carts as history. It is also what makes
  the get-or-create race safe: the losing INSERT is rejected here.
* ``ck_inventory_quantities_balance`` is named explicitly because the
  adjustment service maps exactly this constraint's violation to
  INVALID_INVENTORY_ADJUSTMENT.
* the four quantity columns are **disjoint buckets**. ``quantity_on_hand``
  is good sellable stock; damaged and quarantined sit alongside it, not
  inside it. That is why the balance check is
  ``quantity_reserved <= quantity_on_hand`` and not a sum of all three.
* ``inventory.variant_id`` and ``cart_items.variant_id`` are RESTRICT, not
  CASCADE: stock records and a customer's cart are never collateral damage
  of a catalogue deletion.

The backfill exists because inventory is deliberately *not* hooked into the
Phase 3 variant-creation service. Variants made after this migration are
provisioned through POST /api/v1/inventory instead.

The new permission codes are seeded separately in 0007, keeping schema and
data changes independent as 0002/0003 and 0004/0005 already do.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def backfill_inventory_for_existing_variants(connection) -> int:
    """Give every variant that predates this phase a zero-quantity row.

    Zero, not a guess: the migration knows how many variants exist but
    nothing about how many of each are in a warehouse. Stock enters through
    the adjustment endpoint, which writes a ledger entry, so the
    reconciliation invariant holds from the very first row.

    Written as SELECT-INSERT with a NOT EXISTS guard rather than a bare
    INSERT ... SELECT so that re-running it is harmless.
    """
    return connection.execute(
        sa.text(
            "INSERT INTO inventory (variant_id) "
            "SELECT v.id FROM product_variants v "
            "WHERE NOT EXISTS (SELECT 1 FROM inventory i WHERE i.variant_id = v.id)"
        )
    ).rowcount


def upgrade() -> None:
    op.create_table('carts',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'ACTIVE'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ACTIVE', 'ABANDONED', 'CONVERTED')", name=op.f('ck_carts_status_valid')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_carts_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_carts'))
    )
    op.create_index(op.f('ix_carts_user_id'), 'carts', ['user_id'], unique=False)
    op.create_index('uq_carts_one_active_per_user', 'carts', ['user_id'], unique=True, postgresql_where=sa.text("status = 'ACTIVE'"))
    op.create_table('cart_items',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('cart_id', sa.UUID(), nullable=False),
    sa.Column('variant_id', sa.UUID(), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_cart_items_quantity_positive')),
    sa.ForeignKeyConstraint(['cart_id'], ['carts.id'], name=op.f('fk_cart_items_cart_id_carts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['variant_id'], ['product_variants.id'], name=op.f('fk_cart_items_variant_id_product_variants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cart_items')),
    sa.UniqueConstraint('cart_id', 'variant_id', name='uq_cart_items_cart_id_variant_id')
    )
    op.create_index(op.f('ix_cart_items_cart_id'), 'cart_items', ['cart_id'], unique=False)
    op.create_index(op.f('ix_cart_items_variant_id'), 'cart_items', ['variant_id'], unique=False)
    op.create_table('inventory',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('variant_id', sa.UUID(), nullable=False),
    sa.Column('quantity_on_hand', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('quantity_reserved', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('quantity_damaged', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('quantity_quarantined', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('quantity_damaged >= 0', name=op.f('ck_inventory_quantity_damaged_non_negative')),
    sa.CheckConstraint('quantity_on_hand >= 0', name=op.f('ck_inventory_quantity_on_hand_non_negative')),
    sa.CheckConstraint('quantity_quarantined >= 0', name=op.f('ck_inventory_quantity_quarantined_non_negative')),
    sa.CheckConstraint('quantity_reserved <= quantity_on_hand', name=op.f('ck_inventory_quantities_balance')),
    sa.CheckConstraint('quantity_reserved >= 0', name=op.f('ck_inventory_quantity_reserved_non_negative')),
    sa.ForeignKeyConstraint(['variant_id'], ['product_variants.id'], name=op.f('fk_inventory_variant_id_product_variants'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inventory')),
    sa.UniqueConstraint('variant_id', name=op.f('uq_inventory_variant_id'))
    )
    op.create_table('inventory_reservations',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('inventory_id', sa.UUID(), nullable=False),
    sa.Column('cart_id', sa.UUID(), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'ACTIVE'"), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('ACTIVE', 'CONVERTED', 'EXPIRED', 'RELEASED')", name=op.f('ck_inventory_reservations_status_valid')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_inventory_reservations_quantity_positive')),
    sa.ForeignKeyConstraint(['cart_id'], ['carts.id'], name=op.f('fk_inventory_reservations_cart_id_carts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['inventory_id'], ['inventory.id'], name=op.f('fk_inventory_reservations_inventory_id_inventory'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inventory_reservations'))
    )
    op.create_index(op.f('ix_inventory_reservations_cart_id'), 'inventory_reservations', ['cart_id'], unique=False)
    op.create_index('ix_inventory_reservations_inventory_id_status', 'inventory_reservations', ['inventory_id', 'status'], unique=False)
    op.create_table('inventory_transactions',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('inventory_id', sa.UUID(), nullable=False),
    sa.Column('transaction_type', sa.String(length=20), nullable=False),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('from_bucket', sa.String(length=20), nullable=True),
    sa.Column('to_bucket', sa.String(length=20), nullable=True),
    sa.Column('reference_type', sa.String(length=50), nullable=True),
    sa.Column('reference_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("from_bucket IS NULL OR from_bucket IN ('ON_HAND', 'DAMAGED', 'QUARANTINED')", name=op.f('ck_inventory_transactions_from_bucket_valid')),
    sa.CheckConstraint("to_bucket IS NULL OR to_bucket IN ('ON_HAND', 'DAMAGED', 'QUARANTINED')", name=op.f('ck_inventory_transactions_to_bucket_valid')),
    sa.CheckConstraint("transaction_type IN ('PURCHASE', 'RESTOCK', 'RETURN', 'ADJUSTMENT', 'DISPOSAL', 'TRANSFER')", name=op.f('ck_inventory_transactions_type_valid')),
    sa.CheckConstraint('from_bucket IS NOT NULL OR to_bucket IS NOT NULL', name=op.f('ck_inventory_transactions_direction_present')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_inventory_transactions_quantity_positive')),
    sa.ForeignKeyConstraint(['inventory_id'], ['inventory.id'], name=op.f('fk_inventory_transactions_inventory_id_inventory'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inventory_transactions'))
    )
    op.create_index(op.f('ix_inventory_transactions_inventory_id'), 'inventory_transactions', ['inventory_id'], unique=False)
    op.create_table('inventory_adjustments',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('inventory_id', sa.UUID(), nullable=False),
    sa.Column('transaction_id', sa.UUID(), nullable=False),
    sa.Column('actor_user_id', sa.UUID(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_inventory_adjustments_actor_user_id_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['inventory_id'], ['inventory.id'], name=op.f('fk_inventory_adjustments_inventory_id_inventory'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['transaction_id'], ['inventory_transactions.id'], name=op.f('fk_inventory_adjustments_transaction_id_inventory_transactions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inventory_adjustments'))
    )
    op.create_index(op.f('ix_inventory_adjustments_actor_user_id'), 'inventory_adjustments', ['actor_user_id'], unique=False)
    op.create_index(op.f('ix_inventory_adjustments_inventory_id'), 'inventory_adjustments', ['inventory_id'], unique=False)
    op.create_index(op.f('ix_inventory_adjustments_transaction_id'), 'inventory_adjustments', ['transaction_id'], unique=False)

    backfill_inventory_for_existing_variants(op.get_bind())


def downgrade() -> None:
    op.drop_index(op.f('ix_inventory_adjustments_transaction_id'), table_name='inventory_adjustments')
    op.drop_index(op.f('ix_inventory_adjustments_inventory_id'), table_name='inventory_adjustments')
    op.drop_index(op.f('ix_inventory_adjustments_actor_user_id'), table_name='inventory_adjustments')
    op.drop_table('inventory_adjustments')
    op.drop_index(op.f('ix_inventory_transactions_inventory_id'), table_name='inventory_transactions')
    op.drop_table('inventory_transactions')
    op.drop_index('ix_inventory_reservations_inventory_id_status', table_name='inventory_reservations')
    op.drop_index(op.f('ix_inventory_reservations_cart_id'), table_name='inventory_reservations')
    op.drop_table('inventory_reservations')
    op.drop_table('inventory')
    op.drop_index(op.f('ix_cart_items_variant_id'), table_name='cart_items')
    op.drop_index(op.f('ix_cart_items_cart_id'), table_name='cart_items')
    op.drop_table('cart_items')
    op.drop_index('uq_carts_one_active_per_user', table_name='carts', postgresql_where=sa.text("status = 'ACTIVE'"))
    op.drop_index(op.f('ix_carts_user_id'), table_name='carts')
    op.drop_table('carts')
