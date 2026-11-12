"""agent_run_events 增加 thread_id（FR-07 / §7.2 事件持久化结构）

Revision ID: f1a2b3c4d5e6
Revises: d4e5f6a7b8c9
Create Date: 2026-11-12

新建独立迁移：不修改历史迁移文件。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agent_run_events') as batch_op:
        batch_op.add_column(sa.Column('thread_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('agent_run_events') as batch_op:
        batch_op.drop_column('thread_id')
