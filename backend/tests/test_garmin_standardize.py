"""Garmin 标准化口径回归测试（TA.4）。

锁定既定口径（V4 已对拍确认）：
- 睡眠时长 = dailySleepDTO.sleepTimeSeconds；
- 睡眠起止 = 本地时间戳按 UTC 墙钟还原（offset=0）；
- 字段映射与现有 468 天数据集一致。
"""

from __future__ import annotations

import datetime as dt
import json

from app.adapters.garmin import GarminAdapter, _epoch_ms_to_local, _grams_to_kg
from app.config import Settings
from app.schemas.domain import RawDailyRef, StandardDaily
from app.storage.standard_store import from_row, to_row

DAY = dt.date(2026, 10, 13)


def _adapter_with_raw(payloads: dict[str, object]) -> GarminAdapter:
    a = GarminAdapter(Settings())
    a.read_raw = lambda dataset, day: payloads.get(dataset)  # type: ignore[method-assign]
    return a


def test_standardize_maps_all_fields():
    a = _adapter_with_raw(
        {
            "daily_summary": {
                "restingHeartRate": 52,
                "minHeartRate": 48,
                "maxHeartRate": 76,
                "totalSteps": 1234,
                "activeKilocalories": 300,
                "moderateIntensityMinutes": 10,
                "vigorousIntensityMinutes": 5,
                "bodyBatteryMostRecentValue": 69,
                "bodyBatteryAtWakeTime": 76,
                "avgWakingRespirationValue": 18,
            },
            "sleep": {
                "dailySleepDTO": {
                    "sleepTimeSeconds": 26400,
                    "sleepStartTimestampLocal": 1791849185000,
                    "sleepScores": {"overall": {"value": 77, "qualifierKey": "FAIR"}},
                }
            },
            "hrv": {"hrvSummary": {"lastNightAvg": 64}},
        }
    )
    sd = a.standardize(DAY)
    assert sd.resting_heart_rate == 52
    assert sd.min_heart_rate == 48
    assert sd.max_heart_rate == 76
    assert sd.steps == 1234
    assert sd.active_calories == 300
    assert sd.intensity_moderate_min == 10
    assert sd.intensity_vigorous_min == 5
    assert sd.body_battery_wake == 69
    assert sd.respiration_avg == 18.0
    assert sd.sleep_duration_sec == 26400
    assert sd.hrv_last_night_avg == 64.0


def test_standardize_parses_sleep_score_and_morning_battery():
    """风险引擎依赖的两个新字段（Q11）：睡眠得分与晨起电量。

    sleep_score ← dailySleepDTO.sleepScores.overall.value
    body_battery_morning ← dailySummary.bodyBatteryAtWakeTime（**非** MostRecent）
    """
    a = _adapter_with_raw(
        {
            "daily_summary": {
                "bodyBatteryMostRecentValue": 38,
                "bodyBatteryAtWakeTime": 76,
            },
            "sleep": {
                "dailySleepDTO": {"sleepScores": {"overall": {"value": 77}}},
            },
        }
    )
    sd = a.standardize(DAY)
    assert sd.sleep_score == 77
    assert sd.body_battery_morning == 76
    # 晨起值与当日最新值是两个不同口径，不得互相顶替
    assert sd.body_battery_wake == 38


def test_standardize_leaves_new_fields_none_when_absent():
    """字段缺失必须留空，不得用 0 或其他值顶替（Q24：形态 1/2 算缺失）。"""
    a = _adapter_with_raw({"daily_summary": {}, "sleep": {"dailySleepDTO": {}}})
    sd = a.standardize(DAY)
    assert sd.sleep_score is None
    assert sd.body_battery_morning is None


def test_sleep_start_is_local_wallclock_as_utc():
    """本地时间戳 → 按 UTC 还原墙钟（offset=0），与 tredict-upload.js 一致。"""
    got = _epoch_ms_to_local(1791849185000)
    assert got == dt.datetime.fromtimestamp(
        1791849185000 / 1000, tz=dt.UTC
    ).replace(tzinfo=None)


def test_standardize_tolerates_missing_datasets():
    a = _adapter_with_raw({})
    sd = a.standardize(DAY)
    assert sd.resting_heart_rate is None
    assert sd.sleep_duration_sec is None
    assert sd.hrv_last_night_avg is None


def test_grams_to_kg():
    assert _grams_to_kg(72500) == 72.5
    assert _grams_to_kg(None) is None


# ============================================================
# source_refs 可回溯（REQ-DATA-004 / D-1）
# ============================================================
def test_standardize_fills_source_refs(tmp_path):
    """仅收录实际存在、且参与取值的原始文件；path 相对 raw_data_dir（跨机可移植）。"""
    settings = Settings(_env_file=None, raw_data_dir=tmp_path / "raw")  # type: ignore[call-arg]
    a = GarminAdapter(settings)
    for dataset, payload in (
        ("daily_summary", {"restingHeartRate": 52}),
        ("sleep", {"dailySleepDTO": {"sleepTimeSeconds": 26400}}),
        # 同步了但**不参与**取值 → 不应出现在 refs
        ("stress", {"avgStressLevel": 30}),
    ):
        p = a._raw_path(dataset, DAY)  # noqa: SLF001
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload), encoding="utf-8")

    sd = a.standardize(DAY)

    datasets = {r.dataset for r in sd.source_refs}
    assert datasets == {"daily_summary", "sleep"}
    assert all(r.source == "garmin" and r.date == DAY for r in sd.source_refs)
    assert all(r.path.startswith("garmin/") for r in sd.source_refs)
    assert all(r.fetched_at is not None for r in sd.source_refs)


def test_standardize_source_refs_empty_when_no_raw(tmp_path):
    settings = Settings(_env_file=None, raw_data_dir=tmp_path / "raw")  # type: ignore[call-arg]
    assert GarminAdapter(settings).standardize(DAY).source_refs == []


def test_standard_store_roundtrips_source_refs():
    ref = RawDailyRef(
        source="garmin",
        dataset="hrv",
        date=DAY,
        path="garmin/hrv/2026-10-13.json",
        fetched_at=dt.datetime(2026, 10, 14, 9, 0),
    )
    sd = StandardDaily(date=DAY, resting_heart_rate=50, source_refs=[ref])
    assert from_row(to_row(sd)).source_refs == [ref]
