"""P1-02：Session 并发控制 + 消息版本乐观锁 + 结果写回 Outbox

- agent_sessions 增加：version（消息写入乐观锁）、active_run_id /
  active_run_lease_expires_at（Session 执行权原子 Claim，单 Session 单活跃 Run）
- 新表 session_result_outbox：Run 终态与 Session 写回解耦的轻量 Outbox
  （同事务入队、按 source_run_id 去重、失败重试、重启可恢复）

Revision ID: b5c6d7e8f9a0
Revises: e3f4a5b6c7d8
"""
from alembic import op
import sqlalchemy as sa

revision = "b5c6d7e8f9a0"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_sessions") as batch_op:
        batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("active_run_id", sa.String(36), nullable=True))
        batch_op.add_column(
            sa.Column("active_run_lease_expires_at", sa.DateTime(), nullable=True)
        )

    op.create_table(
        "session_result_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        # 不设外键：Session/Run 删除不被写回任务阻塞；run_id 唯一约束
        # 保证一 Run 一条写回任务（source_run_id 去重的持久化依据）
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("run_id", name="uq_session_result_outbox_run"),
    )
    op.create_index("ix_session_result_outbox_session_id", "session_result_outbox", ["session_id"])
    op.create_index("ix_session_result_outbox_status", "session_result_outbox", ["status"])


def downgrade() -> None:
    # 默认发布流程不执行 downgrade（任务书 §8.3）；此处仅保证脚本完整可逆
    op.drop_index("ix_session_result_outbox_status", table_name="session_result_outbox")
    op.drop_index("ix_session_result_outbox_session_id", table_name="session_result_outbox")
    op.drop_table("session_result_outbox")

    with op.batch_alter_table("agent_sessions") as batch_op:
        batch_op.drop_column("active_run_lease_expires_at")
        batch_op.drop_column("active_run_id")
        batch_op.drop_column("version")
