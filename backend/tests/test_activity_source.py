"""活动原始数据读取（2026-10-21 决策 Q5=B：废弃 garmin_activity 表）。

raw 是唯一事实源：``raw/garmin/activities/<date>.json``（活动列表）+ 
``raw/garmin/fit_jsonl/<aid>.jsonl.gz``（FIT 消息，力量动作 set 在这里）。

本模块替代原表的读取职责；4 个消费方（上传/匹配/负荷验证/校验）改读它。
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

from app.storage.activity_source import ActivityRef, load_activities, load_sets

AID = "652990997"


def _write_activity(tmp_path: Path, payload: list[dict]) -> Path:
    d = tmp_path / "garmin" / "activities"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "2026-10-20.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _write_jsonl(tmp_path: Path, aid: str, rows: list[dict]) -> Path:
    d = tmp_path / "garmin" / "fit_jsonl"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{aid}.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def _strength_payload(aid: str = AID) -> dict:
    return {
        "activityId": aid,
        "activityName": "力量训练",
        "activityType": {"typeKey": "strength_training"},
        "startTimeLocal": "2026-10-20 16:10:33",
        "startTimeGMT": "2026-10-20 08:10:33",
        "duration": 3126.5,
        "durationSeconds": 3126,
        "averageHR": 113,
        "summarizedExerciseSets": [{"category": "SQUAT", "sets": 3, "reps": 10, "volume": 0}],
    }


def test_load_activities_flattens_day_files(tmp_path: Path) -> None:
    _write_activity(tmp_path, [_strength_payload()])
    acts = load_activities(tmp_path / "garmin")
    assert set(acts) == {AID}
    a = acts[AID]
    assert isinstance(a, ActivityRef)
    assert a.activity_id == AID
    assert a.activity_type == "strength_training"
    assert a.name == "力量训练"
    assert a.start_local == dt.datetime(2026, 10, 20, 16, 10, 33)
    assert a.start_utc == dt.datetime(2026, 10, 20, 8, 10, 33)
    assert a.duration_sec == 3126
    assert a.payload["averageHR"] == 113


def test_load_activities_handles_multiple_per_day_and_missing_fields(tmp_path: Path) -> None:
    second = {"activityId": "2", "activityType": {"typeKey": "running"}}
    _write_activity(tmp_path, [_strength_payload(), second])
    acts = load_activities(tmp_path / "garmin")
    assert set(acts) == {AID, "2"}
    assert acts["2"].start_local is None  # 缺字段不抛，置 None
    assert acts["2"].duration_sec == 0


def test_load_activities_empty_when_no_dir(tmp_path: Path) -> None:
    assert load_activities(tmp_path / "nope") == {}


def test_load_sets_reads_fit_jsonl(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path,
        AID,
        [
            {"m": "record", "d": {"heart_rate": 120}},
            {"m": "set", "d": {"category": "SQUAT", "reps": 10, "weight": 0}},
            {"m": "set", "d": {"category": "PLANK", "duration": 30}},
        ],
    )
    sets = load_sets(tmp_path / "garmin", AID)
    assert len(sets) == 2
    assert sets[0]["category"] == "SQUAT"
    assert sets[1]["category"] == "PLANK"


def test_load_sets_empty_when_missing(tmp_path: Path) -> None:
    assert load_sets(tmp_path / "garmin", "no-such") == []


def test_load_sets_tolerates_bad_lines(tmp_path: Path) -> None:
    d = tmp_path / "garmin" / "fit_jsonl"
    d.mkdir(parents=True)
    with gzip.open(d / f"{AID}.jsonl.gz", "wt", encoding="utf-8") as f:
        f.write('{"m": "set", "d": {"category": "ROW"}}\n')
        f.write("not-json\n")
    assert [s["category"] for s in load_sets(tmp_path / "garmin", AID)] == ["ROW"]
