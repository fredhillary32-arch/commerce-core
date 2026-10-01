"""phase 3 catalogue schema

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-18

Creates the eight catalogue tables. Three details are load-bearing and easy
to lose in a later autogenerate:

* ``product_variants.variant_signature`` is nullable, and
  UNIQUE (product_id, variant_signature) relies on PostgreSQL treating every
  null as distinct, so unconfigured draft variants never collide.
* ``variant_option_values`` carries a composite foreign key onto
  (option_id, id) of product_option_values - which is why that table needs
  the otherwise-redundant UNIQUE (option_id, id).
* "at most one primary image per product" is a *partial* unique index, not a
  constraint, so a product may still hold many non-primary images.

The new permission codes are seeded separately in 0005, keeping schema and
data changes independent as 0002/0003 already do.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('categories',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=255), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['parent_id'], ['categories.id'], name=op.f('fk_categories_parent_id_categories'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_categories')),
    sa.UniqueConstraint('slug', name=op.f('uq_categories_slug'))
    )
    op.create_index(op.f('ix_categories_parent_id'), 'categories', ['parent_id'], unique=False)
    op.create_table('products',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'DRAFT'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'ARCHIVED', 'DISCONTINUED')", name=op.f('ck_products_status_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_products')),
    sa.UniqueConstraint('slug', name=op.f('uq_products_slug'))
    )
    op.create_table('product_categories',
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('category_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'], name=op.f('fk_product_categories_category_id_categories'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_product_categories_product_id_products'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('product_id', 'category_id', name=op.f('pk_product_categories'))
    )
    op.create_index(op.f('ix_product_categories_category_id'), 'product_categories', ['category_id'], unique=False)
    op.create_table('product_images',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('storage_path', sa.String(length=500), nullable=False),
    sa.Column('alt_text', sa.String(length=255), nullable=True),
    sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_product_images_product_id_products'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_product_images')),
    sa.UniqueConstraint('storage_path', name=op.f('uq_product_images_storage_path'))
    )
    op.create_index(op.f('ix_product_images_product_id'), 'product_images', ['product_id'], unique=False)
    op.create_index('uq_product_images_one_primary_per_product', 'product_images', ['product_id'], unique=True, postgresql_where=sa.text('is_primary'))
    op.create_table('product_options',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_product_options_product_id_products'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_product_options'))
    )
    op.create_index(op.f('ix_product_options_product_id'), 'product_options', ['product_id'], unique=False)
    op.create_table('product_variants',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=False),
    sa.Column('sku', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=20), server_default=sa.text("'DRAFT'"), nullable=False),
    sa.Column('price', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('variant_signature', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'DISCONTINUED')", name=op.f('ck_product_variants_status_valid')),
    sa.CheckConstraint('price IS NULL OR price > 0', name=op.f('ck_product_variants_price_positive')),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_product_variants_product_id_products'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_product_variants')),
    sa.UniqueConstraint('product_id', 'variant_signature', name='uq_product_variants_product_id_signature'),
    sa.UniqueConstraint('sku', name=op.f('uq_product_variants_sku'))
    )
    op.create_index(op.f('ix_product_variants_product_id'), 'product_variants', ['product_id'], unique=False)
    op.create_table('product_option_values',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('option_id', sa.UUID(), nullable=False),
    sa.Column('value', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['option_id'], ['product_options.id'], name=op.f('fk_product_option_values_option_id_product_options'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_product_option_values')),
    sa.UniqueConstraint('option_id', 'id', name='uq_product_option_values_option_id_id')
    )
    op.create_index(op.f('ix_product_option_values_option_id'), 'product_option_values', ['option_id'], unique=False)
    op.create_table('variant_option_values',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('variant_id', sa.UUID(), nullable=False),
    sa.Column('option_id', sa.UUID(), nullable=False),
    sa.Column('option_value_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['option_id', 'option_value_id'], ['product_option_values.option_id', 'product_option_values.id'], name='fk_variant_option_values_option_id_value_id', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['option_id'], ['product_options.id'], name=op.f('fk_variant_option_values_option_id_product_options'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['variant_id'], ['product_variants.id'], name=op.f('fk_variant_option_values_variant_id_product_variants'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_variant_option_values')),
    sa.UniqueConstraint('variant_id', 'option_id', name='uq_variant_option_values_variant_id_option_id')
    )
    op.create_index(op.f('ix_variant_option_values_option_id'), 'variant_option_values', ['option_id'], unique=False)
    op.create_index(op.f('ix_variant_option_values_option_value_id'), 'variant_option_values', ['option_value_id'], unique=False)
    op.create_index(op.f('ix_variant_option_values_variant_id'), 'variant_option_values', ['variant_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_variant_option_values_variant_id'), table_name='variant_option_values')
    op.drop_index(op.f('ix_variant_option_values_option_value_id'), table_name='variant_option_values')
    op.drop_index(op.f('ix_variant_option_values_option_id'), table_name='variant_option_values')
    op.drop_table('variant_option_values')
    op.drop_index(op.f('ix_product_option_values_option_id'), table_name='product_option_values')
    op.drop_table('product_option_values')
    op.drop_index(op.f('ix_product_variants_product_id'), table_name='product_variants')
    op.drop_table('product_variants')
    op.drop_index(op.f('ix_product_options_product_id'), table_name='product_options')
    op.drop_table('product_options')
    op.drop_index('uq_product_images_one_primary_per_product', table_name='product_images', postgresql_where=sa.text('is_primary'))
    op.drop_index(op.f('ix_product_images_product_id'), table_name='product_images')
    op.drop_table('product_images')
    op.drop_index(op.f('ix_product_categories_category_id'), table_name='product_categories')
    op.drop_table('product_categories')
    op.drop_table('products')
    op.drop_index(op.f('ix_categories_parent_id'), table_name='categories')
    op.drop_table('categories')
