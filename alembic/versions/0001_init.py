"""init

Revision ID: 0001
Revises:
Create Date: 2026-04-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("retailer", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("image_url", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("retailer", "external_id", name="uq_product_retailer_external"),
    )

    op.create_table(
        "price_records",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("unit_price", sa.Float, nullable=True),
        sa.Column("unit", sa.String(50), nullable=True),
        sa.Column("in_stock", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("captured_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "tracked_products",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"), nullable=False),
        sa.Column("alert_threshold", sa.Float, nullable=True),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    op.create_index("ix_price_records_product_id", "price_records", ["product_id"])
    op.create_index("ix_tracked_products_product_id", "tracked_products", ["product_id"])
    op.create_index("ix_tracked_products_active", "tracked_products", ["active"])


def downgrade() -> None:
    op.drop_table("tracked_products")
    op.drop_table("price_records")
    op.drop_table("products")
