"""stage7 acwr.ratio 可空

训练负荷重构（需求 8.4，2026-10-20）：慢性 EWMA 为 0 时 **ACWR 不可计算，
不伪造数值**。但 ``acwr.ratio`` 建表时是 NOT NULL，新口径回写 ratio=NULL
会被数据库拒绝（``NOT NULL constraint failed: acwr.ratio``），
全量重算一旦遇到「无历史负荷的开局日」整批失败。

本迁移把 ``ratio`` 改为可空。语义随之明确：

- ``ratio IS NULL`` → 当日不可计算（慢性为 0 或数据不足无法判定），
  读出处必须按不可计算处理，**不得**补 0 或估算；
- 已有的行全部是旧口径值（恒有 ratio），不受影响。

Revision ID: d5f3a1c9e7b2
Revises: c7e1a4b9d2f8
Create Date: 2026-10-20 15:40:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f3a1c9e7b2"
down_revision: str | None = "c7e1a4b9d2f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table：SQLite 无法直接 ALTER COLUMN，由 Alembic 重建表
    with op.batch_alter_table("acwr", schema=None) as batch_op:
        batch_op.alter_column(
            "ratio",
            existing_type=sa.Float(),
            nullable=True,
            comment=None,
            existing_comment=None,
        )


def downgrade() -> None:
    # 回退前先把 NULL 抹成 0？不 —— 那正是本次迁移要消灭的「伪造数值」。
    # 回退只改结构；残留 NULL 行说明库处于不可计算状态，应由上层拒绝而不是填 0。
    with op.batch_alter_table("acwr", schema=None) as batch_op:
        batch_op.alter_column(
            "ratio",
            existing_type=sa.Float(),
            nullable=False,
            comment=None,
            existing_comment=None,
        )
