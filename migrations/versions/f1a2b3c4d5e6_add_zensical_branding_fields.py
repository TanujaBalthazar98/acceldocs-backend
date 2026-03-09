"""Add Zensical branding fields to organizations

Revision ID: f1a2b3c4d5e6
Revises: e44ff52b441e
Create Date: 2026-03-09 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e44ff52b441e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("analytics_property_id", sa.String(50), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("copyright", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "copyright")
    op.drop_column("organizations", "analytics_property_id")
