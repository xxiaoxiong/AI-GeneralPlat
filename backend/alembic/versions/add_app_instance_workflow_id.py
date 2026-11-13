"""app_instances.workflow_id（模型/迁移漂移修复：登录后 /apps/instances 500）。

SQLAlchemy 模型 AppInstance 含 workflow_id，但历史迁移建表时缺列，迁移驱动的
生产库（MySQL）查询报 1054 Unknown column。SQLite 测试库走 create_all 不暴露
该漂移。本迁移独立补列，不修改历史迁移语义。

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-11-13
"""
from alembic import op
import sqlalchemy as sa

revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("app_instances") as batch_op:
        batch_op.add_column(sa.Column("workflow_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("app_instances") as batch_op:
        batch_op.drop_column("workflow_id")
