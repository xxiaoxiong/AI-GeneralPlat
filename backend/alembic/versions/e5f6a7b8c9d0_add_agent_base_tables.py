"""Baseline repair: create agent-module base tables missing from the initial
migration (agents, agent_sessions, prompt_templates, prompt_template_versions).

空库执行 alembic upgrade 时，历史迁移 add_database_connections 依赖 agents 表，
但初始迁移从未创建该表（生产环境一直由 init_db()/create_all 补齐）。
本 revision 补齐空库升级路径；已部署数据库不会执行到此处（版本在下游）。

注意：agents 表在此不含 database_connection_id 列 —— 该列由下游迁移
add_database_connections 负责添加，保持与历史迁移语义一致。

Revision ID: e5f6a7b8c9d0
Revises: 9ce3d801b702
Create Date: 2026-11-12 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = '9ce3d801b702'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if not _has_table('agents'):
        op.create_table(
            'agents',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('icon', sa.String(length=10), nullable=True),
            sa.Column('model_config_id', sa.Integer(), nullable=True),
            sa.Column('system_prompt', sa.Text(), nullable=True),
            sa.Column('max_iterations', sa.Integer(), nullable=True),
            sa.Column('max_tokens', sa.Integer(), nullable=True),
            sa.Column('temperature', sa.String(length=10), nullable=True),
            sa.Column('tools', sa.JSON(), nullable=True),
            sa.Column('knowledge_base_id', sa.Integer(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=True),
            sa.Column('owner_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['model_config_id'], ['model_configs.id']),
            sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id']),
            sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if not _has_table('agent_sessions'):
        op.create_table(
            'agent_sessions',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('agent_id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('title', sa.String(length=200), nullable=True),
            sa.Column('messages', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['agent_id'], ['agents.id']),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if not _has_table('prompt_templates'):
        op.create_table(
            'prompt_templates',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('name', sa.String(length=100), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('category', sa.String(length=50), nullable=True),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('variables', sa.JSON(), nullable=True),
            sa.Column('is_public', sa.Boolean(), nullable=True),
            sa.Column('owner_id', sa.Integer(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=True),
            sa.Column('tags', sa.String(length=200), nullable=True),
            sa.Column('usage_count', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )

    if not _has_table('prompt_template_versions'):
        op.create_table(
            'prompt_template_versions',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('template_id', sa.Integer(), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('content', sa.Text(), nullable=False),
            sa.Column('variables', sa.JSON(), nullable=True),
            sa.Column('change_note', sa.String(length=200), nullable=True),
            sa.Column('created_by', sa.Integer(), nullable=False),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['template_id'], ['prompt_templates.id']),
            sa.ForeignKeyConstraint(['created_by'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )


def downgrade() -> None:
    # 基线补齐表：仅在由本 revision 创建时删除（下游 add_agent_runs 等依赖前两张表）
    if _has_table('prompt_template_versions'):
        op.drop_table('prompt_template_versions')
    if _has_table('prompt_templates'):
        op.drop_table('prompt_templates')
