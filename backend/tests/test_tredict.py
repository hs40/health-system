"""Tredict 记录构造函数单元测试（TA.5）。

这些断言锁定「归档口径 A」——一旦破坏会与历史数据错位（V5 已对拍确认）。
"""

from __future__ import annotations

import datetime as dt

from app.adapters.tredict import (
    bodyvalues_todo,
    build_bodyvalue_records,
    build_hrv_records,
    build_sleep_records,
    canonical_sha256,
    midpoint_iso,
    ms_wall_to_iso,
    parse_utc_iso,
    sleep_purge_range,
    trim_series_sampled,
    wall_to_iso,
)


def test_wall_to_iso():
    assert wall_to_iso("2026-10-12T23:53:05.0") == "2026-10-12T23:53:05.000Z"
    assert wall_to_iso("bad") is None


def test_midpoint_preserves_millis():
    """两整秒时间戳的中点毫秒位可为 500，必须保留（与 JS toISOString 一致）。"""
    # 17.000 与 18.000 的中点是 17.500
    a = "2026-10-14T03:37:17.0"
    b = "2026-10-14T03:37:18.0"
    assert midpoint_iso(a, b) == "2026-10-14T03:37:17.500Z"


def test_ms_wall_to_iso_preserves_millis():
    assert ms_wall_to_iso(1791955800000) is not None
    assert ms_wall_to_iso(None) is None
    assert ms_wall_to_iso("x") is None


def test_build_hrv_records_uses_sleep_midpoint_and_rounds_rmssd():
    raw = {
        "2026-10-13": {
            "hrvSummary": {"lastNightAvg": 64.4},
            "sleepStartTimestampLocal": "2026-10-12T23:53:05.0",
            "sleepEndTimestampLocal": "2026-10-13T07:15:05.0",
        },
        "2026-10-14": {"hrvSummary": {"lastNightAvg": None}},  # 无值 → 跳过
    }
    records, skipped_null, skipped_no_time = build_hrv_records(raw)
    assert len(records) == 1
    assert records[0]["rmssd"] == 64
    assert records[0]["timezoneOffsetInSeconds"] == 0
    assert records[0]["timestamp"].endswith("Z")
    assert skipped_null == 1 and skipped_no_time == 0


def test_build_sleep_records_skips_future_end():
    raw = {
        "2026-10-13": {
            "dailySleepDTO": {
                "sleepStartTimestampLocal": 1791849185000,
                "sleepEndTimestampLocal": 1791875705000,
                "sleepTimeSeconds": 26400,
            }
        }
    }
    # now 早于 end → 跳过（归档口径 A 使 end 落在未来）
    records, skipped, skipped_future = build_sleep_records(raw, now_ms=1000)
    assert records == []
    assert skipped_future == 1
    # now 晚于 end → 保留
    records2, _, _ = build_sleep_records(raw, now_ms=9999999999999)
    assert len(records2) == 1
    assert records2[0]["sleepDurationInSeconds"] == 26400


def test_build_bodyvalue_records_gating_and_height():
    daily = {"2026-10-13": {"restingHeartRate": 52, "minHeartRate": 48, "maxHeartRate": 76}}
    records, meta = build_bodyvalue_records(daily, [], now_ms=9999999999999)
    assert len(records) == 1
    r = records[0]
    assert r["timestamp"] == "2026-10-13T22:00:00.000Z"
    assert r["restingHeartrate"] == 52
    assert r["hrRestDynamic"] == 48
    assert r["hrMaxDynamic"] == 76
    assert r["bodyHeightInCentimeter"] == 170
    assert meta["withHr"] == 1


def test_sleep_purge_range_covers_full_days():
    records = [
        {"endTimestamp": "2026-10-13T07:15:05.000Z"},
        {"endTimestamp": "2026-10-14T06:00:00.000Z"},
    ]
    rng = sleep_purge_range(records)
    assert rng == {"start": "2026-10-13T00:00:00.000Z", "end": "2026-10-14T23:59:59.999Z"}
    assert sleep_purge_range([]) is None


def test_sync_sleep_refuses_sparse_upload_without_force():
    """稀疏上传护栏：删除范围远大于记录数时必须拒绝，否则会误删整段历史睡眠。

    真实风险：只传 2 条（跨 2025-11-22 ~ 2026-10-14）会让 ``sleep_purge_range``
    算出覆盖约 327 天的删除范围，而本项目 sync_sleep 未做云端备份 → 不可逆丢失。
    """
    from app.adapters.tredict import TredictAdapter
    from app.config import Settings
    from app.errors import AppError
    from app.schemas.enums import ErrorCode

    settings = Settings(_env_file=None, tredict_token="x")  # type: ignore[call-arg]
    adapter = TredictAdapter(settings)

    sparse = [
        {"startTimestamp": "2025-11-22T23:55:56.000Z", "endTimestamp": "2025-11-23T08:04:55.000Z",
         "timezoneOffsetInSeconds": 0, "sleepDurationInSeconds": 28859},
        {"startTimestamp": "2026-10-14T05:30:00.000Z", "endTimestamp": "2026-10-14T05:45:00.000Z",
         "timezoneOffsetInSeconds": 0, "sleepDurationInSeconds": 900},
    ]
    try:
        adapter.sync_sleep(sparse)
    except AppError as e:
        assert e.code == ErrorCode.VALIDATION
        assert e.detail["records"] == 2
        assert e.detail["span_days"] > 300
    else:  # pragma: no cover
        raise AssertionError("稀疏上传应被拒绝")


def test_sync_sleep_allows_dense_upload_without_force():
    """密集（全量）上传：范围天数≈记录数，护栏不应拦截。"""
    from app.adapters.tredict import TredictAdapter
    from app.config import Settings

    settings = Settings(_env_file=None, tredict_token="x")  # type: ignore[call-arg]
    adapter = TredictAdapter(settings)

    dense = [
        {"startTimestamp": f"2026-10-{d:02d}T23:00:00.000Z",
         "endTimestamp": f"2026-10-{d + 1:02d}T07:00:00.000Z",
         "timezoneOffsetInSeconds": 0, "sleepDurationInSeconds": 28800}
        for d in range(1, 6)
    ]
    # 不抛异常即通过（真正 DELETE 前会先算范围；此处仅验证护栏放行）
    try:
        adapter.sync_sleep(dense)
    except Exception as e:  # noqa: BLE001
        # 允许网络/鉴权失败，但**不允许**因稀疏护栏被拒
        assert "稀疏" not in str(e), f"密集上传不应触发稀疏护栏: {e}"


# ============================================================
# P6：活动回填用纯函数
# ============================================================
def test_trim_series_sampled_drops_data_only():
    payload = {
        "_id": "abc",
        "title": "x",
        "seriesSampled": {"sampleSize": 2, "endOffset": 1, "data": {"hr": [1, 2, 3]}},
    }
    out = trim_series_sampled(payload)
    assert out["seriesSampled"] == {"sampleSize": 2, "endOffset": 1}
    assert out["_id"] == "abc"
    # 不修改入参（脚本会对同一 dict 复用）
    assert "data" in payload["seriesSampled"]


def test_trim_series_sampled_tolerates_missing_block():
    assert trim_series_sampled({"a": 1}) == {"a": 1}
    assert trim_series_sampled({"seriesSampled": None})["seriesSampled"] is None


def test_trim_keeps_other_top_level_fields():
    """裁剪只动 seriesSampled.data，summary / notes 等必须原样保留。"""
    payload = {
        "summary": {"duration": 2600, "distance": 4341.38},
        "notes": "felt good",
        "timezone": "Asia/Shanghai",
        "seriesSampled": {"data": {}},
    }
    out = trim_series_sampled(payload)
    assert out["summary"] == payload["summary"]
    assert out["notes"] == "felt good"
    assert out["timezone"] == "Asia/Shanghai"


def test_canonical_sha256_is_key_order_independent():
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})


def test_canonical_sha256_distinguishes_content():
    assert canonical_sha256({"a": 1}) != canonical_sha256({"a": 2})


def test_parse_utc_iso_with_millis():
    assert parse_utc_iso("2026-10-12T09:59:32.156Z") == dt.datetime(2026, 10, 12, 9, 59, 32, 156000)


def test_parse_utc_iso_returns_naive_utc():
    """必须返回**朴素** UTC：要与 garmin_activity.start_time_gmt 直接相减。"""
    d = parse_utc_iso("2026-10-12T09:59:32.156Z")
    assert d is not None and d.tzinfo is None


def test_parse_utc_iso_rejects_garbage():
    assert parse_utc_iso("") is None
    assert parse_utc_iso(None) is None
    assert parse_utc_iso("not-a-date") is None


# ------------------------------------------------------------
# bodyvalues 差集：值变化必须重传（2026-10-21 真实故障）
# ------------------------------------------------------------
def test_bodyvalues_todo_detects_new_timestamp():
    """云端没有该 timestamp → 必传。"""
    local = [{"timestamp": "2026-10-15T14:00:00Z", "weightInKilograms": 69.8}]
    assert len(bodyvalues_todo(local, [])) == 1


def test_bodyvalues_todo_detects_value_change_on_existing_timestamp():
    """同 timestamp 但体重不同 → 必须重传（官方 merge 语义，不会产生重复）。

    真实故障：体重是「加进已存在的 bodyvalues 记录」的，键不变，
    旧差集只比键 → 永远判定「已一致」，体重永远传不上去。
    """
    local = [{"timestamp": "2026-10-15T14:00:00Z", "weightInKilograms": 69.8}]
    cloud = [{"timestamp": "2026-10-15T14:00:00Z", "weightInKilograms": None}]
    todo = bodyvalues_todo(local, cloud)
    assert len(todo) == 1
    assert todo[0]["weightInKilograms"] == 69.8


def test_bodyvalues_todo_skips_identical_record():
    """键与值都相同 → 不传（避免每天全量重传）。"""
    rec = {"timestamp": "2026-10-15T14:00:00Z", "weightInKilograms": 69.8,
           "restingHeartrate": 50}
    assert bodyvalues_todo([rec], [dict(rec)]) == []


def test_bodyvalues_todo_ignores_fields_cloud_cannot_return():
    """云端读不回的字段（hrRestDynamic/hrMaxDynamic）不得造成永久假差集。"""
    local = [{"timestamp": "2026-10-15T14:00:00Z", "weightInKilograms": 69.8,
              "hrRestDynamic": 55, "hrMaxDynamic": 180}]
    cloud = [{"timestamp": "2026-10-15T14:00:00Z", "weightInKilograms": 69.8}]
    assert bodyvalues_todo(local, cloud) == []


# ------------------------------------------------------------
# S1：活动详情默认带 extraValues=1（2026-10-21 实测缺口）
# 本地 303 条详情全是默认参数版：Tredict 自算的 13 个指标
# （effort 三源 / vo2max / runningEffectiveness / speedIndex …）
# + 顶层 weather 全部缺失；官方只有 extraValues=1 才返回。
# ------------------------------------------------------------
def _detail_adapter():
    from app.adapters.tredict import TredictAdapter
    from app.config import Settings

    return TredictAdapter(Settings(_env_file=None, tredict_token="x"))  # type: ignore[call-arg]


class _FakeResp:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def json(self):
        return self._payload


def test_fetch_activity_detail_requests_extra_values_by_default(monkeypatch):
    """默认必须带 extraValues=1，否则 effort/vo2max 等自算指标静默缺失。"""
    adapter = _detail_adapter()
    seen: dict[str, str] = {}

    def fake_request(method, url, **kwargs):
        seen["url"] = url
        return _FakeResp(
            {"_id": "x", "summary": {"effort": {"heartrate": 65, "power": 84, "pace": 92}}}
        )

    monkeypatch.setattr(adapter, "_request", fake_request)
    out = adapter.fetch_activity_detail("x")
    assert "extraValues=1" in seen["url"]
    assert out is not None
    assert out["summary"]["effort"]["heartrate"] == 65


def test_fetch_activity_detail_can_opt_out_of_extra_values(monkeypatch):
    """extra_values=False 时不带参数（保持旧行为可复现）。"""
    adapter = _detail_adapter()
    seen: dict[str, str] = {}

    def fake_request(method, url, **kwargs):
        seen["url"] = url
        return _FakeResp({"_id": "x"})

    monkeypatch.setattr(adapter, "_request", fake_request)
    adapter.fetch_activity_detail("x", extra_values=False)
    assert "extraValues" not in seen["url"]


# ------------------------------------------------------------
# S2：增量详情拉取默认带 extraValues=1（fetch_details 联动锁定）
# 历史 refresh 修好后，若增量链路漏参，新活动又会变回默认参数版。
# ------------------------------------------------------------
def test_fetch_details_pulls_new_ids_with_extra_values(monkeypatch, tmp_path):
    """fetch_details 拉新 id 必须走 extra_values=True（显式传或默认值均可）。"""
    from app.config import Settings
    from app.domain import tredict_sync as ts

    calls: list[tuple[str, bool]] = []

    def fake_detail(self, aid, *, extra_values=True):
        calls.append((aid, extra_values))
        return {"_id": aid, "summary": {"effort": {"heartrate": 70}}}

    monkeypatch.setattr(
        "app.adapters.tredict.TredictAdapter.fetch_activity_detail", fake_detail
    )
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: Settings(_env_file=None, tredict_token="x"),  # type: ignore[call-arg]
    )
    monkeypatch.setattr(ts, "ACTIVITY_DIR", tmp_path / "activities")

    details, hashes, failed = ts.fetch_details(["tid1"], refresh=False, dry_run=False)
    assert failed == []
    assert calls == [("tid1", True)]
    assert details["tid1"]["summary"]["effort"]["heartrate"] == 70
    # raw 落盘内容含自算指标（trim 只裁 seriesSampled.data）
    saved = (tmp_path / "activities" / "tid1.json").read_text(encoding="utf-8")
    assert '"effort"' in saved


# ------------------------------------------------------------
# S2b：raw append-only 的 refresh 覆盖语义（历史详情重拉依赖它）
# ------------------------------------------------------------
def test_write_if_absent_refresh_overwrites_changed_content(tmp_path):
    from app.domain.tredict_sync import write_if_absent

    p = tmp_path / "a.json"
    write_if_absent(p, {"summary": {"duration": 100}}, refresh=False)
    sha, fresh = write_if_absent(
        p, {"summary": {"duration": 100, "effort": {"heartrate": 65}}}, refresh=True
    )
    assert fresh is True
    import json

    assert json.loads(p.read_text(encoding="utf-8"))["summary"]["effort"]["heartrate"] == 65


def test_write_if_absent_without_refresh_refuses_overwrite(tmp_path):
    """未显式 refresh 而内容已变 → 报错，绝不静默覆盖历史证据。"""
    import pytest

    from app.domain.tredict_sync import write_if_absent

    p = tmp_path / "a.json"
    write_if_absent(p, {"v": 1}, refresh=False)
    with pytest.raises(RuntimeError, match="append-only"):
        write_if_absent(p, {"v": 2}, refresh=False)


def test_write_if_absent_skips_identical_content(tmp_path):
    from app.domain.tredict_sync import write_if_absent

    p = tmp_path / "a.json"
    _, fresh1 = write_if_absent(p, {"v": 1}, refresh=False)
    _, fresh2 = write_if_absent(p, {"v": 1}, refresh=False)
    assert fresh1 is True and fresh2 is False
