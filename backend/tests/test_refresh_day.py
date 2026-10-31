"""refresh_day：重试时必须把 07:30 的占位睡眠更新为完整记录（2026-10-31 故障）。

背景：07:30 Garmin 返回 ``dailySleepDTO`` 全 null 的占位记录（非空 dict → 被判"已写入"）。
``sync_day`` 是 append-only，后续重试即使拿到完整睡眠也不会覆盖它。故重试改用
``refresh_day``（滚动窗口），把占位记录刷新为完整记录；activities 仍按 id 合并，不降级。
"""

from __future__ import annotations

import datetime as dt
import json

from app.adapters.garmin import GarminAdapter
from app.config import Settings

DAY = dt.date.today() - dt.timedelta(days=1)  # 窗口内

PLACEHOLDER_SLEEP = {
    "dailySleepDTO": {
        "calendarDate": DAY.isoformat(),
        "sleepStartTimestampLocal": None,
        "sleepEndTimestampLocal": None,
        "sleepTimeSeconds": None,
    }
}
COMPLETE_SLEEP = {
    "dailySleepDTO": {
        "calendarDate": DAY.isoformat(),
        "sleepStartTimestampLocal": 1792972713000,
        "sleepEndTimestampLocal": 1792999233000,
        "sleepTimeSeconds": 26520,
    }
}
A = {"activityId": 1, "activityName": "morning run", "activityType": {"typeKey": "running"}}
B = {
    "activityId": 2,
    "activityName": "evening strength",
    "activityType": {"typeKey": "strength_training"},
}


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
        garmin_tokenstore=tmp_path / "garmin_tokens",
    )


def _seed(settings: Settings, dataset: str, payload) -> None:  # noqa: ANN001
    p = settings.raw_data_dir / "garmin" / dataset / f"{DAY.isoformat()}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class _FakeClient:
    def __init__(self, sleep: dict, activities: list[dict]):
        self._sleep = sleep
        self._activities = activities

    def __getattr__(self, name: str):  # noqa: ANN401
        if name == "get_sleep_data":
            return lambda d: self._sleep
        if name == "get_activities_by_date":
            return lambda a, b: list(self._activities)
        return lambda *a, **k: {}


def test_refresh_day_replaces_placeholder_sleep(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    adapter = GarminAdapter(settings)
    _seed(settings, "sleep", PLACEHOLDER_SLEEP)
    _seed(settings, "activities", [A])
    monkeypatch.setattr(GarminAdapter, "client", lambda self: _FakeClient(COMPLETE_SLEEP, [A, B]))

    adapter.refresh_day(DAY)

    dto = adapter.read_raw("sleep", DAY)["dailySleepDTO"]
    assert dto["sleepStartTimestampLocal"] == 1792972713000, "占位睡眠必须被完整记录替换"
    assert dto["sleepEndTimestampLocal"] == 1792999233000

    ids = {str(x["activityId"]) for x in (adapter.read_raw("activities", DAY) or [])}
    assert ids == {"1", "2"}, "刷新时 activities 仍须合并，不得降级"


def test_refresh_day_absorbs_client_failure(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)
    adapter = GarminAdapter(settings)

    def _boom(self):  # noqa: ANN001
        raise RuntimeError("网络中断")

    monkeypatch.setattr(GarminAdapter, "client", _boom)

    result, _changed = adapter.refresh_day(DAY)  # 不得抛异常

    assert result.status == "failed"
    assert result.errors, "客户端失败必须逐数据集记录错误"
