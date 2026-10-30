"""P4 FIT 解析引擎测试。

分两层：

- **纯函数层**：不依赖任何 FIT 文件，覆盖归一化、换算、序列化规则。
  这几条规则是 P4 最容易错的地方（数字字段号、scale、数组、嵌套 dict）。
- **集成层**：解析真实 FIT，用 Garmin API 的 ``vO2MaxValue`` 做锚点。
  数据目录不存在时自动跳过，不拖累干净检出。
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.parsers.fit import (
    ParsedFit,
    RecordPoint,
    _apply_scale,
    _build_records,
    _jsonable,
    _mesg_name,
    _resolve_field,
    parse_fit,
    write_jsonl_gz,
)

FIT_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "garmin" / "fit"


# ============================================================
# 纯函数
# ============================================================
def test_mesg_name_known() -> None:
    assert _mesg_name("record_mesgs") == ("record", 20)
    assert _mesg_name("session_mesgs")[0] == "session"


def test_mesg_name_private() -> None:
    """私有消息以裸数字 key 出现，输出要带 private_ 前缀。"""
    assert _mesg_name("233") == ("private_233", 233)
    assert _mesg_name("79") == ("private_79", 79)


def test_resolve_field_uses_overlay() -> None:
    """79 的字段号 0 在 fit-file-parser 里有定义，要还原成 vo2_max。"""
    name, defn = _resolve_field(79, "0")
    assert name == "vo2_max"
    assert defn is not None
    assert defn["scale"] == pytest.approx(2048 / 7)


def test_resolve_field_unknown_stays_numeric() -> None:
    """record 的 107 连 fit-file-parser 都没定义 —— 不猜语义，保留字段号。"""
    name, defn = _resolve_field(20, "107")
    assert name == "private_107"
    assert defn is None


def test_resolve_field_named_untouched() -> None:
    name, defn = _resolve_field(20, "heart_rate")
    assert (name, defn) == ("heart_rate", None)


def test_apply_scale_vo2max() -> None:
    """FIT 规范：actual = raw / scale - offset。"""
    _, defn = _resolve_field(79, "0")
    assert _apply_scale(11757, defn) == pytest.approx(11757 / (2048 / 7))


def test_apply_scale_no_definition_is_identity() -> None:
    assert _apply_scale(74, None) == 74


def test_apply_scale_ignores_bool_and_str() -> None:
    _, defn = _resolve_field(79, "0")
    assert _apply_scale(True, defn) is True
    assert _apply_scale("x", defn) == "x"


def test_build_records_merges_fractional_cadence() -> None:
    """骑行/跑步的 0.5 步频在 fractional_cadence 里，不合并就丢精度。"""
    rows = [
        {
            "timestamp": datetime(2026, 10, 14, 9, 43, 12, tzinfo=UTC),
            "heart_rate": 74,
            "cadence": 80,
            "fractional_cadence": 0.5,
        }
    ]
    pts = _build_records(rows)
    assert len(pts) == 1
    assert pts[0].cadence == pytest.approx(80.5)


def test_build_records_falls_back_to_plain_fields() -> None:
    """没有 enhanced_* 时回落到 speed / altitude。"""
    rows = [
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "speed": 2.5,
            "altitude": 100.0,
        }
    ]
    pts = _build_records(rows)
    assert pts[0].speed == pytest.approx(2.5)
    assert pts[0].altitude == pytest.approx(100.0)


def test_build_records_skips_rows_without_timestamp() -> None:
    pts = _build_records([{"heart_rate": 100}, {"timestamp": None}])
    assert pts == []


def test_jsonable_handles_nested_and_bytes() -> None:
    value = {
        "when": datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        "pair": [1, 2],
        "blob": b"\x01\x02",
        "inner": {"k": datetime(2026, 1, 1, tzinfo=UTC)},
    }
    out = _jsonable(value)
    assert out["when"] == "2026-10-17T12:00:00+00:00"
    assert out["pair"] == [1, 2]
    assert out["blob"] == "0102"
    assert out["inner"]["k"] == "2026-01-01T00:00:00+00:00"
    json.dumps(out)  # 必须可序列化


def test_write_jsonl_gz_roundtrip(tmp_path: Path) -> None:
    parsed = ParsedFit(
        activity_id="1",
        file_sha256="x",
        file_bytes=0,
        records=[RecordPoint(seq=0, t=1, hr=100)],
        messages={
            "record": [{"timestamp": datetime(2026, 1, 1, tzinfo=UTC), "heart_rate": 100}],
            "private_233": [{"private_2": [0, 0, 0, 64]}],
        },
    )
    out = tmp_path / "1.jsonl.gz"
    lines = write_jsonl_gz(parsed, out)
    assert lines == 2

    with gzip.open(out, "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    names = [r["m"] for r in rows]
    assert names == ["private_233", "record"]  # 按消息名排序
    assert rows[1]["d"]["heart_rate"] == 100


# ============================================================
# 集成（依赖真实数据，缺失则跳过）
# ============================================================
pytestmark_data = pytest.mark.skipif(
    not FIT_DIR.exists(), reason="本地无 FIT 数据目录"
)


@pytestmark_data
def test_parse_real_fit_with_cross_check() -> None:
    """真实文件走完整链路（含 fitdecode 交叉校验）。"""
    path = FIT_DIR / "651549432.fit"
    if not path.exists():
        pytest.skip("缺少样本文件 651549432.fit")

    parsed = parse_fit(path)
    assert parsed.activity_id == "651549432"
    assert parsed.record_count > 1000
    assert parsed.counts["record"] == parsed.record_count


@pytestmark_data
def test_vo2max_matches_garmin_api_after_rounding() -> None:
    """锚点：FIT 的 activity_metrics.vo2_max 四舍五入后等于 API 的 vO2MaxValue。

    实测 18/18 一致。注意取 ``private_140``（本次活动算出），
    不是 ``private_79``（账号级用户指标，会随时间漂移）。
    """
    path = FIT_DIR / "651549432.fit"
    if not path.exists():
        pytest.skip("缺少样本文件 651549432.fit")

    parsed = parse_fit(path, cross_check=False)
    vo2 = parsed.messages["private_140"][0]["vo2_max"]
    assert round(vo2) == 40  # Garmin Connect 显示 40
