"""计划服务共享辅助：LLM 生成上下文构造（T2.4 / T2.7 / E23 / E24）。

被 ``plan_service``（周计划）与 ``plan12w_service``（12 周计划）共同使用，
从 ``plan_service.py`` 拆分时抽出的公共部分（ARCHITECTURE_REVIEW A.1.2）。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session

from app.config import Settings
from app.domain.baseline import with_thresholds
from app.domain.environment_rules import evaluate_environment
from app.domain.report_link import build_strategy_link
from app.domain.season_policy import build_season_policy
from app.storage.environment_store import get_environment


def baseline_desc(baselines: dict) -> dict:
    """给 LLM 的基线描述：使用**真实判定口径**（E23/E24），而非分位数。

    旧实现只给 ``p10/p90``，但 R-001/R-002/R-004 已不再按分位数判定
    （HRV 用设备基线低区上沿；静息心率/睡眠用全历史 mean±1σ），
    继续给 p10/p90 会让模型按错误的阈值推理。
    """
    out: dict = {}
    for k, b in baselines.items():
        bb = with_thresholds(k, b)
        out[k] = {
            "mean": round(bb.mean, 2),
            "std": round(bb.std, 2),
            "threshold_low": None if bb.threshold_low is None else round(bb.threshold_low, 2),
            "threshold_high": None if bb.threshold_high is None else round(bb.threshold_high, 2),
            "mature": bb.mature,
        }
    return out


def environment_context(session: Session, week_start: dt.date, settings: Settings) -> str:
    """构造季节策略 + 环境硬约束的生成上下文（T2.4/T2.7）。

    - 季节策略由 ``domain.season_policy`` 纯计算（不预设固定比例，§4.5）；
    - 环境硬约束由 ``domain.environment_rules`` 判定（雨雪/<5°C → 室内，§4.6），
      **优先级高于季节策略**；
    - 环境数据缺失 → 明确标注，不编造。
    """
    policy = build_season_policy(week_start)
    # 环境：取本周首日已落库数据（无则标注缺失）
    env = get_environment(session, week_start)
    if env is not None:
        decision = evaluate_environment(
            temp_c=env.temp_c,
            is_rain=env.is_rain,
            is_snow=env.is_snow,
            weather_available=env.available,
        )
    else:
        decision = evaluate_environment(temp_c=None, weather_available=False)

    hard = "；".join(decision.hard_reasons) or "无"
    advisory = "；".join(decision.advisory_reasons) or "无"
    return (
        "环境与季节上下文（生成时必须遵守）：\n"
        f"- 季节：{policy.season}｜侧重：{policy.focus}\n"
        f"- 季节限制：{'；'.join(policy.limitations) or '无'}\n"
        f"- 长期优先级：{'>'.join(policy.long_term_priority)}\n"
        f"- 【硬性约束】{hard}\n"
        f"- 【建议】{advisory}\n"
        "- 硬性约束优先于季节策略：雨雪或气温<5°C 必须安排室内训练。"
    )


def strategy_link(
    session: Session, settings: Settings, start: dt.date, end: dt.date
) -> dict:
    """计划与策略版本关联（T4.1，§11.1）：记录生成时实际使用的有效策略版本。"""
    return build_strategy_link(
        session,
        report_generated_at=dt.datetime.now(),
        data_range=(start, end),
        rule_version=settings.risk_rule_version,
        model_version=getattr(settings, "llm_model_daily", "") or "",
        strategy_type=settings.strategy_type,
    )
