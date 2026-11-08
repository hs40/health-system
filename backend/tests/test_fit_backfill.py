"""逐秒心率明细补齐（v3.4.5）。

Seam: ``app/services/fit_backfill.py`` 的公开接口（解析存储 + 补齐编排）。
解析引擎 ``app/parsers/fit.py`` 已有自己的测试，本文件不重复测 FIT 二进制解码：
用手工构造的 ``ParsedFit`` + fake fetcher / fake parser，不联网、不依赖真实 .fit 样本。

背景：``fit_jsonl/<aid>.jsonl.gz`` 历来由本机 scripts 生成后人工搬到 NAS，
容器镜像只 ``COPY backend/app``（不含 scripts），scheduler 也无解析 job。
本机 daily 自 2026-10-26 22:30 停跑后，NAS 每天新增 activities 永远没有
对应 jsonl.gz → 训练详情 ``hr_data_state=fit_missing``。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.parsers.fit import ParsedFit
from app.services.fit_backfill import backfill_missing, write_parsed_fit
from app.storage.activity_source import load_activities, load_fit_messages


def _parsed(aid: str, hrs: list[int]) -> ParsedFit:
    """构造一个最小的解析结果：只带 record + heart_rate（不碰 FIT 解码）。"""
    return ParsedFit(
        activity_id=aid,
        file_sha256="fake-sha256",
        file_bytes=128,
        messages={
            "record": [
                {"timestamp": f"2026-11-08T07:00:{i:02d}+00:00", "heart_rate": hr}
                for i, hr in enumerate(hrs)
            ]
        },
    )


def test_write_parsed_fit_produces_readable_jsonl_with_hr(tmp_path: Path) -> None:
    """S1：给定解析结果 → 写 gz → load_fit_messages 读到 present + heart_rate。"""
    root = tmp_path / "garmin"

    lines = write_parsed_fit(root, _parsed("655296550", [130, 135, 140]))

    assert lines == 3
    result = load_fit_messages(root, "655296550", {"record"})
    assert result.source == "present"
    assert [r["heart_rate"] for r in result.by_type["record"]] == [130, 135, 140]


def _write_activities(root: Path, aids: list[str]) -> None:
    """写一份最小 activities/<date>.json，供 load_activities 读（驼峰 activityId）。"""
    d = root / "activities"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-11-08.json").write_text(
        json.dumps(
            [
                {
                    "activityId": aid,
                    "activityType": {"typeKey": "running"},
                    "startTimeLocal": "2026-11-08 07:00:00",
                    "startTimeGMT": "2026-11-08 07:00:00",
                }
                for aid in aids
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_backfill_fills_only_ids_without_jsonl_and_never_overwrites(
    tmp_path: Path,
) -> None:
    """S2-a：只处理缺 gz 的活动；已有 gz 不被覆盖、也不再走 fetcher。"""
    root = tmp_path / "garmin"
    _write_activities(root, ["a1", "a2"])
    write_parsed_fit(root, _parsed("a1", [999]))
    fetched: list[str] = []

    def fetcher(aid: str) -> bytes:
        fetched.append(aid)
        return b"fake-fit"

    result = backfill_missing(
        root,
        load_activities(root),
        fetcher,
        parse=lambda p: _parsed(p.stem, [130, 135]),
    )

    assert fetched == ["a2"], "已有 gz 的活动不应再走 fetcher"
    assert result.ok == ["a2"]
    assert result.no_fit == [] and result.parse_failed == []
    assert result.skipped == 1
    # 幂等：a1 的既有内容未被覆盖
    assert [
        r["heart_rate"]
        for r in load_fit_messages(root, "a1", {"record"}).by_type["record"]
    ] == [999]
    # a2 已补齐且可读
    assert [
        r["heart_rate"]
        for r in load_fit_messages(root, "a2", {"record"}).by_type["record"]
    ] == [130, 135]


def test_backfill_isolates_failures_and_records_reasons(tmp_path: Path) -> None:
    """S2-b：单条 fetch/parse 失败不阻断其余，且失败原因可观测（供 ledger）。"""
    root = tmp_path / "garmin"
    _write_activities(root, ["b1", "b2", "b3"])

    def fetcher(aid: str) -> bytes | None:
        return None if aid == "b1" else b"fake-fit"

    def parser(path: Path) -> ParsedFit:
        if path.stem == "b2":
            raise RuntimeError("decode failed")
        return _parsed(path.stem, [140, 145])

    result = backfill_missing(root, load_activities(root), fetcher, parse=parser)

    assert result.ok == ["b3"], "b1/b2 失败不应阻断 b3"
    assert result.no_fit == ["b1"]
    assert result.parse_failed == ["b2"]
    assert result.failures["b1"]
    assert "decode failed" in result.failures["b2"]
    assert load_fit_messages(root, "b3", {"record"}).source == "present"
