"""Make approvals.document_id nullable for page-based approvals

Revision ID: a1b2c3d4e5f7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite doesn't support ALTER COLUMN, so we recreate the table
    op.execute('''
        CREATE TABLE IF NOT EXISTS approvals_new (
            id INTEGER PRIMARY KEY,
            document_id INTEGER,
            page_id INTEGER,
            entity_type VARCHAR(20),
            user_id INTEGER NOT NULL,
            action VARCHAR(50) NOT NULL,
            comment TEXT,
            created_at DATETIME NOT NULL,
            FOREIGN KEY (document_id) REFERENCES documents(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    op.execute('''
        INSERT INTO approvals_new (id, document_id, page_id, entity_type, user_id, action, comment, created_at)
        SELECT id, document_id, page_id, entity_type, user_id, action, comment, created_at FROM approvals
    ''')
    op.execute('DROP TABLE approvals')
    op.execute('ALTER TABLE approvals_new RENAME TO approvals')


def downgrade() -> None:
    # This is a one-way migration - document_id should remain nullable
    pass
