"""standard_daily 新增 sleep_score / body_battery_morning

风险引擎重设计（2026-10-22，Q11）：P1/P2/P3 的阈值判定需要「睡眠得分」与
「晨起身体电量」两个口径，此前库内均无对应字段。

- ``sleep_score`` ← raw ``dailySleepDTO.sleepScores.overall.value``（0–100）
- ``body_battery_morning`` ← raw ``dailySummary.bodyBatteryAtWakeTime``

**与既有 ``body_battery_wake`` 的区别（重要）**：后者取自
``bodyBatteryMostRecentValue``，含义是「当日最后一次同步读到的电量」。每日任务
在傍晚运行，读到的是当天消耗后的残值（实测近 30 天中位数仅 5，而晨起值中位数
为 60），两者量级不同，**不得互相顶替**，故新增而非改名。

两列均可空：缺失即留空，由风险引擎按 Q24 判为「缺失」并降级 B 态，不填 0。

Revision ID: f2a8c4d6e9b1
Revises: e8b2c5d1f4a7
Create Date: 2026-10-22 15:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a8c4d6e9b1"
down_revision: str | None = "e8b2c5d1f4a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("standard_daily", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sleep_score", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("body_battery_morning", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("standard_daily", schema=None) as batch_op:
        batch_op.drop_column("body_battery_morning")
        batch_op.drop_column("sleep_score")
