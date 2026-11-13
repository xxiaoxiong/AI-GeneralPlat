"""P1-03 + P1-04：人工核对审计列 + Run 取消请求

- agent_runs 增加：cancel_requested（取消意图的唯一真相源）、
  cancel_requested_at / cancel_requested_by（取消审计）
- agent_tool_executions 增加：reconcile_decision / reconcile_note /
  reconciled_by / reconciled_at（needs_reconciliation 后的人工核对处置审计）

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
"""
from alembic import op
import sqlalchemy as sa

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(
            sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("cancel_requested_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("cancel_requested_by", sa.Integer(), nullable=True))

    with op.batch_alter_table("agent_tool_executions") as batch_op:
        batch_op.add_column(sa.Column("reconcile_decision", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("reconcile_note", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("reconciled_by", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("reconciled_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    # 默认发布流程不执行 downgrade（任务书 §8.3）；此处仅保证脚本完整可逆
    with op.batch_alter_table("agent_tool_executions") as batch_op:
        batch_op.drop_column("reconciled_at")
        batch_op.drop_column("reconciled_by")
        batch_op.drop_column("reconcile_note")
        batch_op.drop_column("reconcile_decision")

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_column("cancel_requested_by")
        batch_op.drop_column("cancel_requested_at")
        batch_op.drop_column("cancel_requested")
