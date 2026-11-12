"""add agent_runs and agent_approvals tables (DeepAgents Runtime)

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-11-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agent_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('agent_id', sa.Integer(), nullable=False),
        sa.Column('session_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('thread_id', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=True),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('final_text', sa.Text(), nullable=True),
        sa.Column('summary', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ),
        sa.ForeignKeyConstraint(['session_id'], ['agent_sessions.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_runs_agent_id', 'agent_runs', ['agent_id'])
    op.create_index('ix_agent_runs_session_id', 'agent_runs', ['session_id'])
    op.create_index('ix_agent_runs_user_id', 'agent_runs', ['user_id'])
    op.create_index('ix_agent_runs_thread_id', 'agent_runs', ['thread_id'])
    op.create_index('ix_agent_runs_status', 'agent_runs', ['status'])
    op.create_index('ix_agent_runs_idempotency_key', 'agent_runs', ['idempotency_key'])

    op.create_table(
        'agent_approvals',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('tool_name', sa.String(length=100), nullable=False),
        sa.Column('action_summary', sa.Text(), nullable=True),
        sa.Column('decision', sa.String(length=24), nullable=True),
        sa.Column('decided_by', sa.Integer(), nullable=True),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ),
        sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_agent_approvals_run_id', 'agent_approvals', ['run_id'])


def downgrade() -> None:
    op.drop_index('ix_agent_approvals_run_id', table_name='agent_approvals')
    op.drop_table('agent_approvals')
    op.drop_index('ix_agent_runs_idempotency_key', table_name='agent_runs')
    op.drop_index('ix_agent_runs_status', table_name='agent_runs')
    op.drop_index('ix_agent_runs_thread_id', table_name='agent_runs')
    op.drop_index('ix_agent_runs_user_id', table_name='agent_runs')
    op.drop_index('ix_agent_runs_session_id', table_name='agent_runs')
    op.drop_index('ix_agent_runs_agent_id', table_name='agent_runs')
    op.drop_table('agent_runs')
