"""处方构造（TA.9 / TA.11）：``TrainingSession`` → Garmin 课程 JSON。

Garmin 课程结构（源自 python-garminconnect ``garminconnect.workout``）：
- 顶层：``workoutName`` / ``sportType`` / ``estimatedDurationInSecs`` / ``workoutSegments``
- 段内步骤：``ExecutableStepDTO``，含 ``stepType`` / ``endCondition`` / ``endConditionValue``
- 步骤类型 ID：warmup=1, cooldown=2, interval=3, recovery=4, rest=5, repeat=6, other=7, main=8
- 结束条件 ID：lap=1, time=2, distance=3, calories=4
- 目标类型 ID：no.target=1, hr.zone=4, speed.zone=5, pace.zone=6
"""

from __future__ import annotations

from typing import Any

from app.domain import rule_registry as rr
from app.domain.plan_rules import zone_int
from app.schemas.domain import StrengthSet, TrainingSession
from app.schemas.enums import Sport

SPORT_TYPE: dict[Sport, dict[str, Any]] = {
    Sport.RUNNING: {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    Sport.INDOOR_CARDIO: {
        "sportTypeId": 6,
        "sportTypeKey": "cardio_training",
        "displayOrder": 6,
    },
    Sport.STRENGTH: {
        "sportTypeId": 5,
        "sportTypeKey": "strength_training",
        "displayOrder": 5,
    },
}

_STEP_TYPE = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "main": 8}
_COND_TIME = {
    "conditionTypeId": 2,
    "conditionTypeKey": "time",
    "displayOrder": 2,
    "displayable": True,
}
_COND_DIST = {
    "conditionTypeId": 3,
    "conditionTypeKey": "distance",
    "displayOrder": 3,
    "displayable": True,
}
_NO_TARGET = {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1}
_HR_ZONE = {
    "workoutTargetTypeId": 4,
    "workoutTargetTypeKey": "heart.rate.zone",
    "displayOrder": 4,
}


def _step(
    order: int,
    kind: str,
    name: str | None = None,
    *,
    duration_sec: float | None = None,
    distance_m: float | None = None,
    hr_zone: int | None = None,
) -> dict[str, Any]:
    if distance_m is not None:
        end_cond, end_val = _COND_DIST, float(distance_m)
    else:
        end_cond, end_val = _COND_TIME, float(duration_sec or 0)
    step: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {
            "stepTypeId": _STEP_TYPE[kind],
            "stepTypeKey": kind,
            "displayOrder": _STEP_TYPE[kind],
        },
        "endCondition": end_cond,
        "endConditionValue": end_val,
        "targetType": _HR_ZONE if hr_zone else _NO_TARGET,
    }
    if hr_zone:
        step["zoneNumber"] = hr_zone
    if name:
        step["description"] = name
    return step


def estimate_duration_sec(session: TrainingSession) -> int:
    """估算总时长（秒）；显式给定时优先。"""
    if session.duration_min:
        return int(session.duration_min * 60)
    total = 0.0
    for group in (session.warmup, session.main, session.cooldown):
        for s in group:
            total += float(s.get("duration_sec") or 0)
    return int(total)


def to_garmin_workout(session: TrainingSession) -> dict[str, Any]:
    """把 ``TrainingSession`` 转换为 Garmin 课程 JSON（可 upload / update）。"""
    steps: list[dict[str, Any]] = []
    order = 1
    for s in session.warmup:
        steps.append(
            _step(order, "warmup", s.get("name"), duration_sec=s.get("duration_sec"))
        )
        order += 1
    for s in session.main:
        steps.append(
            _step(
                order,
                "main",
                s.get("name"),
                duration_sec=s.get("duration_sec"),
                distance_m=s.get("distance_m"),
                hr_zone=s.get("hr_zone"),
            )
        )
        order += 1
    for s in session.cooldown:
        steps.append(
            _step(order, "cooldown", s.get("name"), duration_sec=s.get("duration_sec"))
        )
        order += 1

    if not steps:
        # 无步骤时给一个占位主步，避免 Garmin 拒绝空课程
        steps.append(_step(1, "main", session.name, duration_sec=session.duration_min or 30))

    sport = SPORT_TYPE[session.sport]
    return {
        "workoutName": session.name,
        "description": session.name,
        "sportType": sport,
        "estimatedDurationInSecs": estimate_duration_sec(session),
        "workoutSegments": [
            {"segmentOrder": 1, "sportType": sport, "workoutSteps": steps}
        ],
    }


# ============================================================
# 处方兜底合成（⑥ 从 plan_service 下沉，2026-10-26；纯函数、无 IO）
# LLM 只返回 sport/name/duration/target_hr_zone 时，按规则补齐热身/主项/放松。
# ============================================================
def _ratio_sec(total_sec: int, ratio: float, lo: int, hi: int) -> int:
    """按比例切分时长并夹在 [lo, hi]；总时长不足时保底取下限。"""
    v = int(round(total_sec * ratio))
    return max(lo, min(hi, v))


def _presc_step(name: str, duration_sec: int, hr_zone: int | None = None) -> dict:
    out: dict = {"name": name, "duration_sec": int(duration_sec)}
    if hr_zone is not None:
        out["hr_zone"] = hr_zone
    return out


def fallback_steps(ts: TrainingSession) -> tuple[list[dict], list[dict], list[dict]]:
    """按运动类型 / 时长 / 目标区间合成 热身/主项/放松（确定性，无 IO）。"""
    sport = ts.sport.value
    zone = zone_int(ts.target_hr_zone)
    total_min = ts.duration_min or 0
    total_sec = total_min * 60

    warm_sec = _ratio_sec(
        total_sec, rr.PRESCRIPTION_WARMUP_RATIO,
        rr.PRESCRIPTION_WARMUP_MIN_SEC, rr.PRESCRIPTION_WARMUP_MAX_SEC,
    )
    cool_sec = _ratio_sec(
        total_sec, rr.PRESCRIPTION_COOLDOWN_RATIO,
        rr.PRESCRIPTION_COOLDOWN_MIN_SEC, rr.PRESCRIPTION_COOLDOWN_MAX_SEC,
    )
    # 无总时长时给一个可执行的最小主项（10min）
    main_sec = max(600, total_sec - warm_sec - cool_sec)

    warmup_name = rr.PRESCRIPTION_WARMUP_NAMES.get(
        sport, rr.PRESCRIPTION_WARMUP_NAMES["running"]
    )
    warmup = [_presc_step(warmup_name, warm_sec)]
    cooldown = [_presc_step(rr.PRESCRIPTION_COOLDOWN_NAME, cool_sec)]

    if sport == Sport.STRENGTH.value:
        # 力量日：主项按组次描述（不编造负荷，load_kg 留空由用户填）
        blocks = max(1, main_sec // (rr.PRESCRIPTION_STRENGTH_SETS * 60))
        main = [
            _presc_step(
                f"{ex} {rr.PRESCRIPTION_STRENGTH_SETS}×{rr.PRESCRIPTION_STRENGTH_REPS}",
                rr.PRESCRIPTION_STRENGTH_SETS * 60,
            )
            for ex in rr.PRESCRIPTION_STRENGTH_EXERCISES[: min(blocks, 4)]
        ]
    else:
        main_name = rr.PRESCRIPTION_MAIN_NAMES.get(
            sport, rr.PRESCRIPTION_MAIN_NAMES["running"]
        )
        main = [_presc_step(main_name, main_sec, zone)]

    return warmup, main, cooldown


def fallback_strength(ts: TrainingSession) -> list[StrengthSet]:
    """力量日兜底力量组：组×次固定，负荷不编造（None 表示由用户按能力填写）。"""
    return [
        StrengthSet(
            exercise=ex,
            sets=rr.PRESCRIPTION_STRENGTH_SETS,
            reps=rr.PRESCRIPTION_STRENGTH_REPS,
            load_kg=None,
            rest_sec=rr.PRESCRIPTION_STRENGTH_REST_SEC,
        )
        for ex in rr.PRESCRIPTION_STRENGTH_EXERCISES
    ]


def complete_steps(ts: TrainingSession) -> TrainingSession:
    """补齐缺失的处方步骤，使前端「展开完整训练处方」始终有内容。

    规则：
    - LLM 已返回的块**原样保留**（不覆盖 AI 结果），只补缺失块；
    - ``strength`` 仅力量日合成，有氧日不强加；
    - ``rationale`` 说明步骤来源（AI 生成 / 规则合成）。
    """
    warmup, main, cooldown = fallback_steps(ts)
    missing = not (ts.warmup and ts.main and ts.cooldown)
    strength = ts.strength
    if strength is None and ts.sport == Sport.STRENGTH:
        strength = fallback_strength(ts)

    if not missing and strength == ts.strength:
        return ts

    return ts.model_copy(
        update={
            "warmup": ts.warmup or warmup,
            "main": ts.main or main,
            "cooldown": ts.cooldown or cooldown,
            "strength": strength,
            "rationale": ts.rationale
            or (
                rr.PRESCRIPTION_FALLBACK_RATIONALE
                if missing
                else rr.PRESCRIPTION_LLM_RATIONALE
            ),
        }
    )
