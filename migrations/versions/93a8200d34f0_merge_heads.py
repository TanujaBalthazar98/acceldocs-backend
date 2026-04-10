"""merge_heads

Revision ID: 93a8200d34f0
Revises: 822aac55eb58, a1b2c3d4e5f7
Create Date: 2026-04-09 01:12:33.188571
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '93a8200d34f0'
down_revision: Union[str, None] = ('822aac55eb58', 'a1b2c3d4e5f7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
