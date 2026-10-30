"""周计划确定性安全规则（plan_rules）单元测试。

背景：``POST /plan/weekly/{week_start}/evaluate`` 原来只把 LLM 输出塞进
``conflicts: list[str]``，前端无法把冲突挂到具体日期、也无法据此**阻止**
不安全计划生效。本模块测试纯函数规则层：

- 硬性阻断（hard_block=True）不得因用户确认而绕过 → 由前端禁用确认按钮承接；
- 软性问题（severity=warn/info）只提示，不阻断；
- 规则层**不依赖 LLM**、可复现、可单测。
"""

from __future__ import annotations

import datetime as dt

from app.domain.plan_rules import (
    ConflictSeverity,
    ConflictType,
    check_plan,
)
from app.schemas.domain import (
    StrengthSet,
    TrainingSession,
    WeekPlanDay,
)
from app.schemas.enums import SessionStatus, Sport

MON = dt.date(2026, 10, 12)


def _session(
    day: dt.date,
    *,
    sport: Sport = Sport.RUNNING,
    zone: str | None = "Z2",
    duration: int | None = 40,
    name: str = "轻松跑",
    strength: list[StrengthSet] | None = None,
) -> TrainingSession:
    return TrainingSession(
        id=f"s-{day.isoformat()}",
        date=day,
        sport=sport,
        name=name,
        target_hr_zone=zone,
        duration_min=duration,
        strength=strength,
        status=SessionStatus.DRAFT,
    )


def _week(
    sessions: dict[int, TrainingSession], nights: set[int] | None = None
) -> list[WeekPlanDay]:
    """按「距周一第几天」构造 7 天计划；未出现的日子为休息日。"""
    nights = nights or set()
    return [
        WeekPlanDay(
            date=MON + dt.timedelta(days=i),
            weekday=i,
            session=sessions.get(i),
            is_night_shift=i in nights,
        )
        for i in range(7)
    ]


# ============================================================
# 恢复间隔
# ============================================================
def test_recovery_gap_too_short_is_hard_block():
    """连续两天高强度（Z4/Z5）间隔 < 24h → 硬性阻断。"""
    days = _week(
        {
            2: _session(MON + dt.timedelta(days=2), zone="Z4", name="间歇跑"),
            3: _session(MON + dt.timedelta(days=3), zone="Z4", name="节奏跑"),
        }
    )
    conflicts = check_plan(days)
    gaps = [c for c in conflicts if c.type is ConflictType.RECOVERY_GAP]
    assert len(gaps) == 1
    assert gaps[0].severity is ConflictSeverity.BLOCK
    assert gaps[0].hard_block is True
    assert set(gaps[0].days) == {
        (MON + dt.timedelta(days=2)).isoformat(),
        (MON + dt.timedelta(days=3)).isoformat(),
    }


def test_recovery_gap_easy_zone_not_flagged():
    """两天都是 Z2 轻松跑 → 不算恢复不足。"""
    days = _week(
        {
            0: _session(MON, zone="Z2"),
            1: _session(MON + dt.timedelta(days=1), zone="Z2"),
        }
    )
    assert [c for c in check_plan(days) if c.type is ConflictType.RECOVERY_GAP] == []


def test_three_consecutive_hard_days_blocked():
    """连续三天中高强度（Z3 及以上）→ 硬性阻断。"""
    days = _week(
        {
            i: _session(MON + dt.timedelta(days=i), zone="Z3", name=f"训练{i}")
            for i in (0, 1, 2)
        }
    )
    conflicts = [c for c in check_plan(days) if c.type is ConflictType.RECOVERY_GAP]
    assert any(c.hard_block for c in conflicts)
    assert {MON.isoformat(), (MON + dt.timedelta(days=2)).isoformat()} <= set(
        conflicts[0].days
    )


# ============================================================
# 夜班影响
# ============================================================
def test_night_shift_next_day_high_intensity_blocked():
    """夜班次日安排 Z4 → 硬性阻断。"""
    days = _week(
        {3: _session(MON + dt.timedelta(days=3), zone="Z4", name="间歇跑")},
        nights={2},
    )
    conflicts = [c for c in check_plan(days) if c.type is ConflictType.NIGHT_SHIFT]
    assert len(conflicts) == 1
    assert conflicts[0].hard_block is True
    assert conflicts[0].days == [
        (MON + dt.timedelta(days=2)).isoformat(),
        (MON + dt.timedelta(days=3)).isoformat(),
    ]


def test_night_shift_next_day_easy_zone_only_warn():
    """夜班次日 Z2 → 仅提示，不阻断。"""
    days = _week(
        {3: _session(MON + dt.timedelta(days=3), zone="Z2")},
        nights={2},
    )
    conflicts = [c for c in check_plan(days) if c.type is ConflictType.NIGHT_SHIFT]
    assert len(conflicts) == 1
    assert conflicts[0].hard_block is False
    assert conflicts[0].severity is ConflictSeverity.INFO


# ============================================================
# 负荷尖峰
# ============================================================
def test_single_day_load_spike_blocked():
    """单日时长 ≥ 当日其余训练总和 × 2 且 ≥ 120min → 硬性阻断。"""
    days = _week(
        {
            0: _session(MON, zone="Z1", duration=20),
            5: _session(MON + dt.timedelta(days=5), zone="Z2", duration=180, name="长距离"),
        }
    )
    spikes = [c for c in check_plan(days) if c.type is ConflictType.LOAD_SPIKE]
    assert len(spikes) >= 1
    assert any(c.hard_block for c in spikes)


def test_balanced_week_has_no_hard_block():
    days = _week(
        {
            0: _session(MON, zone="Z2", duration=40),
            2: _session(MON + dt.timedelta(days=2), zone="Z4", duration=45),
            4: _session(MON + dt.timedelta(days=4), zone="Z2", duration=40),
            5: _session(MON + dt.timedelta(days=5), zone="Z2", duration=70),
        }
    )
    assert [c for c in check_plan(days) if c.hard_block] == []


# ============================================================
# 训练类型冲突
# ============================================================
def test_same_type_consecutive_strength_warned():
    """连续两天力量训练 → 提示（同肌群恢复不足），不阻断。"""
    sets = [StrengthSet(exercise="深蹲", sets=5, reps=5, load_kg=80, rest_sec=180)]
    days = _week(
        {
            0: _session(MON, sport=Sport.STRENGTH, zone=None, strength=sets, name="力量A"),
            1: _session(
                MON + dt.timedelta(days=1),
                sport=Sport.STRENGTH,
                zone=None,
                strength=sets,
                name="力量B",
            ),
        }
    )
    conflicts = [c for c in check_plan(days) if c.type is ConflictType.SPORT_CONFLICT]
    assert len(conflicts) == 1
    assert conflicts[0].hard_block is False


# ============================================================
# 结果结构
# ============================================================
def test_conflict_serialises_to_dict():
    """结构化冲突必须能直接序列化给前端（days/kind/severity/hard_block/message/source）。"""
    days = _week({3: _session(MON + dt.timedelta(days=3), zone="Z4")}, nights={2})
    payload = check_plan(days)[0].to_dict()
    assert set(payload) == {"days", "kind", "severity", "hard_block", "message", "source"}
    assert payload["kind"] == "night_shift"
    assert payload["severity"] == "block"
    assert payload["hard_block"] is True
    assert payload["source"] == "rule"
    assert isinstance(payload["message"], str) and payload["message"]


def test_has_hard_block_helper():
    """confirm 前置判断用：任一 hard_block 即 true。"""
    from app.domain.plan_rules import has_hard_block

    blocked = _week({3: _session(MON + dt.timedelta(days=3), zone="Z4")}, nights={2})
    assert has_hard_block(check_plan(blocked)) is True
    assert has_hard_block([]) is False
