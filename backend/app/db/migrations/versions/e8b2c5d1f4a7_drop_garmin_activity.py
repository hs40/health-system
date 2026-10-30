"""stage8 废弃 garmin_activity 表（raw 成为唯一事实源）

2026-10-21 用户决策 Q5=B：**废弃 ``garmin_activity``，上传/校验直接读 raw + FIT**。

为什么废弃：该表全项目**只有一个写入方**（一次性 ``backfill_garmin_activity.py``），
日常管线从不更新 → 表冻结在 301 条；而上传脚本 ``upload_tredict_activity.py``
读的正是这张表，于是「表 301 ↔ Tredict 301」永远判定「已一致」，
新活动（如 2026-10-20 那条）永远传不上去。

替代物：``app/storage/activity_source.py``（``load_activities`` / ``load_sets``），
直接读 ``raw/garmin/activities/*.json`` 与 ``raw/garmin/fit_jsonl/*.jsonl.gz``。

数据去向：``laps`` / ``sets`` / ``zones`` 三列是 FIT 派生计数，
可从 ``fit_jsonl`` 重新得到；删表前已备份 ``backups/health.db.bak-before-drop-*``。
``garmin_activity_record``（逐秒数据）**保留**，它由 FIT 解析写入、仍在用。

Revision ID: e8b2c5d1f4a7
Revises: d5f3a1c9e7b2
Create Date: 2026-10-21 15:20:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b2c5d1f4a7"
down_revision: str | None = "d5f3a1c9e7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_garmin_activity_payload_sha256", table_name="garmin_activity")
    op.drop_index("ix_garmin_activity_activity_type", table_name="garmin_activity")
    op.drop_index("ix_garmin_activity_start_time_local", table_name="garmin_activity")
    op.drop_table("garmin_activity")


def downgrade() -> None:
    # 回滚只恢复空表结构（数据无法凭空恢复，需从备份库导入）
    op.create_table(
        "garmin_activity",
        sa.Column("activity_id", sa.String(), primary_key=True),
        sa.Column("start_time_local", sa.DateTime(), nullable=False),
        sa.Column("start_time_gmt", sa.DateTime(), nullable=True),
        sa.Column("activity_type", sa.String(), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("activity_name", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("laps", sa.JSON(), nullable=True),
        sa.Column("sets", sa.JSON(), nullable=True),
        sa.Column("zones", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_garmin_activity_start_time_local", "garmin_activity", ["start_time_local"]
    )
    op.create_index("ix_garmin_activity_activity_type", "garmin_activity", ["activity_type"])
    op.create_index(
        "ix_garmin_activity_payload_sha256", "garmin_activity", ["payload_sha256"]
    )
