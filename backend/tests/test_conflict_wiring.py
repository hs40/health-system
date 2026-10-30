"""冲突检测接线测试（T2.6）：端到端验证冲突进入质量分级与风险决策链。

覆盖一期缺口修复：`report_service` 曾写死 `conflicts=[]`；现由
`detect_day_conflicts` 产出并传给 `grade(conflicts=...)` 与 `RiskInput.conflicts`。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.schemas.domain import RawDailyRef, StandardDaily
from app.services.report_service import detect_day_conflicts


def _sd_with_sources(sources: list[str]) -> StandardDaily:
    return StandardDaily(
        date=dt.date(2026, 10, 14),
        resting_heart_rate=50,
        source_refs=[
            RawDailyRef(
                source=s,
                dataset="daily_summary",
                date=dt.date(2026, 10, 14),
                path=f"{s}/daily_summary/2026-10-14.json",
                fetched_at=dt.datetime(2026, 10, 14, 7, 0),
            )
            for s in sources
        ],
    )


def test_single_source_no_conflict():
    """单来源（现实情形）→ 不误报冲突。"""
    assert detect_day_conflicts(_sd_with_sources(["garmin"])) == []


def test_multi_source_is_explicitly_unimplemented():
    """多来源读数尚未接入（L-8，2026-10-25）：必须**显式失败**，不得静默返回空。

    此前这里给每个来源填同一份标准化值 → 两源恒等 → 永远返回 []，却在注释里
    写着「已接线」，读代码的人会误以为多源检测已可用。现在改为显式不可达：
    真接入时须逐源读取原始文件再构造读数。
    """
    sd = _sd_with_sources(["garmin", "tredict"])
    with pytest.raises(NotImplementedError):
        detect_day_conflicts(sd)
def test_conflict_enters_risk_input_as_conflicts():
    """冲突指标名进入 RiskInput.conflicts → 随证据落库供追溯。

    旧 R-008「冲突不得抵消危险信号」已随 R-001~R-008 一并退役（Q1/Q8）：
    四级优先级体系下**冲突不产生独立规则**，只作为证据保留；
    「不抵消危险信号」由「取最高优先级」这一结构保证 —— 见下一条用例。
    """
    from app.domain.risk import RiskInput, evaluate

    a = evaluate(RiskInput(on_date=dt.date(2026, 10, 14), conflicts=["resting_heart_rate"]))
    assert a.evidence["conflicts"] == ["resting_heart_rate"]
    assert a.triggered_rules == []  # C 态兜底，冲突不产生规则


def test_conflict_does_not_cancel_higher_priority():
    """冲突存在时，P1 判据仍然生效 → 危险信号不被「多数指标正常」抵消。"""
    from app.domain.risk import RiskInput, blocks_generate, evaluate
    from app.schemas.enums import PriorityLevel

    a = evaluate(
        RiskInput(
            on_date=dt.date(2026, 10, 14),
            acwr_ratio=1.6,
            hrv_status="LOW",
            sleep_score=85,
            body_battery_morning=85,
            conflicts=["resting_heart_rate"],
        )
    )
    assert a.priority is PriorityLevel.REST
    assert blocks_generate(a) is True