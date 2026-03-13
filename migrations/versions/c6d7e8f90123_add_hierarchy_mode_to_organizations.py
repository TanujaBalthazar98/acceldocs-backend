"""Add hierarchy_mode to organizations

Revision ID: c6d7e8f90123
Revises: f1a2b3c4d5e6
Create Date: 2026-03-13 10:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c6d7e8f90123"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("hierarchy_mode", sa.String(length=20), nullable=True, server_default="product"),
    )


def downgrade() -> None:
    op.drop_column("organizations", "hierarchy_mode")
