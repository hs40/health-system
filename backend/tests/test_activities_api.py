"""运动训练页面接口（三期 §7/§8）。

FastAPI TestClient + ``get_settings`` 覆盖到 tmp_path 的 raw 目录；
只读 raw，不依赖 DB，也不需要 lifespan（避免 init_db 触碰真实数据）。
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app

_DAY = "2026-11-05"
_ACTIVITY = {
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


def _write_activity_day(root: Path) -> None:
    acts = root / "raw" / "garmin" / "activities"
    acts.mkdir(parents=True, exist_ok=True)
    (acts / f"{_DAY}.json").write_text(
        json.dumps([_ACTIVITY], ensure_ascii=False), encoding="utf-8"
    )


def _write_fit(root: Path, rows: list[dict]) -> None:
    d = root / "raw" / "garmin" / "fit_jsonl"
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / "a1.jsonl.gz", "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    _write_activity_day(tmp_path)
    # 详情页两张心率图的唯一数据源；此前 fixture 完全没建 fit_jsonl，
    # 所以「有逐秒心率」这条正路径从未被接口层测试覆盖过（测试盲区）。
    _write_fit(
        tmp_path,
        [
            {"m": "record", "d": {"timestamp": "2026-11-05T07:00:00+00:00", "heart_rate": 130}},
            {"m": "record", "d": {"timestamp": "2026-11-05T07:00:01+00:00", "heart_rate": 135}},
            {"m": "record", "d": {"timestamp": "2026-11-05T07:00:02+00:00", "heart_rate": 140}},
        ],
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


def test_detail_endpoint_exposes_hr_chart_data_from_fit_jsonl(client: TestClient) -> None:
    """正路径：详情必须带出两张心率图的数据，期望值来自 fixture 里的独立字面量。"""
    body = client.get("/api/activities/detail", params={"date": _DAY}).json()
    a = body["activity"]
    assert a["hr_data_state"] == "ok"
    assert [p["hr"] for p in a["hr_series"]] == [130, 135, 140]
    assert a["hr_zones"]["counted_seconds"] == 3


def test_detail_endpoint_reports_fit_missing_when_raw_has_no_fit_jsonl(tmp_path: Path) -> None:
    """NAS 故障现场：只挂了 activities、没有 fit_jsonl，接口必须显式报 fit_missing。"""
    _write_activity_day(tmp_path)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, raw_data_dir=tmp_path / "raw"
    )
    try:
        body = TestClient(app).get("/api/activities/detail", params={"date": _DAY}).json()
    finally:
        app.dependency_overrides.clear()
    a = body["activity"]
    assert a["hr_data_state"] == "fit_missing"
    assert a["hr_series"] == []
    assert a["hr_zones"]["counted_seconds"] == 0


def test_activities_endpoints_are_described_by_openapi_response_models() -> None:
    """训练详情/月历必须纳入 OpenAPI 契约，前端才能从 schema.d.ts 派生而非手抄。"""
    app.openapi_schema = None
    schema = app.openapi()
    for path, model in (
        ("/api/activities/detail", "DetailResponse"),
        ("/api/activities/calendar", "CalendarResponse"),
    ):
        op = schema["paths"][path]["get"]
        response_schema = (
            op["responses"]["200"]
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
        )
        assert response_schema.get("$ref") == f"#/components/schemas/{model}", path
    props = schema["components"]["schemas"]["ActivityDetail"]["properties"]
    assert {"hr_data_state", "hr_series", "hr_zones"} <= set(props)
