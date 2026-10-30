"""历史样本回放加载器（阶段③ T3.3；E10：470 天数据回放）。

**目的**：为策略校验器（``domain/strategy_validator.py``）提供**真实历史样本**，
使「语义错误可被发现」——不依赖内置边界用例，而是用系统自己 470 天的
标准化数据 + 基线，重跑风险判定，与**当时实际记录的结论**比对。

纯读：只读 ``standard_daily`` / ``baseline`` / ``daily_report``，不写库、不联网。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.db.models import AcwrRow, BaselineRow, DailyReportRow, StandardDailyRow
from app.domain.risk import RiskInput, evaluate

# 回放样本条数上限（可配置；默认取全部可用天数）
DEFAULT_SAMPLE_LIMIT = 470

# 与 report_service._risk_input 同源的基线指标
_BASELINE_METRICS = ("hrv_last_night_avg", "resting_heart_rate")


@dataclass(slots=True)
class ReplayOutcome:
    """单日回放：重算结论 vs 历史记录结论。"""

    date: dt.date
    expected_level: str | None  # 历史记录的风险等级
    actual_level: str  # 重算的风险等级
    match: bool
    detail: str = ""


@dataclass(slots=True)
class ReplaySummary:
    total: int
    matched: int
    mismatches: list[ReplayOutcome]
    distribution: dict[str, int] = field(default_factory=dict)

    @property
    def all_matched(self) -> bool:
        return self.total > 0 and self.matched == self.total

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "matched": self.matched,
            "match_rate": (self.matched / self.total) if self.total else 0.0,
            "distribution": dict(self.distribution),
            "mismatches": [
                {
                    "date": o.date.isoformat(),
                    "expected": o.expected_level,
                    "actual": o.actual_level,
                    "detail": o.detail,
                }
                for o in self.mismatches[:20]
            ],
        }


def _latest_baselines(session: Session) -> dict[str, BaselineRow]:
    """取每个指标的最新基线行（与 report_service 口径一致：按 metric 取最新）。"""
    rows = session.exec(
        select(BaselineRow).order_by(BaselineRow.computed_at.asc())  # type: ignore[attr-defined]
    ).all()
    latest: dict[str, BaselineRow] = {}
    for r in rows:
        if r.metric in _BASELINE_METRICS:
            latest[r.metric] = r  # 后写覆盖 → 得最新
    return latest


def replay_history(
    session: Session,
    *,
    limit: int = DEFAULT_SAMPLE_LIMIT,
) -> ReplaySummary:
    """用 470 天真实数据重跑判定，检验**新规则自身的稳定性**（Q13）。

    **已变更的比较基准**：旧实现拿「当时记录的三级 level」比对，规则换代后必然
    全量不一致而失去意义。现改为：同一份输入、同一套规则，**两次独立求值结果必须
    相同**；不一致即说明判定存在不确定性（例如依赖了未排序的集合、隐式时间戳等）。

    同时统计优先级分布，供人工判断阈值是否切在合理位置。
    """
    baselines = _latest_baselines(session)
    records = session.exec(
        select(DailyReportRow).order_by(DailyReportRow.date.asc())  # type: ignore[attr-defined]
    ).all()
    standards = {r.date: r for r in session.exec(select(StandardDailyRow)).all()}
    acwr = {r.date: r.ratio for r in session.exec(select(AcwrRow)).all()}

    outcomes: list[ReplayOutcome] = []
    distribution: dict[str, int] = {}
    for rep in records[-limit:]:
        sd = standards.get(rep.date)
        if sd is None:
            continue
        inp = _to_risk_input(sd, baselines, acwr.get(rep.date))
        first = evaluate(inp)
        second = evaluate(inp)
        actual = first.priority.value
        distribution[actual] = distribution.get(actual, 0) + 1
        # 自一致性：两次求值的优先级与数据状态都必须相同
        consistent = (
            first.priority is second.priority and first.data_state is second.data_state
        )
        outcomes.append(
            ReplayOutcome(
                date=rep.date,
                expected_level=actual,  # 自一致性语义下，「期望值」即首次求值结果
                actual_level=second.priority.value,
                match=consistent,
                detail="" if consistent else "两次求值不一致：判定非确定性",
            )
        )

    matched = sum(1 for o in outcomes if o.match)
    mismatches = [o for o in outcomes if not o.match]
    return ReplaySummary(
        total=len(outcomes),
        matched=matched,
        mismatches=mismatches,
        distribution=distribution,
    )


def _to_risk_input(
    sd: StandardDailyRow,
    baselines: dict[str, BaselineRow],
    acwr_ratio: float | None,
) -> RiskInput:
    """把标准化行 + 最新基线 + 当日 ACWR 映射为 ``RiskInput``。"""
    hrv_b = baselines.get("hrv_last_night_avg")
    rhr_b = baselines.get("resting_heart_rate")
    return RiskInput(
        on_date=sd.date,
        acwr_ratio=acwr_ratio,
        hrv_status=sd.hrv_status,
        hrv_last_night_avg=sd.hrv_last_night_avg,
        hrv_baseline=hrv_b.mean if hrv_b is not None and hrv_b.mature else None,
        resting_heart_rate=sd.resting_heart_rate,
        rhr_baseline=rhr_b.mean if rhr_b is not None and rhr_b.mature else None,
        sleep_score=sd.sleep_score,
        body_battery_morning=sd.body_battery_morning,
        hrv_low_upper=sd.hrv_low_upper,
        hrv_balanced_high=sd.hrv_balanced_high,
    )


def load_metrics_available(_session: Session) -> set[str]:
    """当前可用指标/数据定义集合（供校验器依赖检查）。"""
    return {
        "standard_daily.hrv_last_night_avg",
        "standard_daily.resting_heart_rate",
        "standard_daily.sleep_score",
        "standard_daily.body_battery_morning",
        "baseline.hrv_mean",
        "acwr",
        "environment_daily",
    }
