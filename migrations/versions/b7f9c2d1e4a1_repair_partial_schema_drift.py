"""repair_partial_schema_drift

Revision ID: b7f9c2d1e4a1
Revises: aee3a6ce7d18
Create Date: 2026-02-24 01:05:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f9c2d1e4a1'
down_revision: Union[str, None] = 'aee3a6ce7d18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return {col['name'] for col in insp.get_columns(table_name)}


def upgrade() -> None:
    doc_cols = _column_names('documents')
    project_cols = _column_names('projects')

    with op.batch_alter_table('documents') as batch:
        if 'is_published' not in doc_cols:
            batch.add_column(sa.Column('is_published', sa.Boolean(), nullable=False, server_default='0'))
        if 'content_html' not in doc_cols:
            batch.add_column(sa.Column('content_html', sa.Text(), nullable=True))
        if 'published_content_html' not in doc_cols:
            batch.add_column(sa.Column('published_content_html', sa.Text(), nullable=True))
        if 'content_id' not in doc_cols:
            batch.add_column(sa.Column('content_id', sa.String(length=255), nullable=True))
        if 'published_content_id' not in doc_cols:
            batch.add_column(sa.Column('published_content_id', sa.String(length=255), nullable=True))
        if 'video_url' not in doc_cols:
            batch.add_column(sa.Column('video_url', sa.String(length=500), nullable=True))
        if 'video_title' not in doc_cols:
            batch.add_column(sa.Column('video_title', sa.String(length=255), nullable=True))
        if 'display_order' not in doc_cols:
            batch.add_column(sa.Column('display_order', sa.Integer(), nullable=False, server_default='0'))
        if 'google_modified_at' not in doc_cols:
            batch.add_column(sa.Column('google_modified_at', sa.String(length=50), nullable=True))

    with op.batch_alter_table('projects') as batch:
        if 'drive_parent_id' not in project_cols:
            batch.add_column(sa.Column('drive_parent_id', sa.String(length=255), nullable=True))
        if 'visibility' not in project_cols:
            batch.add_column(sa.Column('visibility', sa.String(length=50), nullable=False, server_default='internal'))
        if 'is_published' not in project_cols:
            batch.add_column(sa.Column('is_published', sa.Boolean(), nullable=False, server_default='0'))
        if 'show_version_switcher' not in project_cols:
            batch.add_column(sa.Column('show_version_switcher', sa.Boolean(), nullable=False, server_default='1'))
        if 'organization_id' not in project_cols:
            batch.add_column(sa.Column('organization_id', sa.Integer(), nullable=True))
        if 'parent_id' not in project_cols:
            batch.add_column(sa.Column('parent_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    # Intentionally no-op: this migration repairs partially applied schema states.
    pass
