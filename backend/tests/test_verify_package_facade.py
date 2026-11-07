"""``app.domain.verify`` 包门面的兼容契约（ARCHITECTURE_REVIEW A.1.2 / C.4.2）。

``verify.py``（1016 行）拆成 ``verify/`` 包后，5 个测试文件 + 2 个脚本仍然
``from app.domain.verify import ...`` 直接引用内部符号。门面必须原样导出，
否则拆分就是一次静默的破坏性变更。

这里的断言是**清单式**的：把「包必须导出什么」写成字面量，新增/删除公开符号
时先改这里，再改实现——避免有人顺手删掉一个「看起来没人用」的常量。
"""

from __future__ import annotations

import pytest

#: 旧 ``verify.py`` 的公开符号全集（拆分前用 AST 扫描得出，共 46 个）。
#: 删除任何一个都是破坏性变更：scripts/verify_sync.py 与 scripts/verify_cloud.py
#: 不在 pytest 覆盖范围内，删了测试也不会红。
PUBLIC_API: tuple[str, ...] = (
    "ABS_TOL",
    "ACTIVITY_DISTANCE_ABS_TOL",
    "ACTIVITY_DISTANCE_FIELD",
    "ACTIVITY_DISTANCE_REL_TOL",
    "ACTIVITY_DURATION_TOL",
    "ACTIVITY_SUMMARY_TOL",
    "BODYVALUES_COMPARABLE",
    "CheckResult",
    "FAIL_STATES",
    "HEALTH_KINDS",
    "HEALTH_RAW_ALIASES",
    "HEALTH_RAW_COMPARABLE",
    "NON_DATA_FIELDS",
    "RECORD_FIELDS",
    "REL_TOL",
    "STANDARD_DAILY_FIELDS",
    "STATE_AMBIGUOUS",
    "STATE_MISMATCH",
    "STATE_MISSING_LOCAL",
    "STATE_MISSING_REMOTE",
    "STATE_OK",
    "SWIM_SUB_SPORTS",
    "activity_local_date",
    "canonical_sha256",
    "check_activity_records",
    "check_garmin_activity",
    "check_standard_daily",
    "check_tredict_activity",
    "code_fingerprint",
    "compare_activity_coverage",
    "compare_cloud_pair",
    "compare_health_fields",
    "compare_health_raw",
    "compare_online_garmin",
    "compare_online_health",
    "compare_online_tredict",
    "compare_tredict_details",
    "deep_diff",
    "diff_fields",
    "duration_fields",
    "exit_code",
    "health_field_value",
    "ids_of_day",
    "rehydrate_record_row",
    "same_value",
    "summarize",
)


def test_facade_exports_every_legacy_public_symbol():
    from app.domain import verify

    missing = [name for name in PUBLIC_API if not hasattr(verify, name)]
    assert missing == [], f"门面缺少符号：{missing}"


def test_all_declares_exactly_the_public_api():
    """``__all__`` 必须与清单一致——它是 ruff 不删 re-export 的唯一依据。"""
    from app.domain import verify

    assert set(verify.__all__) == set(PUBLIC_API)


@pytest.mark.parametrize("name", PUBLIC_API)
def test_symbol_is_reachable_from_package(name: str):
    import app.domain.verify as verify

    assert getattr(verify, name) is not None


def test_state_constants_keep_their_wire_values():
    """状态是写进 ``sync_ledger`` 与报告 JSON 的字面量，改动即破坏历史数据可读性。"""
    from app.domain import verify

    assert verify.STATE_OK == "ok"
    assert verify.STATE_MISMATCH == "mismatch"
    assert verify.STATE_MISSING_LOCAL == "missing_local"
    assert verify.STATE_MISSING_REMOTE == "missing_remote"
    assert verify.STATE_AMBIGUOUS == "ambiguous"
    assert frozenset(
        {verify.STATE_MISMATCH, verify.STATE_MISSING_LOCAL, verify.STATE_MISSING_REMOTE}
    ) == verify.FAIL_STATES


def test_canonical_sha256_is_stable_across_dict_ordering():
    """哈希口径是跨进程契约：键序不影响结果，中文不转义。"""
    from app.domain import verify

    a = verify.canonical_sha256({"b": 2, "a": "中文"})
    b = verify.canonical_sha256({"a": "中文", "b": 2})
    assert a == b
    assert len(a) == 64


def test_code_fingerprint_covers_three_algorithms():
    """指纹是「算法变了 vs 数据变了」的唯一区分手段，三段缺一不可。"""
    from app.domain import verify

    fp = verify.code_fingerprint()
    assert set(fp) == {"standardize", "build_records", "match_activities"}
    assert all(len(v) == 16 for v in fp.values())
