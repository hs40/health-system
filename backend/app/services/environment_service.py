"""环境服务（阶段② T2.2）：和风天气 → EnvironmentDaily → 落库。

职责边界：
- 调用 ``WeatherAdapter`` 拉取实时/预报；**失败不编造**（返回 available=False 的记录）；
- 常用训练区域管理（REQ §13.7：用户可手动设置；AI 建议需用户确认）；
- 与季节策略/环境硬约束的关系：本服务只负责**取数与落库**，硬约束判定在
  ``domain/environment_rules.py``（纯函数）。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app.adapters.weather import WeatherAdapter
from app.config import Settings
from app.db.models import EnvironmentDailyRow
from app.schemas.domain import EnvironmentDaily
from app.storage.environment_store import get_environment, upsert_environment

# 常用训练区域：单用户场景，以进程内集合覆盖（默认区域为种子）。
# 说明：这是最小实现；持久化到库可在后续阶段按需增强。
_LOCATIONS_OVERRIDE: list[str] = []


def get_environment_cached(
    session: Session, day: dt.date, location: str | None = None
) -> EnvironmentDaily | None:
    """读已落库的环境数据（不发起网络请求）。"""
    location_id = ""
    if location:
        # 按地点名匹配已存记录
        row = session.exec(
            select(EnvironmentDailyRow).where(
                EnvironmentDailyRow.date == day,
                EnvironmentDailyRow.location_name == location,
            )
        ).first()
        location_id = row.location_id if row else ""
    return get_environment(session, day, location_id)


def set_locations(_session: Session, locations: list[str]) -> None:
    """设置常用训练区域（§13.7：用户手动设置）。"""
    _LOCATIONS_OVERRIDE.clear()
    _LOCATIONS_OVERRIDE.extend([x for x in locations if x and x.strip()])

# 常用训练区域存储：以配置的默认区域为种子（E21）；用户可通过 API 覆盖。
# 说明：单用户场景，用轻量 JSON 持久化在 environment_daily 之外的表亦可，
# 这里为最小实现，使用配置 + 运行时内存覆盖。


def fetch_environment(
    session: Session, day: dt.date, settings: Settings, *, location: str | None = None
) -> EnvironmentDaily:
    """拉取指定日期的环境数据并落库（失败 → available=False，不编造）。

    优先用实时数据（若为今天）；否则用逐日预报匹配该日。
    """
    adapter = WeatherAdapter(settings)
    loc = adapter.resolve_training_location(location)
    if loc is None:
        env = EnvironmentDaily(
            date=day,
            location_id="",
            location_name=location or settings.qweather_location,
            available=False,
            source="qweather",
            fetched_at=dt.datetime.now(),
        )
        upsert_environment(session, env)
        return env

    # 实时（今天）优先
    today = dt.date.today()
    result = adapter.current(loc.lat, loc.lon) if day == today else None
    if result is None or not result.available:
        daily = adapter.daily(loc.lat, loc.lon, days=10)
        result = _pick_daily(daily, day)

    if result is None or not result.available:
        env = EnvironmentDaily(
            date=day,
            location_id=loc.location_id,
            location_name=loc.name,
            lat=loc.lat,
            lon=loc.lon,
            available=False,
            source="qweather",
            fetched_at=dt.datetime.now(),
        )
        upsert_environment(session, env)
        return env

    env = _normalize(day, loc, result.data, result.available)
    upsert_environment(session, env)
    return env


def _pick_daily(daily_result, day: dt.date):
    """从逐日预报结果中挑出目标日（返回单元素 WeatherResult 语义）。

    v1 新版逐日结构：``{"days": [{forecastStartTime, temperatureMax:{value}, ...}]}``；
    日期以 ``forecastStartTime``（UTC ISO8601）判定，取本地日历日匹配。
    """
    from app.adapters.weather import WeatherResult

    if not daily_result.available:
        return daily_result
    items = daily_result.data.get("days") or daily_result.data.get("daily") or []
    for item in items:
        if _daily_date(item) == day:
            return WeatherResult(available=True, data={"day": item})
    # 未匹配到该日 → 不可用（不编造）
    return WeatherResult(available=False, error=f"预报中无 {day.isoformat()} 数据")


def _daily_date(item: dict) -> dt.date | None:
    """取逐日条目的日期（兼容 v1 ``forecastStartTime`` 与旧版 ``fxDate``）。"""
    raw = item.get("forecastStartTime") or item.get("fxDate")
    if not raw:
        return None
    text = str(raw)
    # fxDate 形如 2026-10-16；forecastStartTime 形如 2026-10-15T16:00Z（UTC）
    try:
        if "T" in text:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            return (parsed + dt.timedelta(hours=8)).date()  # 转北京时间日历日
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _normalize(day: dt.date, loc, data: dict, available: bool) -> EnvironmentDaily:
    """把和风返回结构归一化为 ``EnvironmentDaily``（v1 新版 + 旧版兼容）。"""
    # 优先取逐日条目 ``day``；否则为实时结构（顶层即天气字段）
    raw = data.get("day") or data.get("now") or data

    # 逐日条目把天气/降水放在 daytime/nighttime 内；实时为顶层
    daytime = raw.get("daytime") or {}
    # 天气文本：v1 daytime.condition.text / 旧版 textDay/text
    text = _first_text(
        daytime.get("condition"),
        raw.get("condition"),
        raw,
        keys=("text", "textDay"),
    )
    # 温度：v1 temperatureMax.value（逐日）或 temperature.value（实时）/ 旧版 temp
    temp = _first_num(
        raw.get("temperatureMax"),
        raw.get("temperature"),
        raw.get("temperatureAvg"),
    )
    if temp is None:
        temp = _to_float(raw.get("temp"))
    # 降水：v1 daytime.precipitation.amount.value / 旧版 precip
    raw_prec = raw.get("precipitation")
    precip = _first_num(
        (daytime.get("precipitation") or {}).get("amount"),
        raw_prec.get("amount") if isinstance(raw_prec, dict) else None,
    )
    if precip is None:
        precip = _to_float(raw.get("precip"))
    # 降水类型：v1 precipitation.type == "rain"/"snow"；旧版靠文本判定
    ptype = ""
    prec_obj = daytime.get("precipitation") or raw.get("precipitation")
    if isinstance(prec_obj, dict):
        ptype = str(prec_obj.get("type") or "")

    is_rain = ptype == "rain" or ("雨" in text)
    is_snow = ptype == "snow" or ("雪" in text)

    # 风力等级：v1 wind.scale / 旧版 windScale(Day)
    wind = raw.get("wind") or daytime.get("wind") or {}
    wind_scale = wind.get("scale") if isinstance(wind, dict) else None
    if wind_scale is None:
        wind_scale = raw.get("windScale") or raw.get("windScaleDay")

    return EnvironmentDaily(
        date=day,
        location_id=loc.location_id,
        location_name=loc.name,
        lat=loc.lat,
        lon=loc.lon,
        available=available,
        temp_c=temp,
        humidity=_first_humidity(raw, daytime),
        precip_mm=precip,
        weather_text=text,
        is_rain=is_rain,
        is_snow=is_snow,
        wind_scale=str(wind_scale) if wind_scale not in (None, "") else None,
        source="qweather",
        fetched_at=dt.datetime.now(),
    )


def _first_text(*candidates, keys: tuple[str, ...] = ("text",)) -> str:
    """从若干候选结构里取第一个非空文本。"""
    for cand in candidates:
        if isinstance(cand, dict):
            for k in keys:
                v = cand.get(k)
                if v:
                    return str(v)
        elif isinstance(cand, str) and cand:
            return cand
    return ""


def _first_num(*candidates) -> float | None:
    """从若干候选结构（``{"value": x}`` 或裸数值）里取第一个可转数值。"""
    for cand in candidates:
        v = cand.get("value") if isinstance(cand, dict) else cand
        num = _to_float(v)
        if num is not None:
            return num
    return None


def _first_humidity(raw: dict, daytime: dict) -> float | None:
    """湿度：v1 为 0~1 小数（转换为百分数）；旧版为百分数。"""
    for obj in (raw, daytime):
        v = obj.get("humidity") if isinstance(obj, dict) else None
        if v is None:
            continue
        num = _to_float(v)
        if num is None:
            return None
        # v1 返回 0~1；旧版返回 0~100
        return round(num * 100, 1) if num <= 1.0 else num
    return None


def _to_float(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


# ---------------------------- 常用训练区域 ----------------------------
def list_locations(session: Session, settings: Settings | None = None) -> list[str]:
    """列出常用训练区域（用户设置优先；默认区域为种子，§13.7）。"""
    if _LOCATIONS_OVERRIDE:
        return list(_LOCATIONS_OVERRIDE)

    names: list[str] = []
    if settings is not None and settings.qweather_location:
        names.append(settings.qweather_location)
    rows = session.exec(select(EnvironmentDailyRow.location_name).distinct()).all()
    for r in rows:
        if r and r not in names:
            names.append(r)
    return names
