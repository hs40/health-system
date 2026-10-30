"""stage9 删除 data_quality 死表（G-17）

2026-10-24 用户决策：**删除**（不补写入）。

为什么删：``DataQualityRow`` 全仓**零写入点**，实测 0 行；唯一读取点
``GET /api/data/quality`` 因此恒 404；前端零调用——日报的质量信息来自
payload 内嵌的 ``quality`` 字段。保留它只会维持「看起来有质量历史可查」
的假象，比没有更危险。

替代物：日报/周报/月报 payload 里的 ``quality``（479 天历史都在）。
将来若需要「按档位查历史质量」，可从 payload 反解，或按需重建表。

Revision ID: b3f7c1d8e6a2
Revises: a7c3e9b5d2f4
Create Date: 2026-10-24 17:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f7c1d8e6a2"
down_revision: str | None = "a7c3e9b5d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_data_quality_level", table_name="data_quality")
    op.drop_index("ix_data_quality_date", table_name="data_quality")
    op.drop_table("data_quality")


def downgrade() -> None:
    # 回滚只恢复空表结构（本来也没有数据）
    op.create_table(
        "data_quality",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("level", sa.String(), nullable=False),
        sa.Column("grade", sa.String(), nullable=False),
        sa.Column("missing", sa.JSON(), nullable=True),
        sa.Column("conflicts", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", "level", name="uq_quality_date_level"),
    )
    op.create_index("ix_data_quality_date", "data_quality", ["date"])
    op.create_index("ix_data_quality_level", "data_quality", ["level"])
