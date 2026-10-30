"""环境数据读写（阶段② T2.2）。

与派生层一致：环境数据可刷新覆盖（``upsert`` 按 (date, location_id) 去重）。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app.db.models import EnvironmentDailyRow
from app.schemas.domain import EnvironmentDaily


def upsert_environment(session: Session, env: EnvironmentDaily) -> None:
    """按 (date, location_id) 写入或更新环境数据。"""
    row = session.exec(
        select(EnvironmentDailyRow).where(
            EnvironmentDailyRow.date == env.date,
            EnvironmentDailyRow.location_id == env.location_id,
        )
    ).first()
    data = {
        "location_name": env.location_name,
        "lat": env.lat,
        "lon": env.lon,
        "available": env.available,
        "temp_c": env.temp_c,
        "humidity": env.humidity,
        "precip_mm": env.precip_mm,
        "weather_text": env.weather_text,
        "is_rain": env.is_rain,
        "is_snow": env.is_snow,
        "wind_scale": env.wind_scale,
        "source": env.source,
        "fetched_at": env.fetched_at or dt.datetime.now(),
    }
    if row is None:
        session.add(
            EnvironmentDailyRow(date=env.date, location_id=env.location_id, **data)
        )
    else:
        for k, v in data.items():
            setattr(row, k, v)
        session.add(row)
    session.commit()


def get_environment(
    session: Session, day: dt.date, location_id: str = ""
) -> EnvironmentDaily | None:
    """取指定日期的环境数据（``location_id`` 为空则取该日**最优**一条）。

    为什么要排序：同一天可能并存两条 —— 地点解析失败时写 ``location_id=""``，
    之后解析成功又写入真实 id（键不同，唯一约束不冲突）。若按插入顺序取，
    可能拿到**失败态**而误报 ``available=False``。故优先取可用数据、再取最新抓取。
    """
    stmt = select(EnvironmentDailyRow).where(EnvironmentDailyRow.date == day)
    if location_id:
        stmt = stmt.where(EnvironmentDailyRow.location_id == location_id)
    stmt = stmt.order_by(
        EnvironmentDailyRow.available.desc(),  # type: ignore[attr-defined]
        EnvironmentDailyRow.fetched_at.desc(),  # type: ignore[attr-defined]
    )
    row = session.exec(stmt).first()
    if row is None:
        return None
    return EnvironmentDaily(
        date=row.date,
        location_id=row.location_id,
        location_name=row.location_name,
        lat=row.lat,
        lon=row.lon,
        available=row.available,
        temp_c=row.temp_c,
        humidity=row.humidity,
        precip_mm=row.precip_mm,
        weather_text=row.weather_text,
        is_rain=row.is_rain,
        is_snow=row.is_snow,
        wind_scale=row.wind_scale,
        source=row.source,
        fetched_at=row.fetched_at,
    )
