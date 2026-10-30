"""标准化层读写（REQ-DATA-002/003/004）。

只负责 ``StandardDaily`` ↔ ``standard_daily`` 表的映射与查询，不含业务逻辑。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app.db.models import StandardDailyRow
from app.schemas.domain import RawDailyRef, StandardDaily

_FIELDS = (
    "resting_heart_rate",
    "min_heart_rate",
    "max_heart_rate",
    "sleep_duration_sec",
    "sleep_start_ts",
    "sleep_end_ts",
    "hrv_last_night_avg",
    # Garmin 自带 HRV 基线区间（E24）：必须随标准化一起落库，否则规则拿不到设备基线
    "hrv_low_upper",
    "hrv_balanced_low",
    "hrv_balanced_high",
    "hrv_weekly_avg",
    "hrv_status",
    "body_battery_wake",
    # 风险引擎口径（Q11）：睡眠得分 / 醒时电量，必须落库否则 P1·P3 无法判定
    "sleep_score",
    "body_battery_morning",
    "weight_kg",
    "steps",
    "active_calories",
    "intensity_moderate_min",
    "intensity_vigorous_min",
    "spo2_avg",
    "respiration_avg",
)


def _refs_payload(sd: StandardDaily) -> dict:
    """``source_refs`` 的落库形态（REQ-DATA-004 可回溯）。"""
    return {"refs": [r.model_dump(mode="json") for r in sd.source_refs]}


def to_row(sd: StandardDaily) -> StandardDailyRow:
    return StandardDailyRow(
        date=sd.date,
        **{f: getattr(sd, f) for f in _FIELDS},
        source_refs=_refs_payload(sd),
    )


def from_row(row: StandardDailyRow) -> StandardDaily:
    refs = (row.source_refs or {}).get("refs") or []
    return StandardDaily(
        date=row.date,
        **{f: getattr(row, f) for f in _FIELDS},
        source_refs=[RawDailyRef(**r) for r in refs],
    )


def upsert_standard(session: Session, sd: StandardDaily) -> None:
    """按日期 upsert（标准化层可重建，允许覆盖）。"""
    existing = session.exec(
        select(StandardDailyRow).where(StandardDailyRow.date == sd.date)
    ).first()
    if existing is None:
        session.add(to_row(sd))
    else:
        for f in _FIELDS:
            setattr(existing, f, getattr(sd, f))
        existing.source_refs = _refs_payload(sd)
        session.add(existing)
    session.commit()


def get_standard(session: Session, day: dt.date) -> StandardDaily | None:
    row = session.exec(select(StandardDailyRow).where(StandardDailyRow.date == day)).first()
    return from_row(row) if row else None


def load_standard_range(
    session: Session, start: dt.date, end: dt.date
) -> list[StandardDaily]:
    """加载 [start, end] 闭区间的标准化日数据（升序）。"""
    rows = session.exec(
        select(StandardDailyRow)
        .where(StandardDailyRow.date >= start, StandardDailyRow.date <= end)
        .order_by(StandardDailyRow.date)  # type: ignore[attr-defined]
    ).all()
    return [from_row(r) for r in rows]
