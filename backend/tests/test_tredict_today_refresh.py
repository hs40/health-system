"""当天活动供给（2026-11-07 修复）：23:00 上传前刷新当天 activities。

根因：23:00 的同步只读 ``activities/*.json`` 快照，而该快照仅由 07:30 日任务
写「当天」+ 次日回补「前一天」。当天 07:30 之后完成的训练，当天 23:00 不可见，
要到次日才落盘、次日 23:00 才上传（稳定延迟一天）。V42/V43 都只修上传执行侧，
从未碰数据供给侧。

测试 seam：**当天刷新编排**（不联网、不写云端）。fake garmin 记录 sync_day 调用，
断言「当天被刷新 / 空返回重拉 / 失败降级 / 不连坐上传」。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import Settings


class FakeGarminSyncResult:
    def __init__(self, *, written=None, skipped=None, errors=None, fetched=None):
        self.written = list(written or [])
        self.skipped = list(skipped or [])
        self.fetched = list(fetched or [])
        self.errors = dict(errors or {})


class FakeGarmin:
    """记录 sync_day 调用；按脚本返回预设结果。"""

    def __init__(self, results: list[FakeGarminSyncResult | Exception]):
        self.results = list(results)
        self.calls: list[tuple[dt.date, tuple[str, ...]]] = []

    def sync_day(self, day: dt.date, *, datasets=None):
        self.calls.append((day, tuple(datasets or ())))
        r = self.results.pop(0) if self.results else FakeGarminSyncResult(written=["activities"])
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture()
def settings(tmp_path):
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "db" / "t.db",
        audit_dir=tmp_path / "audit",
        garmin_tokenstore=tmp_path / "tokens",
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_refresh_syncs_today_activities_only():
    """核心：23:00 刷新的是「当天」activities，且只拉这一个数据集。"""
    from app.services.tredict_sync_service import refresh_today_activities

    today = dt.date(2026, 11, 5)
    garmin = FakeGarmin([FakeGarminSyncResult(written=["activities"])])

    out = refresh_today_activities(garmin, settings=None, today=today)

    assert garmin.calls == [(today, ("activities",))], (
        "必须只对当天调用一次 sync_day(datasets=['activities'])"
    )
    assert out["refreshed"] is True
    assert out["date"] == "2026-11-05"
    assert out["empty"] is False


def test_empty_result_retries_three_times_then_treats_as_rest_day():
    """Q3：空返回最多重拉 3 次；仍空按休息日正常结束、不抛、不告警。"""
    from app.services.tredict_sync_service import refresh_today_activities

    today = dt.date(2026, 11, 5)
    garmin = FakeGarmin([FakeGarminSyncResult(fetched=["activities"]) for _ in range(3)])

    out = refresh_today_activities(garmin, settings=None, today=today)

    assert len(garmin.calls) == 3
    assert out["attempts"] == 3
    assert out["empty"] is True
    assert out["refreshed"] is False
    assert out["errors"] == [], "休息日空返回不应记错误或告警"


def test_retry_stops_as_soon_as_activities_arrive():
    """第 2 次拉到活动即停止，不做第 3 次无谓调用。"""
    from app.services.tredict_sync_service import refresh_today_activities

    today = dt.date(2026, 11, 5)
    garmin = FakeGarmin([
        FakeGarminSyncResult(fetched=["activities"]),
        FakeGarminSyncResult(written=["activities"]),
    ])

    out = refresh_today_activities(garmin, settings=None, today=today)

    assert len(garmin.calls) == 2
    assert out["refreshed"] is True
    assert out["empty"] is False


def test_exception_is_contained_and_never_raised():
    """Q1：局部容错 —— Garmin 异常收敛进 errors，绝不连坐健康上传。"""
    from app.services.tredict_sync_service import refresh_today_activities

    today = dt.date(2026, 11, 5)
    garmin = FakeGarmin([RuntimeError("auth expired") for _ in range(3)])

    out = refresh_today_activities(garmin, settings=None, today=today)

    assert len(garmin.calls) == 3
    assert len(out["errors"]) == 3
    assert all("auth expired" in e for e in out["errors"])
    assert out["refreshed"] is False


# ============================================================
# 接线：run_tredict_sync 编排刷新 → 当天活动进入待传集合
# ============================================================
class _FakeTredict:
    """最小云端替身：清单为空 → 所有本地活动都算 missing。"""

    def __init__(self):
        self.upload_calls: list[tuple[str, bytes, str, str]] = []

    def fetch_activity_list(self, *, extended=False):
        return []

    def upload_activity(self, aid, fit_bytes, *, name="", notes=""):
        self.upload_calls.append((aid, fit_bytes, name, notes))
        return {"ok": True, "status": 201, "exists": False, "text": ""}

    def fetch_health(self):
        return {"hrv": {}, "sleep": {}, "bodyvalues": []}

    def _post_batch(self, url, key, records):
        return {"ok": len(records), "fail": 0, "errors": []}


def test_service_invokes_today_refresher_before_computing_missing(session, settings):
    """核心接线：refresher 在 missing 判定之前执行，结果并入 result。

    这是根因闭环的关键 —— 刷新后新写的当天活动必须被 load_activities() 看到，
    否则仍会稳定延迟一天。
    """
    import json

    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    today = dt.date(2026, 11, 5)
    order: list[str] = []

    def _refresher(day: dt.date) -> dict:
        order.append("refresh")
        assert day == today
        # 模拟 sync_day 刚把当天 19:00 完成的训练写进 activities/2026-11-05.json
        d = raw_garmin / "activities"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{today.isoformat()}.json").write_text(
            json.dumps([{
                "activityId": "659044501",
                "activityName": "轻松跑 Z2",
                "startTimeGMT": "2026-11-05 10:25:23",
                "startTimeLocal": "2026-11-05 18:25:23",
                "activityType": {"typeKey": "running"},
                "duration": 2857,
            }], ensure_ascii=False),
            encoding="utf-8",
        )
        (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
        (raw_garmin / "fit" / "659044501.fit").write_bytes(b"FITDATA")
        return {"date": day.isoformat(), "refreshed": True, "empty": False,
                "attempts": 1, "errors": []}

    cloud = _FakeTredict()
    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=today, today_refresher=_refresher,
    )

    assert order == ["refresh"], "refresher 必须被调用一次"
    assert "659044501" in out["activities"]["missing"], (
        "当天 19:00 完成的训练必须进入当天待传集合（根因修复点）"
    )
    assert [c[0] for c in cloud.upload_calls] == ["659044501"]
    assert out["today_refresh"]["refreshed"] is True


# ============================================================
# 接线：run_tredict_sync_job 注入真实 refresher（不改调度拓扑）
# ============================================================
def test_job_injects_today_refresher(monkeypatch, settings):
    """job 层必须把 refresher 传给服务；refresher 用真实 GarminAdapter 刷新当天。

    不改 build_scheduler() 注册拓扑（Q3=A），只在 job 内注入一个回调。
    """
    from contextlib import contextmanager

    from app.orchestrator import scheduler as sched

    captured: dict = {}
    refreshed: list[dt.date] = []

    @contextmanager
    def _scope():
        yield object()

    def _fake_refresh(garmin, st, day, **kw):
        refreshed.append(day)
        return {"date": day.isoformat(), "refreshed": True, "empty": False,
                "attempts": 1, "errors": []}

    def _spy(sess, st, **kwargs):
        captured.update(kwargs)
        # 真实 job 会传一个可调用 refresher；执行它以验证接线完整
        captured["refresher_result"] = kwargs["today_refresher"](dt.date(2026, 11, 5))
        return {"ok": True}

    monkeypatch.setattr(sched, "session_scope", _scope)
    monkeypatch.setattr(sched, "get_settings", lambda: settings)
    monkeypatch.setattr(sched, "run_tredict_sync", _spy)
    monkeypatch.setattr(sched, "refresh_today_activities", _fake_refresh)

    out = sched.run_tredict_sync_job()

    assert out["ok"] is True
    assert callable(captured.get("today_refresher"))
    assert refreshed == [dt.date(2026, 11, 5)]
    assert captured["refresher_result"]["refreshed"] is True


# ============================================================
# Q2 遗留①：不再假绿 —— 上传后云端未确认必须可见
# ============================================================
def test_uploaded_but_absent_from_cloud_is_reported_not_silently_green(session, settings):
    """上传返回 2xx 但云端清单仍无该活动 → 记 pending_cloud_index 并告警。

    V43 把 confirmed（本次 uploaded）直接并入 covered，导致「云端没真建记录」
    也判 ok=True、永不告警。修复后要把「已发上传但云端尚未索引」显式暴露，
    同时不误报为硬失败（避免刚传完就假红）。
    """
    import json

    from app.db.models import MessageRow
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    d = raw_garmin / "activities"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-11-05.json").write_text(
        json.dumps([{
            "activityId": "659044501", "activityName": "轻松跑",
            "startTimeGMT": "2026-11-05 10:25:23",
            "startTimeLocal": "2026-11-05 18:25:23",
            "activityType": {"typeKey": "running"}, "duration": 2857,
        }], ensure_ascii=False),
        encoding="utf-8",
    )
    (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
    (raw_garmin / "fit" / "659044501.fit").write_bytes(b"FITDATA")

    cloud = _FakeTredict()  # 上传后 fetch_activity_list 仍返回 []：云端未索引
    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    assert out["verify"]["pending_cloud_index"] == ["659044501"], (
        "已上传但云端未确认必须显式记录，不能并入 covered 后静默判绿"
    )
    assert out["verify"]["only_garmin"] == [], "索引延迟不应误报为硬缺失（不假红）"
    assert out["ok"] is True, "上传请求成功不应误判整体失败"
    msgs = session.exec(select(MessageRow)).all()
    assert any("云端" in (m.body or "") or "索引" in (m.body or "") for m in msgs), (
        "待复核状态必须落消息中心告警，杜绝 V42 式静默"
    )


# ============================================================
# Q2 遗留② / Q4：notes 随上传 + 只补空幂等回填
# ============================================================
class _NotesTredict:
    """支持 update_activity 的云端替身。"""

    def __init__(self, cloud=None):
        self.cloud = list(cloud or [])
        self.upload_calls: list[tuple[str, bytes, str, str]] = []
        self.update_calls: list[tuple[str, dict]] = []

    def fetch_activity_list(self, *, extended=False):
        return list(self.cloud)

    def upload_activity(self, aid, fit_bytes, *, name="", notes=""):
        self.upload_calls.append((aid, fit_bytes, name, notes))
        return {"ok": True, "status": 201, "exists": False, "text": ""}

    def update_activity(self, tid, patch):
        self.update_calls.append((tid, dict(patch)))
        return {"ok": True, "status": 200, "text": ""}

    def fetch_health(self):
        return {"hrv": {}, "sleep": {}, "bodyvalues": []}

    def _post_batch(self, url, key, records):
        return {"ok": len(records), "fail": 0, "errors": []}


def _write_strength_activity(raw_garmin: Path, aid: str, gmt: str) -> None:
    import json

    d = raw_garmin / "activities"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-11-05.json").write_text(
        json.dumps([{
            "activityId": aid,
            "activityName": "力量训练A",
            "startTimeGMT": gmt,
            "startTimeLocal": gmt.replace("GMT", "LOCAL"),
            "activityType": {"typeKey": "strength_training"},
            "duration": 1800,
            "summarizedExerciseSets": [
                {"category": "SQUAT", "subCategory": "BARBELL_SQUAT",
                 "sets": 3, "reps": 10, "volume": 0, "duration": 0, "maxWeight": 0},
            ],
        }], ensure_ascii=False),
        encoding="utf-8",
    )


def test_strength_upload_carries_notes(session, settings):
    """力量活动上传时必须带 notes（旧脚本同口径），而非只传 name。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    _write_strength_activity(raw_garmin, "659000001", "2026-11-05 10:00:00")
    (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
    (raw_garmin / "fit" / "659000001.fit").write_bytes(b"FITDATA")

    cloud = _NotesTredict()
    run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    assert len(cloud.upload_calls) == 1
    notes = cloud.upload_calls[0][3]
    assert notes.startswith("力量明细："), f"力量上传必须带 notes，实际 {notes!r}"
    assert "深蹲" in notes or "SQUAT" in notes.upper()


def test_backfills_notes_for_existing_cloud_record_without_notes(session, settings):
    """Q4：云端已存在但 notes 为空 → update_activity 补写（历史回填）。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    gmt = "2026-11-05 10:00:00"
    _write_strength_activity(raw_garmin, "659000002", gmt)
    (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
    (raw_garmin / "fit" / "659000002.fit").write_bytes(b"FITDATA")

    cloud = _NotesTredict(cloud=[{
        "id": "T-EXIST", "date": "2026-11-05T10:00:00.000Z",
        "sportType": "misc", "subSportType": "strength_training",
        "summary": {"duration": 1800}, "notes": "",
    }])
    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    assert cloud.upload_calls == [], "已存在不应重复上传"
    assert [(t, sorted(p)) for t, p in cloud.update_calls] == [("T-EXIST", ["notes"])]
    assert out["activities"]["meta_updated"] == [
        {"activity_id": "659000002", "tredict_id": "T-EXIST", "fields": ["notes"]}
    ]


def test_does_not_overwrite_existing_notes(session, settings):
    """Q4 只补空、幂等：云端已有 notes 时绝不覆盖用户手写内容。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    gmt = "2026-11-05 10:00:00"
    _write_strength_activity(raw_garmin, "659000003", gmt)
    (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
    (raw_garmin / "fit" / "659000003.fit").write_bytes(b"FITDATA")

    cloud = _NotesTredict(cloud=[{
        "id": "T-KEEP", "date": "2026-11-05T10:00:00.000Z",
        "sportType": "misc", "subSportType": "strength_training",
        "summary": {"duration": 1800}, "notes": "我手写的训练感受",
    }])
    run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    assert cloud.update_calls == [], "已有 notes 不得覆盖（只补空）"


# ============================================================
# A 层（v3.4.6）：生成 notes 前就地补 fit_jsonl
# ============================================================
def test_strength_notes_use_fit_per_set_before_upload(session, settings, monkeypatch):
    """上传前就地补 fit_jsonl → notes 走 FIT 逐组，而非「共N次」兜底。"""
    import app.services.fit_backfill as fb
    from app.parsers.fit import ParsedFit
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    _write_strength_activity(raw_garmin, "659000009", "2026-11-05 10:00:00")
    (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
    (raw_garmin / "fit" / "659000009.fit").write_bytes(b"FITDATA")

    def fake_parse(path: Path) -> ParsedFit:
        return ParsedFit(
            activity_id="659000009", file_sha256="fake", file_bytes=8,
            messages={"set": [
                {"set_type": "active", "category": ["squat", "squat"],
                 "category_subtype": [0, 0], "repetitions": 20,
                 "duration": 0, "weight": 0, "start_time": None}
                for _ in range(3)
            ]},
        )

    monkeypatch.setattr(fb, "parse_fit", fake_parse)

    cloud = _NotesTredict()
    run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    assert len(cloud.upload_calls) == 1
    notes = cloud.upload_calls[0][3]
    assert "20次 × 3组" in notes, f"应走 FIT 逐组分支，实际 {notes!r}"
    assert "缺逐组明细" not in notes, "A 层不该再触发 C 层兜底"
    assert (raw_garmin / "fit_jsonl" / "659000009.jsonl.gz").exists()


def test_backfill_notes_also_uses_fit_per_set(session, settings, monkeypatch):
    """补空路径（_backfill_notes）生成 notes 前也就地补 gz、拿 FIT 逐组。"""
    import app.services.fit_backfill as fb
    from app.parsers.fit import ParsedFit
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    _write_strength_activity(raw_garmin, "659000010", "2026-11-05 10:00:00")
    (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
    (raw_garmin / "fit" / "659000010.fit").write_bytes(b"FITDATA")

    def fake_parse(path: Path) -> ParsedFit:
        return ParsedFit(
            activity_id="659000010", file_sha256="fake", file_bytes=8,
            messages={"set": [
                {"set_type": "active", "category": ["squat", "squat"],
                 "category_subtype": [0, 0], "repetitions": 20,
                 "duration": 0, "weight": 0, "start_time": None}
                for _ in range(3)
            ]},
        )

    monkeypatch.setattr(fb, "parse_fit", fake_parse)

    cloud = _NotesTredict(cloud=[{
        "id": "T-BACKFILL", "date": "2026-11-05T10:00:00.000Z",
        "sportType": "misc", "subSportType": "strength_training",
        "summary": {"duration": 1800}, "notes": "",
    }])
    run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    assert cloud.upload_calls == [], "已存在不应重复上传"
    assert len(cloud.update_calls) == 1
    patched_notes = cloud.update_calls[0][1]["notes"]
    assert "20次 × 3组" in patched_notes, f"应走 FIT 逐组分支，实际 {patched_notes!r}"
    assert "缺逐组明细" not in patched_notes
    assert (raw_garmin / "fit_jsonl" / "659000010.jsonl.gz").exists()


# ============================================================
# Q2 遗留③：sync_ledger 记账（运维台直接读这张表）
# ============================================================
def test_upload_writes_sync_ledger(session, settings):
    """上传成功必须写 local2tredict 台账（运维台据此显示同步状态）。"""
    from sqlmodel import select

    from app.db.models import SyncLedgerRow
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    d = raw_garmin / "activities"
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("2026-11-05.json").write_text(
        '[{"activityId":"659044501","activityName":"轻松跑",'
        '"startTimeGMT":"2026-11-05 10:25:23","startTimeLocal":"2026-11-05 18:25:23",'
        '"activityType":{"typeKey":"running"},"duration":2857}]',
        encoding="utf-8",
    )
    (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
    (raw_garmin / "fit" / "659044501.fit").write_bytes(b"FITDATA")

    run_tredict_sync(
        session, settings, garmin=object(), tredict=_FakeTredict(),
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    rows = session.exec(
        select(SyncLedgerRow).where(
            SyncLedgerRow.entity_id == "659044501",
            SyncLedgerRow.direction == "local2tredict",
        )
    ).all()
    assert len(rows) == 1, "上传成功必须落一条 local2tredict 台账"
    assert rows[0].state == "ok"
    assert rows[0].detail.get("action") == "upload"


def test_upload_failure_ledger_state_is_missing_remote(session, settings):
    """上传失败 → 台账 state=missing_remote（可见的失败，不静默）。"""
    from sqlmodel import select

    from app.db.models import SyncLedgerRow
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    d = raw_garmin / "activities"
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("2026-11-05.json").write_text(
        '[{"activityId":"659044502","activityName":"轻松跑",'
        '"startTimeGMT":"2026-11-05 10:25:23","startTimeLocal":"2026-11-05 18:25:23",'
        '"activityType":{"typeKey":"running"},"duration":2857}]',
        encoding="utf-8",
    )
    (raw_garmin / "fit").mkdir(parents=True, exist_ok=True)
    (raw_garmin / "fit" / "659044502.fit").write_bytes(b"FITDATA")

    class _FailTredict(_FakeTredict):
        def upload_activity(self, aid, fit_bytes, *, name="", notes=""):
            return {"ok": False, "status": 500, "exists": False, "text": "boom"}

    run_tredict_sync(
        session, settings, garmin=object(), tredict=_FailTredict(),
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    rows = session.exec(
        select(SyncLedgerRow).where(SyncLedgerRow.entity_id == "659044502")
    ).all()
    assert len(rows) == 1
    assert rows[0].state == "missing_remote"


def test_new_cloud_activity_lands_in_tredict_activity_mirror(session, settings):
    """Q2 遗留③：云端出现新活动时补进 tredict_activity 镜像（运维台计数可见）。

    只做「不存在则插入」，已存在的交给 backfill 维护（避免两套 sha 口径反复覆盖）。
    """
    from sqlmodel import select

    from app.db.models import TredictActivityRow
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    (raw_garmin / "activities").mkdir(parents=True, exist_ok=True)

    cloud = _NotesTredict(cloud=[{
        "id": "T-NEW", "date": "2026-11-05T10:25:23.000Z",
        "sportType": "running", "subSportType": "generic",
        "title": "轻松跑", "trainingType": "file", "notes": "",
        "summary": {"duration": 2857, "distance": 5541.13},
    }])
    run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    rows = session.exec(select(TredictActivityRow)).all()
    assert len(rows) == 1
    assert rows[0].tredict_id == "T-NEW"
    assert rows[0].local_date == dt.date(2026, 11, 5)
    assert rows[0].sport_type == "running"
    assert rows[0].duration_sec == 2857


def test_existing_mirror_row_is_not_rewritten(session, settings):
    """已存在的镜像行不重写（幂等，避免与 backfill 的详情 sha 反复覆盖）。"""
    from sqlmodel import select

    from app.db.models import TredictActivityRow
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    (raw_garmin / "activities").mkdir(parents=True, exist_ok=True)

    session.add(TredictActivityRow(
        tredict_id="T-OLD", utc_datetime=dt.datetime(2026, 11, 5, 2, 0, 0),
        local_date=dt.date(2026, 11, 5), sport_type="running",
        sub_sport_type="generic", title="旧标题", duration_sec=1800,
        duration_total_sec=1800, payload_sha256="old-sha",
    ))
    session.commit()

    cloud = _NotesTredict(cloud=[{
        "id": "T-OLD", "date": "2026-11-05T10:00:00.000Z",
        "sportType": "running", "subSportType": "generic",
        "title": "新标题", "trainingType": "file", "notes": "",
        "summary": {"duration": 2857},
    }])
    run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=lambda aid: None, today=dt.date(2026, 11, 5),
    )

    row = session.exec(
        select(TredictActivityRow).where(TredictActivityRow.tredict_id == "T-OLD")
    ).one()
    assert row.title == "旧标题", "已存在镜像行不得被同步路径覆盖"
    assert row.payload_sha256 == "old-sha"
