"""标准层字段唯一注册表（② 白名单收敛，2026-10-26）。

接缝：
- ``app.domain.standard_fields.STANDARD_FIELDS`` —— 标准层字段的**唯一事实源**；
- ``app.api.data._STANDARD_FIELDS`` —— 数据 API 的全量暴露投影（Garmin key → 列）；
- ``app.domain.verify.STANDARD_DAILY_FIELDS`` —— 云端校验的比对投影（列名）。

要锁死的性质：**注册表是标准字段的唯一来源，投影必须由它派生**。

在此之前五处各写一份：``data.py``(22) / ``verify.py``(20) / ``_report_payload``(隐私子集)
/ 前端 ``DataPage.METRICS``(图表子集) / ``test_privacy.ALLOWED_KEYS``(测试预言)。
其中 ``verify.py`` 少了 ``sleep_score`` / ``body_battery_morning``（20 vs 模型 22），
导致云端 ``standard_daily`` 校验**静默跳过**这 2 个字段——这正是「改一处漏一处」的实证。

``_report_payload`` 与 ``test_privacy.ALLOWED_KEYS`` **刻意保持独立**（隐私边界 / 测试预言），
派生它们会让测试自证、失去防护力；本文件只锁「标准字段全量清单」这一条。
"""

from __future__ import annotations

from app.db.models import StandardDailyRow

# 模型里不属于「标准层指标」的元数据列
_META_COLUMNS = {"id", "date", "source_refs", "created_at"}


def _model_data_columns() -> set[str]:
    return {
        col.name
        for col in StandardDailyRow.__table__.columns
        if col.name not in _META_COLUMNS
    }


def test_registry_covers_every_model_column() -> None:
    """注册表的字段集 == ``StandardDailyRow`` 的数据列集。

    给模型加一列却忘了登记注册表 → 这里立刻红（反向也成立：登记了不存在的列 → 红）。
    """
    from app.domain.standard_fields import DB_FIELDS

    assert set(DB_FIELDS) == _model_data_columns()
    assert len(DB_FIELDS) == len(set(DB_FIELDS)), "注册表存在重复 db_field"


def test_registry_garmin_keys_are_unique_and_nonempty() -> None:
    from app.domain.standard_fields import STANDARD_FIELDS

    garmin_keys = [f.garmin_key for f in STANDARD_FIELDS]
    assert len(garmin_keys) == len(set(garmin_keys)), "Garmin key 重复"
    assert all(f.garmin_key and f.db_field for f in STANDARD_FIELDS)


def test_data_api_fields_derive_from_registry() -> None:
    """``api/data.py`` 的全量暴露映射必须等于注册表的 garmin→db 投影。"""
    from app.api import data
    from app.domain.standard_fields import GARMIN_TO_DB

    assert dict(GARMIN_TO_DB) == data._STANDARD_FIELDS


def test_verify_fields_derive_from_registry_and_cover_all() -> None:
    """``verify.py`` 的比对清单必须等于注册表的列投影，且覆盖此前漏掉的 2 个字段。"""
    from app.domain import verify
    from app.domain.standard_fields import DB_FIELDS

    assert verify.STANDARD_DAILY_FIELDS == DB_FIELDS
    # 曾经的盲区（20 vs 22）：这两项必须进入校验清单
    assert "sleep_score" in verify.STANDARD_DAILY_FIELDS
    assert "body_battery_morning" in verify.STANDARD_DAILY_FIELDS
