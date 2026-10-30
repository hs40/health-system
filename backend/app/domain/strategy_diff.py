"""策略版本三层差异比较（阶段④ T4.3 / T4.6；REQ §9.6）。

用户已确认版本比较采用**摘要、关键差异、完整差异**三层结构（§9.6）：

- **第一层 版本摘要**：版本变化概况、变更原因、实际评估效果、主要风险、生效时间、变更类型。
- **第二层 关键差异**：策略方向、关键参数、适用条件、安全约束、重要依赖、预期效果与实际效果。
- **第三层 完整规则和参数差异**：展开**所有**规则和参数逐项比较，**包括未发生变化的项目**；
  不得只显示变化的参数而隐藏完整规则结构（§9.6 硬性要求）。

纯逻辑、无 IO：入参为两个版本的领域模型字典。
"""

from __future__ import annotations

# 关键参数（第二层重点比较项）
_KEY_PARAMS = (
    "endurance_ratio",
    "strength_ratio",
    "recovery_ratio",
    "weekly_load_factor",
    "intensity_ceiling",
)

# 顶层参与完整比较的字典字段
_DICT_FIELDS = ("payload", "applicability_conditions", "safety_constraints")

# 标量字段（参与完整比较）
_SCALAR_FIELDS = (
    "version_number",
    "status",
    "change_type",
    "change_reason",
    "evidence_summary",
    "expected_effect",
    "actual_effect",
    "validation_status",
    "created_by",
    "created_at",
    "effective_at",
)


def diff_versions(a: dict, b: dict) -> dict:
    """比较两个版本（a=基准/旧，b=目标/新），返回三层差异结构（§9.6）。

    :param a: 旧版本（``StrategyVersion.model_dump(mode="json")``）。
    :param b: 新版本（同上）。
    """
    return {
        "summary": _summary(a, b),
        "key_changes": _key_changes(a, b),
        "full_diff": _full_diff(a, b),
    }


def _summary(a: dict, b: dict) -> dict:
    """第一层：版本摘要（§9.6.1）。"""
    payload_a, payload_b = a.get("payload") or {}, b.get("payload") or {}
    return {
        "change_overview": (
            f"{a.get('version_number', '?')} → {b.get('version_number', '?')}"
        ),
        "change_type": b.get("change_type"),
        "change_reason": b.get("change_reason") or "",
        "actual_effect": b.get("actual_effect"),
        "expected_effect": b.get("expected_effect") or "",
        "main_risks": list(b.get("limitations") or []),
        "effective_at": b.get("effective_at"),
        "created_at": b.get("created_at"),
        "season_from": payload_a.get("season"),
        "season_to": payload_b.get("season"),
        "validation_status": b.get("validation_status"),
    }


def _key_changes(a: dict, b: dict) -> dict:
    """第二层：关键差异（§9.6.2）—— 策略方向 / 关键参数 / 适用条件 /
    安全约束 / 重要依赖 / 预期效果与实际效果。
    """
    pa, pb = a.get("payload") or {}, b.get("payload") or {}
    return {
        "strategy_direction": _cmp("策略方向", pa.get("focus"), pb.get("focus")),
        "season": _cmp("季节", pa.get("season"), pb.get("season")),
        "key_params": {
            k: _cmp(k, pa.get(k), pb.get(k)) for k in _KEY_PARAMS
        },
        "applicability_conditions": _dict_cmp(
            a.get("applicability_conditions") or {}, b.get("applicability_conditions") or {}
        ),
        "safety_constraints": _dict_cmp(
            a.get("safety_constraints") or {}, b.get("safety_constraints") or {}
        ),
        "dependencies": _cmp(
            "重要依赖", (pa.get("dependencies") or []), (pb.get("dependencies") or [])
        ),
        "expected_vs_actual": {
            "expected_effect": b.get("expected_effect") or "",
            "actual_effect": b.get("actual_effect"),
        },
    }


def _dict_cmp(a: dict, b: dict) -> dict:
    """逐键比较（**包含未变化项**，§9.6.3）。"""
    keys = sorted(set(a) | set(b))
    return {k: _cmp(k, a.get(k), b.get(k)) for k in keys}


def _full_diff(a: dict, b: dict) -> dict:
    """第三层：完整规则和参数差异（§9.6.3）—— 逐项比较，
    **包括未发生变化的项目**，不得隐藏完整规则结构。
    """
    scalar: dict[str, dict] = {}
    for k in _SCALAR_FIELDS:
        scalar[k] = _cmp(k, a.get(k), b.get(k))

    payload_a, payload_b = a.get("payload") or {}, b.get("payload") or {}
    payload_keys = sorted(set(payload_a) | set(payload_b))
    payload_cmp = {k: _cmp(k, payload_a.get(k), payload_b.get(k)) for k in payload_keys}

    meta: dict[str, dict] = {}
    for f in _DICT_FIELDS:
        meta[f] = _dict_cmp(a.get(f) or {}, b.get(f) or {})

    limitations_a = list(a.get("limitations") or [])
    limitations_b = list(b.get("limitations") or [])
    validation_a = a.get("validation_result") or {}
    validation_b = b.get("validation_result") or {}

    return {
        "scalar_fields": scalar,
        "payload": payload_cmp,
        "meta_fields": meta,
        "limitations": _cmp("limitations", limitations_a, limitations_b),
        "validation_result": _dict_cmp(validation_a, validation_b),
        "note": "包含未发生变化的项目；不得只显示变化项（§9.6.3）",
    }


def _cmp(name: str, a: object, b: object) -> dict:
    """单项比较：标注 from/to 与是否变化（**未变化项也返回**）。"""
    return {
        "field": name,
        "from": a,
        "to": b,
        "changed": a != b,
    }


__all__ = ["diff_versions"]
