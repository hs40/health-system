"""运动训练页面接口（三期 §7/§8）。

FastAPI TestClient + ``get_settings`` 覆盖到 tmp_path 的 raw 目录；
只读 raw，不依赖 DB，也不需要 lifespan（避免 init_db 触碰真实数据）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    acts = tmp_path / "raw" / "garmin" / "activities"
    acts.mkdir(parents=True, exist_ok=True)
    (acts / "2026-11-05.json").write_text(
        json.dumps(
            [
                {
                    "activityId": "a1",
                    "activityName": "晨跑",
                    "activityType": {"typeKey": "running"},
                    "startTimeLocal": "2026-11-05 07:00:00",
                    "startTimeGMT": "2026-11-05 07:00:00",
                    "durationSeconds": 1800,
                    "distance": 5000,
                    "averageHR": 130,
                    "maxHR": 150,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, raw_data_dir=tmp_path / "raw"
    )
    c = TestClient(app)
    yield c
    app.dependency_overrides.clear()


def test_calendar_endpoint_returns_full_month_and_validates_ym(client: TestClient) -> None:
    r = client.get("/api/activities/calendar", params={"ym": "2026-11"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["days"]) == 30
    assert body["filter"] == "all"
    assert body["days"][4]["items"][0]["type"] == "running"

    filtered = client.get(
        "/api/activities/calendar", params={"ym": "2026-11", "type": "walking"}
    ).json()
    assert filtered["filter"] == "walking"
    assert filtered["days"][4]["items"] == []  # 筛选后仍保留完整月历

    # 非法 ym 必须是 400（AppError.VALIDATION），不能 500
    for bad in ("2026-13", "2026/11", "abc"):
        assert client.get("/api/activities/calendar", params={"ym": bad}).status_code == 400


def test_detail_endpoint_contract_empty_state_and_validation(client: TestClient) -> None:
    r = client.get("/api/activities/detail", params={"date": "2026-11-05"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["seq"] == 1
    assert body["activity"]["template"] == "running"
    assert body["activity"]["activity_id"] == "a1"
    assert body["prev_date"] == "2026-11-04"
    assert body["next_date"] == "2026-11-06"

    # §7.6 无训练日空态，仍保留自然日导航
    empty = client.get("/api/activities/detail", params={"date": "2026-11-04"}).json()
    assert empty["has_training"] is False
    assert empty["activity"] is None
    assert empty["prev_date"] == "2026-11-03"
    assert empty["next_date"] == "2026-11-05"

    # 非法 / 不存在的日期必须是 400，不能 500
    for bad in ("2026-02-30", "2026/11/05", "abc"):
        assert client.get("/api/activities/detail", params={"date": bad}).status_code == 400

    # seq 越界返回空 activity；小于 1 由 FastAPI 参数校验挡下
    assert (
        client.get("/api/activities/detail", params={"date": "2026-11-05", "seq": 9}).json()[
            "activity"
        ]
        is None
    )
    assert (
        client.get("/api/activities/detail", params={"date": "2026-11-05", "seq": 0}).status_code
        == 422
    )
