"""校验口径（2026-10-21 决策 Q7）：递归叶子比对 + 双向覆盖。

真实故障背景：校验把嵌套结构（``splitSummaries``）**整体**比较，
Garmin 后来给接口加了 ``numClimbsAttempted`` / ``numClimbsCompleted`` 两个字段，
本地冻结的 raw 没有 → 156 条活动永久报「字段不符」，真缺口（体重 0 条、
活动少 1 条）被噪声淹没。

新口径：
- **只有同一路径的叶子值不同才算错误**；
- 云端新增字段 = schema 漂移（信息，不报错）；
- 覆盖必须**双向**：Garmin 有而 Tredict 无（上传遗漏）也要报。
"""

from __future__ import annotations

from app.domain.verify import compare_activity_coverage, compare_tredict_details, deep_diff


def test_new_cloud_field_is_drift_not_mismatch() -> None:
    """云端多出的字段 → 不算错误（drift）。"""
    local = {"splitSummaries": [{"duration": 585.424, "splitType": "INTERVAL_ACTIVE"}]}
    remote = {
        "splitSummaries": [
            {
                "duration": 585.424,
                "splitType": "INTERVAL_ACTIVE",
                "numClimbsAttempted": 0,
                "numClimbsCompleted": 0,
            }
        ]
    }
    mismatches, cloud_only, local_only = deep_diff(local, remote)
    assert mismatches == {}
    assert "splitSummaries[0].numClimbsAttempted" in cloud_only
    assert local_only == []


def test_leaf_value_difference_is_mismatch() -> None:
    """同一路径值不同 → 必须报错，并定位到叶子。"""
    local = {"splitSummaries": [{"duration": 585.424, "splitType": "INTERVAL_ACTIVE"}]}
    remote = {"splitSummaries": [{"duration": 600.0, "splitType": "INTERVAL_ACTIVE"}]}
    mismatches, cloud_only, _ = deep_diff(local, remote)
    assert "splitSummaries[0].duration" in mismatches
    assert mismatches["splitSummaries[0].duration"]["local"] == 585.424
    assert mismatches["splitSummaries[0].duration"]["remote"] == 600.0
    assert cloud_only == []


def test_local_only_field_is_reported_separately() -> None:
    """本地有、云端没有 → 单独归类（可能是云端下线字段，不算数据错）。"""
    mismatches, cloud_only, local_only = deep_diff({"legacyField": 1, "x": 2}, {"x": 2})
    assert mismatches == {} and cloud_only == []
    assert local_only == ["legacyField"]


def test_list_length_difference_is_mismatch() -> None:
    """列表长度不同 → 报错（不是 schema 漂移）。"""
    mismatches, _, _ = deep_diff({"laps": [1, 2, 3]}, {"laps": [1, 2]})
    assert "laps" in mismatches


def test_numeric_tolerance_respected() -> None:
    """浮点**表示**噪声在容差内不算错（项目口径 1e-9，刻意严格）。"""
    mismatches, _, _ = deep_diff({"v": 585.424}, {"v": 585.4240000000001})
    assert mismatches == {}


# ------------------------------------------------------------
# 双向覆盖
# ------------------------------------------------------------
def test_coverage_reports_upload_missing() -> None:
    """Garmin 有 302、Tredict 只有 301 → 必须报出漏传的那条。"""
    out = compare_activity_coverage({"a", "b", "c"}, {"a", "b"})
    assert out["only_garmin"] == ["c"]
    assert out["only_tredict"] == []


def test_coverage_reports_extra_in_tredict() -> None:
    """Tredict 多出的（来源异常/手工上传）也要报。"""
    out = compare_activity_coverage({"a"}, {"a", "zzz"})
    assert out["only_tredict"] == ["zzz"]


def test_coverage_empty_when_identical() -> None:
    out = compare_activity_coverage({"a", "b"}, {"b", "a"})
    assert out == {"only_garmin": [], "only_tredict": []}


# ------------------------------------------------------------
# Tredict 详情快照 ↔ 云端实时（D 环）
# B 环只比 id 集合 → 详情腐烂不可见。真实风险：capacity/zones
# 修订后 Tredict 重算历史 effort（summary.effort.* 变），本地快照不变。
# ------------------------------------------------------------
def test_tredict_details_identical_snapshots() -> None:
    local = {"t1": {"summary": {"effort": {"heartrate": 65}, "duration": 2600}}}
    cloud = {"t1": {"summary": {"effort": {"heartrate": 65}, "duration": 2600}}}
    out = compare_tredict_details(local, cloud)
    assert out["sampled"] == 1
    assert out["value_mismatch"] == {}
    assert out["drift_paths"] == []


def test_tredict_details_detects_recalculated_effort() -> None:
    """云端 effort 被重算（capacity 修订触发）→ 必须报错并定位到叶子。"""
    local = {"t1": {"summary": {"effort": {"heartrate": 65, "power": 84}}}}
    cloud = {"t1": {"summary": {"effort": {"heartrate": 70, "power": 84}}}}
    out = compare_tredict_details(local, cloud)
    assert "t1" in out["value_mismatch"]
    paths = out["value_mismatch"]["t1"]
    assert "summary.effort.heartrate" in paths
    assert paths["summary.effort.heartrate"]["local"] == 65
    assert paths["summary.effort.heartrate"]["remote"] == 70


def test_tredict_details_new_cloud_field_is_drift() -> None:
    """Tredict 新增字段（schema 漂移）不算错——与 A 环同口径。"""
    local = {"t1": {"summary": {"duration": 2600}}}
    cloud = {"t1": {"summary": {"duration": 2600, "newMetric": 1}}}
    out = compare_tredict_details(local, cloud)
    assert out["value_mismatch"] == {}
    assert "t1:summary.newMetric" in out["drift_paths"]


def test_tredict_details_reports_missing_on_both_sides() -> None:
    local = {"t1": {"x": 1}, "t2": {"x": 2}}
    cloud = {"t1": {"x": 1}}
    out = compare_tredict_details(local, cloud)
    assert out["missing_remote"] == ["t2"]  # 云端已删 → 本地快照过期
    assert out["missing_local"] == []       # 本地没有云端有 → 拉取遗漏（由 B 环负责，这里只登记）
