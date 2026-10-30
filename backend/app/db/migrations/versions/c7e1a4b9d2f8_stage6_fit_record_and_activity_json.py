"""stage6 FIT 逐秒表 + 活动三列

P4（2026-10-17）：为 FIT 解析引擎建存储结构。

两件事：

1. **新建 ``garmin_activity_record``** —— 逐秒数据（7 通道）。
   基线实测：301 个活动共 **542,977** 条 record，单表约 39 MB。
   主键用 ``(activity_id, seq)`` 而非 ``(activity_id, t)``：
   同一条活动内可能存在相同秒的两条 record，用 t 做主键会静默丢行。
   查询走 ``(activity_id, t)`` 索引。

2. **``garmin_activity`` 加 3 个 JSON 列**（Q35=A）：
   ``laps`` / ``sets`` / ``zones``。原始 FIT 里这些是消息数组，
   塞进 SQLite 逐行反而要多 3 张表 + 关联查询，实测 12 周报表场景
   只快 4 毫秒（5.6ms → 1.3ms），不值得。

**不加外键**（与 stage5 一致）：逐秒表数据量是活动表的 1800 倍，
外键级联删除会拖慢批量导入；孤儿行由校验脚本清理。

逐秒表**不建** ``payload`` 之类的兜底列 —— 全量归档在
``data/raw/garmin/fit_jsonl/<id>.jsonl.gz``，SQLite 只存能算的。

Revision ID: c7e1a4b9d2f8
Revises: b4d9e2f7a1c6
Create Date: 2026-10-17 23:05:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c7e1a4b9d2f8'
down_revision: str | None = 'b4d9e2f7a1c6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "garmin_activity_record",
        sa.Column("activity_id", sa.String(), nullable=False),
        # 文件内序号：保留原始顺序，也允许同一秒出现多条
        sa.Column("seq", sa.Integer(), nullable=False),
        # UTC epoch 秒（口径 A：活动时间戳是真 UTC）
        sa.Column("t", sa.Integer(), nullable=False),
        sa.Column("hr", sa.Integer(), nullable=True),
        sa.Column("speed", sa.Float(), nullable=True),
        sa.Column("power", sa.Float(), nullable=True),
        sa.Column("cadence", sa.Float(), nullable=True),
        sa.Column("altitude", sa.Float(), nullable=True),
        sa.Column("distance", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("activity_id", "seq"),
    )
    op.create_index(
        "ix_garmin_activity_record_activity_t",
        "garmin_activity_record",
        ["activity_id", "t"],
    )

    # Q35=A：lap / set / zone 进 garmin_activity 的 3 个 JSON 列
    op.add_column("garmin_activity", sa.Column("laps", sa.JSON(), nullable=True))
    op.add_column("garmin_activity", sa.Column("sets", sa.JSON(), nullable=True))
    op.add_column("garmin_activity", sa.Column("zones", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("garmin_activity", "zones")
    op.drop_column("garmin_activity", "sets")
    op.drop_column("garmin_activity", "laps")
    op.drop_index(
        "ix_garmin_activity_record_activity_t", table_name="garmin_activity_record"
    )
    op.drop_table("garmin_activity_record")
