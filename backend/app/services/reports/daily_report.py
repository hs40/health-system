"""日报生成（REQ-REPORT-001）+ 周计划卡片 + 负荷/ACWR 重算 + 冲突检测。

从原 ``report_service.py`` 拆出（①，2026-10-26），行为不变。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app.adapters.garmin import GarminAdapter
from app.config import Settings
from app.db.models import (
    DailyReportRow,
    TrainingConflictRow,
    TrainingSessionRow,
    WeeklyPlanRow,
)
from app.domain.adjustment import (
    ADJUSTMENT_PREFIX,
    DailyAdjustment,
    apply_adjustment_name,
    card_view,
    day_reason,
    judge_day,
)
from app.domain.algo_version import algo_versions
from app.domain.baseline import compute_all
from app.domain.dates import week_start as _week_start_of
from app.domain.load import (
    CHRONIC_DAYS,
    METHOD_MIXED,
    SPORT_TIERS,
    TIER_COUNTED,
    acwr_for,
    hr_mod,
)
from app.domain.load.types import VS_INSUFFICIENT, VS_UNREGISTERED
from app.domain.quality import REQUIRED_METRICS, grade
from app.domain.report_link import build_strategy_link
from app.domain.risk import (
    RECOVERY_ADVISORY,
    RiskInput,
    evaluate,
)
from app.domain.rule_registry import ACWR_STALE_WINDOW_DAYS
from app.errors import AppError
from app.schemas.api import WeeklyPlan
from app.schemas.domain import (
    AcwrSnapshot,
    DailyReport,
    DataConflict,
    StandardDaily,
    TrainingLoad,
    WeekPlanDay,
)
from app.schemas.enums import (
    AuditCategory,
    ErrorCode,
    PriorityLevel,
    QualityGrade,
    QualityLevel,
)
from app.services.reports.goal_progress import load_goal_progress
from app.services.reports.report_store import _persist
from app.storage.derived_store import (
    load_acwr,
    load_range,
    upsert_acwr,
    upsert_baselines,
    upsert_load,
)
from app.storage.standard_store import get_standard, load_standard_range

# 基线回溯起点（E23）：静息心率/睡眠改用「全历史均值 ± 1σ」后，基线必须看到
# 截至当日的**全部**历史，故不再设 90 天截断。性能影响可忽略（470 行量级）。
_BASELINE_HISTORY_START = dt.date(1970, 1, 1)

# 需求 6.3：验证状态 → 日报披露文案（各运动类型独立管理，互不影响）。
# 「照常计入」必须写进文案：本条的告知对象是「这个分值能不能跨类型比」，不是
# 「这个分值算错了」——避免用户误以为负荷被算漏。
_VERIFY_NOTE = {
    VS_INSUFFICIENT: "可比性尚未验证（分值为可计算值，照常计入）",
    VS_UNREGISTERED: "可比性待验证（无专属公式，按通用公式兜底，分照常计入）",
}


def _present_metrics(sd: StandardDaily, level: QualityLevel) -> set[str]:
    return {m for m in REQUIRED_METRICS[level] if getattr(sd, m, None) is not None}


def _hrv_balanced_streak(dailies: list, day: dt.date) -> int:
    """连续 BALANCED 天数（含当天，Q17）。遇到非 BALANCED 即停。"""
    by_date = {d.date: d.hrv_status for d in dailies if d.date <= day}
    streak = 0
    cur = day
    while True:
        if by_date.get(cur) != "BALANCED":
            break
        streak += 1
        cur = cur - dt.timedelta(days=1)
        if streak > 30:  # 防御：窗口上限
            break
    return streak


def _risk_input(
    day: dt.date,
    sd: StandardDaily,
    baselines: dict,
    *,
    session: Session,
    acwr_snap=None,
    dailies: list | None = None,
    missing_critical: list[str] | None = None,
    conflicts: list[str] | None = None,
) -> RiskInput:
    """组装风险判定输入（2026-10-22 新契约）。

    ACWR 缺失时按 Q3 回落到最近 ≤3 天内有值的快照，并打 ``acwr_stale`` 标记。
    """
    hrv_b = baselines.get("hrv_last_night_avg")
    rhr_b = baselines.get("resting_heart_rate")

    # —— ACWR（含 ≤3 天惯性回落，Q3 / Q27）——
    acwr_ratio = acwr_snap.ratio if acwr_snap is not None else None
    acwr_source_date = day if acwr_ratio is not None else None
    acwr_stale = False
    if acwr_ratio is None:
        for k in range(1, ACWR_STALE_WINDOW_DAYS + 1):
            snap = load_acwr(session, day - dt.timedelta(days=k))
            if snap is not None and snap.ratio is not None:
                acwr_ratio = snap.ratio
                acwr_source_date = snap.date
                acwr_stale = True
                break

    return RiskInput(
        on_date=day,
        acwr_ratio=acwr_ratio,
        acwr_stale=acwr_stale,
        acwr_source_date=acwr_source_date,
        hrv_status=sd.hrv_status,
        hrv_last_night_avg=sd.hrv_last_night_avg,
        hrv_baseline=hrv_b.mean if hrv_b is not None and hrv_b.mature else None,
        hrv_balanced_streak=_hrv_balanced_streak(dailies or [], day),
        resting_heart_rate=sd.resting_heart_rate,
        rhr_baseline=rhr_b.mean if rhr_b is not None and rhr_b.mature else None,
        sleep_score=sd.sleep_score,
        body_battery_morning=sd.body_battery_morning,
        hrv_low_upper=sd.hrv_low_upper,
        hrv_balanced_high=sd.hrv_balanced_high,
        conflicts=list(conflicts or []),
    )


# 参与跨源冲突检测的字段（E6 全量字段：取标准化层全部数值字段）
def _sync_errors_from_audit(session: Session, day: dt.date) -> dict[str, str]:
    """取当天 `DATA_SOURCE` 审计事件里的同步错误（供日报重生成时复原披露）。"""
    from sqlmodel import select

    from app.db.models import AuditEventRow

    row = session.exec(
        select(AuditEventRow)
        .where(
            AuditEventRow.category == AuditCategory.DATA_SOURCE.value,
            AuditEventRow.ref == day.isoformat(),
        )
        .order_by(AuditEventRow.at.desc())  # type: ignore[attr-defined]
    ).first()
    if row is None:
        return {}
    errors = (row.detail or {}).get("errors") or {}
    return {str(k): str(v) for k, v in errors.items()}


def detect_day_conflicts(sd: StandardDaily) -> list[DataConflict]:
    """检测当日标准化数据的跨源冲突（T2.5/T2.6，E6）。

    数据来源：``StandardDaily.source_refs`` 中实际存在的来源（当前主要是 garmin）。
    单来源时**不会**产生冲突（这是正常情形，不误报）。

    > 说明：本系统为单用户、单设备（Garmin）为主。跨源冲突的**完整**能力（如与
    > Tredict 读数、手动录入并置）依赖多源数据接入；此处已把检测**接线到决策链**，
    > 一旦出现多源即生效（避免一期「写死 conflicts=[]」的死管道）。
    """
    sources = sorted({r.source for r in sd.source_refs})
    if len(sources) < 2:
        return []  # 单来源：无可比对象，不误报

    # 多来源：**读数未接入**（L-8，2026-10-25 用户决策）。
    # 此前这里给每个来源填同一份 sd 值 → 两源恒等 → detect_conflicts 永远返回 []，
    # 注释却写着「已接线」，读代码的人会误以为多源检测已可用（比没有更危险）。
    # 现在显式不可达：真正接入多源时，必须逐源读取原始文件再构造读数。
    # 引擎 ``domain/conflict_detector.py`` 保留——它是那时唯一可用的载体。
    raise NotImplementedError(
        "多源读数尚未接入：请先实现逐源读取（raw/<source>/...）再放开冲突检测"
    )


def persist_conflicts(session: Session, day: dt.date, conflicts: list[DataConflict]) -> None:
    """把冲突记录落库（T2.6：``training_conflict`` 表），供审计与前端展示。"""
    for c in conflicts:
        kept = next((x for x in c.values if "kept_value" in x), {})
        session.add(
            TrainingConflictRow(
                date=day,
                metric=c.metric,
                severity=c.severity.value,
                resolution=c.resolution.value,
                kept_source=str(kept.get("kept_source", "")),
                kept_value={"value": kept.get("kept_value")},
                detail={"values": c.values},
            )
        )
    if conflicts:
        session.commit()


def _week_plan(session: Session, day: dt.date) -> list[WeekPlanDay] | None:
    """取本周计划（若已生成，阶段 B）；否则返回 None。"""
    ws = _week_start_of(day)
    row = session.exec(
        select(WeeklyPlanRow)
        .where(WeeklyPlanRow.week_start == ws)
        .order_by(WeeklyPlanRow.version.desc())  # type: ignore[attr-defined]
    ).first()
    if row is None:
        return None
    days = (row.payload or {}).get("days")
    return [WeekPlanDay(**d) for d in days] if days else None


def _latest_weight(session: Session, day: dt.date, lookback_days: int = 180) -> float | None:
    """最近有效体重（前向沿用）。

    体重是慢变量，单日缺失不该让自重动作整片变成「不可计算」——
    实测 66 条力量活动有 11 条当日无体重。这里取 ``day`` 之前最近一次实测值，
    **不用估算、不用均值**；超过 ``lookback_days`` 仍无记录则返回 ``None``
    （宁可不可计算，也不拿半年前的体重充数）。
    """
    rows = load_standard_range(session, day - dt.timedelta(days=lookback_days), day)
    for r in sorted(rows, key=lambda x: x.date, reverse=True):
        if r.weight_kg:
            return float(r.weight_kg)
    return None


# Garmin typeKey → 周计划 sport 值（2026-10-20：两套词汇在此单一归一）。
# 计划侧用 LLM 缩写（strength / indoorCardio，见 Sport 枚举），Garmin 侧用
# snake_case typeKey（strength_training / indoor_cardio）。完成度判定与卡片
# 摘要都先过这张表，否则同一种运动会被误判成「替代训练」。
_GARMIN_TO_PLAN_SPORT = {
    "running": "running",
    "strength_training": "strength",
    "indoor_cardio": "indoorCardio",
}


def _plan_sport(type_key: str | None) -> str | None:
    """Garmin typeKey → 计划侧 sport 值（未登记的类型原样返回）。"""
    if not type_key:
        return None
    return _GARMIN_TO_PLAN_SPORT.get(type_key, type_key)


def _day_actual_summary(settings: Settings, day: dt.date) -> str | None:
    """当天实际训练情况摘要（§12.3 卡片 factual 展示：全部记录，非仅 counted 档）。

    卡片回答的是「实际发生了什么」，与完成度判定（只算 counted 档）是两回事，
    故这里列全部活动；无记录返回 None。
    """
    raw = GarminAdapter(settings).read_raw("activities", day) or []
    parts: list[str] = []
    for a in raw:
        sport = (a.get("activityType") or {}).get("typeKey") or "other"
        sec = a.get("durationSeconds") or a.get("duration") or 0
        mins = round(float(sec or 0) / 60.0)
        if mins > 0:
            parts.append(f"{sport} {mins}min")
    return "；".join(parts) if parts else None


def build_week_cards(session: Session, settings: Settings, plan: WeeklyPlan) -> dict[str, dict]:
    """周计划卡片视图（§12.3；2026-10-20 决策 Q3/Q10）：逐日 ``card_view``，现算不落库。

    调整证据（两处任一）：当日日报 ``adjustment_reason`` 非空，或当日训练名带
    「调整」前缀（§12.2 规范标记）。调整日附五项详情；未调整日只带原安排。
    """
    cards: dict[str, dict] = {}
    for d in plan.days:
        day = d.date
        planned_name = d.session.name if d.session else None

        row = session.exec(
            select(TrainingSessionRow).where(
                TrainingSessionRow.id == f"session-{day.isoformat()}"
            )
        ).first()
        report_row = session.exec(
            select(DailyReportRow).where(DailyReportRow.date == day)
        ).first()
        reason = (report_row.payload or {}).get("adjustment_reason") if report_row else None
        adjusted = bool(reason) or (row is not None and row.name.startswith(ADJUSTMENT_PREFIX))

        adjustments: list[DailyAdjustment] = []
        if adjusted and row is not None:
            adjustments.append(
                DailyAdjustment(
                    date=day,
                    original_name=planned_name or "",
                    adjusted_name=apply_adjustment_name(planned_name or "", row.name),
                    reason=reason or "",
                    report_ref=f"daily:{day.isoformat()}",
                    actual=_day_actual_summary(settings, day),
                )
            )
        cards[day.isoformat()] = card_view(planned_name, adjustments, date=day)
    return cards


def _yesterday_completion(
    session: Session, day: dt.date, settings: Settings
) -> dict | None:
    """前一日训练完成度（§12.4；2026-10-20 决策 Q7）。

    日报 07:30 生成时，``day - 1`` 已完整过去（23:00 活动同步已跑），
    此时判定最稳。取前一日**当天生效计划**（被日报调整则取调整后计划）、
    前一日实际活动（Garmin raw，counted 档过滤与负荷域同口径），
    交域层 ``judge_day`` 判定，序列化进日报 payload 供周报提取聚合（S5）。

    无计划无记录 → None（不判定、不伪造）。
    """
    y = day - dt.timedelta(days=1)

    # 当天生效计划：默认取周计划原安排；被日报调整则取调整后当日训练。
    week_days = _week_plan(session, y) or []
    planned_day = next((d for d in week_days if d.date == y), None)
    planned = planned_day.session if planned_day else None
    eff_sport: str | None = planned.sport.value if planned else None
    eff_min = planned.duration_min if planned else None
    eff_zone = planned.target_hr_zone if planned else None

    adjusted = False
    daily_row = session.exec(
        select(TrainingSessionRow).where(TrainingSessionRow.id == f"session-{y.isoformat()}")
    ).first()
    if daily_row is not None:
        payload = daily_row.payload or {}
        # 调整标记：日报 adjustment_reason，或名称「调整」前缀（§12.2 规范标记）
        report_row = session.exec(
            select(DailyReportRow).where(DailyReportRow.date == y)
        ).first()
        reason = (report_row.payload or {}).get("adjustment_reason") if report_row else None
        adjusted = bool(reason) or daily_row.name.startswith(ADJUSTMENT_PREFIX)
        if adjusted:
            eff_sport = str(payload.get("sport") or daily_row.sport)
            eff_min = payload.get("duration_min")
            eff_zone = payload.get("target_hr_zone")

    # 实际活动：Garmin raw 归一成 {sport, minutes, average_hr}
    # （sport 归一到计划侧词汇，供同口径比较；分钟/心率保持原值）
    raw = GarminAdapter(settings).read_raw("activities", y) or []
    acts = [
        {
            "sport": _plan_sport((a.get("activityType") or {}).get("typeKey")),
            "minutes": float(a.get("durationSeconds") or a.get("duration") or 0) / 60.0,
            "average_hr": a.get("averageHR"),
        }
        for a in raw
    ]
    # counted 档位取自负荷域 SPORT_TIERS（单一事实来源），词汇归一到计划侧，
    # 使档位过滤与「计划 vs 实际」比较同一套口径
    counted = frozenset(
        _plan_sport(k) for k, v in SPORT_TIERS.items() if v == TIER_COUNTED
    )

    j = judge_day(
        y,
        planned_sport=eff_sport,
        planned_min=eff_min,
        target_zone=eff_zone,
        adjusted=adjusted,
        activities=acts,
        counted_sports=counted,
        as_of=day,
    )
    if j.status is None:
        return None
    out = j.to_dict()
    out["reason"] = day_reason(j)
    return out


def _yesterday_training(
    session: Session,
    settings: Settings,
    day: dt.date,
    *,
    completion: dict | None = None,
) -> dict | None:
    """前一日实际训练内容（逐活动：类型 / 名称 / 时长 / 负荷 / 完成度）。

    2026-10-26 修复：此前 ``generate_daily`` 硬编码 ``yesterday_training=None``，
    导致前端「前一日运动训练回顾」永远空白——即便 raw 活动已同步、完成度也已判定。
    """
    y = day - dt.timedelta(days=1)
    adapter = GarminAdapter(settings)
    raw = adapter.read_raw("activities", y) or []
    if not raw:
        return None

    # 逐活动负荷：与当日同口径（standardize_activities 的 contributions）
    sd = get_standard(session, y) or StandardDaily(date=y)
    daily = adapter.standardize_activities(
        y, sd, body_weight_kg=_latest_weight(session, y) or sd.weight_kg
    )
    score_by_id = {str(c.activity_id): c.score for c in daily.contributions}
    status = (completion or {}).get("status")

    items: list[dict] = []
    for a in raw:
        aid = str(a.get("activityId"))
        sport = _plan_sport((a.get("activityType") or {}).get("typeKey")) or "other"
        sec = a.get("duration") or a.get("durationSeconds") or 0
        items.append(
            {
                "sport": sport,
                "name": a.get("activityName") or "训练记录",
                "duration_sec": int(round(float(sec or 0))),
                "load": round(score_by_id[aid], 1) if aid in score_by_id else None,
                "status": status,
            }
        )
    return {"date": y.isoformat(), "activities": items}


def recompute_day_load(
    session: Session, day: dt.date, settings: Settings
) -> TrainingLoad | None:
    """按**已入库**的原始活动重算某日负荷并落库（需求 10.1–10.2）。

    与 ``generate_daily`` 内的当日负荷计算同一口径；区别是不触碰基线/风险/日报，
    供重算编排从最早受影响日逐日回放。
    """
    sd = get_standard(session, day) or StandardDaily(date=day)
    adapter = GarminAdapter(settings)
    daily = adapter.standardize_activities(
        day, sd, body_weight_kg=_latest_weight(session, day) or sd.weight_kg
    )
    if not (daily.contributions or daily.not_computable):
        return None
    load = TrainingLoad(
        date=day,
        by_sport=daily.by_sport,
        combined=daily.total,
        method=METHOD_MIXED,
        not_computable=[c.to_dict() for c in daily.not_computable],
        unverified_sports=daily.unverified_sports,
    )
    upsert_load(session, load)
    return load


def refresh_acwr(session: Session, day: dt.date) -> AcwrSnapshot | None:
    """按当前负荷序列重算某日 ACWR 并落库（需求 8.x，EWMA τ=7/28）。

    只读已落库的 ``training_load``；缺失日按 0 参与 EWMA（需求 8.3）。
    """
    lookback = CHRONIC_DAYS * 2  # 让 EWMA 有足够预热
    start = day - dt.timedelta(days=lookback)
    history = load_range(session, start, day)
    if not history:
        return None
    totals = {x.date: x.combined for x in history}
    state = acwr_for(totals, day, lookback_start=start)
    snap = AcwrSnapshot(
        date=day,
        acute=state.acute_ewma,
        chronic=state.chronic_ewma,
        ratio=state.ratio,
        sufficient=state.sufficient,
        note=state.note,
    )
    upsert_acwr(session, snap)
    return snap


def _loads_and_acwr(
    session: Session, day: dt.date, sd: StandardDaily, settings: Settings
) -> tuple[TrainingLoad | None, AcwrSnapshot | None]:
    """计算并落库当日训练负荷与 ACWR。

    口径（需求 4.4 / 8.1，用户 2026-10-20 确认）：
    - 当日负荷：逐活动先标准化到 0–100，再按类型求和、跨类型相加；
      无活动（休息日）→ ``None``；
    - ACWR：**EWMA**（τ=7/28），不是简单日均比；慢性为 0 → 不产出；
    - 不可计算的活动随 ``not_computable`` 一并落库，供日报披露。
    """
    adapter = GarminAdapter(settings)
    daily = adapter.standardize_activities(
        day, sd, body_weight_kg=_latest_weight(session, day) or sd.weight_kg
    )
    today_load = None
    if daily.contributions or daily.not_computable:
        today_load = TrainingLoad(
            date=day,
            by_sport=daily.by_sport,
            combined=daily.total,
            method=METHOD_MIXED,
            not_computable=[c.to_dict() for c in daily.not_computable],
            unverified_sports=daily.unverified_sports,
        )
        upsert_load(session, today_load)

    return today_load, refresh_acwr(session, day)


def generate_daily(
    session: Session,
    day: dt.date,
    settings: Settings,
    *,
    during_recompute: bool = False,
    sync_errors: dict[str, str] | None = None,
) -> DailyReport:
    """生成并落库指定日期的日报。

    Q4（用户 2026-10-20 确认）：**重算执行期间不生成当天日报**，重算结束后再生成。
    ``during_recompute=True`` 只允许重算任务内部调用（此时重算任务持锁）。

    ``sync_errors``：当日 Garmin 同步的错误表（``GarminSyncResult.errors``）。
    **只用于披露**——活动侧「休息日」与「未同步」在数据层同形（休息日不写 raw
    文件），唯一诚实的信号是同步错误态；它不得改质量分级、不得影响计划闸门
    （G-7 第一步，2026-10-24 决策）。
    """
    # Q4（2026-10-25）：重生成日报时拿不到当次 GarminSyncResult —— 回到审计日志取，
    # 否则「活动同步失败」这条披露会在重算/手动重生成后静默消失。
    if sync_errors is None:
        sync_errors = _sync_errors_from_audit(session, day)

    if not during_recompute:
        from app.services import recompute_service  # 局部导入：避免循环依赖

        # blocking_task：先收割僵尸（崩溃残留的 retry）再判定——
        # 否则一个死任务会按 Q4 永久挡住日报生成
        running = recompute_service.blocking_task(session)
        if running is not None:
            raise AppError(
                ErrorCode.TASK_IN_PROGRESS,
                f"训练负荷重算进行中（task={running.id}），重算结束后会自动生成当天日报",
                {"taskId": running.id, "startedAt": running.started_at.isoformat()},
            )

    lookback_start = _BASELINE_HISTORY_START
    dailies = load_standard_range(session, lookback_start, day)
    sd = get_standard(session, day) or StandardDaily(date=day)

    baselines = compute_all(dailies, day)
    upsert_baselines(session, baselines)

    # 跨源冲突检测（T2.5/T2.6，E6）：全量字段跨源比对 → 冲突进质量分级与风险。
    # 多源读数尚未接入时 `detect_day_conflicts` 会抛 NotImplementedError（L-8）：
    # 这里降级为「不判冲突 + 披露」，绝不让日报生成因未来接入第二来源而崩。
    conflict_note: str | None = None
    try:
        conflicts = detect_day_conflicts(sd)
    except NotImplementedError as exc:
        conflicts = []
        conflict_note = f"多源冲突检测未接入：{exc}"
    persist_conflicts(session, day, conflicts)

    level = QualityLevel.ADJUSTMENT
    quality = grade(day, level, _present_metrics(sd, level), conflicts=conflicts, notes=[])
    # 关键安全数据不足 → 该日的数据状态会落到 B/C（不再靠旧 R-007 硬拦截表达）
    missing_critical = quality.missing if quality.grade == QualityGrade.INSUFFICIENT else []

    # 负荷与 ACWR 必须在风险判定**之前**算好：P1/P2/P3 全部依赖 ACWR。
    load, acwr_snap = _loads_and_acwr(session, day, sd, settings)

    risk = evaluate(
        _risk_input(
            day,
            sd,
            baselines,
            session=session,
            acwr_snap=acwr_snap,
            dailies=dailies,
            missing_critical=missing_critical,
            conflicts=[c.metric for c in conflicts],
        )
    )

    limitations: list[str] = []
    if quality.grade.value != "ok":
        missing_txt = "、".join(quality.missing) or "无"
        limitations.append(f"数据质量：{quality.grade.value}（缺失：{missing_txt}）")
    if not baselines or not all(b.mature for b in baselines.values()):
        limitations.append("个人基线尚未成熟，风险判定走保守分支")
    if acwr_snap is None:
        limitations.append("训练负荷历史不足，ACWR 暂不可用")
    elif not acwr_snap.sufficient and acwr_snap.note:
        limitations.append(f"ACWR 仅供参考：{acwr_snap.note}")
    # 需求 4.5：不可计算的活动必须披露，不得静默当作 0
    for item in (load.not_computable if load else []):
        limitations.append(
            f"活动 {item.get('activity_id')}（{item.get('sport')}）负荷不可计算："
            f"{item.get('reason')}"
        )
    # 需求 6.3：跨运动类型可比性未验证的类型**照常计分**，但须显式披露状态
    for sport_key, status in (load.unverified_sports if load else {}).items():
        note = _VERIFY_NOTE.get(status, "")
        limitations.append(f"运动类型 {sport_key}：{note}（{status}）")
    # G-7 第一步（2026-10-24）：活动同步失败只披露。活动侧「休息日」与「未同步」
    # 在数据层同形（休息日不写 raw 文件），唯一诚实的信号是同步错误态；
    # 不进 quality.grade、不进拦截——否则同步一出错就会误拦计划生成。
    if conflict_note:
        limitations.append(conflict_note)
    activity_sync_error = (sync_errors or {}).get("activities")
    if activity_sync_error:
        limitations.append(
            f"活动数据同步失败（{activity_sync_error}）：当天负荷与 ACWR 可能偏低，"
            f"不能据此判断训练量"
        )

    # —— 四级优先级的对外文案（Q6 / Q22 / Q26）——
    advisory: list[str] = []
    priority = risk.priority
    if priority is PriorityLevel.REST:
        advisory.append(RECOVERY_ADVISORY)
        limitations.append(
            f"优先级 P1（强制休息）：不生成当天训练计划、不上传；"
            f"命中 {', '.join(risk.triggered_rules)}"
        )
    elif priority is PriorityLevel.REDUCE:
        parts = []
        if risk.volume_halved:
            parts.append("容量减半（负荷偏高）")
        if risk.intensity_reduced:
            parts.append("强度减量（恢复不足）")
        if risk.floored_at_recovery_template:
            parts.append("叠加执行后不低于恢复日模板（15–20min Z1）")
        limitations.append(
            f"优先级 P2（减量）：{'；'.join(parts)}；需人工确认后才能上传"
        )
    elif priority is PriorityLevel.PROGRESS:
        advisory.append(
            f"优先级 P3（进阶加量）：时长/距离 +{risk.progress_duration_pct[0]}~"
            f"{risk.progress_duration_pct[1]}%，力量 +{risk.progress_strength_pct[0]}~"
            f"{risk.progress_strength_pct[1]}% 或 +{risk.progress_strength_reps[0]}~"
            f"{risk.progress_strength_reps[1]} 次"
        )

    yesterday_completion = _yesterday_completion(session, day, settings)
    report = DailyReport(
        date=day,
        yesterday_training=_yesterday_training(
            session, settings, day, completion=yesterday_completion
        ),
        load=load,
        acwr=acwr_snap,
        # 需求 5.4：当日生效心率参数随日报披露。取 hr_mod 默认常量 ——
        # 与 _loads_and_acwr → GarminAdapter → compute_day 用的是同一组值。
        hr_params=hr_mod.params().to_dict(),
        sleep={
            "duration_sec": sd.sleep_duration_sec,
            "start_ts": sd.sleep_start_ts.isoformat() if sd.sleep_start_ts else None,
            "end_ts": sd.sleep_end_ts.isoformat() if sd.sleep_end_ts else None,
        },
        recovery={
            "resting_heart_rate": sd.resting_heart_rate,
            "hrv_last_night_avg": sd.hrv_last_night_avg,
            # body_battery_morning：**醒时电量**（AtWakeTime），真正的「晨起电量」。
            # 风险规则与前端卡片都消费这个口径（2026-10-22 修正：此前前端误取下面的残值）。
            "body_battery_morning": sd.body_battery_morning,
            # body_battery_wake：同步时刻残值（MostRecent）。傍晚任务运行时当天已接近耗尽，
            # 实测近 60 天中位数仅 5 —— 仅保留供排查，**不得当「晨起」展示**。
            "body_battery_wake": sd.body_battery_wake,
            "sleep_score": sd.sleep_score,
        },
        capability={
            "hrv_baseline_p10": baselines["hrv_last_night_avg"].p10
            if "hrv_last_night_avg" in baselines
            else None,
        },
        over_under_analysis=None,
        today_plan_id=None,
        adjustment_reason="；".join(limitations) or None,
        # 前一日训练完成度（§12.4；周报据此提取聚合，免重算）
        yesterday_completion=yesterday_completion,
        week_start=_week_start_of(day),
        week_plan=_week_plan(session, day),
        # 四项长期目标进展（日报中部图表）：近 7 天滚动窗口，基线为前一个 7 天区间。
        # 与周报/月报共用同一口径函数，避免两处实现漂移。
        goal_progress={
            "range": {
                "start": (day - dt.timedelta(days=6)).isoformat(),
                "end": day.isoformat(),
            },
            "goals": load_goal_progress(session, day - dt.timedelta(days=6), day),
        },
        quality=quality,
        risk=risk,
        limitations=limitations,
        advisory=advisory,
        # G-16：记录本报告使用的口径版本（旧 payload 无该键 = legacy）
        algo_versions=algo_versions(),
        version=1,
    )

    # 报告与策略版本关联（T4.1，§11.1）：记录生成时实际使用的有效策略版本。
    report.strategy = build_strategy_link(
        session,
        report_generated_at=dt.datetime.now(),
        data_range=(lookback_start, day),
        rule_version=risk.rule_version,
        model_version=getattr(settings, "llm_model_daily", "") or "",
        risk_state=risk.priority.value,
        strategy_type=settings.strategy_type,
    )

    _persist(session, report, settings)
    return report
