"""Phase 2 durable execution: run lease/idempotency, approvals identity,
tool execution ledger, run event log.

Revision ID: d4e5f6a7b8c9
Revises: b7c8d9e0f1a2
Create Date: 2026-11-12 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── agent_runs：幂等唯一约束 + 租约/恢复字段 ─────────────────────────
    with op.batch_alter_table('agent_runs') as batch_op:
        batch_op.add_column(sa.Column('request_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('input_payload', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('agent_config_snapshot', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('started_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('worker_id', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('attempt', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('lease_version', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('last_event_seq', sa.BigInteger(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('recovery_error', sa.Text(), nullable=True))
        batch_op.create_unique_constraint('uq_agent_runs_idem', ['user_id', 'idempotency_key'])

    # ── agent_approvals：稳定动作身份 + 决策幂等 ─────────────────────────
    with op.batch_alter_table('agent_approvals') as batch_op:
        batch_op.add_column(sa.Column('action_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('tool_call_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('args_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('action_payload', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('decision_status', sa.String(length=24), nullable=True, server_default='pending'))
        batch_op.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
        batch_op.create_unique_constraint('uq_agent_approvals_action', ['run_id', 'action_id'])

    # ── agent_tool_executions：工具执行账本 ──────────────────────────────
    op.create_table(
        'agent_tool_executions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('tool_call_id', sa.String(length=64), nullable=False),
        sa.Column('tool_name', sa.String(length=100), nullable=False),
        sa.Column('args_hash', sa.String(length=64), nullable=True),
        sa.Column('args_snapshot', sa.JSON(), nullable=True),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True),
        sa.Column('status', sa.String(length=24), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('result_payload', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('external_operation_id', sa.String(length=128), nullable=True),
        sa.Column('worker_id', sa.String(length=128), nullable=True),
        sa.Column('lease_version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'tool_call_id', name='uq_tool_exec_run_call'),
    )
    op.create_index('ix_tool_exec_run_status', 'agent_tool_executions', ['run_id', 'status'])
    op.create_index('ix_tool_exec_status_updated', 'agent_tool_executions', ['status', 'updated_at'])
    op.create_index('ix_agent_tool_executions_run_id', 'agent_tool_executions', ['run_id'])
    op.create_index('ix_agent_tool_executions_status', 'agent_tool_executions', ['status'])

    # ── agent_run_events：持久化事件日志 ─────────────────────────────────
    op.create_table(
        'agent_run_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=48), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'sequence', name='uq_run_event_seq'),
    )
    op.create_index('ix_run_event_run_seq', 'agent_run_events', ['run_id', 'sequence'])


def downgrade() -> None:
    op.drop_index('ix_run_event_run_seq', table_name='agent_run_events')
    op.drop_table('agent_run_events')

    op.drop_index('ix_agent_tool_executions_status', table_name='agent_tool_executions')
    op.drop_index('ix_agent_tool_executions_run_id', table_name='agent_tool_executions')
    op.drop_index('ix_tool_exec_status_updated', table_name='agent_tool_executions')
    op.drop_index('ix_tool_exec_run_status', table_name='agent_tool_executions')
    op.drop_table('agent_tool_executions')

    with op.batch_alter_table('agent_approvals') as batch_op:
        batch_op.drop_constraint('uq_agent_approvals_action', type_='unique')
        batch_op.drop_column('updated_at')
        batch_op.drop_column('decision_status')
        batch_op.drop_column('action_payload')
        batch_op.drop_column('args_hash')
        batch_op.drop_column('tool_call_id')
        batch_op.drop_column('action_id')

    with op.batch_alter_table('agent_runs') as batch_op:
        batch_op.drop_constraint('uq_agent_runs_idem', type_='unique')
        batch_op.drop_column('recovery_error')
        batch_op.drop_column('last_event_seq')
        batch_op.drop_column('lease_version')
        batch_op.drop_column('attempt')
        batch_op.drop_column('heartbeat_at')
        batch_op.drop_column('lease_expires_at')
        batch_op.drop_column('worker_id')
        batch_op.drop_column('started_at')
        batch_op.drop_column('agent_config_snapshot')
        batch_op.drop_column('input_payload')
        batch_op.drop_column('request_hash')
