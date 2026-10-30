"""日任务编排测试（G-9 / 2026-10-19 拆成「两同步 + 一校验」）。

日任务的价值全在「顺序」「开关」「触发关系」与「修复边界」上，一旦改错，
后果都是**静默漏数据**：

- 顺序：解析必须早于上传；拉健康必须早于传健康
- 开关：活动上传必须是 ``--only-missing --apply``——漏了 ``--apply`` 就只演练不上传
  （正是 G-9 事故的形态），加了 ``--all`` 则可能重复推历史活动
- 触发：校验必须**晚于**两条同步链（否则「本地有云端无」是必然的，天天误报）；
  ``--chain`` 是「同步完立即校验」的实现方式
- 修复边界：只有 ``verify_cloud`` 带 ``repair``，且只指向上传步；不能让校验去
  触发「重新拉云端」或「重解析」，那会把问题掩盖成数据漂移
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "daily_sync.py"
_spec = importlib.util.spec_from_file_location("daily_sync", SCRIPT)
daily = importlib.util.module_from_spec(_spec)
# dataclass 装饰器要靠 cls.__module__ 反查 sys.modules，必须先登记再执行
sys.modules["daily_sync"] = daily
_spec.loader.exec_module(daily)


def keys(profile: str) -> list[str]:
    return [s.key for s in daily.PROFILES[profile]]


def step(profile: str, key: str):
    return next(s for s in daily.PROFILES[profile] if s.key == key)


# ============================================================
# 结构
# ============================================================
def test_profiles_exist() -> None:
    assert set(daily.PROFILES) == {"activity", "health", "verify", "weekly", "all"}


def test_step_keys_unique_within_profile() -> None:
    for profile in daily.PROFILES:
        ks = keys(profile)
        assert len(ks) == len(set(ks)), f"{profile} 有重复步骤"


def test_sync_profiles_do_not_contain_verify() -> None:
    """校验独立成档：同步档里出现校验会让「同步完立即校验」变成两份实现。"""
    for profile in ("activity", "health"):
        assert "verify_cloud" not in keys(profile)


def test_verify_profile_has_no_sync_steps() -> None:
    ks = keys("verify")
    assert ks == ["verify_cloud", "md_report"]


def test_verify_profile_ends_with_doc() -> None:
    assert keys("verify")[-1] == "md_report"


def test_weekly_has_no_sync_steps() -> None:
    """周任务只校验，不搬运——搬运每天已经做过。"""
    for s in daily.STEPS_WEEKLY:
        assert "upload" not in s.args[0]
        assert "pull" not in s.key


def test_all_profile_is_activity_plus_health_plus_verify() -> None:
    assert keys("all") == keys("activity") + keys("health") + keys("verify")


# ============================================================
# 顺序
# ============================================================
def test_activity_parse_runs_before_upload() -> None:
    ks = keys("activity")
    assert ks.index("fit_parse") < ks.index("activity_upload")


def test_health_pull_runs_before_health_upload() -> None:
    """G-9 补的缺口：raw 没更新，上传只会「成功地上传 0 条」。"""
    ks = keys("health")
    assert ks.index("health_pull") < ks.index("health_upload")


def test_health_pull_does_not_generate_report() -> None:
    """同步档只搬数据；日报 / LLM 是另一条链路（Q1=A）。"""
    assert step("health", "health_pull").args == ("pull_garmin_health.py",)


# ============================================================
# 开关
# ============================================================
def test_activity_upload_is_missing_only_and_applies() -> None:
    s = step("activity", "activity_upload")
    assert "--apply" in s.args
    assert "--only-missing" in s.args
    assert "--all" not in s.args
    assert "--force-meta" not in s.args


def test_only_pull_and_parse_are_critical() -> None:
    critical = {s.key for s in daily.STEPS_ACTIVITY if s.critical}
    assert critical == {"garmin_pull", "fit_parse"}


# ============================================================
# 触发与修复
# ============================================================
def test_repair_points_only_to_upload_steps() -> None:
    """自动修复只能重跑「上传」，不能重跑「拉取/解析」——后者会掩盖问题。"""
    for s in daily.PROFILES["verify"]:
        for target in s.repair:
            assert "upload" in target, f"{s.key} 的修复目标 {target} 不是上传步"


def test_repair_targets_exist_in_global_table() -> None:
    """verify 档里没有 upload 步，所以修复目标必须能从全局表查到。"""
    for s in daily.PROFILES["verify"]:
        for target in s.repair:
            assert target in daily.ALL_STEPS, f"{target} 不在 ALL_STEPS 中"


def test_repair_covers_both_chains() -> None:
    s = step("verify", "verify_cloud")
    assert set(s.repair) == {"activity_upload", "health_upload"}


def test_repairable_exit_code_is_three() -> None:
    assert daily.EXIT_REPAIRABLE == 3


def test_chain_appends_verify_steps() -> None:
    """``--chain`` 的语义：同步档后面接 verify 档（顺序不能反）。"""
    chained = list(daily.STEPS_HEALTH) + [
        s for s in daily.STEPS_VERIFY if s.key not in {"health_pull", "health_upload"}
    ]
    ks = [s.key for s in chained]
    assert ks.index("health_upload") < ks.index("verify_cloud")
    assert ks[-1] == "md_report"


# ============================================================
# dry-run
# ============================================================
def test_dry_run_does_not_spawn_subprocess() -> None:
    s = daily.PROFILES["activity"][0]
    result = daily.run_step(s, io.StringIO(), dry_run=True)
    assert result.ok is True
    assert result.exit_code == 0
    assert result.tail == ["(dry-run)"]
