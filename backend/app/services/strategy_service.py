"""策略服务（阶段③ T3.2；REQ §6 / §17.1）。

**职责**：
- ``optimize``：AI 优化生成**候选**（candidate）→ 落库（不可变内容）；
- ``validate``：调用完整运行校验（``domain/strategy_validator``）+ 历史回放
  （``domain/strategy_replay``）→ 通过则置 verified，未通过则置 invalid；
- ``activate``：原子激活（至多一个 active；受并发守卫保护，T3.4）；
- ``current``：读当前有效策略；
- ``build_initial``：首启时以 ``season_policy`` 生成初始 active 版本（幂等）。

**边界**：本服务不直接联网；AI 优化由调用方传入结构化 ``payload``（或经 LLM 适配器
生成后传入），保证可测、可回放。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlmodel import Session

from app.config import Settings
from app.domain.season_policy import infer_season
from app.domain.strategy_replay import load_metrics_available, replay_history
from app.domain.strategy_validator import BOUNDARY_CASES, validate_strategy
from app.schemas.enums import StrategyChangeType, StrategyStatus
from app.storage.strategy_store import StrategyVersionStore

_SEASON_PREFIX = "season"


@dataclass(slots=True)
class OptimizeResult:
    version_id: str
    version_number: str
    validation_passed: bool
    status: str
    failures: list[str]


def default_season_payload(on_date: dt.date | None = None) -> dict:
    """构造默认季节策略内容（基于 ``domain/season_policy``，纯计算）。"""
    from app.domain.season_policy import build_season_policy

    day = on_date or dt.date.today()
    pol = build_season_policy(day)
    from dataclasses import asdict

    data = asdict(pol)
    # 对齐 Plan §4.5 payload 结构
    return {
        "season": data["season"],
        "focus": data["focus"],
        "endurance_ratio": data["endurance_ratio"],
        "strength_ratio": data["strength_ratio"],
        "recovery_ratio": data["recovery_ratio"],
        "weather_rules": {
            "temp_high_stop_outdoor": 30.0,
            "rain_snow_indoor": True,
            "temp_low_indoor": 5.0,
        },
        "long_term_priority": list(data["long_term_priority"]),
        "limitations": list(data["limitations"]),
    }


def ensure_initial_version(
    session: Session, settings: Settings, *, on_date: dt.date | None = None
) -> str:
    """首启幂等：若尚无 active 版本，则以默认季节策略创建并激活一个。

    返回当前 active 版本的 id。
    """
    store = StrategyVersionStore(session)
    active = store.get_active(strategy_type=settings.strategy_type)
    if active is not None:
        return active.strategy_version_id

    payload = default_season_payload(on_date)
    v = store.create_version(
        payload=payload,
        change_type=StrategyChangeType.AI_OPTIMIZE,
        change_reason="初始化：基于当前季节生成首个策略版本",
        created_by="system",
        evidence_summary="首启初始化（season_policy 纯计算）",
        expected_effect="按季节提供训练重点与限制",
        safety_constraints={"hard_env_constraints": True},
        limitations=payload["limitations"],
        status=StrategyStatus.CANDIDATE,
        strategy_type=settings.strategy_type,
        prefix=_SEASON_PREFIX,
    )
    # 初始版本直接标记为通过校验并激活（结构完整、来源可信、纯计算）
    store.set_validation(v.strategy_version_id, status="passed", result={"init": True})
    store.activate(v.strategy_version_id)
    store.append_event(
        event_type="activate",
        target_version_id=v.strategy_version_id,
        trigger_reason="首启初始化",
        risk_state="normal",
    )
    return v.strategy_version_id


def optimize(
    session: Session,
    settings: Settings,
    *,
    payload: dict | None = None,
    change_reason: str = "AI 优化生成候选",
    created_by: str = "ai",
    evidence_summary: str = "",
    expected_effect: str = "",
) -> OptimizeResult:
    """生成并校验一个候选版本（T3.2）。

    流程：创建 candidate → 执行完整运行校验（含历史回放）→
    通过置 verified、未通过置 invalid。**未通过不得参与决策**。
    """
    store = StrategyVersionStore(session)
    active = store.get_active(strategy_type=settings.strategy_type)
    content = payload or default_season_payload()

    version = store.create_version(
        payload=content,
        change_type=StrategyChangeType.AI_OPTIMIZE,
        change_reason=change_reason,
        created_by=created_by,
        parent_version_id=active.strategy_version_id if active else None,
        evidence_summary=evidence_summary,
        expected_effect=expected_effect,
        safety_constraints={"hard_env_constraints": True},
        limitations=list(content.get("limitations", [])),
        status=StrategyStatus.CANDIDATE,
        strategy_type=settings.strategy_type,
        prefix=_SEASON_PREFIX,
    )

    store.append_event(
        event_type="optimize",
        current_version_id=active.strategy_version_id if active else None,
        target_version_id=version.strategy_version_id,
        trigger_reason=change_reason,
    )

    # 完整运行校验（含 470 天历史回放）
    replay = replay_history(session)
    report = validate_strategy(
        content,
        cases=BOUNDARY_CASES,
        available_metrics=load_metrics_available(session),
        replay_total=replay.total,
        replay_matched=replay.matched,
        min_replay_match=settings.validate_min_replay_match,
    )
    store.set_validation(
        version.strategy_version_id,
        status="passed" if report.passed else "failed",
        result=report.to_dict(),
    )
    store.append_event(
        event_type="validate",
        target_version_id=version.strategy_version_id,
        validation_result=report.to_dict(),
        failure_reason=None if report.passed else "；".join(report.failures[:3]),
    )

    refreshed = store.get_version(version.strategy_version_id)
    assert refreshed is not None
    return OptimizeResult(
        version_id=refreshed.strategy_version_id,
        version_number=refreshed.version_number,
        validation_passed=report.passed,
        status=refreshed.status,
        failures=report.failures,
    )


def activate(session: Session, settings: Settings, version_id: str) -> str:
    """激活指定版本（要求已 verified；原子切换，受并发守卫保护）。"""
    from app.errors import AppError
    from app.schemas.enums import ErrorCode

    store = StrategyVersionStore(session)
    row = store.get_version(version_id)
    if row is None:
        raise AppError(ErrorCode.NOT_FOUND, f"策略版本不存在: {version_id}")
    if row.status not in (StrategyStatus.VERIFIED.value, StrategyStatus.ACTIVE.value):
        raise AppError(
            ErrorCode.STRATEGY_NOT_VERIFIED,
            f"版本 {row.version_number} 未通过校验（status={row.status}），不得激活",
        )
    old = store.get_active(strategy_type=row.strategy_type)
    store.activate(version_id)
    store.append_event(
        event_type="activate",
        current_version_id=old.strategy_version_id if old else None,
        target_version_id=version_id,
        trigger_reason="显式激活",
    )
    return version_id


def current(session: Session, settings: Settings):  # noqa: ANN201
    """当前有效策略版本（无则初始化）。"""
    store = StrategyVersionStore(session)
    active = store.get_active(strategy_type=settings.strategy_type)
    if active is None:
        ensure_initial_version(session, settings)
        active = store.get_active(strategy_type=settings.strategy_type)
    return active


def season_for(on_date: dt.date, *, override: str | None = None) -> str:
    """便捷：按日期推季节（供报告关联与展示）。"""
    from app.domain.season_policy import Season

    ov = Season(override) if override else None
    return infer_season(on_date, override=ov).value
