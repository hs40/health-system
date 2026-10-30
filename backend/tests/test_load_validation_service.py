"""验证服务测试（需求 6.4 / 7.1 / 7.2 / 7.4，开发计划 4.10）。

断言全部来自业务规则：

| 测试组 | 对应需求 |
|---|---|
| 只读：不写库、不联网 | 6.4 |
| effort 配对：按同日开始时间顺序，数量不一致整天跳过 | 6.4 配对口径 |
| heartrate/power/pace 仅跑步 | 7.1 |
| 同一活动可按指标分别产出样本，但每组只含本指标 | 7.2 |
| Tredict 指标只做样本，不进负荷 | 7.4 |
"""

from __future__ import annotations

import datetime as dt
import json
import socket

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.db.models import TredictGeneratedRow
from app.services import validation_service

D = dt.date(2026, 10, 14)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def settings(tmp_path):
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
    )


def _activity(settings, day: dt.date, idx: int, sport: str, minutes: float, hr: int,
              power: float | None = None, speed: float | None = None,
              hour: int = 8) -> None:
    """写一条 raw 活动（2026-10-21：``garmin_activity`` 表废弃，改读 raw）。"""
    payload: dict = {
        "activityId": f"{day.isoformat()}-{idx}",
        "activityType": {"typeKey": sport},
        "duration": minutes * 60.0,
        "durationSeconds": minutes * 60,
        "averageHR": hr,
        "startTimeLocal": dt.datetime.combine(day, dt.time(hour, idx)).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "startTimeGMT": dt.datetime.combine(day, dt.time(hour, idx)).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }
    if power is not None:
        payload["avgPower"] = power
    if speed is not None:
        payload["avgSpeed"] = speed
    d = settings.raw_data_dir / "garmin" / "activities"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{day.isoformat()}.json"
    items = json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    items.append(payload)
    f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def _effort_row(day: dt.date, values: list[float]) -> TredictGeneratedRow:
    return TredictGeneratedRow(
        entity="effort",
        entity_key=day.isoformat(),
        version=1,
        content_hash=f"effort-{day.isoformat()}",
        payload={"date": day.strftime("%Y%m%d"), "efforts": [[v] for v in values]},
    )


def _run(session, settings, **kw):
    return validation_service.run_validation(session, settings, **kw)


# ------------------------------------------------------------
# 需求 6.4：只读
# ------------------------------------------------------------
def test_validation_does_not_write_to_db(session, settings):
    """需求 6.4：验证不写库 —— 跑前跑后所有表行数不变。"""
    _activity(settings, D, 0, "running", 40.0, 150.0, hour=7)
    _activity(settings, D, 1, "running", 30.0, 140.0, hour=18)
    session.add(_effort_row(D, [56.0, 42.0]))
    session.commit()

    before = {TredictGeneratedRow: len(session.query(TredictGeneratedRow).all())}
    out = _run(session, settings)
    after = {TredictGeneratedRow: len(session.query(TredictGeneratedRow).all())}
    assert before == after
    assert out["pairing"]["paired"] == 2


def test_validation_never_touches_network(session, settings, monkeypatch):
    """需求 6.4：只用本地数据 —— 全程不允许发起网络连接。"""
    def _boom(*args, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("验证过程发起了网络连接（Garmin/Tredict 都不应被调用）")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    _activity(settings, D, 0, "running", 40.0, 150.0)
    session.add(_effort_row(D, [56.0]))
    session.commit()
    out = _run(session, settings)
    assert out["metrics"], "至少要产出指标结果"


# ------------------------------------------------------------
# 需求 6.4 配对口径 + 7.2 指标隔离
# ------------------------------------------------------------
def test_effort_pairs_by_start_time_order(session, settings):
    """同日多活动：effort 按开始时间顺序一一配对，不交叉。"""
    _activity(settings, D, 0, "running", 60.0, 160.0, hour=7)   # 较早 → effort 大
    _activity(settings, D, 1, "running", 20.0, 120.0, hour=20)  # 较晚 → effort 小
    session.add(_effort_row(D, [90.0, 12.0]))
    session.commit()

    samples, stats = validation_service.collect_samples(session, settings)
    assert stats["activities"] == 2 and stats["paired"] == 2
    by_id = {s.activity_id: s for s in samples if s.metric == "trainingEfforts"}
    assert by_id[f"{D.isoformat()}-0"].y == pytest.approx(90.0)
    assert by_id[f"{D.isoformat()}-1"].y == pytest.approx(12.0)
    # 负荷与 effort 同向（长+高心率 → 大 effort）
    assert by_id[f"{D.isoformat()}-0"].x > by_id[f"{D.isoformat()}-1"].x


def test_day_with_mismatched_effort_count_is_skipped_whole_day(session, settings):
    """当日活动数与 effort 数不一致 → 整天不配对，宁可不配也不错配。"""
    _activity(settings, D, 0, "running", 40.0, 150.0)
    _activity(settings, D, 1, "running", 30.0, 140.0)
    session.add(_effort_row(D, [56.0]))  # 只有 1 个 effort，活动有 2 个
    session.commit()

    samples, stats = validation_service.collect_samples(session, settings)
    assert not any(s.metric == "trainingEfforts" for s in samples)
    assert stats["unmatched_days"] == 1
    # paired 只数「产出任一指标准样本」的活动：effort 断了，
    # 但两条跑步活动的 heartrate/power/pace 仍来自活动 payload 本身，应照常配对
    assert stats["paired"] == 2


def test_same_activity_yields_per_metric_samples(session, settings):
    """同一条跑步活动可同时产出四个指标样本（各归各的 metric，不混）。"""
    _activity(settings, D, 0, "running", 40.0, 150.0, power=210.0, speed=3.1)
    session.add(_effort_row(D, [56.0]))
    session.commit()

    samples, stats = validation_service.collect_samples(session, settings)
    assert {s.metric for s in samples} == {
        "trainingEfforts", "effort.heartrate", "effort.power", "effort.pace",
    }
    assert stats["paired"] == 1            # 1 条活动
    assert stats["sample_rows"] == 4       # 4 行指标样本
    hr = next(s for s in samples if s.metric == "effort.heartrate")
    pace = next(s for s in samples if s.metric == "effort.pace")
    assert hr.y == pytest.approx(150.0)
    assert pace.y == pytest.approx(3.1)


def test_non_running_activity_contributes_only_training_effort(session, settings):
    """需求 7.1：heartrate/power/pace 仅跑步；HIIT（有负荷、非跑步）只进 trainingEfforts。"""
    _activity(settings, D, 0, "hiit", 30.0, 165.0)
    session.add(_effort_row(D, [20.0]))
    session.commit()

    samples, stats = validation_service.collect_samples(session, settings)
    assert {s.metric for s in samples} == {"trainingEfforts"}
    assert all(s.sport == "hiit" for s in samples)
    assert stats["paired"] == 1


def test_walking_record_only_activity_is_not_paired(session, settings):
    """步行属 record_only：负荷本就不可计算，不配对、不计 0（与生产口径一致）。"""
    _activity(settings, D, 0, "walking", 30.0, 105.0)
    session.add(_effort_row(D, [20.0]))
    session.commit()

    samples, stats = validation_service.collect_samples(session, settings)
    assert stats["activities"] == 1
    assert stats["no_load"] == 1
    assert samples == []


def test_report_separates_metrics_and_sports(session, settings):
    """报告按指标 × 运动类型分组，且不含通过/排名字段（需求 7.3）。"""
    for i in range(12):
        day = D - dt.timedelta(days=i)
        _activity(settings, day, 0, "running", 30.0 + i, 140.0 + i,
                              power=200.0 + i, speed=3.0, hour=7)
        session.add(_effort_row(day, [40.0 + 2.0 * i]))
    session.commit()

    out = _run(session, settings)
    metrics = {m["metric"]: m for m in out["metrics"]}
    assert set(metrics) == {
        "trainingEfforts", "effort.heartrate", "effort.power", "effort.pace",
    }
    for entry in out["metrics"] + out["by_sport"]:
        assert "fit" in entry
        for banned in ("pass", "passed", "fail", "verdict", "score", "rank"):
            assert banned not in entry
            assert banned not in entry["fit"]
    # 每个指标都覆盖至少一个运动类型分组
    sports = {r["metric"] for r in out["by_sport"]}
    assert sports == set(metrics)


def test_non_computable_activity_is_disclosed_not_zero_filled(session, settings):
    """负荷不可计算的活动：如实计入 no_load，不配对（不填 0、不估算）。"""
    _activity(settings, D, 0, "running", 0.0, 0)  # 时长/心率无效
    _activity(settings, D, 1, "running", 40.0, 150.0)
    session.add(_effort_row(D, [56.0]))
    session.commit()

    samples, stats = validation_service.collect_samples(session, settings)
    assert stats["activities"] == 2
    assert stats["no_load"] == 1
    assert all(s.activity_id.endswith("-1") for s in samples)


# ------------------------------------------------------------
# 需求 6.4：后台管理接口（手动触发、只读）
# ------------------------------------------------------------
def test_ops_endpoint_returns_report(session, settings, monkeypatch):
    """后台管理接口：与脚本同一服务层，结果同构、只读。"""
    from app.api import ops

    monkeypatch.setattr("app.api.ops.get_settings", lambda: settings)
    _activity(settings, D, 0, "running", 40.0, 150.0)
    session.add(_effort_row(D, [56.0]))
    session.commit()

    # 直接调用函数时显式传 None（HTTP 路径下由 FastAPI 依赖注入解析默认值）
    out = ops.validate_load(start=None, end=None, session=session)
    assert "metrics" in out and "by_sport" in out and "disclaimer" in out
    assert out["pairing"]["paired"] == 1


def test_ops_endpoint_date_filter(session, settings, monkeypatch):
    """接口支持 from/to 过滤，范围外数据不参与。"""
    from app.api import ops

    monkeypatch.setattr("app.api.ops.get_settings", lambda: settings)
    _activity(settings, D, 0, "running", 40.0, 150.0)          # 范围内
    _activity(settings, D - dt.timedelta(days=10), 0, "running", 40.0, 150.0)  # 范围外
    session.add(_effort_row(D, [56.0]))
    session.commit()

    out = ops.validate_load(start=D, end=D, session=session)
    assert out["pairing"]["activities"] == 1
