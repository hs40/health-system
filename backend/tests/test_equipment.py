"""器材配置单元测试（V8 / REQ-PLAN-003）。"""

from __future__ import annotations

from app.config import Settings
from app.domain import equipment

W = [2.5, 3.0, 4.0, 5.5, 6.5, 7.0, 9.5]


def test_default_settings_match_verified_equipment():
    s = Settings(_env_file=None)  # 忽略本地 .env，验证内置默认值
    assert s.dumbbell_is_pair is True
    assert s.dumbbell_weights_kg == W


def test_describe_pair_mentions_tiers_and_doubling():
    text = equipment.describe(W, is_pair=True)
    assert "成对" in text
    assert "2.5/3/4/5.5/6.5/7/9.5" in text
    assert "×2" in text


def test_describe_single_no_doubling():
    text = equipment.describe(W, is_pair=False)
    assert "单只" in text
    assert "×2" not in text


def test_snap_load_to_nearest_tier():
    assert equipment.snap_load_kg(4.2, W) == 4.0
    assert equipment.snap_load_kg(9.0, W) == 9.5
    assert equipment.snap_load_kg(1.0, W) == 2.5  # 低于最小档 → 吸附到最小档
    assert equipment.snap_load_kg(5.0, []) == 5.0  # 无档位时不改动


def test_pair_total_kg():
    assert equipment.pair_total_kg(6.5, is_pair=True) == 13.0
    assert equipment.pair_total_kg(6.5, is_pair=False) == 6.5
