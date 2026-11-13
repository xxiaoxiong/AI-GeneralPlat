"""Run 级执行预算持久化（P1-05 §13）：跨崩溃/HITL 恢复不重置。

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
"""
from alembic import op
import sqlalchemy as sa

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(sa.Column("budget_model_calls", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("budget_tool_calls", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("budget_steps", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("budget_tokens", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("budget_first_started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("budget_deadline_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("budget_version", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("hitl_wait_started_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        for col in (
            "budget_model_calls", "budget_tool_calls", "budget_steps",
            "budget_tokens", "budget_first_started_at", "budget_deadline_at",
            "budget_version", "hitl_wait_started_at",
        ):
            batch_op.drop_column(col)
