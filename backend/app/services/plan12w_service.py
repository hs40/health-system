"""计划服务 · 12 周计划生成与滚动更新（REQ-PLAN-007 / T2.4 / T2.7）。

从 ``plan_service.py`` 拆出（ARCHITECTURE_REVIEW A.1.2）。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app.config import Settings
from app.db.models import Plan12WeekRow
from app.domain.dates import week_start as _monday_of
from app.domain.season_policy import build_season_policy
from app.schemas.api import Plan12Week
from app.schemas.enums import AuditCategory
from app.services.plan_context import strategy_link
from app.storage.audit_store import record as audit

PLAN12W_WEEKS = 12

_LOAD_FACTORS = [1.00, 1.10, 1.20, 0.80, 1.05, 1.15, 1.25, 0.85, 1.10, 1.20, 1.30, 0.75]
_EVAL_WEEKS = {4, 8, 12}  # 每 4 周评估一次

DEFAULT_GOAL_WEIGHTS: dict[str, float] = {
    "endurance": 0.4,
    "strength": 0.3,
    "recovery": 0.3,
}


def _phase_of(index: int) -> str:
    """第 index 周（0 起）的训练阶段。"""
    if (index + 1) % 4 == 0:
        return "deload"
    if index < 4:
        return "base"
    if index < 8:
        return "build"
    return "peak"


def latest_plan12w(session: Session) -> Plan12Week | None:
    row = session.exec(
        select(Plan12WeekRow).order_by(  # type: ignore[attr-defined]
            Plan12WeekRow.start_date.desc(), Plan12WeekRow.version.desc()
        )
    ).first()
    return Plan12Week(**row.payload) if row else None


def generate_plan12w(
    session: Session,
    settings: Settings,
    *,
    start_date: dt.date | None = None,
    goal_weights: dict[str, float] | None = None,
) -> Plan12Week:
    """生成/滚动更新 12 周计划（每周更新、每 4 周评估，REQ-PLAN-007）。

    起点默认取本周周一（D17）；重复调用即为「滚动更新」。
    """
    start = _monday_of(start_date or dt.date.today())
    weights = goal_weights or dict(DEFAULT_GOAL_WEIGHTS)

    weeks: list[dict] = []
    for i in range(PLAN12W_WEEKS):
        ws = start + dt.timedelta(days=7 * i)
        phase = _phase_of(i)
        # 季节评估（T2.7）：12 周跨越换季，**每一周按其起始日重新评估季节**（§4.5）
        policy = build_season_policy(ws)
        weeks.append(
            {
                "week_index": i + 1,
                "week_start": ws.isoformat(),
                "week_end": (ws + dt.timedelta(days=6)).isoformat(),
                "phase": phase,
                "focus": {
                    "base": "有氧基础与动作模式",
                    "build": "专项负荷渐进",
                    "peak": "强度峰值与专项耐力",
                    "deload": "减量恢复与状态评估",
                }[phase],
                "season": policy.season,
                "season_focus": policy.focus,
                "long_term_priority": list(policy.long_term_priority),
                "target_load_factor": _LOAD_FACTORS[i],
                "evaluation_due": (i + 1) in _EVAL_WEEKS,
            }
        )

    plan = Plan12Week(start_date=start, weeks=weeks, goal_weights=weights)
    plan.strategy = strategy_link(
        session, settings, start, start + dt.timedelta(days=7 * PLAN12W_WEEKS - 1)
    )
    session.add(
        Plan12WeekRow(start_date=start, payload=plan.model_dump(mode="json"), version=1)
    )
    session.commit()
    audit(
        session,
        AuditCategory.PLAN_VERSION,
        ref=f"plan12w:{start.isoformat()}",
        detail={
            "op": "generate",
            "weeks": PLAN12W_WEEKS,
            "eval_weeks": sorted(_EVAL_WEEKS),
            "goal_weights": weights,
            "seasons": [w["season"] for w in weeks],
        },
        audit_dir=settings.audit_dir,
    )
    return plan
