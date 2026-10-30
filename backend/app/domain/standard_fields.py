"""标准层字段**唯一注册表**（② 白名单收敛，2026-10-26）。

本模块是「``standard_daily`` 有哪些字段」的全仓唯一事实源。其余各处**派生**自它，
不再各写一份：

- ``app/api/data.py::_STANDARD_FIELDS`` —— 数据 API 全量暴露（Garmin key → 列）；
- ``app/domain/verify.py::STANDARD_DAILY_FIELDS`` —— 云端校验比对（列名）；
- ``app/services/coach_service.py::_report_payload`` —— LLM 隐私子集（**显式保留**，
  因为它是安全边界，且含派生指标；只发其中 5 个标准字段）；
- 前端 ``DataPage.tsx::METRICS`` —— 趋势图子集（**显式保留**，UI 选择）；
- ``tests/test_privacy.py::ALLOWED_KEYS`` —— 隐私测试的独立预言（**显式保留**，
  派生会让测试自证、失去防护力）。

新增一个标准层指标 = 在 ``STANDARD_FIELDS`` 加一行（API 与校验自动跟上）；
是否发给 LLM / 是否上图表，各自按需显式声明。

与 ``StandardDailyRow`` 的列集由 ``tests/test_standard_fields.py`` 锁定一致
（模型加列忘了登记 → 测试红）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StandardField:
    """一条标准层字段：Garmin 原始键 → ``standard_daily`` 列名。"""

    garmin_key: str
    db_field: str


# 顺序即数据 API 的暴露顺序；与 StandardDailyRow 数据列一一对应。
STANDARD_FIELDS: tuple[StandardField, ...] = (
    StandardField("restingHeartRate", "resting_heart_rate"),
    StandardField("minHeartRate", "min_heart_rate"),
    StandardField("maxHeartRate", "max_heart_rate"),
    StandardField("sleepDurationSec", "sleep_duration_sec"),
    StandardField("sleepStartTs", "sleep_start_ts"),
    StandardField("sleepEndTs", "sleep_end_ts"),
    StandardField("hrvLastNightAvg", "hrv_last_night_avg"),
    # Garmin 自带 HRV 基线区间（E24）：随标准层详情一并暴露（不参与趋势图）
    StandardField("hrvLowUpper", "hrv_low_upper"),
    StandardField("hrvBalancedLow", "hrv_balanced_low"),
    StandardField("hrvBalancedHigh", "hrv_balanced_high"),
    StandardField("hrvWeeklyAvg", "hrv_weekly_avg"),
    StandardField("hrvStatus", "hrv_status"),
    # bodyBatteryWake：同步时刻残值（MostRecent）；bodyBatteryMorning：醒时值（AtWakeTime）。
    # 二者口径不同，见 api/data.py 与 DataPage 的注释。
    StandardField("bodyBatteryWake", "body_battery_wake"),
    StandardField("bodyBatteryMorning", "body_battery_morning"),
    StandardField("sleepScore", "sleep_score"),
    StandardField("weightKg", "weight_kg"),
    StandardField("steps", "steps"),
    StandardField("activeCalories", "active_calories"),
    StandardField("intensityModerateMin", "intensity_moderate_min"),
    StandardField("intensityVigorousMin", "intensity_vigorous_min"),
    StandardField("spo2Avg", "spo2_avg"),
    StandardField("respirationAvg", "respiration_avg"),
)

# —— 派生投影 ——
GARMIN_TO_DB: dict[str, str] = {f.garmin_key: f.db_field for f in STANDARD_FIELDS}
"""Garmin 原始键 → 列名（数据 API 全量暴露用）。"""

DB_FIELDS: tuple[str, ...] = tuple(f.db_field for f in STANDARD_FIELDS)
"""列名元组（云端校验比对用，顺序稳定）。"""
