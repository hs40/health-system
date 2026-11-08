"""逐秒心率补齐的告警口径与端到端回归（v3.4.5，Q4 + S5）。

Seam:
- 纯函数 ``build_alert`` / ``dingtalk_reasons`` / ``write_ledger``：不碰 DB、不联网；
- ``run_backfill`` 编排是否把三者接起来；
- ``/api/activities/detail`` 在补齐写入 gz 后从 ``fit_missing`` → ``ok``。

Q4 口径：钉钉只在「整批失败 / Garmin 登录失效 / 连续 N 天仍有缺口」时报，
不逐条报；成功补齐不产生告警，避免每天刷屏。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.parsers.fit import ParsedFit
from app.services.fit_backfill import (
    backfill_missing,
    build_alert,
    dingtalk_reasons,
    load_activities,
    write_ledger,
)
from app.storage.activity_source import load_fit_messages

_TODAY = dt.date(2026, 11, 8)


def _summary(**kw: object) -> dict:
    base: dict = {
        "ok": True,
        "filled": [],
        "no_fit": [],
        "parse_failed": [],
        "skipped": 0,
        "failures": {},
    }
    base.update(kw)
    return base


def _parsed(aid: str, hrs: list[int]) -> ParsedFit:
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


def _write_activities(root: Path, aids: list[str]) -> None:
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


# ============================================================
# build_alert：全绿不告警，有失败同日去重
# ============================================================
def test_build_alert_is_none_when_everything_is_green() -> None:
    assert build_alert(_summary(filled=["a1"]), _TODAY) is None


def test_build_alert_summarizes_failures_with_day_scoped_ref_id() -> None:
    alert = build_alert(
        _summary(
            no_fit=["a1", "a2"],
            parse_failed=["a3"],
            failures={"a1": "无 FIT", "a3": "decode failed"},
        ),
        _TODAY,
    )
    assert alert is not None
    assert "缺 FIT 2 条" in alert["body"]
    assert "解析失败 1 条" in alert["body"]
    assert alert["ref_id"] == "fitBackfill:2026-11-08"


# ============================================================
# dingtalk_reasons：只在三种严重情形上报
# ============================================================
def test_dingtalk_stays_silent_on_partial_failures(tmp_path: Path) -> None:
    s = _summary(filled=["a1"], no_fit=["a2"], failures={"a2": "超时"})
    assert dingtalk_reasons(s, tmp_path, _TODAY) == []


def test_dingtalk_fires_on_total_batch_failure(tmp_path: Path) -> None:
    s = _summary(no_fit=["a1", "a2"], failures={"a1": "x", "a2": "y"})
    assert "整批补齐失败" in dingtalk_reasons(s, tmp_path, _TODAY)


def test_dingtalk_fires_on_auth_failure(tmp_path: Path) -> None:
    s = _summary(
        filled=["a1"], no_fit=["a2"], failures={"a2": "取 FIT 异常 401 Unauthorized"}
    )
    assert any("登录" in r for r in dingtalk_reasons(s, tmp_path, _TODAY))


def test_dingtalk_fires_on_persistent_gap(tmp_path: Path) -> None:
    for i in range(3):
        d = _TODAY - dt.timedelta(days=i)
        (tmp_path / f"{d.isoformat()}.json").write_text(
            json.dumps(_summary(no_fit=["x"], failures={"x": "y"})), encoding="utf-8"
        )
    s = _summary(filled=["a1"], no_fit=["a2"], failures={"a2": "超时"})
    assert any("连续" in r for r in dingtalk_reasons(s, tmp_path, _TODAY))


def test_dingtalk_silent_when_no_attempt_at_all(tmp_path: Path) -> None:
    assert dingtalk_reasons(_summary(skipped=10), tmp_path, _TODAY) == []


# ============================================================
# write_ledger：逐条失败可查
# ============================================================
def test_write_ledger_records_per_activity_failures(tmp_path: Path) -> None:
    s = _summary(filled=["a1"], no_fit=["a2"], failures={"a2": "Garmin 未返回 FIT"})
    p = write_ledger(tmp_path / "garmin", s, _TODAY)
    assert p == tmp_path / "garmin" / "_backfill_reports" / "2026-11-08.json"
    body = json.loads(p.read_text(encoding="utf-8"))
    assert body["failures"] == {"a2": "Garmin 未返回 FIT"}
    assert body["filled"] == ["a1"]


# ============================================================
# run_backfill：把 ledger / 消息中心 / 钉钉接起来
# ============================================================
def test_run_backfill_writes_ledger_and_dispatches_alerts(tmp_path, monkeypatch) -> None:
    import app.services.fit_backfill as mod

    settings = Settings(_env_file=None, raw_data_dir=tmp_path / "raw")
    root = tmp_path / "raw" / "garmin"

    monkeypatch.setattr(mod, "load_activities", lambda _root: {"x1": object()})
    sent: list[dict] = []
    ding: list[list[str]] = []
    monkeypatch.setattr(mod, "_send_message_center", lambda a: sent.append(a))
    monkeypatch.setattr(mod, "_send_dingtalk", lambda s, r: ding.append(r))

    summary = mod.run_backfill(
        settings,
        fetcher=lambda aid: None,  # 全部 no_fit → 整批失败
        today=_TODAY,
    )

    assert summary["no_fit"] == ["x1"]
    assert (root / "_backfill_reports" / "2026-11-08.json").exists()
    assert len(sent) == 1
    assert any("整批" in r for reasons in ding for r in reasons)


# ============================================================
# S5：补齐写入 gz 后，详情接口从 fit_missing → ok
# ============================================================
def test_detail_transitions_from_fit_missing_to_ok_after_backfill(tmp_path: Path) -> None:
    root = tmp_path / "raw" / "garmin"
    _write_activities(root, ["a1"])
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, raw_data_dir=tmp_path / "raw"
    )
    c = TestClient(app)
    try:
        params = {"date": "2026-11-08", "seq": 1}
        before = c.get("/api/activities/detail", params=params).json()
        assert before["activity"]["hr_data_state"] == "fit_missing"

        backfill_missing(
            root,
            load_activities(root),
            lambda aid: b"fake-fit",
            parse=lambda p: _parsed(p.stem, [130, 135, 140]),
        )

        after = c.get("/api/activities/detail", params=params).json()
        assert after["activity"]["hr_data_state"] == "ok"
        assert len(after["activity"]["hr_series"]) == 3
        assert load_fit_messages(root, "a1", {"record"}).source == "present"
    finally:
        app.dependency_overrides.clear()
