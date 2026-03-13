"""add_github_fields_to_organizations

Revision ID: 26e00f96add3
Revises: 540df71f01c6
Create Date: 2026-03-09 12:13:13.292940
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '26e00f96add3'
down_revision: Union[str, None] = '540df71f01c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('organizations', sa.Column('github_username', sa.String(length=255), nullable=True))
    op.add_column('organizations', sa.Column('github_token_encrypted', sa.Text(), nullable=True))
    op.add_column('organizations', sa.Column('github_repo_name', sa.String(length=255), nullable=True))
    op.add_column('organizations', sa.Column('github_repo_full_name', sa.String(length=500), nullable=True))
    op.add_column('organizations', sa.Column('github_pages_url', sa.String(length=500), nullable=True))
    op.add_column('organizations', sa.Column('github_custom_domain', sa.String(length=255), nullable=True))
    op.add_column('organizations', sa.Column('github_domain_verified', sa.Boolean(), nullable=True, server_default='0'))
    op.add_column('organizations', sa.Column('last_published_at', sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column('organizations', 'last_published_at')
    op.drop_column('organizations', 'github_domain_verified')
    op.drop_column('organizations', 'github_custom_domain')
    op.drop_column('organizations', 'github_pages_url')
    op.drop_column('organizations', 'github_repo_full_name')
    op.drop_column('organizations', 'github_repo_name')
    op.drop_column('organizations', 'github_token_encrypted')
    op.drop_column('organizations', 'github_username')
