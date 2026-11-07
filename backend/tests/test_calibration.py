"""阈值校准对照回放的行为契约（ARCHITECTURE_REVIEW A.5.2）。

Seam：``app.domain.calibration.calibrate(session, overrides)``。

要证明的是「换一套阈值真的会改变判定结果」，而不是这台机器能否跑通——所以
断言全部落在**已知答案的构造样本**上：

- 样本 A：ACWR 1.48、HRV UNBALANCED。当前 ``ACWR_HIGH=1.5`` → 不触发 P1；
  候选 1.45 → 触发 P1。一天一个方向，答案可以手算。
- 样本 B：ACWR 1.42、HRV UNBALANCED。候选 1.45 也不够 → 两边都不触发。

覆盖写错的 rule_id / threshold 键必须报错，不能静默忽略（静默忽略会让人
以为「调了但没生效」）。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import AcwrRow, DailyReportRow, StandardDailyRow
from app.domain import rule_registry as rr
from app.domain.calibration import (
    CalibrationReport,
    OverrideError,
    build_candidate_rules,
    calibrate,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _seed(session: Session, days: list[tuple[dt.date, float, str]]) -> None:
    """写入 (日期, ACWR, HRV 状态) 样本；同时补一条日报（回放以日报为驱动）。"""
    for day, ratio, hrv in days:
        session.add(
            StandardDailyRow(
                date=day,
                hrv_status=hrv,
                hrv_last_night_avg=60.0,
                resting_heart_rate=50,
                sleep_score=80,
                body_battery_morning=80,
            )
        )
        session.add(AcwrRow(date=day, acute=ratio * 100, chronic=100.0, ratio=ratio))
        session.add(DailyReportRow(date=day, payload={"date": day.isoformat(), "version": 1}))
    session.commit()


def test_build_candidate_rules_overrides_threshold_without_touching_global():
    before = dict(rr.RULES["P1-LOAD-HRV"].thresholds)

    candidate = build_candidate_rules({"P1-LOAD-HRV": {"acwr_high": 1.45}})

    assert candidate["P1-LOAD-HRV"].thresholds["acwr_high"] == 1.45
    # 全局规则集必须原封不动——校准是只读操作
    assert rr.RULES["P1-LOAD-HRV"].thresholds == before


def test_build_candidate_rules_rejects_unknown_rule_id():
    with pytest.raises(OverrideError) as exc:
        build_candidate_rules({"P9-NOPE": {"acwr_high": 1.4}})
    assert "P9-NOPE" in str(exc.value)


def test_build_candidate_rules_rejects_unknown_threshold_key():
    with pytest.raises(OverrideError) as exc:
        build_candidate_rules({"P1-LOAD-HRV": {"acwr_highh": 1.4}})
    assert "acwr_highh" in str(exc.value)


def test_lowering_acwr_high_turns_a_borderline_day_into_p1(session):
    """ACWR 1.48 + HRV UNBALANCED：1.5 不触发，1.45 触发。"""
    day = dt.date(2026, 3, 10)
    _seed(session, [(day, 1.48, "UNBALANCED")])

    report = calibrate(session, {"P1-LOAD-HRV": {"acwr_high": 1.45}})

    assert report.total == 1
    assert report.changed == 1
    shift = report.shifts[0]
    assert shift.baseline_priority != "P1"
    assert shift.candidate_priority == "P1"
    assert report.migration_matrix() == {f"{shift.baseline_priority} -> P1": 1}


def test_candidate_that_does_not_bite_reports_zero_changes(session):
    """ACWR 1.42 连候选 1.45 都够不到 → 两边一致。"""
    day = dt.date(2026, 3, 11)
    _seed(session, [(day, 1.42, "UNBALANCED")])

    report = calibrate(session, {"P1-LOAD-HRV": {"acwr_high": 1.45}})

    assert report.total == 1
    assert report.changed == 0
    assert report.change_rate == 0.0
    assert report.migration_matrix() == {}


def test_only_the_borderline_day_shifts(session):
    """两条样本只应有一天翻转——证明差异来自阈值，不是「全量重算」。"""
    _seed(
        session,
        [
            (dt.date(2026, 3, 10), 1.48, "UNBALANCED"),
            (dt.date(2026, 3, 11), 1.42, "UNBALANCED"),
            (dt.date(2026, 3, 12), 1.05, "BALANCED"),
        ],
    )

    report = calibrate(session, {"P1-LOAD-HRV": {"acwr_high": 1.45}})

    assert report.total == 3
    assert report.changed == 1
    assert [s.date for s in report.shifts if s.changed] == [dt.date(2026, 3, 10)]


def test_report_serializes_to_json_safe_dict(session):
    _seed(session, [(dt.date(2026, 3, 10), 1.48, "UNBALANCED")])
    report = calibrate(session, {"P1-LOAD-HRV": {"acwr_high": 1.45}})

    out = report.to_dict()

    assert out["total"] == 1
    assert out["changed"] == 1
    assert out["overrides"] == {"P1-LOAD-HRV": {"acwr_high": 1.45}}
    assert out["shifts"][0]["date"] == "2026-03-10"
    assert set(out["shifts"][0]) == {"date", "from", "to", "rules"}


def test_empty_history_yields_empty_report(session):
    report = calibrate(session, {"P1-LOAD-HRV": {"acwr_high": 1.45}})
    assert isinstance(report, CalibrationReport)
    assert report.total == 0
    assert report.change_rate == 0.0
