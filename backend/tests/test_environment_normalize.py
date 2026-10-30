"""环境数据归一化测试（阶段② T2.2，v1 新版格式）。

验证和风 v1 新版响应 → ``EnvironmentDaily`` 的字段映射：
- 实时：``temperature.value`` / ``condition.text`` / ``humidity``(0~1) / ``precipitation.type``；
- 逐日：``days[].forecastStartTime``（UTC）→ 本地日历日匹配、
  ``temperatureMax.value`` / ``daytime.condition.text``。

纯函数测试，不联网。
"""

from __future__ import annotations

import datetime as dt

from app.adapters.weather import LocationInfo, WeatherResult
from app.services.environment_service import _normalize, _pick_daily

_LOC = LocationInfo(location_id="101220601", name="宜秀区", lat="30.51", lon="117.03")
_DAY = dt.date(2026, 10, 16)

# 与真实和风响应同构（已裁剪）
_CURRENT = {
    "metadata": {"tag": "abc"},
    "condition": {"text": "雾", "code": "501"},
    "temperature": {"value": 21.25, "unit": "°C"},
    "humidity": 0.91,
    "precipitation": {"amount": {"value": 0.09, "unit": "mm"}, "type": "rain"},
    "wind": {"scale": 1},
}

_DAILY_ITEM = {
    "forecastStartTime": "2026-10-15T16:00Z",  # → 北京时间 2026-10-16 00:00
    "temperatureMax": {"value": 24.14, "unit": "°C"},
    "temperatureMin": {"value": 20.45, "unit": "°C"},
    "daytime": {
        "condition": {"text": "阵雨", "code": "300"},
        "precipitation": {"amount": {"value": 5.01, "unit": "mm"}, "type": "rain"},
        "wind": {"scale": 3},
        "humidity": 0.86,
    },
}


def test_normalize_current_v1():
    env = _normalize(_DAY, _LOC, _CURRENT, True)
    assert env.available is True
    assert env.temp_c == 21.25
    assert env.humidity == 91.0          # 0.91 → 91%
    assert env.weather_text == "雾"
    assert env.is_rain is True           # precipitation.type == rain
    assert env.is_snow is False
    assert env.precip_mm == 0.09
    assert env.wind_scale == "1"
    assert env.location_id == "101220601"


def test_normalize_daily_v1():
    env = _normalize(_DAY, _LOC, {"day": _DAILY_ITEM}, True)
    assert env.temp_c == 24.14           # temperatureMax.value
    assert env.humidity == 86.0
    assert env.weather_text == "阵雨"     # daytime.condition.text
    assert env.is_rain is True
    assert env.precip_mm == 5.01
    assert env.wind_scale == "3"


def test_normalize_daily_snow_detected():
    item = {
        "forecastStartTime": "2026-10-15T16:00Z",
        "temperatureMax": {"value": -1.0},
        "daytime": {
            "condition": {"text": "小雪"},
            "precipitation": {"amount": {"value": 2.0}, "type": "snow"},
        },
    }
    env = _normalize(_DAY, _LOC, {"day": item}, True)
    assert env.is_snow is True
    assert env.is_rain is False
    assert env.temp_c == -1.0


def test_pick_daily_matches_local_calendar_day():
    """forecastStartTime 为 UTC；转北京时间后日历日匹配。"""
    daily = WeatherResult(available=True, data={"days": [_DAILY_ITEM]})
    picked = _pick_daily(daily, _DAY)
    assert picked.available is True
    assert picked.data["day"] is _DAILY_ITEM


def test_pick_daily_no_match_is_unavailable():
    daily = WeatherResult(available=True, data={"days": [_DAILY_ITEM]})
    picked = _pick_daily(daily, dt.date(2030, 1, 1))
    assert picked.available is False
    assert "2030-01-01" in (picked.error or "")


def test_pick_daily_propagates_unavailable():
    daily = WeatherResult(available=False, error="boom")
    picked = _pick_daily(daily, _DAY)
    assert picked.available is False


# ============================================================
# 回归护栏：同一天并存「失败态」与「可用态」时，读取必须优先返回可用数据
# ============================================================
def _db_session():
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.db import models as _models  # noqa: F401 — 注册全部表

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_get_environment_prefers_available_record():
    """地点解析失败先写 ``location_id=""``，之后成功写入真实 id → 同一天两条。

    读取必须返回**可用**那条，否则会误报「天气不可用」并让硬约束失效。
    """
    from app.schemas.domain import EnvironmentDaily
    from app.storage.environment_store import get_environment, upsert_environment

    with _db_session() as s:
        # 先写失败态（location_id 为空）
        upsert_environment(
            s,
            EnvironmentDaily(
                date=_DAY,
                location_id="",
                location_name="安徽省安庆市宜秀区",
                available=False,
                source="qweather",
                fetched_at=dt.datetime(2026, 10, 16, 21, 41),
            ),
        )
        # 后写可用态（真实 location_id）
        upsert_environment(
            s,
            EnvironmentDaily(
                date=_DAY,
                location_id="101220601",
                location_name="安庆",
                lat="30.53183",
                lon="117.11535",
                available=True,
                temp_c=27.5,
                weather_text="雾",
                source="qweather",
                fetched_at=dt.datetime(2026, 10, 17, 12, 13),
            ),
        )

        got = get_environment(s, _DAY)
        assert got is not None
        assert got.available is True, "读取应优先返回可用记录，而非先插入的失败态"
        assert got.temp_c == 27.5
        assert got.location_id == "101220601"


def test_get_environment_returns_latest_when_both_unavailable():
    """两条都不可用时，返回最新抓取的那条（观测数据不该倒退）。"""
    from app.schemas.domain import EnvironmentDaily
    from app.storage.environment_store import get_environment, upsert_environment

    with _db_session() as s:
        for loc, hour, name in (("", 9, "旧"), ("101220601", 12, "新")):
            upsert_environment(
                s,
                EnvironmentDaily(
                    date=_DAY,
                    location_id=loc,
                    location_name=name,
                    available=False,
                    source="qweather",
                    fetched_at=dt.datetime(2026, 10, 17, hour, 0),
                ),
            )
        got = get_environment(s, _DAY)
        assert got is not None
        assert got.location_name == "新"


def test_qweather_key_path_not_relative_to_backend_cwd():
    """私钥默认路径必须回退一级（``../secrets``），否则从 backend/ 启动恒失败。"""
    from app.config import Settings

    p = Settings.model_fields["qweather_private_key_path"].default
    assert str(p).replace("\\", "/").startswith("../secrets/"), (
        f"私钥路径不应相对 backend/ 解析（当前 {p}）——仓库根 secrets/ 才正确"
    )
