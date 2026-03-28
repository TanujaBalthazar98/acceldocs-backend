"""add index on pages org_id section_id

Revision ID: 822aac55eb58
Revises: 26e00f96add3
Create Date: 2026-03-29 00:38:41.340932
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '822aac55eb58'
down_revision: Union[str, None] = '26e00f96add3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_pages_org_section', 'pages', ['organization_id', 'section_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_pages_org_section', table_name='pages')
