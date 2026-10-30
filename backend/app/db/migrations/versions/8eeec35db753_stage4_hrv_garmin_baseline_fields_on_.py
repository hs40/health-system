"""stage4 hrv garmin baseline fields on standard_daily

阶段④（E24，2026-10-16 用户决策）：R-001 改用 **Garmin 设备自带的 HRV 基线区间**
（取代自算 28 天 p10 分位）。为此 ``standard_daily`` 需保留设备的基线带与状态：

- ``hrv_low_upper``      低区上沿 → 低于即「恢复不足」（R-001 判定线）
- ``hrv_balanced_low``   平衡区下限
- ``hrv_balanced_high``  平衡区上限 → 高于即「有余力」（加量建议，非风险）
- ``hrv_weekly_avg``     7 天均值（Garmin status 的输入）
- ``hrv_status``         BALANCED / UNBALANCED / LOW / HIGH / NONE

全部可空：onboarding 期（实测 18 天）设备不带 baseline，留空走保守分支。

Revision ID: 8eeec35db753
Revises: 4f6a697be3c3
Create Date: 2026-10-16 22:12:05.611591

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = '8eeec35db753'
down_revision: str | None = '4f6a697be3c3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "hrv_low_upper",
    "hrv_balanced_low",
    "hrv_balanced_high",
    "hrv_weekly_avg",
    "hrv_status",
)


def upgrade() -> None:
    # SQLite 用 batch 模式改表（render_as_batch=True，见 env.py）。
    with op.batch_alter_table("standard_daily") as batch:
        batch.add_column(sa.Column("hrv_low_upper", sa.Float(), nullable=True))
        batch.add_column(sa.Column("hrv_balanced_low", sa.Float(), nullable=True))
        batch.add_column(sa.Column("hrv_balanced_high", sa.Float(), nullable=True))
        batch.add_column(sa.Column("hrv_weekly_avg", sa.Float(), nullable=True))
        batch.add_column(sa.Column("hrv_status", sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("standard_daily") as batch:
        for name in reversed(_COLUMNS):
            batch.drop_column(name)
