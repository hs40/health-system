"""Garmin 数据集扩充（2026-10-21）。

背景（真实故障）：`DATASETS` 里没有 ``weight`` → 用户在 Garmin Connect 手动录入的
体重（10-15~10-19、10-21）永远拉不到本地；``lifestyle_logging`` / ``hydration``
等习惯数据同样从未同步。本文件锁定新数据集的拉取口径。

断言全部来自真实 API 返回形状（2026-10-21 实测）。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.adapters.garmin import DATASETS, OPTIONAL_DATASETS, GarminAdapter
from app.config import Settings

DAY = dt.date(2026, 10, 15)


class _FakeClient:
    """按接口名返回预置响应；未预置的接口一律断言失败（防拼错方法名）。"""

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses

    def __getattr__(self, name: str):  # noqa: ANN401
        if name.startswith("_"):
            raise AttributeError(name)

        def _call(*_a: object, **_kw: object) -> object:
            if name not in self._responses:
                raise AssertionError(f"未预期的 Garmin 接口：{name}")
            return self._responses[name]

        return _call


def _adapter() -> GarminAdapter:
    return GarminAdapter(Settings(_env_file=None))  # type: ignore[call-arg]


def test_new_datasets_are_registered() -> None:
    """契约：这三个数据集必须进入同步清单（否则永远不拉）。"""
    for name in ("weight", "lifestyle_logging", "hydration"):
        assert name in DATASETS, f"{name} 未进入 DATASETS"
    # 无数据属正常（没称重 / 没打卡），不得计为缺失
    for name in ("weight", "lifestyle_logging", "hydration"):
        assert name in OPTIONAL_DATASETS


def test_fetch_weight_returns_first_record() -> None:
    """体重：取 dateWeightList 首条（与既有 weight/*.json 单条格式兼容）。"""
    rec = {
        "samplePk": 1792021923570,
        "calendarDate": "2026-10-15",
        "weight": 69800.0,
        "sourceType": "MANUAL",
        "timestampGMT": 1792020840000,
    }
    client = _FakeClient({"get_body_composition": {"dateWeightList": [rec]}})
    got = _adapter()._fetch(client, "weight", DAY)  # noqa: SLF001
    assert got["weight"] == 69800.0
    assert got["timestampGMT"] == 1792020840000
    assert got["calendarDate"] == "2026-10-15"


def test_fetch_weight_empty_returns_none() -> None:
    """当天没称 → None（可选数据集，不计缺失）。"""
    client = _FakeClient({"get_body_composition": {"dateWeightList": []}})
    assert _adapter()._fetch(client, "weight", DAY) is None  # noqa: SLF001


def test_fetch_lifestyle_logging_returns_payload() -> None:
    payload = {
        "calendarDate": "2026-10-20",
        "dailyLogsReport": [
            {"behaviourId": 1, "name": "Alcohol", "logStatus": "NO", "category": "LIFESTYLE"}
        ],
    }
    client = _FakeClient({"get_lifestyle_logging_data": payload})
    assert _adapter()._fetch(client, "lifestyle_logging", DAY) == payload  # noqa: SLF001


def test_fetch_hydration_returns_payload() -> None:
    payload = {"calendarDate": "2026-10-20", "valueInML": 0.0, "goalInML": 3280.0}
    client = _FakeClient({"get_hydration_data": payload})
    assert _adapter()._fetch(client, "hydration", DAY) == payload  # noqa: SLF001


def test_sync_day_does_not_mark_empty_new_datasets_missing(monkeypatch) -> None:
    """新数据集空载荷 → 记为 fetched，不得进 missing（否则每天都判 partial）。"""
    client = _FakeClient(
        {
            "get_user_summary": {"totalSteps": 8000},
            "get_sleep_data": {"dailySleepDTO": {}},
            "get_hrv_data": {"hrvSummary": {}},
            "get_heart_rates": {},
            "get_body_battery": [],
            "get_stress_data": {},
            "get_spo2_data": {},
            "get_respiration_data": {},
            "get_activities_by_date": [],
            "get_body_composition": {"dateWeightList": []},
            "get_lifestyle_logging_data": {"calendarDate": DAY.isoformat(), "dailyLogsReport": []},
            "get_hydration_data": {"calendarDate": DAY.isoformat(), "valueInML": 0.0},
        }
    )
    a = _adapter()
    monkeypatch.setattr(GarminAdapter, "client", lambda self: client)
    monkeypatch.setattr(GarminAdapter, "_write_raw", lambda self, ds, d, p: True)
    res = a.sync_day(DAY)
    for name in ("weight", "lifestyle_logging", "hydration"):
        assert name not in res.missing, f"{name} 空载荷被误判为缺失"


# ------------------------------------------------------------
# S1b：其余按日数据集（「个人健康/训练全集」，2026-10-21 决策 Q4）
# ------------------------------------------------------------
PER_DAY_NEW = {
    "training_readiness": "get_training_readiness",
    "training_status": "get_training_status",
    "max_metrics": "get_max_metrics",
    "fitness_age": "get_fitnessage_data",
    "endurance_score": "get_endurance_score",
    "hill_score": "get_hill_score",
    "rhr_daily": "get_rhr_day",
    "steps_data": "get_steps_data",
    "floors": "get_floors",
    "intensity_minutes": "get_intensity_minutes_data",
    "calories_daily": "get_calories_daily",
    "all_day_events": "get_all_day_events",
}


@pytest.mark.parametrize("dataset,method", sorted(PER_DAY_NEW.items()))
def test_fetch_maps_new_per_day_dataset(dataset: str, method: str) -> None:
    payload = {"probe": dataset}
    client = _FakeClient({method: payload})
    assert _adapter()._fetch(client, dataset, DAY) == payload  # noqa: SLF001


def test_all_new_per_day_datasets_registered_and_optional() -> None:
    for name in PER_DAY_NEW:
        assert name in DATASETS, f"{name} 未进入 DATASETS"
        assert name in OPTIONAL_DATASETS, f"{name} 应允许空载荷"


def test_sync_day_respects_dataset_subset(monkeypatch) -> None:
    """回补只拉指定数据集：未指定的接口不得被调用（_FakeClient 会断言失败）。"""
    rec = {"weight": 69800.0, "timestampGMT": 1792020840000, "calendarDate": "2026-10-15"}
    client = _FakeClient({"get_body_composition": {"dateWeightList": [rec]}})
    a = _adapter()
    monkeypatch.setattr(GarminAdapter, "client", lambda self: client)
    monkeypatch.setattr(GarminAdapter, "_write_raw", lambda self, ds, d, p: True)

    res = a.sync_day(DAY, datasets=["weight"])
    assert res.fetched == ["weight"]
    assert res.missing == [] and res.errors == {}
