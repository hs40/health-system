"""周计划处方步骤补全（REQ-PLAN-002 前端展开详情依赖）。

2026-10-22 验收发现：``generate_weekly_plan`` 只向 LLM 索取
``sport/name/duration_min/target_hr_zone``，产出的 session 的
``warmup/main/cooldown`` 全为空数组，前端「点击卡片展开完整训练处方」
只能看到一行「目标 xxx」，无热身/主项/放松内容。

本测试锁定：
1. 确定性兜底：即使 LLM 未返回步骤，也按 运动类型/时长/心率区间 合成完整处方；
2. LLM 返回步骤时**原样采用**，不被兜底覆盖；
3. 兜底步骤时长之和 ≈ duration_min（允许少量取整误差）；
4. 强度区间写入主项目标，供 Garmin 上传与强度判定使用。
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.schemas.domain import TrainingSession
from app.schemas.enums import Sport
from app.services.plan_service import _complete_steps

D = dt.date(2026, 10, 19)


def _ts(**kw) -> TrainingSession:
    base = dict(
        id=f"session-{D.isoformat()}",
        date=D,
        sport=Sport.RUNNING,
        name="轻松有氧跑",
        target_hr_zone="Z2",
        duration_min=40,
        status=None,
    )
    base.update(kw)
    from app.schemas.enums import SessionStatus

    base["status"] = SessionStatus.DRAFT
    return TrainingSession(**base)


# ---------------------------- 兜底合成 ----------------------------
def test_fallback_fills_all_three_blocks():
    ts = _complete_steps(_ts())
    assert ts.warmup, "热身不能为空"
    assert ts.main, "主项不能为空"
    assert ts.cooldown, "放松不能为空"
    kinds = {b["name"] for b in ts.warmup}
    assert kinds, "热身步骤需要有名称"


def test_fallback_respects_duration():
    ts = _complete_steps(_ts(duration_min=40))
    total = sum(
        int(s.get("duration_sec") or 0)
        for block in (ts.warmup, ts.main, ts.cooldown)
        for s in block
    )
    # 允许 ±90s 取整误差；兜底必须先满足总时长
    assert abs(total - 40 * 60) <= 90, f"总时长 {total}s 与 40min 偏差过大"


def test_fallback_main_carries_zone():
    ts = _complete_steps(_ts(target_hr_zone="Z4", sport=Sport.RUNNING))
    zones = [s.get("hr_zone") for s in ts.main if s.get("hr_zone") is not None]
    assert zones, "主项必须携带心率区间（强度判定依赖）"
    assert all(z == 4 for z in zones)


def test_fallback_strength_has_sets_and_reps():
    ts = _complete_steps(_ts(sport=Sport.STRENGTH, name="全身力量", target_hr_zone=None))
    assert ts.strength, "力量日必须有力量组（组×次×负荷）"
    for st in ts.strength:
        # StrengthSet 是 pydantic 模型；load_kg 必须为 None（系统不编造负荷）
        assert st.sets > 0 and st.reps > 0
        assert st.load_kg is None
        assert st.rest_sec > 0


def test_fallback_no_duration_still_produces_steps():
    ts = _complete_steps(_ts(duration_min=None))
    assert ts.warmup and ts.main and ts.cooldown


# ---------------------------- 不覆盖 LLM 结果 ----------------------------
def test_llm_steps_are_preserved():
    given = TrainingSession(
        id="s",
        date=D,
        sport=Sport.RUNNING,
        name="间歇",
        target_hr_zone="Z5",
        duration_min=30,
        warmup=[{"name": "LLM 热身", "duration_sec": 120}],
        main=[{"name": "LLM 主项", "duration_sec": 1500, "hr_zone": 5}],
        cooldown=[{"name": "LLM 放松", "duration_sec": 180}],
    )
    ts = _complete_steps(given)
    assert [s["name"] for s in ts.main] == ["LLM 主项"]
    assert ts.main[0]["duration_sec"] == 1500
    assert ts.strength is None or isinstance(ts.strength, list)


def test_partial_llm_blocks_get_completed():
    """LLM 只给 main 时，warmup/cooldown 由兜底补齐。"""
    given = TrainingSession(
        id="s",
        date=D,
        sport=Sport.RUNNING,
        name="节奏跑",
        target_hr_zone="Z3",
        duration_min=45,
        main=[{"name": "LLM 主项", "duration_sec": 2000, "hr_zone": 3}],
    )
    ts = _complete_steps(given)
    assert [s["name"] for s in ts.main] == ["LLM 主项"]
    assert ts.warmup and ts.cooldown


def test_rationale_always_present():
    ts = _complete_steps(_ts())
    assert ts.rationale, "处方依据必须可解释（展开详情要展示）"


def test_zone_int_parsing():
    assert _complete_steps(_ts(target_hr_zone="Z3")).main[0]["hr_zone"] == 3
    assert _complete_steps(_ts(target_hr_zone=None)).main[0].get("hr_zone") is None
    # 脏数据不得崩
    assert _complete_steps(_ts(target_hr_zone="Z9")).main[0].get("hr_zone") is None
    assert _complete_steps(_ts(target_hr_zone="abc")).main[0].get("hr_zone") is None


@pytest.mark.parametrize(
    "sport,name",
    [
        (Sport.RUNNING, "轻松有氧跑"),
        (Sport.INDOOR_CARDIO, "室内单车"),
        (Sport.STRENGTH, "全身力量"),
    ],
)
def test_all_sports_get_valid_steps(sport, name):
    ts = _complete_steps(_ts(sport=sport, name=name))
    assert ts.warmup and ts.main and ts.cooldown
    for block in (ts.warmup, ts.main, ts.cooldown):
        for s in block:
            assert isinstance(s.get("name"), str) and s["name"]
