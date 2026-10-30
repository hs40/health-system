"""stage5 sync tables

阶段⑤（2026-10-17 用户决策 Q6/Q17/Q28/Q29）：建立 Garmin ↔ 本地 ↔ Tredict
的**数据同步链路**所需的 4 张表。

本轮范围（E28）：**只建数据链路，不改任何计算逻辑**。这 4 张表不参与
``baseline`` / ``risk`` / ``report``；Tredict 自有数据只存档、不进计算层。

表清单：

- ``garmin_activity``    Garmin 活动权威记录，``payload`` 存 121 字段原文
- ``tredict_activity``   Tredict 活动精简版，仅用于上传校验与追溯
- ``tredict_generated``  Tredict 自有 / 自算数据（capacity/zones/effort/…）
- ``sync_ledger``        逐条同步状态，取代原设计的 ``verify_run`` 批次表

**全部不加外键**（Q29）：
- ``tredict_activity.matched_garmin_id`` 可空——匹配可能失败（ambiguous），
  加 FK 会导致未匹配记录插不进去；
- ``sync_ledger.entity_id`` 是多态外键（指向三张不同表），SQLite 无法表达。

**迁移只建结构**，数据回填走独立幂等脚本 ``scripts/backfill_sync_tables.py``：
回填依赖本地文件与 Tredict API，放进迁移会导致迁移不可离线执行、不可回滚。

Revision ID: b4d9e2f7a1c6
Revises: 8eeec35db753
Create Date: 2026-10-17 16:15:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b4d9e2f7a1c6'
down_revision: str | None = '8eeec35db753'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------- garmin_activity ----------------
    op.create_table(
        "garmin_activity",
        sa.Column("activity_id", sa.String(), nullable=False),
        sa.Column("start_time_local", sa.DateTime(), nullable=False),
        sa.Column("start_time_gmt", sa.DateTime(), nullable=True),
        sa.Column("activity_type", sa.String(), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("activity_name", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("payload_sha256", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("activity_id"),
    )
    op.create_index(
        "ix_garmin_activity_start_time_local", "garmin_activity", ["start_time_local"]
    )
    op.create_index("ix_garmin_activity_activity_type", "garmin_activity", ["activity_type"])
    op.create_index("ix_garmin_activity_payload_sha256", "garmin_activity", ["payload_sha256"])

    # ---------------- tredict_activity ----------------
    op.create_table(
        "tredict_activity",
        sa.Column("tredict_id", sa.String(), nullable=False),
        sa.Column("utc_datetime", sa.DateTime(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("sport_type", sa.String(), nullable=False),
        sa.Column("sub_sport_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("duration_total_sec", sa.Integer(), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("has_notes", sa.Boolean(), nullable=False),
        sa.Column("training_type", sa.String(), nullable=False),
        sa.Column("matched_garmin_id", sa.String(), nullable=True),
        sa.Column("match_confidence", sa.String(), nullable=False),
        sa.Column("payload_sha256", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("tredict_id"),
    )
    op.create_index("ix_tredict_activity_local_date", "tredict_activity", ["local_date"])
    op.create_index("ix_tredict_activity_sport_type", "tredict_activity", ["sport_type"])
    op.create_index(
        "ix_tredict_activity_matched_garmin_id", "tredict_activity", ["matched_garmin_id"]
    )

    # ---------------- tredict_generated ----------------
    op.create_table(
        "tredict_generated",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("entity_key", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("remote_updated_at", sa.DateTime(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity", "entity_key", "version", name="uq_tredict_generated"),
    )
    op.create_index("ix_tredict_generated_entity", "tredict_generated", ["entity"])
    op.create_index("ix_tredict_generated_entity_key", "tredict_generated", ["entity_key"])
    op.create_index("ix_tredict_generated_content_hash", "tredict_generated", ["content_hash"])

    # ---------------- sync_ledger ----------------
    op.create_table(
        "sync_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("local_hash", sa.String(), nullable=False),
        sa.Column("remote_hash", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("first_failed_at", sa.DateTime(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity", "entity_id", "direction", name="uq_sync_ledger"),
    )
    op.create_index("ix_sync_ledger_entity", "sync_ledger", ["entity"])
    op.create_index("ix_sync_ledger_entity_id", "sync_ledger", ["entity_id"])
    op.create_index("ix_sync_ledger_direction", "sync_ledger", ["direction"])
    op.create_index("ix_sync_ledger_state", "sync_ledger", ["state"])
    op.create_index("ix_sync_ledger_state_direction", "sync_ledger", ["state", "direction"])


def downgrade() -> None:
    op.drop_index("ix_sync_ledger_state_direction", table_name="sync_ledger")
    op.drop_index("ix_sync_ledger_state", table_name="sync_ledger")
    op.drop_index("ix_sync_ledger_direction", table_name="sync_ledger")
    op.drop_index("ix_sync_ledger_entity_id", table_name="sync_ledger")
    op.drop_index("ix_sync_ledger_entity", table_name="sync_ledger")
    op.drop_table("sync_ledger")

    op.drop_index("ix_tredict_generated_content_hash", table_name="tredict_generated")
    op.drop_index("ix_tredict_generated_entity_key", table_name="tredict_generated")
    op.drop_index("ix_tredict_generated_entity", table_name="tredict_generated")
    op.drop_table("tredict_generated")

    op.drop_index("ix_tredict_activity_matched_garmin_id", table_name="tredict_activity")
    op.drop_index("ix_tredict_activity_sport_type", table_name="tredict_activity")
    op.drop_index("ix_tredict_activity_local_date", table_name="tredict_activity")
    op.drop_table("tredict_activity")

    op.drop_index("ix_garmin_activity_payload_sha256", table_name="garmin_activity")
    op.drop_index("ix_garmin_activity_activity_type", table_name="garmin_activity")
    op.drop_index("ix_garmin_activity_start_time_local", table_name="garmin_activity")
    op.drop_table("garmin_activity")
