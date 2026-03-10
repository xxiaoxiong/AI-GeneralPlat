"""add database_connections table and agents.database_connection_id column

Revision ID: a1b2c3d4e5f6
Revises: 9ce3d801b702
Create Date: 2026-02-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '9ce3d801b702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 创建 database_connections 表
    op.create_table('database_connections',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('db_type', sa.String(length=20), nullable=False),
        sa.Column('host', sa.String(length=255), server_default='localhost'),
        sa.Column('port', sa.Integer(), server_default='3306'),
        sa.Column('database', sa.String(length=255), nullable=False),
        sa.Column('username', sa.String(length=255), server_default=''),
        sa.Column('password', sa.String(length=255), server_default=''),
        sa.Column('extra_params', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1'),
        sa.Column('last_tested_at', sa.DateTime(), nullable=True),
        sa.Column('last_test_ok', sa.Boolean(), server_default='0'),
        sa.Column('owner_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_database_connections_id'), 'database_connections', ['id'], unique=False)

    # 给 agents 表添加 database_connection_id 列
    op.add_column('agents', sa.Column('database_connection_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_agents_database_connection_id',
        'agents', 'database_connections',
        ['database_connection_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_agents_database_connection_id', 'agents', type_='foreignkey')
    op.drop_column('agents', 'database_connection_id')
    op.drop_index(op.f('ix_database_connections_id'), table_name='database_connections')
    op.drop_table('database_connections')
