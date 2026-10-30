"""规则登记表回归测试（Plan §5 T1.2/T1.4/T1.5）。

2026-10-22 风险引擎重设计：旧 R-001~R-008 三级体系**全量替换**为
P1–P4 四级优先级规则族（Q1/Q12），本文件随之重写。

覆盖：
- 登记表完整性：字段齐全、阈值只在登记表定义、版本一致；
- 依据等级：不冒充医学标准（T1.4）；
- 输出侧拦截语义：P1 拦生成+上传，P2 只拦上传（Q26）；
- 每天必有结论：P4 兜底存在（Q15）。
"""

from __future__ import annotations

from datetime import date

from app.domain import rule_registry as rr
from app.domain.risk import RiskInput, evaluate
from app.domain.rule_registry import RULE_VERSION, RULES, Basis, active_rules, get_rule
from app.schemas.enums import PriorityLevel

_D = date(2026, 10, 16)


# ============================================================
# 规则族结构
# ============================================================
def test_registry_covers_all_four_priorities():
    """每级优先级都至少有一条规则，保证判定链完整。"""
    priorities = {spec.priority for spec in RULES.values()}
    assert priorities == {"P1", "P2", "P3", "P4"}


def test_all_rules_have_required_fields():
    for rid, spec in active_rules().items():
        assert spec.rule_id == rid
        assert spec.priority in {"P1", "P2", "P3", "P4"}
        assert spec.title
        assert isinstance(spec.basis, Basis)
        assert spec.basis_note, f"{rid} 缺少依据说明"
        assert spec.trigger
        assert spec.action
        assert spec.version == RULE_VERSION


def test_all_rule_ids_share_one_version():
    """单一版本号，杜绝新旧规则混用（§7.4.9）。"""
    assert {spec.version for spec in RULES.values()} == {RULE_VERSION}


def test_old_rule_ids_are_gone():
    """旧 R-001~R-008 已全量删除，不得残留（Q12：不保留兼容层）。"""
    assert not any(rid.startswith("R-") for rid in RULES)


# ============================================================
# 依据等级（T1.4：不得冒充医学标准）
# ============================================================
def test_candidate_thresholds_are_marked():
    """候选值类阈值必须标为非 MEDICAL，且说明里点明「候选值」。"""
    for rid in ("P1-LOAD-HRV", "P1-SLEEP-BATTERY", "P2-SLEEP", "P3-PROGRESS"):
        spec = get_rule(rid)
        assert spec.basis != Basis.MEDICAL, f"{rid} 不得冒充医学标准"
        assert "候选值" in spec.basis_note, f"{rid} 未声明候选值性质"


def test_thresholds_live_only_in_registry():
    """阈值唯一事实来源：登记表常量与 RuleSpec.thresholds 必须一致。"""
    assert get_rule("P1-LOAD-HRV").thresholds["acwr_high"] == rr.ACWR_HIGH
    assert get_rule("P2-LOAD").thresholds["acwr_elevated"] == rr.ACWR_ELEVATED
    assert get_rule("P2-SLEEP").thresholds["sleep_low"] == rr.SLEEP_SCORE_LOW
    assert get_rule("P1-SLEEP-BATTERY").thresholds["battery_critical"] == rr.BATTERY_CRITICAL
    assert get_rule("P3-PROGRESS").thresholds["rhr_tolerance_bpm"] == rr.RHR_FLAT_TOLERANCE_BPM


# ============================================================
# 输出侧拦截语义（Q26 两级硬拦截）
# ============================================================
def test_p1_rules_block_both_generate_and_upload():
    for spec in rr.rules_for_priority("P1"):
        assert spec.hard_block_generate is True, f"{spec.rule_id} 必须拦截生成"
        assert spec.hard_block_upload is True, f"{spec.rule_id} 必须拦截上传"


def test_p2_rules_block_upload_only():
    for spec in rr.rules_for_priority("P2"):
        assert spec.hard_block_generate is False, f"{spec.rule_id} 不得拦截生成"
        assert spec.hard_block_upload is True, f"{spec.rule_id} 必须拦截上传"


def test_p3_p4_block_nothing():
    for pid in ("P3", "P4"):
        for spec in rr.rules_for_priority(pid):
            assert spec.hard_block_generate is False
            assert spec.hard_block_upload is False


def test_p2_adjustments_are_split_by_trigger():
    """Q19：负荷类 → 容量减半；恢复类 → 强度减量。"""
    assert get_rule("P2-LOAD").adjust_volume is True
    assert get_rule("P2-LOAD").adjust_intensity is False
    for rid in ("P2-HRV", "P2-SLEEP", "P2-RHR"):
        assert get_rule(rid).adjust_intensity is True
        assert get_rule(rid).adjust_volume is False


# ============================================================
# 不变量：每条规则都能被实际触发（防登记表与实现脱节）
# ============================================================
def test_every_rule_can_be_triggered():
    """逐条构造命中输入，确保登记表里的规则在实现里真实存在。"""
    cases = {
        "P1-LOAD-HRV": dict(acwr_ratio=1.6, hrv_status="LOW"),
        "P1-SLEEP-BATTERY": dict(sleep_score=40, body_battery_morning=30),
        "P2-LOAD": dict(acwr_ratio=1.35),
        "P2-HRV": dict(hrv_last_night_avg=50.0),  # 基线 60 → 低于 5ms 以上
        "P2-SLEEP": dict(sleep_score=55),
        "P2-RHR": dict(resting_heart_rate=56),  # 基线 52 → +4bpm
        "P3-PROGRESS": {},
    }
    base = dict(
        acwr_ratio=1.0,
        hrv_status="BALANCED",
        hrv_last_night_avg=60.0,
        hrv_baseline=60.0,
        hrv_balanced_streak=3,
        resting_heart_rate=52,
        rhr_baseline=52.0,
        sleep_score=80,
        body_battery_morning=80,
    )
    for rid, override in cases.items():
        inp = RiskInput(on_date=_D, **{**base, **override})
        a = evaluate(inp)
        assert rid in a.triggered_rules, (
            f"{rid} 未被触发（实际命中 {a.triggered_rules}，优先级 {a.priority.value}）"
        )


def test_p4_is_reachable_as_fallback():
    """P4 兜底必须可达（Q15：每天必有结论）。"""
    a = evaluate(
        RiskInput(
            on_date=_D,
            acwr_ratio=1.15,
            hrv_status="BALANCED",
            hrv_last_night_avg=60.0,
            hrv_baseline=60.0,
            hrv_balanced_streak=1,
            resting_heart_rate=52,
            rhr_baseline=52.0,
            sleep_score=80,
            body_battery_morning=80,
        )
    )
    assert a.priority is PriorityLevel.MAINTAIN
    assert a.triggered_rules == []
