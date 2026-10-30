"""同步状态与校验失败通知（2026-10-21 决策 Q8：消息中心推送 + 运维台展示）。

背景：校验失败此前只有 exit code 与日志，用户看不到 → 真缺口长期无人知。
新行为：
- 校验有差异 → 写一条消息中心消息（含摘要与报告路径）；
- 无差异 → **不**写（不制造噪音）；
- 运维台「数据同步状态」卡片展示各数据集本地覆盖 + 最近一次校验结果。
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.services.sync_status import build_sync_status, verification_message


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
    )


# ------------------------------------------------------------
# 校验失败 → 消息中心
# ------------------------------------------------------------
def test_verification_failure_sends_message(session):
    msg = verification_message(session, diff_total=3, report_path="cloud-verify-x.json")
    assert msg is not None
    assert msg.title.startswith("云端校验失败")
    assert "3" in msg.body and "cloud-verify-x.json" in msg.body


def test_verification_clean_sends_nothing(session):
    """无差异 → 不写消息（否则每天一条噪音）。"""
    assert verification_message(session, diff_total=0, report_path=None) is None


def test_verification_message_is_idempotent_per_report(session):
    """同一份报告只发一次（重复跑校验不刷屏）。"""
    first = verification_message(session, diff_total=2, report_path="r1.json")
    second = verification_message(session, diff_total=2, report_path="r1.json")
    assert first is not None and second is None


# ------------------------------------------------------------
# 运维台状态快照
# ------------------------------------------------------------
def test_build_sync_status_reports_dataset_coverage(tmp_path):
    raw = tmp_path / "raw" / "garmin"
    for name, dates in (
        ("daily_summary", ["2026-10-18", "2026-10-19", "2026-10-20"]),
        ("weight", ["2026-10-15", "2026-10-19"]),
    ):
        d = raw / name
        d.mkdir(parents=True)
        for x in dates:
            (d / f"{x}.json").write_text("{}", encoding="utf-8")
    build = build_sync_status(tmp_path / "raw" / "garmin")
    assert "daily_summary" in build["datasets"]
    assert build["datasets"]["daily_summary"] == {
        "files": 3,
        "latest": "2026-10-20",
    }
    assert build["datasets"]["weight"]["files"] == 2


def test_build_sync_status_empty_when_no_raw(tmp_path):
    build = build_sync_status(tmp_path / "nope")
    assert build["datasets"] == {}


# ------------------------------------------------------------
# 运维台端点（S5）
# ------------------------------------------------------------
def test_sync_status_endpoint_returns_coverage(tmp_path, monkeypatch, session):
    """GET /api/ops/sync-status：数据集覆盖 + 最近校验结果。"""
    from app.api import ops as ops_api

    raw = tmp_path / "raw" / "garmin"
    (raw / "weight").mkdir(parents=True)
    (raw / "weight" / "2026-10-19.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ops_api, "get_settings", lambda: _settings(tmp_path))

    out = ops_api.sync_status(session=session)
    assert out["datasets"]["weight"]["files"] == 1
    assert out["tredict_activity_mirror"] == 0
