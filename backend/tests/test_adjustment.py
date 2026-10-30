"""日报调整层级测试（阶段④ T4.2；验收 §12.1~§12.4）。

覆盖：
- 日报调整不覆盖原周计划（§12.1）；
- 命名规则：仅日报调整加「调整」前缀（§12.2）；
- 周计划卡片「日报已调整」标识（§12.3）；
- 周报对比：执行状态分类 + 计划量与实际量分开统计（§12.4）。
"""

from __future__ import annotations

import datetime as dt

from app.domain.adjustment import (
    DailyAdjustment,
    ExecutionStatus,
    apply_adjustment_name,
    build_week_execution,
    card_view,
    judge_day,
)

D = dt.date(2026, 10, 16)

# 判定用运动档位（与负荷域 SPORT_TIERS 同一口径；用例里显式传入，保持域函数纯净）
COUNTED = frozenset(
    {"running", "strength_training", "indoor_cardio", "lap_swimming", "hiit", "hiking"}
)


def _act(sport: str, minutes: float, average_hr: float | None = None) -> dict:
    """一条实际活动（服务层已把 Garmin payload 归一成这个形状）。"""
    return {"sport": sport, "minutes": minutes, "average_hr": average_hr}


# ---------------------------- 命名规则（§12.2）----------------------------
def test_adjustment_name_prefix():
    """日报调整后的训练名加「调整」前缀。"""
    assert apply_adjustment_name("x", "1012-有氧轻松跑 Z2 25min") == (
        "调整1012-有氧轻松跑 Z2 25min"
    )


def test_adjustment_name_idempotent():
    """已带前缀不重复添加。"""
    once = apply_adjustment_name("x", "1012-有氧轻松跑 Z2 25min")
    assert apply_adjustment_name("x", once) == once
# ---------------------------- 不覆盖原周计划（§12.1）----------------------------
# ---------------------------- 卡片标识（§12.3）----------------------------
def test_card_view_adjusted_badge():
    """带前缀的调整名 → 卡片标记「日报已调整」。"""
    adj = DailyAdjustment(
        date=D,
        original_name="1012-有氧轻松跑 Z2 30min",
        adjusted_name=apply_adjustment_name("1012-有氧轻松跑 Z2 30min", "1012-有氧轻松跑 Z2 25min"),
        reason="减量",
        report_ref="daily:2026-10-16",
        actual="完成 25min",
    )
    view = card_view("1012-有氧轻松跑 Z2 30min", [adj], date=D)
    assert view["is_adjusted"] is True
    assert view["badge"] == "日报已调整"
    assert view["original"] == "1012-有氧轻松跑 Z2 30min"
    assert view["adjusted"] == "调整1012-有氧轻松跑 Z2 25min"
    assert view["reason"] == "减量"
    assert view["report_ref"] == "daily:2026-10-16"
    assert view["actual"] == "完成 25min"


def test_card_view_not_adjusted():
    view = card_view("1012-有氧轻松跑 Z2 30min", [], date=D)
    assert view["is_adjusted"] is False
    assert view["badge"] is None
    assert view["adjusted"] is None
# ---------------------------- 周报执行状态（§12.4）----------------------------
# ---------------------------- 周报对比：量分开统计（§12.4）----------------------------
# ---------------------------- 单日完成度判定（§12.4 + Q6/Q9 规则）----------------------------
def test_judge_day_completed_when_duration_and_zone_match():
    """切片1：计划 Z2 30min 跑步，实际同区同量 → 完整完成（示踪子弹）。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("running", 30, 135)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.COMPLETED
    assert j.duration_met is True
    assert j.intensity_met is True
    assert j.actual_zone == "Z2"  # 135 bpm 落在 Z2（129–142）


def test_judge_day_partial_when_duration_short():
    """切片2：同区但时长差 10 分钟（超 2min 容差）→ 部分完成。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("running", 20, 135)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.PARTIAL
    assert j.duration_met is False
    assert j.intensity_met is True  # 强度仍然达标，栽在时长


def test_judge_day_duration_tolerance_absorbs_counting_noise():
    """切片2b：差 2 分钟以内（记账容差）仍算时长达标——容差只吸收噪声。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("running", 28.5, 135)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.duration_met is True
    assert j.status == ExecutionStatus.COMPLETED


def test_judge_day_partial_when_zone_mismatch():
    """切片3：计划 Z2、全程均心落在 Z3（强度不同区）→ 部分完成（Q9-A 同区才算达标）。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("running", 30, 150)],  # 150 ∈ Z3（142–154）
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.PARTIAL
    assert j.duration_met is True
    assert j.intensity_met is False
    assert j.actual_zone == "Z3"
    # 强度判定的已知局限必须随结果披露（全程均心，含热身冷身）
    assert any("全程平均心率" in n for n in j.notes)


def test_judge_day_zone_mismatch_below_plan_also_partial():
    """切片3b：练轻了（该 Z2 实际 Z1）同样不是计划内的刺激 → 部分完成。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("running", 30, 122)],  # 122 ∈ Z1（116–129）
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.PARTIAL
    assert j.actual_zone == "Z1"
    assert j.intensity_met is False


def test_judge_day_missed_when_no_activity():
    """切片4：有计划无 counted 活动 → 未完成。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.MISSED
    assert j.actual_min == 0.0


def test_judge_day_missed_when_only_non_counted_activity():
    """切片4b：只走了路（record_only 档）不算执行 → 计划日仍未完成（Q5-A）。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("walking", 60, 100)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.MISSED


def test_judge_day_substituted_when_sport_differs():
    """切片5：计划跑步实际去做力量 → 替代训练。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("strength_training", 40, 120)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.SUBSTITUTED


def test_judge_day_substituted_when_no_plan_but_recorded():
    """切片5b：计划外执行了 counted 运动 → 替代训练。"""
    j = judge_day(
        D,
        planned_sport=None,
        activities=[_act("hiit", 25, 145)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.SUBSTITUTED


def test_judge_day_unjudged_when_no_plan_no_record():
    """切片6：无计划无记录 → 不判定（不进 status_counts，不伪造）。"""
    j = judge_day(
        D,
        planned_sport=None,
        activities=[],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status is None
    assert any("无计划且无记录" in n for n in j.notes)


def test_judge_day_adjusted_completed_compares_against_adjusted_plan():
    """切片7：调整日与**调整后**计划比——原计划 30 调 25，跑 25 同区 → 日报调整后完成。

    若误与原计划 30 比会把 25 判成部分完成（Q6：调整日基准是调整后计划）。
    """
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=25,  # 调整后时长
        target_zone="Z2",
        adjusted=True,
        activities=[_act("running", 25, 135)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.ADJUSTED_COMPLETED


def test_judge_day_adjusted_but_under_done_is_partial():
    """切片7b：调整日也没做足调整后计划 → 部分完成（不是调整后完成）。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=25,
        target_zone="Z2",
        adjusted=True,
        activities=[_act("running", 18, 135)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.PARTIAL


def test_judge_day_without_target_zone_judges_duration_only_and_discloses():
    """切片8：历史计划无强度目标 → 只按时长判，且必须披露未做强度判定（Q8-A）。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone=None,
        activities=[_act("running", 32, 100)],  # 心率再低也不影响判定
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.COMPLETED
    assert j.duration_met is True
    assert j.intensity_met is None  # 未做强度判定，不是 False
    assert any("无强度目标" in n for n in j.notes)


def test_judge_day_future_day_not_judged():
    """切片9：未到期不判定（周报只评已过去的日子，不预判）。"""
    j = judge_day(
        D + dt.timedelta(days=1),
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status is None
    assert any("未到期" in n for n in j.notes)


def test_day_judgment_to_dict_roundtrip_for_payload():
    """切片10：判定结果必须可序列化进日报 payload（周报据此提取，S3 的前置）。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("running", 30, 135)],
        counted_sports=COUNTED,
        as_of=D,
    )
    d = j.to_dict()
    assert d["date"] == D.isoformat()
    assert d["status"] == ExecutionStatus.COMPLETED
    assert d["duration_met"] is True and d["intensity_met"] is True
    assert d["target_zone"] == "Z2" and d["actual_zone"] == "Z2"
    assert d["planned_min"] == 30 and d["actual_min"] == 30
    assert isinstance(d["notes"], list)


def test_judge_day_without_heartrate_does_not_fake_intensity_failure():
    """切片11：实际活动无平均心率 → 强度**无从判定**（不是不达标），按时长判并披露。"""
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("running", 30, None)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.intensity_met is None
    assert j.actual_zone is None
    assert j.status == ExecutionStatus.COMPLETED  # 时长达标即完成，不因缺心率假装部分完成
    assert any("无平均心率" in n for n in j.notes)


def test_judge_day_actual_volume_counts_all_counted_minutes():
    """切片12：实际训练量=当天全部 counted 档分钟；判定分钟只算计划项内。

    计划 30min 跑步、实际跑了 30min 又练了 25min 力量：完成度看 30（计划项），
    实际训练量记 55（§12.4 分开统计的口径：实际发生的都算）。
    """
    j = judge_day(
        D,
        planned_sport="running",
        planned_min=30,
        target_zone="Z2",
        activities=[_act("running", 30, 135), _act("strength_training", 25, 120)],
        counted_sports=COUNTED,
        as_of=D,
    )
    assert j.status == ExecutionStatus.COMPLETED
    assert j.actual_min == 30      # 判定用：计划项内分钟
    assert j.actual_volume_min == 55  # 统计用：全部 counted 分钟


# ---------------------------- 周聚合（§12.4，S2）----------------------------
def _week_mixed_judgments() -> list:
    """一周样例：完成/部分(时短)/未完成/替代/调整后完成/无计划无记录/未到期。"""
    base = dict(counted_sports=COUNTED, as_of=D)
    return [
        judge_day(D, planned_sport="running", planned_min=30, target_zone="Z2",
                  activities=[_act("running", 30, 135)], **base),                       # 完成
        judge_day(D - dt.timedelta(days=1), planned_sport="running", planned_min=30,
                  target_zone="Z2", activities=[_act("running", 20, 135)], **base),     # 部分：时短
        judge_day(D - dt.timedelta(days=2), planned_sport="hiit", planned_min=25,
                  target_zone="Z4", activities=[], **base),                             # 未完成
        judge_day(D - dt.timedelta(days=3), planned_sport="running", planned_min=30,
                  target_zone="Z2", activities=[_act("strength_training", 40, 120)],
                  **base),                                                              # 替代
        judge_day(D - dt.timedelta(days=4), planned_sport="running", planned_min=25,
                  target_zone="Z2", adjusted=True,
                  activities=[_act("running", 25, 135)], **base),                       # 调整后完成
        judge_day(D - dt.timedelta(days=5), planned_sport=None, activities=[],
                  **base),  # 无计划无记录
        judge_day(D + dt.timedelta(days=1), planned_sport="running", planned_min=30,
                  target_zone="Z2", activities=[], **base),                            # 未到期
    ]


def test_week_aggregation_counts_only_judged_days():
    """S2-1：status_counts 只统计已判定日；未到期/无计划无记录单独披露，不混入。"""
    out = build_week_execution(_week_mixed_judgments())
    assert out["status_counts"] == {
        ExecutionStatus.COMPLETED: 1,
        ExecutionStatus.PARTIAL: 1,
        ExecutionStatus.MISSED: 1,
        ExecutionStatus.SUBSTITUTED: 1,
        ExecutionStatus.ADJUSTED_COMPLETED: 1,
    }
    assert [u["date"] for u in out["unjudged_days"]] == [
        (D - dt.timedelta(days=5)).isoformat(),
        (D + dt.timedelta(days=1)).isoformat(),
    ]
    assert any("未到期" in u["reason"] for u in out["unjudged_days"])


def test_week_aggregation_separates_planned_and_actual_volume():
    """S2-2：计划量与实际量分开统计（计划 30+30+25+30+25=140；实际 30+20+0+40+25=115）。"""
    out = build_week_execution(_week_mixed_judgments())
    assert out["planned_volume_min"] == 140
    assert out["actual_volume_min"] == 115
    assert out["planned_volume_min"] != out["actual_volume_min"]


def test_week_aggregation_records_diff_reasons_per_day():
    """S2-3：差异原因逐日记录（部分完成要说清栽在时长还是强度）。"""
    out = build_week_execution(_week_mixed_judgments())
    details = {d["date"]: d for d in out["day_details"]}
    assert "时长不足" in details[(D - dt.timedelta(days=1)).isoformat()]["reason"]
    assert "未执行" in details[(D - dt.timedelta(days=2)).isoformat()]["reason"]


def test_week_aggregation_lists_adjusted_days():
    """S2-4：调整日单独列出（§12.4「记录日报调整」）。"""
    out = build_week_execution(_week_mixed_judgments())
    assert out["adjusted_days"] == [(D - dt.timedelta(days=4)).isoformat()]
