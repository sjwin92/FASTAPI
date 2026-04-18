"""initial schema

Revision ID: 0001_init
Revises: 
Create Date: 2026-04-18 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "0001_init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("retailer", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("brand", sa.String(length=255), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="GBP"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("retailer", "external_id", name="uq_products_retailer_external_id"),
    )
    op.create_index("ix_products_retailer", "products", ["retailer"], unique=False)
    op.create_index("ix_products_name", "products", ["name"], unique=False)

    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_price_history_product_id", "price_history", ["product_id"], unique=False)
    op.create_index("ix_price_history_captured_at", "price_history", ["captured_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_price_history_captured_at", table_name="price_history")
    op.drop_index("ix_price_history_product_id", table_name="price_history")
    op.drop_table("price_history")

    op.drop_index("ix_products_name", table_name="products")
    op.drop_index("ix_products_retailer", table_name="products")
    op.drop_table("products")
