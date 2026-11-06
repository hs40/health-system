"""逐秒心率 → Karvonen 五区时长（三期 §8 运动详情，Q8 口径）。

FIT ``record`` 消息逐秒一条，本模块把逐秒心率序列按项目既有的 Karvonen 五区
（``domain/load/hr.py``）分桶，得到各区停留秒数。

口径（Q8 用户确认）：

- 详情页**唯一**口径 = 逐秒 ``record`` 心率 + 项目 Karvonen 五区重算；
- 不展示 Garmin 原始 ``hrTimeInZone_*``（14 条冥想/呼吸活动该字段全零，
  但逐秒心率实际存在）；
- 心率缺失的秒数**不补 0**，单独计数供脚注披露。

分区判断直接复用 ``hr.zone_index()``，避免与负荷/日报口径产生第二套边界规则。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.load import hr as hr_mod


@dataclass(frozen=True, slots=True)
class ZoneSeconds:
    """一个心率区间的停留秒数（含边界，供前端柱状图标注）。"""

    name: str
    low_bpm: int
    high_bpm: int
    seconds: int


@dataclass(frozen=True, slots=True)
class HrZoneStats:
    """一次活动的五区时长统计。"""

    zones: tuple[ZoneSeconds, ...]
    counted_seconds: int
    below_zone_seconds: int
    missing_seconds: int

    def to_dict(self) -> dict:
        """供 API/前端序列化的稳定契约（Q8 脚注披露字段在此显式给出）。"""
        return {
            "zones": [
                {
                    "name": z.name,
                    "low_bpm": z.low_bpm,
                    "high_bpm": z.high_bpm,
                    "seconds": z.seconds,
                }
                for z in self.zones
            ],
            "counted_seconds": self.counted_seconds,
            "below_zone_seconds": self.below_zone_seconds,
            "missing_seconds": self.missing_seconds,
            "method": hr_mod.HR_METHOD,
            "max_hr": hr_mod.MAX_HR,
            "rest_hr": hr_mod.REST_HR_BASELINE,
        }


def summarize(hrs: Sequence[int | None]) -> HrZoneStats:
    """逐秒心率序列 → 五区时长；1 条记录 = 1 秒。

    ``None`` 表示该秒心率缺失：不计入任何区，也不补 0，只累加 ``missing_seconds``。
    """
    bands = hr_mod.build_zones()
    seconds = [0] * len(bands)
    counted = below = missing = 0
    for value in hrs:
        if value is None:
            missing += 1
            continue
        idx = hr_mod.zone_index(float(value), bands)
        if idx is None:
            below += 1
            continue
        seconds[idx] += 1
        counted += 1
    return HrZoneStats(
        zones=tuple(
            ZoneSeconds(name=b.name, low_bpm=b.low_bpm, high_bpm=b.high_bpm, seconds=seconds[i])
            for i, b in enumerate(bands)
        ),
        counted_seconds=counted,
        below_zone_seconds=below,
        missing_seconds=missing,
    )
