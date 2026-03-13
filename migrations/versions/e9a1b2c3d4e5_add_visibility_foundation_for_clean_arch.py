"""Add visibility fields and external access grants for clean-arch docs

Revision ID: e9a1b2c3d4e5
Revises: d8f1a2b3c4d5
Create Date: 2026-03-13 18:55:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9a1b2c3d4e5"
down_revision: Union[str, None] = "d8f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sections",
        sa.Column("visibility", sa.String(length=20), nullable=False, server_default="public"),
    )
    op.create_index(
        "ix_sections_org_parent_visibility",
        "sections",
        ["organization_id", "parent_id", "visibility"],
        unique=False,
    )

    op.add_column(
        "pages",
        sa.Column("visibility_override", sa.String(length=20), nullable=True),
    )
    op.create_index(
        "ix_pages_org_visibility_override",
        "pages",
        ["organization_id", "visibility_override"],
        unique=False,
    )

    op.create_table(
        "external_access_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "email", name="uq_external_access_org_email"),
    )
    op.create_index(
        "ix_external_access_org_active",
        "external_access_grants",
        ["organization_id", "is_active"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_external_access_org_active", table_name="external_access_grants")
    op.drop_table("external_access_grants")

    op.drop_index("ix_pages_org_visibility_override", table_name="pages")
    op.drop_column("pages", "visibility_override")

    op.drop_index("ix_sections_org_parent_visibility", table_name="sections")
    op.drop_column("sections", "visibility")
