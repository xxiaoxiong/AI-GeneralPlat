"""agent_runs 增加 recovery_metadata（P0-04 §6.3 恢复决策元数据持久化）

Revision ID: e3f4a5b6c7d8
Revises: f1a2b3c4d5e6
Create Date: 2026-11-13

新建独立迁移：不修改历史迁移文件。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('agent_runs') as batch_op:
        batch_op.add_column(sa.Column('recovery_metadata', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('agent_runs') as batch_op:
        batch_op.drop_column('recovery_metadata')
