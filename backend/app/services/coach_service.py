"""教练服务（TA.10）：LLM 编排 —— 日报叙述分析 + 当日计划生成。

**硬性边界（REQ-RISK-002 / REQ-RISK-003）**：
- 风险判定由 ``domain.risk`` 的程序逻辑给出，**模型不得覆盖**；
- 高风险或存在硬性拦截 → **不生成当天计划**（即便模型给出了方案）；
- LLM 仅负责「叙述 / 解释 / 建议」，且输出必须通过 JSON 结构校验。
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlmodel import Session, select

from app.adapters.llm import DEFAULT_SCOPE, LlmAdapter
from app.config import Settings
from app.db.models import DailyReportRow, RiskAssessmentRow, TrainingSessionRow
from app.domain import equipment
from app.domain import rule_registry as rr
from app.domain.plan_rules import valid_zone as _valid_zone
from app.domain.quality import may_generate_plan
from app.domain.risk import allows_recovery_day, blocks_generate, blocks_upload
from app.errors import AppError
from app.schemas.domain import DailyReport, RiskAssessment, StrengthSet, TrainingSession
from app.schemas.enums import (
    AuditCategory,
    DataState,
    ErrorCode,
    PriorityLevel,
    SessionStatus,
    Sport,
)
from app.storage.audit_store import record as audit

logger = logging.getLogger(__name__)

_SPORTS = {s.value: s for s in Sport}


def _planned_zone(report: DailyReport, day: dt.date) -> str | None:
    """周计划当天安排的目标区间（调整日强度基准的继承来源）。"""
    for d in report.week_plan or []:
        if d.date == day:
            return _valid_zone(d.session.target_hr_zone) if d.session else None
    return None


def _equipment_line(settings: Settings) -> str:
    """力量器材说明（REQ-PLAN-003），供计划提示使用。"""
    return equipment.describe(
        settings.dumbbell_weights_kg, is_pair=settings.dumbbell_is_pair
    )


def _parse_strength(raw: object) -> list[StrengthSet] | None:
    """解析模型给出的力量处方（REQ-PLAN-002 负重）；单条不合法则跳过，不影响整体。"""
    if not isinstance(raw, list) or not raw:
        return None
    out: list[StrengthSet] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(StrengthSet(**item))
        except Exception:  # noqa: BLE001 - 单条不合法不拖垮整份计划
            logger.warning("忽略无法解析的力量组：%s", item)
    return out or None


ANALYSIS_SYSTEM = (
    "你是一名严谨的运动与恢复分析助手。你只依据给定的聚合指标做解释与建议，"
    "不诊断疾病、不给出用药建议。若数据不足或存在风险标记，必须明确指出并建议保守处理。"
    "输出必须是 JSON 对象。"
)

PLAN_SYSTEM = (
    "你是一名训练处方助手。基于给定的恢复指标、基线与风险等级，生成**当天一次**训练处方。"
    "若风险为高风险或数据不足，请把 sport 设为 null 并在 rationale 说明原因。"
    "不要给出医学诊断。输出必须是 JSON 对象。"
)


def _resolve_risk(session: Session, report: DailyReport) -> RiskAssessment | None:
    """取得日报对应的风险结论：优先 payload 快照，缺失时回落到 ``risk_assessment`` 表。

    2026-10-22 起历史 payload 不再携带 ``risk``（Q29/Q30），权威数据在独立表中。
    两者都没有 → ``None``（该日报生成于新规则上线之前且未回填），调用方按
    「无判定」处理：不拦截、不做强度调整。
    """
    if report.risk is not None:
        return report.risk
    row = session.exec(
        select(RiskAssessmentRow).where(RiskAssessmentRow.date == report.date)
    ).first()
    if row is None:
        return None
    return RiskAssessment(
        date=row.date,
        priority=PriorityLevel(row.priority),
        data_state=DataState(row.data_state),
        triggered_rules=list((row.triggered_rules or {}).get("rules", [])),
        evidence=row.evidence or {},
        missing_inputs=list((row.missing_inputs or {}).get("items", [])),
        volume_halved=row.volume_halved,
        intensity_reduced=row.intensity_reduced,
        floored_at_recovery_template=row.floored_at_recovery_template,
        rule_version=row.rule_version,
        decided_at=row.decided_at,
    )


def _report_payload(report: DailyReport) -> dict:
    """只发送聚合/派生指标（REQ-PRIV-001），不含原始逐秒数据。

    2026-10-22：``risk_level`` / ``risk_rules``（三级体系）已随 Q1/Q8 退役，
    改为发送 ``priority`` / ``data_state`` / ``missing_inputs``。
    """
    risk = report.risk
    return {
        "date": report.date.isoformat(),
        "priority": risk.priority.value if risk else None,
        "data_state": risk.data_state.value if risk else None,
        "risk_rules": risk.triggered_rules if risk else [],
        "missing_inputs": risk.missing_inputs if risk else [],
        "data_quality": report.quality.grade.value,
        "quality_missing": report.quality.missing,
        "resting_heart_rate": (report.recovery or {}).get("resting_heart_rate"),
        "hrv_last_night_avg": (report.recovery or {}).get("hrv_last_night_avg"),
        # 晨起电量（醒时值）是风险判据；残值 body_battery_wake 不发给模型，
        # 否则模型会把傍晚残值当成「晨起电量」写进早间建议。
        "body_battery_morning": (report.recovery or {}).get("body_battery_morning"),
        "sleep_score": (report.recovery or {}).get("sleep_score"),
        "sleep_duration_sec": (report.sleep or {}).get("duration_sec"),
        "acwr": report.acwr.model_dump(mode="json") if report.acwr else None,
        "load_combined": report.load.combined if report.load else None,
        "limitations": report.limitations,
    }


def analyze_daily(
    session: Session, day: dt.date, settings: Settings, *, model: str | None = None
) -> dict:
    """对已生成的日报做 LLM 叙述分析，并写回 ``over_under_analysis``。"""
    row = session.exec(
        select(DailyReportRow).where(DailyReportRow.date == day)
    ).first()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"日报不存在，无法分析: {day}")
    report = DailyReport(**row.payload)
    # 历史 payload 可能不含 risk（Q29/Q30 已清理）→ 从 risk_assessment 表取回
    report.risk = _resolve_risk(session, report)

    llm = LlmAdapter(settings)
    messages = [
        {"role": "system", "content": ANALYSIS_SYSTEM},
        {
            "role": "user",
            "content": (
                "以下是当日聚合指标（JSON）：\n"
                + json.dumps(_report_payload(report), ensure_ascii=False)
                + "\n请输出 JSON，字段：summary(string)、over_under(string，过量/不足分析)、"
                "suggestions(string[]，可执行建议)、cautions(string[]，注意事项)。"
            ),
        },
    ]
    data = llm.chat_json(
        messages,
        model=model,
        required_keys=("summary", "over_under", "suggestions"),
    )

    report.over_under_analysis = data.get("over_under")
    report.version += 1
    row.payload = report.model_dump(mode="json")
    row.version = report.version
    session.add(row)
    session.commit()

    audit(
        session,
        AuditCategory.MODEL_CHANGE,
        ref=day.isoformat(),
        detail={
            "purpose": "daily_analysis",
            "scope": DEFAULT_SCOPE,
            "model": model or settings.llm_model_daily,
            "result": "ok",
            "output": data,
        },
        audit_dir=settings.audit_dir,
    )
    return data


def build_today_plan(
    session: Session,
    day: dt.date,
    settings: Settings,
    *,
    model: str | None = None,
) -> TrainingSession | None:
    """生成当天训练处方。

    - P1 → 返回 15–20min Z1 恢复日模板（``upload_allowed=False``）；
    - 关键安全数据不足 → 返回 ``None``（不生成计划）；
    - 其余（P2/P3/P4、C 态兜底）→ 走 LLM 生成。
    """
    row = session.exec(
        select(DailyReportRow).where(DailyReportRow.date == day)
    ).first()
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"日报不存在，无法生成计划: {day}")
    report = DailyReport(**row.payload)
    risk = _resolve_risk(session, report)
    priority = risk.priority.value if risk else "P4"
    data_state = risk.data_state.value if risk else "C"
    rules = risk.triggered_rules if risk else []

    # —— 硬性边界：模型不得覆盖风险结论 ——
    # Q26 / Q6：P1 → **不生成常规计划**，改出 15–20min Z1 恢复日模板（不调 LLM、不上传）。
    # 注意顺序：必须先判 P1 恢复日，再判「拦截」—— P1 的拦截语义是「拦常规计划生成」，
    # 而不是「什么都不产出」，否则恢复日模板这条分支永远走不到。
    if risk is not None and allows_recovery_day(risk):
        ts = _recovery_session(day)
        _upsert_session(session, ts)
        audit(
            session,
            AuditCategory.RISK,
            ref=day.isoformat(),
            detail={
                "op": "recovery_plan",
                "reason": "P1 强制休息命中，输出 15–20min Z1 恢复日模板",
                "priority": priority,
                "data_state": data_state,
                "rules": rules,
                "session_id": ts.id,
                "upload_allowed": False,
            },
            audit_dir=settings.audit_dir,
        )
        logger.info("P1 命中：%s 生成恢复日计划（15–20min Z1、不上传）", day)
        return ts

    if risk is not None and blocks_generate(risk):
        reason = f"P1 强制休息（{data_state} 态判定），不生成当天计划"
        audit(
            session,
            AuditCategory.RISK,
            ref=day.isoformat(),
            detail={
                "op": "plan_blocked",
                "reason": reason,
                "priority": priority,
                "data_state": data_state,
                "rules": rules,
                "quality_grade": report.quality.grade.value,
                "quality_missing": report.quality.missing,
            },
            audit_dir=settings.audit_dir,
        )
        logger.warning("拦截：%s 不生成计划（%s）", day, reason)
        return None

    if not may_generate_plan(report.quality):
        audit(
            session,
            AuditCategory.RISK,
            ref=day.isoformat(),
            detail={
                "op": "plan_blocked",
                "reason": "关键安全数据不足，不生成当天计划",
                "quality_grade": report.quality.grade.value,
                "quality_missing": report.quality.missing,
            },
            audit_dir=settings.audit_dir,
        )
        return None

    llm = LlmAdapter(settings)
    messages = [
        {"role": "system", "content": PLAN_SYSTEM},
        {
            "role": "user",
            "content": (
                "当日聚合指标（JSON）：\n"
                + json.dumps(_report_payload(report), ensure_ascii=False)
                + f"\n可用器材：{_equipment_line(settings)}\n"
                "力量课只使用上述器材，负重必须取自上述单只档位（双臂动作可双手各持同重一只）。\n"
                "请输出 JSON，字段：sport(running|indoorCardio|strength|null)、name(string)、"
                "duration_min(int)、warmup([{name,duration_sec}])、"
                "main([{name,duration_sec|distance_m,hr_zone?}])、cooldown([{name,duration_sec}])、"
                "target_hr_zone(Z1|Z2|Z3|Z4|Z5，主阶段目标心率区间，Karvonen 口径)、"
                "strength([{exercise,sets,reps,load_kg,rest_sec}]，仅当 sport=strength)、"
                "rationale(string)。"
            ),
        },
    ]
    data = llm.chat_json(
        messages, model=model, required_keys=("sport", "name", "duration_min", "rationale")
    )

    sport_raw = data.get("sport")
    if not sport_raw or sport_raw not in _SPORTS:
        rationale = str(data.get("rationale") or "模型判定当日不宜训练")
        logger.info("LLM 判定当日不宜训练：%s", rationale)
        report.adjustment_reason = rationale
        report.version += 1
        row.payload = report.model_dump(mode="json")
        row.version = report.version
        session.add(row)
        session.commit()
        audit(
            session,
            AuditCategory.MODEL_CHANGE,
            ref=day.isoformat(),
            detail={
                "purpose": "today_plan",
                "scope": DEFAULT_SCOPE,
                "model": model or settings.llm_model_daily,
                "result": "declined",
                "rationale": rationale,
            },
            audit_dir=settings.audit_dir,
        )
        return None

    ts = TrainingSession(
        id=f"session-{day.isoformat()}",
        date=day,
        sport=_SPORTS[sport_raw],
        name=str(data.get("name") or "训练"),
        warmup=list(data.get("warmup") or []),
        main=list(data.get("main") or []),
        cooldown=list(data.get("cooldown") or []),
        # 目标心率区间（Q8-A）：模型输出优先；未给/越界则继承周计划当天安排
        # （否则调整日的完成度判定没有强度基准，只能降级为仅时长）。
        target_hr_zone=_valid_zone(data.get("target_hr_zone"))
        or _planned_zone(report, day),
        strength=_parse_strength(data.get("strength")),
        duration_min=int(data.get("duration_min") or 0) or None,
        status=SessionStatus.DRAFT,
        # Q26：P2 只拦上传 —— 计划可生成，但必须标记为不可上传，
        # 否则 `upload_session` 的 upload_allowed 守卫拿不到 P2 的拦截语义。
        upload_allowed=not (risk is not None and blocks_upload(risk)),
    )

    _upsert_session(session, ts)
    audit(
        session,
        AuditCategory.MODEL_CHANGE,
        ref=day.isoformat(),
        detail={
            "purpose": "today_plan",
            "scope": DEFAULT_SCOPE,
            "model": model or settings.llm_model_daily,
            "result": "ok",
            "rationale": data.get("rationale"),
            "session_id": ts.id,
        },
        audit_dir=settings.audit_dir,
    )
    return ts


def _recovery_session(day: dt.date) -> TrainingSession:
    """P1 恢复日固定模板（Q6）：**不调 LLM**，保证强度可控且可复现。

    15–20min Z1 主动恢复 + 拉伸，替代旧的 28min 恢复计划；
    仅本地可见，**不得上传**（由 ``upload_session`` 守卫）。
    时长常量取自 ``rule_registry``，不在此处硬编码。
    """
    total_sec = rr.RECOVERY_TEMPLATE_MAX_SEC
    warmup_sec = 180
    stretch_sec = 240
    cooldown_sec = 120
    cardio_sec = total_sec - warmup_sec - stretch_sec - cooldown_sec
    return TrainingSession(
        id=f"session-{day.isoformat()}",
        date=day,
        sport=Sport.INDOOR_CARDIO,
        name=f"恢复日：{rr.RECOVERY_TEMPLATE_MIN_SEC // 60}–"
        f"{rr.RECOVERY_TEMPLATE_MAX_SEC // 60}min 主动恢复",
        target_hr_zone=rr.RECOVERY_TEMPLATE_ZONE,
        warmup=[{"name": "关节活动（颈肩髋踝）", "duration_sec": warmup_sec}],
        main=[
            {
                "name": "慢走或极低强度有氧",
                "duration_sec": cardio_sec,
                "hr_zone": rr.RECOVERY_TEMPLATE_ZONE,
            },
            {"name": "全身静态拉伸", "duration_sec": stretch_sec},
        ],
        cooldown=[{"name": "呼吸放松", "duration_sec": cooldown_sec}],
        duration_min=total_sec // 60,
        status=SessionStatus.DRAFT,
        rationale=rr.RECOVERY_TEMPLATE_NOTE,
        upload_allowed=False,
    )


def _upsert_session(session: Session, ts: TrainingSession) -> None:
    existing = session.get(TrainingSessionRow, ts.id)
    if existing is None:
        session.add(
            TrainingSessionRow(
                id=ts.id,
                date=ts.date,
                sport=ts.sport.value,
                name=ts.name,
                payload=ts.model_dump(mode="json"),
                status=ts.status.value,
            )
        )
    else:
        existing.payload = ts.model_dump(mode="json")
        existing.status = ts.status.value
        session.add(existing)
    session.commit()
