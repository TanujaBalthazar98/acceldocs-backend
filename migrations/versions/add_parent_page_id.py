"""Add parent_page_id to pages table for sub-page hierarchy

Revision ID: add_parent_page_id
Revises: d8f1a2b3c4d5
Create Date: 2026-04-12 20:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "add_parent_page_id"
down_revision: Union[str, None] = "d8f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pages",
        sa.Column("parent_page_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_pages_parent_page_id",
        "pages",
        ["parent_page_id"],
    )
    op.create_foreign_key(
        "fk_pages_parent_page_id",
        "pages",
        "pages",
        ["parent_page_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_pages_parent_page_id", "pages", type_="foreignkey")
    op.drop_index("ix_pages_parent_page_id", "pages")
    op.drop_column("pages", "parent_page_id")