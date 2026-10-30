"""策略三层差异 + 恢复前检查测试（阶段④ T4.3 / T4.6；§9.2 / §9.6）。

覆盖：
- 三层差异：摘要 / 关键差异 / 完整差异（§9.6）；
- **完整差异包含未变化项**，不得只显示变化项（§9.6.3）；
- 恢复前检查 7 项（§9.2）：冲突时拒绝并说明原因。
"""

from __future__ import annotations

from app.domain.strategy_diff import diff_versions
from app.domain.strategy_restore import check_restorable

_ALL_METRICS = {
    "standard_daily.hrv_last_night_avg",
    "standard_daily.resting_heart_rate",
    "standard_daily.sleep_score",
    "standard_daily.body_battery_morning",
    "baseline.hrv_mean",
    "acwr",
    "environment_daily",
}


def _v(number: str, *, season: str = "autumn", focus: str = "秋季有氧", **extra) -> dict:
    base = {
        "version_number": number,
        "status": "active",
        "change_type": "aiOptimize",
        "change_reason": "换季优化",
        "expected_effect": "提升有氧占比",
        "actual_effect": None,
        "validation_status": "passed",
        "created_by": "ai",
        "created_at": "2026-10-16T08:00:00",
        "effective_at": "2026-10-16T08:00:00",
        "payload": {"season": season, "focus": focus},
        "applicability_conditions": {"min_data_days": 14},
        "safety_constraints": {"hard_env_constraints": True},
        "validation_result": {},
        "limitations": [],
    }
    base.update(extra)
    return base


# ---------------------------- 三层差异（§9.6）----------------------------
def test_diff_has_three_layers():
    d = diff_versions(_v("season-1.0.0"), _v("season-1.0.1", focus="秋季力量"))
    assert set(d) >= {"summary", "key_changes", "full_diff"}


def test_diff_summary_layer():
    """第一层：版本摘要（变化概况/原因/效果/风险/生效时间/变更类型）。"""
    d = diff_versions(_v("season-1.0.0"), _v("season-1.0.1"))
    s = d["summary"]
    assert s["change_overview"] == "season-1.0.0 → season-1.0.1"
    assert s["change_type"] == "aiOptimize"
    assert s["change_reason"] == "换季优化"
    assert s["effective_at"] == "2026-10-16T08:00:00"


def test_diff_key_changes_layer():
    """第二层：关键差异（策略方向/关键参数/适用条件/安全约束/依赖/预期与实际）。"""
    a = _v("season-1.0.0", focus="秋季有氧", seasonal_extra=None)
    b = _v("season-1.0.1", focus="秋季力量")
    d = diff_versions(a, b)
    kc = d["key_changes"]
    assert kc["strategy_direction"]["changed"] is True
    assert kc["strategy_direction"]["from"] == "秋季有氧"
    assert kc["strategy_direction"]["to"] == "秋季力量"
    assert "safety_constraints" in kc
    assert "applicability_conditions" in kc
    assert "expected_vs_actual" in kc


def test_diff_full_layer_includes_unchanged():
    """第三层：完整差异**包含未变化项**（§9.6.3）。"""
    a = _v("season-1.0.0")
    b = _v("season-1.0.1", focus="秋季力量")
    d = diff_versions(a, b)
    full = d["full_diff"]

    # 未变化的字段也必须出现在完整差异中
    assert "version_number" in full["scalar_fields"]
    assert full["scalar_fields"]["status"]["changed"] is False
    # 安全约束未变化但仍在完整差异里
    assert "safety_constraints" in full["meta_fields"]
    assert full["meta_fields"]["safety_constraints"]["hard_env_constraints"]["changed"] is False
    # 声明：不得只显示变化项
    assert "包含未发生变化的项目" in full["note"]


def test_diff_marks_changed_field():
    a = _v("season-1.0.0")
    b = _v("season-1.0.1", focus="秋季力量")
    d = diff_versions(a, b)
    assert d["full_diff"]["payload"]["focus"]["changed"] is True
    assert d["full_diff"]["payload"]["season"]["changed"] is False


# ---------------------------- 恢复前检查（§9.2）----------------------------
def test_restorable_passes_with_valid_target():
    out = check_restorable(
        target={"season": "autumn", "focus": "秋季有氧"},
        available_metrics=_ALL_METRICS,
        replay_total=470,
        replay_matched=470,
    )
    assert out["allowed"] is True
    assert out["conflicts"] == []
    assert all(out["checks"].values())


def test_restore_refused_when_target_incomplete():
    """目标版本不完整 → 不得恢复。"""
    out = check_restorable(
        target={},
        available_metrics=_ALL_METRICS,
        replay_total=470,
        replay_matched=470,
    )
    assert out["allowed"] is False
    assert out["checks"]["target_complete"] is False
    assert any("不完整" in c for c in out["conflicts"])


def test_restore_refused_when_dependencies_unavailable():
    """当前策略依赖不可用 → 不得恢复。"""
    out = check_restorable(
        target={"season": "autumn", "focus": "秋季有氧"},
        available_metrics={"environment_daily"},  # 缺关键指标
        replay_total=470,
        replay_matched=470,
    )
    assert out["allowed"] is False
    assert out["checks"]["dependencies_available"] is False
    assert any("依赖不可用" in c for c in out["conflicts"])


def test_restore_refused_when_no_replay_sample():
    """无历史回放样本 → 不得判定通过 → 不得恢复（无证据不标成功）。"""
    out = check_restorable(
        target={"season": "autumn", "focus": "秋季有氧"},
        available_metrics=_ALL_METRICS,
        replay_total=0,
        replay_matched=0,
    )
    assert out["allowed"] is False
    assert any("恢复后可能影响关键训练决策" in c for c in out["conflicts"])


def test_restore_refused_on_known_defects():
    """存在已知缺陷 → 不得恢复。"""
    out = check_restorable(
        target={"season": "autumn", "focus": "秋季有氧"},
        available_metrics=_ALL_METRICS,
        known_defect_reasons=["该版本在高温下未验证"],
        replay_total=470,
        replay_matched=470,
    )
    assert out["allowed"] is False
    assert out["checks"]["no_known_defects"] is False
    assert any("已知缺陷" in c for c in out["conflicts"])


def test_restore_refused_when_safety_boundary_disallows():
    """当前安全边界不允许 → 不得恢复。"""
    out = check_restorable(
        target={"season": "autumn", "focus": "秋季有氧"},
        available_metrics=_ALL_METRICS,
        safety_boundary_ok=False,
        replay_total=470,
        replay_matched=470,
    )
    assert out["allowed"] is False
    assert out["checks"]["safety_boundary_ok"] is False


def test_restore_reports_all_seven_checks():
    """恢复前检查覆盖 §9.2 全部 7 项。"""
    out = check_restorable(
        target={"season": "autumn", "focus": "秋季有氧"},
        available_metrics=_ALL_METRICS,
        replay_total=470,
        replay_matched=470,
    )
    assert set(out["checks"]) == {
        "target_complete",
        "target_verified",
        "safety_boundary_ok",
        "data_definition_compatible",
        "dependencies_available",
        "no_known_defects",
        "decision_impact_acceptable",
    }
