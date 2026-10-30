"""注册表驱动执行（③ 风险规则双轨制的收敛方向，2026-10-25）。

接缝：
- ``app.domain.priority.judge_priority`` —— 判定行为；
- ``app.domain.rule_registry.RULES`` —— 规则定义（阈值 / 输出标志 / 参数）。

要锁死的性质：**改注册表 → 判定随之改变**。
在此之前 ``priority.py`` 直接读模块常量，注册表只是被测试朗读的文档，
于是「文字写的阈值」与「代码用的阈值」存在漂移空间（安全规则最不该有的东西）。

本文件只测这条性质；每条规则的开/闭边界由 ``test_priority_rules.py`` 的特征用例看守。
"""

from __future__ import annotations

import pytest

from app.domain import rule_registry as rr
from app.domain.priority import DataStateInput, judge_data_state, judge_priority
from app.schemas.enums import PriorityLevel

D = __import__("datetime").date(2026, 10, 22)


def _inp(**over: object) -> DataStateInput:
    base = {
        "on_date": D,
        "acwr_ratio": 1.05,
        "acwr_stale": False,
        "hrv_status": "BALANCED",
        "hrv_last_night_avg": 70.0,
        "hrv_baseline": 70.0,
        "rhr_today": 51,
        "rhr_baseline": 51.0,
        "sleep_score": 78,
        "body_battery_morning": 82,
        "hrv_balanced_streak": 3,
    }
    base.update(over)
    return DataStateInput(**base)  # type: ignore[arg-type]


def _judge(**over: object):
    inp = _inp(**over)
    state, _ = judge_data_state(inp)
    return judge_priority(inp, state)


# ------------------------------------------------------------
# 能力一：注册表里的阈值驱动判定
# ------------------------------------------------------------
def test_threshold_change_in_registry_changes_judgment() -> None:
    """把注册表里 P2-LOAD 的阈值从 1.3 抬到 1.5 → 同一输入（ACWR 1.35）不再命中。"""
    spec = rr.RULES["P2-LOAD"]
    assert _judge(acwr_ratio=1.35).priority is PriorityLevel.REDUCE

    old = spec.thresholds["acwr_elevated"]
    spec.thresholds["acwr_elevated"] = 1.5
    try:
        assert _judge(acwr_ratio=1.35).priority is PriorityLevel.MAINTAIN
    finally:
        spec.thresholds["acwr_elevated"] = old


# ------------------------------------------------------------
# 能力二：注册表里的输出标志驱动拦截行为
# ------------------------------------------------------------
def test_output_flag_change_in_registry_changes_blocks() -> None:
    """把注册表里 P2-LOAD 的 hard_block_upload 关掉 → 判定仍 REDUCE，但不再拦上传。"""
    spec = rr.RULES["P2-LOAD"]
    assert _judge(acwr_ratio=1.4).blocks_upload is True

    object.__setattr__(spec, "hard_block_upload", False)
    try:
        result = _judge(acwr_ratio=1.4)
        assert result.priority is PriorityLevel.REDUCE
        assert result.blocks_upload is False
    finally:
        object.__setattr__(spec, "hard_block_upload", True)


# ------------------------------------------------------------
# 能力三：文档阈值 = 行为翻转点（文字与代码不得漂移）
# ``op`` 是注册表 trigger 文字声明的比较符：ge=「≥ 阈值才触发」，lt=「< 阈值才触发」。
# 若有人只改代码常量不改注册表 thresholds，这里立刻红。
# ------------------------------------------------------------
@pytest.mark.parametrize(
    ("rule_id", "key", "field", "op", "over"),
    [
        ("P2-LOAD", "acwr_elevated", "acwr_ratio", "ge", {}),
        ("P2-SLEEP", "sleep_low", "sleep_score", "lt", {}),
        ("P1-SLEEP-BATTERY", "sleep_critical", "sleep_score", "lt", {"body_battery_morning": 30}),
        ("P1-SLEEP-BATTERY", "battery_critical", "body_battery_morning", "lt", {"sleep_score": 30}),
    ],
)
def test_documented_threshold_equals_behavior_flip_point(
    rule_id: str, key: str, field: str, op: str, over: dict
) -> None:
    spec = rr.RULES[rule_id]
    threshold = spec.thresholds[key]
    below = _judge(**{field: threshold - 0.01}, **over)
    at = _judge(**{field: threshold}, **over)
    if op == "ge":
        assert rule_id not in below.triggered_rules, "低于阈值不应触发"
        assert rule_id in at.triggered_rules, "达到阈值应触发"
    else:
        assert rule_id in below.triggered_rules, "低于阈值应触发"
        assert rule_id not in at.triggered_rules, "达到阈值不应触发"
