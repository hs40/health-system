"""接入层：外部系统适配器。

- garmin.py   —— python-garminconnect：同步 + 计划上传/排期/查询（独立鉴权）
- tredict.py  —— Tredict Python 重写（对拍 Node 脚本）
- llm.py      —— OpenAI 兼容 provider（DeepSeek）

所有适配器负责把外部超时/未知状态归一化为统一任务状态（REQ-TASK-004）。
"""

from app.adapters.garmin import GarminAdapter, GarminSyncResult
from app.adapters.tredict import (
    HealthSyncResult,
    TredictAdapter,
    build_bodyvalue_records,
    build_hrv_records,
    build_sleep_records,
)

__all__ = [
    "GarminAdapter",
    "GarminSyncResult",
    "HealthSyncResult",
    "TredictAdapter",
    "build_bodyvalue_records",
    "build_hrv_records",
    "build_sleep_records",
]
