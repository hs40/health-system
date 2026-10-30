"""Tredict 自生成数据拉取的决策锁定（2026-10-21 Q4）。

用户决策：``plannedTrainingList`` / ``equipmentList`` **不再拉取**——端点存在
但账号实测为空（从未排过计划、无装备），每日空拉无存档价值。

锁定方式：FakeAdapter **不定义** ``fetch_planned_trainings`` / ``fetch_equipment``
方法——若 collect() 还敢调它们，直接 AttributeError（测试红）。这比「断言没
调用」更强：调用即炸，不依赖调用记录。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pull_tredict_generated.py"


def _load():
    spec = importlib.util.spec_from_file_location("pull_gen_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeAdapter:
    """只有 3 个拉取方法——planned/equipment 方法**不存在**。"""

    def fetch_efforts(self, *, since=None, until=None):
        return {"20261021": [[65]]}

    def fetch_capacity(self):
        return {"running": [{"timestamp": "2026-09-01T13:41:46Z", "hrMax": 168}]}

    def fetch_zones(self):
        return {
            "running": {
                "heartrate": {
                    "2026-10-12T13:48:29Z": [
                        {"name": "Easy", "from": -1, "to": 139, "intensity": 0}
                    ]
                }
            }
        }


def test_collect_only_pulls_three_entities() -> None:
    """collect() 只拉 effort/capacity/zones；planned/equipment 方法不存在，调了就炸。"""
    mod = _load()
    out = mod.collect(_FakeAdapter(), effort_since=dt.date(2026, 1, 1))
    assert set(out) == {"effort", "capacity", "zones"}
    for entity, (payload, error) in out.items():
        assert error is None, f"{entity} 拉取失败: {error}"
        assert payload is not None


def test_flatten_capacity_keys_by_sport_and_timestamp() -> None:
    mod = _load()
    out = mod.flatten_capacity(
        {"running": [{"timestamp": "2026-09-01T13:41:46Z", "hrMax": 168, "hrLth": 158}]}
    )
    assert list(out) == ["running|2026-09-01T13:41:46Z"]
    assert out["running|2026-09-01T13:41:46Z"]["hrMax"] == 168


def test_flatten_zones_keys_by_sport_type_and_timestamp() -> None:
    mod = _load()
    out = mod.flatten_zones(
        {
            "running": {
                "heartrate": {
                    "2026-10-12T13:48:29Z": [{"name": "Easy", "to": 139}]
                }
            }
        }
    )
    key = "running|heartrate|2026-10-12T13:48:29Z"
    assert list(out) == [key]
    assert out[key]["bands"][0]["name"] == "Easy"
