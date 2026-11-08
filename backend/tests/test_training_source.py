"""运动训练详情页所需 FIT 消息读取（三期 §8，Q8/Q12）。

raw 是唯一事实源；一次解压按消息类型分组，坏行容忍。
缺文件必须显式标记 ``source=missing``，不能静默返回空 dict —— 否则
「FIT 明细未同步」与「本次运动没有逐秒心率」在详情页长得一模一样。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from app.storage.activity_source import load_fit_messages

AID = "652990997"


def _write(tmp_path: Path, rows: list[object], *, bad_line: bool = True) -> Path:
    d = tmp_path / "garmin" / "fit_jsonl"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{AID}.jsonl.gz"
    with gzip.open(p, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        if bad_line:
            f.write("not-json\n")
    return p


def test_load_fit_messages_groups_by_type_and_tolerates_bad_lines(tmp_path: Path) -> None:
    _write(
        tmp_path,
        [
            {"m": "record", "d": {"heart_rate": 120}},
            {"m": "record", "d": {"heart_rate": None}},
            {"m": "session", "d": {"avg_heart_rate": 130}},
            {"m": "lap", "d": {"total_distance": 1000}},
            {"m": "set", "d": {"repetitions": 10}},
            {"m": "length", "d": {"swim_stroke": "breaststroke"}},
        ],
    )
    result = load_fit_messages(tmp_path / "garmin", AID)
    assert result.source == "present"
    msgs = result.by_type
    assert sorted(msgs) == ["lap", "length", "record", "session", "set"]
    assert [r["heart_rate"] for r in msgs["record"]] == [120, None]
    assert msgs["session"] == [{"avg_heart_rate": 130}]
    assert msgs["length"][0]["swim_stroke"] == "breaststroke"


def test_load_fit_messages_reports_missing_file_instead_of_silent_empty(tmp_path: Path) -> None:
    """NAS 单容器缺 data/raw/garmin/fit_jsonl/ 时的真实场景。

    以前返回裸 ``{}``，前端只能显示「本次无逐秒心率数据」，把「数据没搬全」
    伪装成「这次运动本来就没心率」。现在必须能区分。
    """
    result = load_fit_messages(tmp_path / "garmin", AID)
    assert result.source == "missing"
    assert result.by_type == {}
