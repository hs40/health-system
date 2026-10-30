"""Alembic 迁移基线测试（阶段① · T1.1）。

验收（Plan §5 T1.1）：
- ``alembic upgrade head`` 在全新库通过，且产出**一期 16 张表**；
- 迁移可重复执行（幂等）；
- 已有库经 ``stamp head`` 后不丢数据、不改结构。

本测试通过 Alembic Python API 在临时库上执行，不依赖外部命令。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

# 一期 16 张表（T1.6 口径，与 docs/一期项目报告.md 一致）
PHASE1_TABLES = {
    "standard_daily",
    "data_quality",
    "baseline",
    "training_load",
    "acwr",
    "risk_assessment",
    "daily_report",
    "weekly_report",
    "monthly_report",
    "weekly_plan",
    "plan_12w",
    "training_session",
    "task_run",
    "audit_event",
    "external_call_log",
    "rule_version",
}

# 二期阶段②新增表（T2.2 / T2.6）
STAGE2_TABLES = {"environment_daily", "training_conflict"}

# 二期阶段③新增表（T3.1 / T3.9 / T3.10）
STAGE3_TABLES = {
    "strategy_version",
    "strategy_event",
    "fault_recovery_task",
    "message",
}

# 阶段⑤新增表（E28 / Q6 / Q17 / Q28 / Q29，2026-10-17）：Garmin ↔ 本地 ↔ Tredict 同步链路
# 2026-10-21：``garmin_activity`` 已废弃（决策 Q5=B，raw 为唯一事实源），
# 由迁移 e8b2c5d1f4a7 删除；此处不再计入 head 表集合。
STAGE5_TABLES = {
    "tredict_activity",
    "tredict_generated",
    "sync_ledger",
}

# 阶段⑥新增表（P4 / Q34 / Q35，2026-10-17）：FIT 逐秒数据
STAGE6_TABLES = {"garmin_activity_record"}

# 阶段⑨删除表（G-17，2026-10-24）：data_quality 零写入点、接口恒 404、前端零调用
# 阶段⑪删除表（2026-10-25）：rule_version 零生产读写、实测 0 行（唯一事实源是常量）
# → 由迁移 b3f7c1d8e6a2 / d4e6f8a1b3c5 删除。PHASE1_TABLES 保留 initial 迁移的
# 历史事实（16 张），head 表集合在此扣掉。
DROPPED_TABLES = {"data_quality", "rule_version"}

HEAD_TABLES = (
    (PHASE1_TABLES - DROPPED_TABLES)
    | STAGE2_TABLES
    | STAGE3_TABLES
    | STAGE5_TABLES
    | STAGE6_TABLES
)

# initial 迁移的版本号（供分阶段断言引用）
INITIAL_REVISION = "abb66b1b9437"
# 阶段③ 迁移的版本号：阶段④（改列）、阶段⑤（建表）、阶段⑥（逐秒表）都叠在它之后
STAGE3_REVISION = "4f6a697be3c3"
# 阶段⑤ 迁移的版本号：阶段⑥叠在它之后
STAGE5_REVISION = "b4d9e2f7a1c6"

_BACKEND = Path(__file__).resolve().parents[1]


def _alembic_config(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    # env.py 从 Settings 读 DB URL；Settings 用 lru_cache，故直接注入 URL 到 Config，
    # 避免同一进程内多次调用被缓存串库。
    monkeypatch.setenv("DB_PATH", str(db_path))
    cfg = Config(str(_BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows if not r[0].startswith("sqlite") and r[0] != "alembic_version"}


def test_upgrade_head_creates_phase1_16_tables(tmp_path, monkeypatch):
    """initial 迁移（升级到 INITIAL_REVISION）→ 恰好一期 16 张表。"""
    db = tmp_path / "fresh.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, INITIAL_REVISION)
    assert _table_names(db) == PHASE1_TABLES


def test_upgrade_head_includes_stage2_tables(tmp_path, monkeypatch):
    """全量 upgrade head → 一期 16 表 − 2 张死表 + 阶段②2 + 阶段③4 + 阶段⑤3（共 24 表）。"""
    db = tmp_path / "head.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, "head")
    assert _table_names(db) == HEAD_TABLES


def test_upgrade_head_is_idempotent(tmp_path, monkeypatch):
    """重复 upgrade head 不报错、表集合不变。"""
    db = tmp_path / "idem.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
    assert _table_names(db) == HEAD_TABLES


def test_stamp_head_preserves_existing_data(tmp_path, monkeypatch):
    """已有库（模拟一期结构 + 数据）stamp head → 数据不丢、结构不变。"""
    db = tmp_path / "existing.db"
    # 先建出一期结构（等价于一期 create_all 的结果）
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, INITIAL_REVISION)

    # 写入一行真实数据
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO standard_daily (date, created_at) "
                "VALUES ('2026-10-14', '2026-10-14 08:00:00')"
            )
        )
    before = _table_names(db)

    # 已有库接入基线：stamp（不执行迁移）
    command.stamp(cfg, "head")

    assert _table_names(db) == before
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM standard_daily")).scalar() == 1
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_stage4_adds_garmin_hrv_baseline_columns(tmp_path, monkeypatch):
    """阶段④ 迁移（E24）：standard_daily 增加 5 个 Garmin HRV 基线列。"""
    db = tmp_path / "hrv.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(standard_daily)"))}
    assert {
        "hrv_low_upper",
        "hrv_balanced_low",
        "hrv_balanced_high",
        "hrv_weekly_avg",
        "hrv_status",
    } <= cols


def test_stage4_downgrade_removes_columns(tmp_path, monkeypatch):
    """阶段④ 迁移可逆：回退到阶段③后 5 列消失，其余结构不变。

    注意：head 现在是阶段⑤，故不能再用 ``-1``（那只会回退阶段⑤的建表）；
    这里显式回退到 ``STAGE3_REVISION``，同时撤销阶段④与阶段⑤。
    """
    db = tmp_path / "hrv_down.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, STAGE3_REVISION)

    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(standard_daily)"))}
    assert "hrv_low_upper" not in cols
    assert "hrv_last_night_avg" in cols  # 一期列仍在


def test_stage5_downgrade_drops_sync_tables(tmp_path, monkeypatch):
    """阶段⑤ 迁移可逆：回退到阶段③后 4 张同步表消失，既有数据不丢。"""
    db = tmp_path / "sync_down.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO standard_daily (date, created_at) "
                "VALUES ('2026-10-17', '2026-10-17 08:00:00')"
            )
        )

    command.downgrade(cfg, STAGE3_REVISION)

    assert _table_names(db) == PHASE1_TABLES | STAGE2_TABLES | STAGE3_TABLES
    with engine.connect() as conn:
        kept = conn.execute(
            text("SELECT COUNT(*) FROM standard_daily WHERE date = '2026-10-17'")
        ).scalar()
    assert kept == 1


def test_stage6_downgrade_drops_record_table_and_columns(tmp_path, monkeypatch):
    """阶段⑥ 迁移可逆：逐秒表消失，garmin_activity 的 3 个 JSON 列回退。"""
    db = tmp_path / "fit_down.db"
    cfg = _alembic_config(db, monkeypatch)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, STAGE5_REVISION)

    assert "garmin_activity_record" not in _table_names(db)

    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(garmin_activity)")}
    finally:
        conn.close()
    assert cols.isdisjoint({"laps", "sets", "zones"})
    assert "garmin_activity" in _table_names(db)
