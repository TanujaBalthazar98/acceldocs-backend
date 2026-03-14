"""Add page feedback and comments tables for rendered docs

Revision ID: a1b2c3d4e5f6
Revises: e9a1b2c3d4e5
Create Date: 2026-03-14 15:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e9a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "page_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("user_email", sa.String(length=255), nullable=True),
        sa.Column("vote", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_page_feedback_org_page_vote",
        "page_feedback",
        ["organization_id", "page_id", "vote"],
        unique=False,
    )
    op.create_index(
        "ix_page_feedback_page_created",
        "page_feedback",
        ["page_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "page_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("page_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("user_email", sa.String(length=255), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_page_comments_org_page_created",
        "page_comments",
        ["organization_id", "page_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_page_comments_page_deleted",
        "page_comments",
        ["page_id", "is_deleted"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_page_comments_page_deleted", table_name="page_comments")
    op.drop_index("ix_page_comments_org_page_created", table_name="page_comments")
    op.drop_table("page_comments")

    op.drop_index("ix_page_feedback_page_created", table_name="page_feedback")
    op.drop_index("ix_page_feedback_org_page_vote", table_name="page_feedback")
    op.drop_table("page_feedback")
