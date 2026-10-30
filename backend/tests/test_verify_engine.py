"""P7 校验引擎测试（纯函数层）。

锁住四条容易写错的规则：

1. **三态空值**（H2=A）：都空 → ok；表空/重算有 → `db_missing`；表有/重算空 → `db_only`。
   写成「只比非空字段」会让「提取失败」永久隐身。
2. **布尔不是数值**：`True == 1` 在 Python 里成立，不特判会把布尔列比错。
3. **逐秒表差异定位**：容差内的差异不能报成问题，否则会产出 `fields: []` 的假告警。
4. **退出码**：`ambiguous` 只警告；在线没跑成也算失败（不能给假绿灯）。
"""

from __future__ import annotations

import datetime as dt

from app.domain.verify import (
    FAIL_STATES,
    STATE_AMBIGUOUS,
    STATE_MISMATCH,
    STATE_MISSING_REMOTE,
    STATE_OK,
    CheckResult,
    canonical_sha256,
    check_activity_records,
    check_garmin_activity,
    check_standard_daily,
    check_tredict_activity,
    code_fingerprint,
    compare_online_garmin,
    compare_online_health,
    compare_online_tredict,
    diff_fields,
    exit_code,
    same_value,
    summarize,
)

DAY = dt.date(2026, 10, 17)
UTC = dt.datetime(2026, 10, 12, 9, 59, 32, 156000)
# UTC 09:59 + 8h = 本地 17:59，仍属同一天
TREDICT_DAY = dt.date(2026, 10, 12)


def rec(seq: int, t: int = 1_700_000_000, hr: int | None = 120) -> tuple:
    return (seq, t, hr, 2.5, 180.0, 84.0, 12.0, 100.0)


# ============================================================
# same_value
# ============================================================
def test_same_value_numbers_within_tolerance() -> None:
    assert same_value(1.0, 1.0 + 1e-12) is True
    assert same_value(100.0, 100.5) is False


def test_same_value_rejects_bool_vs_int() -> None:
    """`True == 1` 在 Python 里成立，校验必须区分。"""
    assert same_value(True, 1) is False
    assert same_value(False, 0) is False
    assert same_value(True, True) is True


def test_same_value_handles_none_and_strings() -> None:
    assert same_value(None, None) is True
    assert same_value(None, 1) is False
    assert same_value("BALANCED", "BALANCED") is True
    assert same_value("BALANCED", "LOW") is False


def test_same_value_handles_datetime() -> None:
    a = dt.datetime(2026, 10, 16, 23, 14, 51)
    assert same_value(a, dt.datetime(2026, 10, 16, 23, 14, 51)) is True
    assert same_value(a, dt.datetime(2026, 10, 16, 23, 14, 52)) is False


# ============================================================
# diff_fields 三态
# ============================================================
def test_diff_fields_both_none_is_ok() -> None:
    assert diff_fields({"a": None}, {"a": None}, ("a",)) == []


def test_diff_fields_db_missing_is_flagged() -> None:
    """raw 里有值但表里空 —— 这是「提取失败」，必须报出来。"""
    out = diff_fields({"a": None}, {"a": 71.0}, ("a",))
    assert out == [{"field": "a", "reason": "db_missing", "recomputed": 71.0}]


def test_diff_fields_db_only_is_flagged() -> None:
    """表里有值但 raw 推不出来 —— 数据来源不明，同样要报。"""
    out = diff_fields({"a": 71.0}, {"a": None}, ("a",))
    assert out == [{"field": "a", "reason": "db_only", "db": 71.0}]


def test_diff_fields_value_mismatch() -> None:
    out = diff_fields({"a": 60}, {"a": 71}, ("a",))
    assert out[0]["reason"] == "value"
    assert out[0]["db"] == 60 and out[0]["recomputed"] == 71


def test_diff_fields_ignores_unknown_keys() -> None:
    assert diff_fields({"a": 1, "id": 9}, {"a": 1, "date": DAY}, ("a",)) == []


# ============================================================
# 哈希
# ============================================================
def test_canonical_sha256_is_key_order_independent() -> None:
    assert canonical_sha256({"a": 1, "b": 2}) == canonical_sha256({"b": 2, "a": 1})


def test_canonical_sha256_handles_datetime() -> None:
    """库里的 datetime 必须能进哈希，否则报告生成会崩。"""
    a = canonical_sha256({"t": dt.datetime(2026, 10, 16, 23, 14, 51)})
    b = canonical_sha256({"t": "2026-10-16 23:14:51"})
    assert a == b


# ============================================================
# garmin_activity
# ============================================================
def test_garmin_activity_ok() -> None:
    payload = {"activityId": 1, "duration": 2600}
    out = check_garmin_activity(
        1, payload, canonical_sha256(payload), laps_count=2, sets_count=0, zones_count=4,
        archive_counts=(2, 0, 4),
    )
    assert out.state == STATE_OK
    assert out.detail == {}


def test_garmin_activity_sha_mismatch() -> None:
    payload = {"activityId": 1}
    out = check_garmin_activity(
        1, payload, "deadbeef" * 8, laps_count=1, sets_count=0, zones_count=0,
        archive_counts=(1, 0, 0),
    )
    assert out.state == STATE_MISMATCH
    assert "payload_sha256" in out.detail["problems"]


def test_garmin_activity_count_mismatch() -> None:
    payload = {"activityId": 1}
    out = check_garmin_activity(
        1, payload, canonical_sha256(payload), laps_count=2, sets_count=0, zones_count=4,
        archive_counts=(3, 0, 4),
    )
    assert out.detail["problems"]["laps"] == {"db": 2, "archive": 3}


def test_garmin_activity_null_columns_is_problem() -> None:
    """P4 之前写入的行三列是 NULL，必须报出来（不是「跳过」）。"""
    payload = {"activityId": 1}
    out = check_garmin_activity(
        1, payload, canonical_sha256(payload), laps_count=None, sets_count=None, zones_count=None,
        archive_counts=(0, 0, 0),
    )
    assert out.state == STATE_MISMATCH
    assert out.detail["problems"]["laps"] == "null"


def test_garmin_activity_missing_archive_is_missing_remote() -> None:
    payload = {"activityId": 1}
    out = check_garmin_activity(
        1, payload, canonical_sha256(payload), laps_count=1, sets_count=0, zones_count=0,
        archive_counts=None,
    )
    assert out.state == STATE_MISSING_REMOTE


# ============================================================
# garmin_activity_record
# ============================================================
def test_records_ok() -> None:
    rows = [rec(0), rec(1)]
    assert check_activity_records("A", rows, rows).state == STATE_OK


def test_records_row_count_differs() -> None:
    out = check_activity_records("A", [rec(0), rec(1)], [rec(0)])
    assert out.state == STATE_MISMATCH
    assert out.detail["problems"]["rows"] == {"db": 2, "archive": 1}


def test_records_value_diff_points_at_seq() -> None:
    out = check_activity_records("A", [rec(0), rec(7, hr=120)], [rec(0), rec(7, hr=121)])
    assert out.state == STATE_MISMATCH
    first = out.detail["problems"]["first_diff"]
    assert first["row"] == 1 and first["seq"] == 7
    assert first["fields"][0]["field"] == "hr"


def test_records_tolerance_does_not_produce_empty_diff() -> None:
    """差异在容差内 → 必须判 ok，不能产出 `fields: []` 的假告警。"""
    a = (0, 1, 120, 2.5, 180.0, 84.0, 12.0, 100.0)
    b = (0, 1, 120, 2.5 + 1e-15, 180.0, 84.0, 12.0, 100.0)
    out = check_activity_records("A", [a], [b])
    assert out.state == STATE_OK


def test_records_null_vs_value_is_diff() -> None:
    out = check_activity_records("A", [rec(0, hr=None)], [rec(0, hr=120)])
    assert out.state == STATE_MISMATCH


# ============================================================
# tredict_activity
# ============================================================
def _tredict(payload: dict | None = None, **kw):
    payload = payload or {"_id": "T1", "sportType": "running"}
    args = {
        "utc_datetime": UTC,
        "local_date": TREDICT_DAY,
        "matched_garmin_id": "G1",
        "match_confidence": "high",
        "garmin_ids": {"G1"},
        "tz_offset": dt.timedelta(hours=8),
    }
    args.update(kw)
    return check_tredict_activity("T1", payload, canonical_sha256(payload), **args)


def test_tredict_ok() -> None:
    assert _tredict().state == STATE_OK


def test_tredict_local_date_must_be_utc_plus_8() -> None:
    """UTC 20:00 → 本地次日 04:00，local_date 必须是次日。"""
    out = _tredict(
        utc_datetime=dt.datetime(2026, 10, 12, 20, 0, 0),
        local_date=dt.date(2026, 10, 12),
    )
    assert out.state == STATE_MISMATCH
    assert out.detail["problems"]["local_date"]["recomputed"] == "2026-10-13"


def test_tredict_matched_id_must_exist_in_garmin() -> None:
    out = _tredict(garmin_ids={"G-other"})
    assert out.state == STATE_MISMATCH
    assert out.detail["problems"]["matched_garmin_id"]["reason"] == "not_in_garmin_activity"


def test_tredict_high_without_match_is_problem() -> None:
    out = _tredict(matched_garmin_id=None, match_confidence="high")
    assert out.state == STATE_MISMATCH


def test_tredict_medium_without_match_is_fine() -> None:
    out = _tredict(matched_garmin_id=None, match_confidence="none")
    assert out.state == STATE_OK


# ============================================================
# standard_daily
# ============================================================
def test_standard_daily_ok() -> None:
    row = {"steps": 2023, "resting_heart_rate": 49, "weight_kg": None}
    assert check_standard_daily(DAY, row, dict(row)).state == STATE_OK


def test_standard_daily_reports_field_level_detail() -> None:
    out = check_standard_daily(DAY, {"steps": 2023}, {"steps": 2024})
    assert out.state == STATE_MISMATCH
    assert out.detail["problems"]["fields"][0]["field"] == "steps"
    assert out.entity_id == "2026-10-17"


def test_standard_daily_missing_weight_is_not_a_problem() -> None:
    """180 天没称重是数据源缺失，不该报错（H2=A）。"""
    row = {"weight_kg": None, "steps": 1}
    assert check_standard_daily(DAY, row, dict(row)).state == STATE_OK


# ============================================================
# 在线比对
# ============================================================
def test_online_garmin_detects_only_remote_and_local() -> None:
    remote = [{"activityId": 1, "duration": 100}, {"activityId": 2, "duration": 200}]
    local = {"1": {"activityId": 1, "duration": 100}, "3": {"activityId": 3, "duration": 300}}
    out = compare_online_garmin(remote, local)
    assert out["only_remote"] == ["2"]
    assert out["only_local"] == ["3"]
    assert out["field_mismatch"] == []


def test_online_garmin_detects_value_change() -> None:
    remote = [{"activityId": 1, "duration": 100}]
    local = {"1": {"activityId": 1, "duration": 999}}
    out = compare_online_garmin(remote, local)
    assert out["field_mismatch"] == [{"activity_id": "1", "fields": ["duration"]}]


def test_online_garmin_reports_unverifiable_fields() -> None:
    """本地 105 字段里那 17 个派生值必须被标成「不可验证」，不能假装验过。"""
    remote = [{"activityId": 1, "duration": 100}]
    local = {"1": {"activityId": 1, "duration": 100, "distanceMeters": 1000}}
    out = compare_online_garmin(remote, local)
    assert out["unverifiable_fields"] == ["distanceMeters"]


def test_online_tredict_compares_date_and_sport() -> None:
    from app.adapters.tredict import parse_utc_iso

    remote = [
        {
            "id": "T1",
            "date": "2026-10-12T09:59:32.156Z",
            "sportType": "running",
            "subSportType": "generic",
        }
    ]
    local = {"T1": {"utc_datetime": UTC, "sport_type": "misc", "sub_sport_type": "generic"}}
    out = compare_online_tredict(remote, local, parse_utc=parse_utc_iso)
    assert out["field_mismatch"][0]["fields"][0]["field"] == "sportType"


def test_online_tredict_ok() -> None:
    from app.adapters.tredict import parse_utc_iso

    remote = [
        {
            "id": "T1",
            "date": "2026-10-12T09:59:32.156Z",
            "sportType": "running",
            "subSportType": "generic",
        }
    ]
    local = {"T1": {"utc_datetime": UTC, "sport_type": "running", "sub_sport_type": "generic"}}
    assert compare_online_tredict(remote, local, parse_utc=parse_utc_iso)["field_mismatch"] == []


# ============================================================
# 健康在线比对（原盲区）
# ============================================================
def test_online_health_detects_lagging_days() -> None:
    """2026-10-15~17 事故的形态：本地有、云端没有 → 必须报出来。"""
    local = {
        "hrv": {"20261014", "20261015", "20261016", "20261017"},
        "sleep": {"20261015", "20261016"},
        "bodyvalues": {"2026-10-17T22:00:00"},
    }
    remote = {
        "hrv": {"20261014"},
        "sleep": {"20261015", "20261016"},
        "bodyvalues": {"2026-10-17T22:00:00"},
    }
    out = compare_online_health(local, remote)
    assert out["kinds"]["hrv"]["only_local"] == ["20261015", "20261016", "20261017"]
    assert out["kinds"]["hrv"]["only_remote"] == []
    assert out["kinds"]["sleep"]["only_local"] == []
    assert out["lagging"] == 3
    assert out["extra"] == 0


def test_online_health_ok_when_keys_match() -> None:
    local = {"hrv": {"20261017"}, "sleep": {"20261017"}, "bodyvalues": {"2026-10-17T22:00:00"}}
    out = compare_online_health(local, dict(local))
    assert out["lagging"] == 0
    assert out["extra"] == 0
    assert all(v["only_local"] == [] for v in out["kinds"].values())


def test_online_health_reports_remote_only() -> None:
    """云端有、本地没有 → 本地 raw 缺失或历史残留，也要报（不是「没问题」）。"""
    out = compare_online_health(
        {"hrv": set(), "sleep": set(), "bodyvalues": set()},
        {"hrv": {"20261001"}, "sleep": set(), "bodyvalues": set()},
    )
    assert out["kinds"]["hrv"]["only_remote"] == ["20261001"]
    assert out["extra"] == 1


def test_online_health_ignores_empty_keys() -> None:
    """``ymd()`` 解析失败会返回 None，不能把它当成一个「日期」参与比对。"""
    out = compare_online_health(
        {"hrv": {None, "20261017"}, "sleep": None, "bodyvalues": []},
        {"hrv": {"20261017"}},
    )
    assert out["kinds"]["hrv"]["local"] == 1
    assert out["kinds"]["hrv"]["only_local"] == []
    assert out["kinds"]["sleep"]["local"] == 0


# ============================================================
# 汇总与退出码
# ============================================================
def test_summarize_counts_every_state() -> None:
    results = [
        CheckResult("e", "1", STATE_OK),
        CheckResult("e", "2", STATE_MISMATCH),
        CheckResult("e", "3", STATE_AMBIGUOUS),
        CheckResult("e", "4", STATE_MISSING_REMOTE),
    ]
    stats = summarize(results)
    assert stats[STATE_OK] == 1
    assert stats[STATE_MISMATCH] == 1
    assert stats[STATE_AMBIGUOUS] == 1
    assert stats[STATE_MISSING_REMOTE] == 1


def test_exit_code_ignores_ambiguous() -> None:
    """Q5=A：ambiguous 只警告，不算失败。"""
    assert exit_code([CheckResult("e", "1", STATE_AMBIGUOUS)]) == 0


def test_exit_code_fails_on_mismatch() -> None:
    assert exit_code([CheckResult("e", "1", STATE_MISMATCH)]) == 1


def test_exit_code_fails_when_online_did_not_run() -> None:
    """D=A：没验成不等于验过了。"""
    assert exit_code([CheckResult("e", "1", STATE_OK)], online_failed=True) == 1


def test_exit_code_fails_when_online_found_diff() -> None:
    """在线跑成了但有差异，也必须失败——只记录不报错等于「看了不说」。"""
    assert exit_code([CheckResult("e", "1", STATE_OK)], online_diff=3) == 1
    assert exit_code([CheckResult("e", "1", STATE_OK)], online_diff=0) == 0


def test_fail_states_excludes_ambiguous_and_ok() -> None:
    assert STATE_OK not in FAIL_STATES
    assert STATE_AMBIGUOUS not in FAIL_STATES


# ============================================================
# 代码指纹
# ============================================================
def test_code_fingerprint_covers_three_modules() -> None:
    fp = code_fingerprint()
    assert set(fp) == {"standardize", "build_records", "match_activities"}
    assert all(len(v) == 16 for v in fp.values())


def test_code_fingerprint_is_stable_within_process() -> None:
    assert code_fingerprint() == code_fingerprint()


# ============================================================
# 归档还原（真实踩过的坑）
# ============================================================
def test_rehydrate_record_row_turns_iso_string_into_datetime() -> None:
    """jsonl 里 timestamp 是 ISO 字符串，`_build_records` 只认 datetime。

    不还原 → 所有 record 被当成「没有时间戳」丢掉 → 301 个活动全部 0 行。
    这条测试锁死这个行为。
    """
    from app.domain.verify import rehydrate_record_row

    row = {"timestamp": "2025-07-04T11:52:37.000Z", "heart_rate": 115}
    out = rehydrate_record_row(row)
    assert isinstance(out["timestamp"], dt.datetime)
    assert out["timestamp"].tzinfo is not None
    want = dt.datetime(2025, 7, 4, 11, 52, 37, tzinfo=dt.UTC)
    assert out["timestamp"].timestamp() == want.timestamp()


def test_rehydrate_record_row_keeps_datetime_untouched() -> None:
    from app.domain.verify import rehydrate_record_row

    row = {"timestamp": dt.datetime(2025, 7, 4, 11, 52, 37)}
    assert rehydrate_record_row(row) is row


def test_rehydrate_record_row_tolerates_garbage() -> None:
    from app.domain.verify import rehydrate_record_row

    assert rehydrate_record_row({"timestamp": "not-a-date"})["timestamp"] == "not-a-date"
    assert rehydrate_record_row({}) == {}


def test_build_records_after_rehydrate_reproduces_seven_channels() -> None:
    """端到端：归档行 → 还原 → `_build_records` → 8 元组。"""
    from app.domain.verify import rehydrate_record_row
    from app.parsers.fit import _build_records

    rows = [
        {"timestamp": "2025-07-04T11:52:37.000Z", "heart_rate": 115, "enhanced_speed": 2.5,
         "power": 180, "cadence": 84, "enhanced_altitude": 12.0, "distance": 100.0},
        {"heart_rate": 116},  # 无时间戳 → 必须被丢弃
    ]
    points = _build_records([rehydrate_record_row(r) for r in rows])
    assert len(points) == 1
    p = points[0]
    assert (p.seq, p.hr, p.speed, p.power, p.cadence, p.altitude, p.distance) == (
        0, 115, 2.5, 180.0, 84.0, 12.0, 100.0,
    )
