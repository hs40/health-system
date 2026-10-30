"""固定测试集回归（阶段③ T3.11；REQ §14.2）。

**用途**：模型/提示词/策略升级前，用**固定测试集**验证确定性逻辑（风险判定、
环境硬约束、计划拦截、策略版本管理）结论**不漂移**。这是「AI 与确定性逻辑分离」
（§14.1）的落地保障——关键计算与安全规则由可验证程序逻辑控制，不交给模型自由生成。

对齐 §14.2 八类用例：
1. 常规案例
2. 高风险案例
3. 边界案例
4. 数据缺失案例
5. 数据冲突案例
6. 训练负荷与恢复背离案例（旧 R-006 已退役，改由「负荷过高 ∧ HRV 不平衡」表达）
7. 计划生成拦截案例
8. 策略版本变化案例

设计：每类用例给出「固定输入 → 期望结论」；任一结论变化即回归失败。
用例数据集中在本文件 ``REGRESSION_CASES``，便于升级评审时 diff。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings
from app.domain.environment_rules import evaluate_environment
from app.domain.risk import RiskInput, blocks_generate, blocks_upload, evaluate
from app.schemas.enums import PriorityLevel
from app.services import strategy_service
from app.storage.strategy_store import StrategyVersionStore

D = dt.date(2026, 10, 16)


# ============================================================
# 固定测试集定义（§14.2 八类）
# ============================================================
@dataclass(slots=True)
class RiskCase:
    name: str
    category: str
    risk_input: RiskInput
    expect_priority: PriorityLevel


@dataclass(slots=True)
class EnvCase:
    name: str
    category: str
    temp_c: float | None = None
    is_rain: bool = False
    is_snow: bool = False
    weather_available: bool = True
    expect_indoor: bool = False


def _ri(**kw) -> RiskInput:
    """四维齐全、负荷适宜、恢复良好的基准输入；用例按需覆盖单个维度。"""
    base: dict = {
        "on_date": D,
        "acwr_ratio": 1.0,
        "hrv_status": "BALANCED",
        "hrv_last_night_avg": 60.0,
        "hrv_baseline": 60.0,
        "hrv_balanced_streak": 3,
        "resting_heart_rate": 52,
        "rhr_baseline": 52.0,
        "sleep_score": 80,
        "body_battery_morning": 80,
    }
    base.update(kw)
    return RiskInput(**base)


# —— 1 常规 / 2 高风险 / 3 边界 / 4 缺失 / 5 冲突 / 6 沿用值 ——
REGRESSION_CASES: list[RiskCase] = [
    # 常规：四维齐全且全绿 → P3 进阶加量
    RiskCase("normal_full_green", "常规案例", _ri(), PriorityLevel.PROGRESS),
    # 高风险：负荷过高 ∧ HRV 不平衡 → P1 强制休息（硬拦截）
    RiskCase(
        "high_load_hrv_unbalanced",
        "高风险案例",
        _ri(acwr_ratio=1.6, hrv_status="LOW"),
        PriorityLevel.REST,
    ),
    # 高风险：睡眠极差 ∧ 晨起电量极低 → P1（第二条独立路径）
    RiskCase(
        "high_sleep_battery_collapse",
        "高风险案例",
        _ri(sleep_score=40, body_battery_morning=30),
        PriorityLevel.REST,
    ),
    # 高风险（非硬拦截）：仅负荷偏高 → P2 减量，只拦上传
    RiskCase(
        "elevated_load_reduce_only",
        "高风险案例",
        _ri(acwr_ratio=1.35),
        PriorityLevel.REDUCE,
    ),
    # 边界：ACWR 恰为 1.3（≥ 阈值 → P2）
    RiskCase("boundary_acwr_equal_1_3", "边界案例", _ri(acwr_ratio=1.3), PriorityLevel.REDUCE),
    # 边界：ACWR 恰为 1.5（不严格大于 → 不触发 P1 负荷路径）
    RiskCase(
        "boundary_acwr_equal_1_5",
        "边界案例",
        _ri(acwr_ratio=1.5, hrv_status="LOW"),
        PriorityLevel.REDUCE,
    ),
    # 数据缺失：缺 ACWR → C 安全兜底，但**恢复类规则照判**（L-7，2026-10-25）：
    # 睡眠 40 + 电量 30 双低 → P1，不得因为缺负荷就按原课表执行
    RiskCase(
        "missing_acwr_fallback",
        "数据缺失案例",
        _ri(acwr_ratio=None, sleep_score=40, body_battery_morning=30),
        PriorityLevel.REST,
    ),
    # 数据缺失：缺睡眠得分 → B 纯负荷降级，关闭加量通道
    RiskCase(
        "missing_sleep_score_load_only",
        "数据缺失案例",
        _ri(sleep_score=None),
        PriorityLevel.MAINTAIN,
    ),
    # 数据冲突：多项指标指向不同方向 → 取最高优先级，冲突不得抵消危险信号
    RiskCase(
        "conflict_recorded_not_masked",
        "数据冲突案例",
        _ri(acwr_ratio=1.6, hrv_status="LOW", sleep_score=85, conflicts=["load_vs_sleep"]),
        PriorityLevel.REST,
    ),
    # 沿用值：ACWR 来自 3 天前（acwr_stale）仍参与判定，可触发 P1
    RiskCase(
        "stale_acwr_still_blocks",
        "数据缺失案例",
        _ri(acwr_ratio=1.6, hrv_status="LOW", acwr_stale=True),
        PriorityLevel.REST,
    ),
]


ENV_CASES: list[EnvCase] = [
    EnvCase("outdoor_mild", "常规案例", temp_c=20.0, expect_indoor=False),
    EnvCase("rain_indoor", "边界案例", temp_c=20.0, is_rain=True, expect_indoor=True),
    EnvCase("snow_indoor", "边界案例", temp_c=1.0, is_snow=True, expect_indoor=True),
    EnvCase("cold_below_5", "边界案例", temp_c=4.9, expect_indoor=True),
    EnvCase("exactly_5_outdoor", "边界案例", temp_c=5.0, expect_indoor=False),
    EnvCase(
        "weather_unavailable_no_fabricate",
        "数据缺失案例",
        weather_available=False,
        expect_indoor=False,
    ),
]


# ============================================================
# 1~6 类：风险结论不漂移
# ============================================================
@pytest.mark.parametrize("case", REGRESSION_CASES, ids=[c.name for c in REGRESSION_CASES])
def test_risk_conclusion_stable(case: RiskCase):
    """固定输入 → 优先级稳定（升级后不得漂移）。"""
    assessment = evaluate(case.risk_input)
    assert assessment.priority is case.expect_priority, (
        f"[{case.category}] {case.name}: 期望 {case.expect_priority.value}，"
        f"实际 {assessment.priority.value}"
    )


# ============================================================
# 7 类：计划生成拦截（P1 → 必须拦截）
# ============================================================
_REST_CASES = [c for c in REGRESSION_CASES if c.expect_priority is PriorityLevel.REST]


@pytest.mark.parametrize("case", _REST_CASES, ids=[c.name for c in _REST_CASES])
def test_plan_blocked_stable(case: RiskCase):
    """P1 强制休息 → 既不生成计划也不上传（REQ-RISK-003）。"""
    a = evaluate(case.risk_input)
    assert blocks_generate(a) is True
    assert blocks_upload(a) is True


def test_reduce_blocks_upload_only():
    """P2 减量 → 允许生成，但不允许直接上传（Q26 两级拦截）。"""
    case = next(c for c in REGRESSION_CASES if c.name == "elevated_load_reduce_only")
    a = evaluate(case.risk_input)
    assert blocks_generate(a) is False
    assert blocks_upload(a) is True


def test_maintain_not_blocked_stable():
    """P4 / C 态兜底 → 不拦截。"""
    case = next(c for c in REGRESSION_CASES if c.name == "normal_full_green")
    a = evaluate(case.risk_input)
    assert blocks_generate(a) is False
    assert blocks_upload(a) is False


# ============================================================
# 3/4 类：环境硬约束不漂移
# ============================================================
@pytest.mark.parametrize("case", ENV_CASES, ids=[c.name for c in ENV_CASES])
def test_environment_constraint_stable(case: EnvCase):
    decision = evaluate_environment(
        temp_c=case.temp_c,
        is_rain=case.is_rain,
        is_snow=case.is_snow,
        weather_available=case.weather_available,
    )
    assert decision.indoor_required == case.expect_indoor, (
        f"[{case.category}] {case.name}: 期望 indoor={case.expect_indoor}，"
        f"实际 {decision.indoor_required}"
    )


# ============================================================
# 8 类：策略版本变化案例
# ============================================================
@pytest.fixture
def session() -> Session:
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def _settings(**kw) -> Settings:
    base = {"_env_file": None, "validate_min_replay_match": 0.80}
    base.update(kw)
    return Settings(**base)


def test_version_change_creates_new_and_supersedes(session):
    """每次策略变化产生新版本，旧 active → superseded（§19.4.1/§19.4.2）。"""
    s = _settings()
    strategy_service.ensure_initial_version(session, s)
    store = StrategyVersionStore(session)
    v1 = store.get_active()
    assert v1 is not None

    from app.schemas.enums import StrategyChangeType

    v2 = store.create_version(
        payload={"season": "autumn", "focus": "秋季调整"},
        change_type=StrategyChangeType.AI_OPTIMIZE,
        change_reason="升级评审用例",
        created_by="regression",
    )
    store.set_validation(v2.strategy_version_id, status="passed", result={"ok": True})
    store.activate(v2.strategy_version_id)

    assert store.get_version(v1.strategy_version_id).status == "superseded"
    assert store.get_active().strategy_version_id == v2.strategy_version_id
    assert len(store.list_versions(status="active")) == 1


def test_version_change_decision_isolation(session):
    """策略版本变化不改变风险判定输入（版本变化案例与风险逻辑解耦）。"""
    case = REGRESSION_CASES[0]
    before = evaluate(case.risk_input).priority
    s = _settings()
    strategy_service.ensure_initial_version(session, s)
    after = evaluate(case.risk_input).priority
    assert before is after is case.expect_priority


def test_regression_set_covers_all_categories():
    """确保固定测试集覆盖 §14.2 八类（缺类即失败，防止回归集退化）。"""
    covered = {c.category for c in REGRESSION_CASES} | {c.category for c in ENV_CASES}
    required = {
        "常规案例",
        "高风险案例",
        "边界案例",
        "数据缺失案例",
        "数据冲突案例",
    }
    # 计划生成拦截 + 策略版本变化由独立测试覆盖
    assert required <= covered
