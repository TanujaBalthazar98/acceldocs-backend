"""Add slug lock and unique org slug index for pages

Revision ID: d8f1a2b3c4d5
Revises: c6d7e8f90123
Create Date: 2026-03-13 18:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8f1a2b3c4d5"
down_revision: Union[str, None] = "c6d7e8f90123"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    op.add_column(
        "pages",
        sa.Column("slug_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # Backfill duplicate slugs before enforcing uniqueness.
    rows = conn.execute(
        sa.text(
            "SELECT id, organization_id, slug FROM pages ORDER BY organization_id, id"
        )
    ).fetchall()
    used_by_org: dict[int, set[str]] = {}
    for row in rows:
        page_id = row[0]
        org_id = row[1]
        current_slug = (row[2] or "").strip() or "page"

        used = used_by_org.setdefault(org_id, set())
        if current_slug not in used:
            used.add(current_slug)
            continue

        suffix = 1
        candidate = f"{current_slug}-{suffix}"
        while candidate in used:
            suffix += 1
            candidate = f"{current_slug}-{suffix}"

        conn.execute(
            sa.text("UPDATE pages SET slug = :slug WHERE id = :id"),
            {"slug": candidate, "id": page_id},
        )
        used.add(candidate)

    op.create_index(
        "ix_pages_org_slug_unique",
        "pages",
        ["organization_id", "slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_pages_org_slug_unique", table_name="pages")
    op.drop_column("pages", "slug_locked")
