"""Tredict 同步服务：容器内真上传的契约测试（V43）。

背景（V42 为什么失效）
----------------------
V42 在调度器里用 ``subprocess`` 调 ``scripts/daily_sync.py --profile all``，
但 ``Dockerfile.single`` 只 ``COPY backend/app`` —— 镜像里根本没有 scripts 目录。
且测试把 ``subprocess.run`` 全程 mock 掉，只断言命令字符串，从不验证脚本存在、
路径可达、或真的调用了上传。结果任务静默失败 10 天无人发现。

用户确认的 4 个 seam（Q4）
--------------------------
1. **构建契约**：被调代码在 ``app/`` 包内（一定被 COPY 进镜像）；调度器不再
   shell-out 到 scripts。
2. **真上传**：注入 fake adapter，断言 ``upload_activity`` 真被调用、条数正确、
   FIT 字节正确——而不是 mock 掉 subprocess 数字符串。
3. **路径来自 settings**：raw / FIT / audit 全部走 ``settings.raw_data_dir``。
4. **失败可观测**：成功与失败都写 audit；失败/校验差异写消息中心告警。

再加 Q2：缺 FIT 的活动会自动通过 ``fit_fetcher`` 拉取后上传。
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.config import Settings
from app.db.models import AuditEventRow, MessageRow


# ============================================================
# 测试替身与夹具
# ============================================================
def _iso_z(naive_utc: str) -> str:
    """``"2026-10-30 01:00:00"`` → ``"2026-10-30T01:00:00.000Z"``。"""
    return naive_utc.replace(" ", "T") + ".000Z"


class FakeTredict:
    """记录所有写调用；绝不联网。"""

    def __init__(self, cloud=None, health=None, upload_error: str | None = None):
        self.cloud = list(cloud or [])
        self.health = health or {"hrv": {}, "sleep": {}, "bodyvalues": []}
        self.upload_error = upload_error
        self.upload_calls: list[tuple[str, bytes, str, str]] = []
        self.post_batches: list[tuple[str, str, list]] = []

    def fetch_activity_list(self, *, extended: bool = False):
        return list(self.cloud)

    def upload_activity(self, aid, fit_bytes, *, name="", notes=""):
        self.upload_calls.append((aid, fit_bytes, name, notes))
        if self.upload_error:
            return {"ok": False, "status": 500, "exists": False, "text": self.upload_error}
        return {"ok": True, "status": 201, "exists": False, "text": ""}

    def fetch_health(self):
        return self.health

    def _post_batch(self, url, key, records):
        self.post_batches.append((url, key, list(records)))
        return {"ok": len(records), "fail": 0, "errors": []}


def _cloud_item(tid: str, gmt: str, *, duration: float = 1800.0) -> dict:
    return {
        "id": tid,
        "date": _iso_z(gmt),
        "sportType": "running",
        "subSportType": "generic",
        "summary": {"duration": duration},
    }


def _activity(aid: str, gmt: str, *, local: str | None = None, duration: int = 1800) -> dict:
    local = local or gmt
    return {
        "activityId": aid,
        "activityName": f"活动{aid}",
        "startTimeGMT": gmt,
        "startTimeLocal": local,
        "activityType": {"typeKey": "running"},
        "duration": duration,
    }


def _write_activities(raw_root: Path, items: list[dict]) -> None:
    d = raw_root / "activities"
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-10-30.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )


def _write_fit(raw_root: Path, aid: str, data: bytes = b"FITDATA") -> None:
    d = raw_root / "fit"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{aid}.fit").write_bytes(data)


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


def _fetcher(mapping: dict[str, bytes]):
    """fit_fetcher(activity_id) → bytes | None。"""
    return lambda aid: mapping.get(aid)


# ============================================================
# Seam 1：构建契约 —— 代码在 app 包内，调度器不 shell-out
# ============================================================
def test_service_module_lives_in_app_package():
    """被调代码在 ``app/`` 下 → Dockerfile 的 ``COPY backend/app`` 一定带上它。"""
    import app.services.tredict_sync_service as svc

    f = Path(svc.__file__).resolve()
    assert f.parent.name == "services"
    assert f.parent.parent.name == "app"


def test_dockerfile_copies_app_package():
    """镜像构建必须 COPY backend/app（服务层的载体）。"""
    dockerfile = Path(__file__).resolve().parents[1] / ".." / "deploy" / "Dockerfile.single"
    text = dockerfile.read_text(encoding="utf-8")
    assert "COPY backend/app ./app" in text


def test_scheduler_calls_service_not_subprocess(monkeypatch, session, settings):
    """调度器直调服务，不再 subprocess 调 scripts/daily_sync.py。"""
    from app.orchestrator import scheduler as sched

    called: dict = {}

    @contextmanager
    def _scope():
        yield session

    monkeypatch.setattr(sched, "session_scope", _scope)
    monkeypatch.setattr(sched, "get_settings", lambda: settings)

    def _spy(sess, st, **kwargs):
        called["yes"] = True
        return {"ok": True}

    monkeypatch.setattr(sched, "run_tredict_sync", _spy)
    # 断言调度器源码不再引用 scripts/daily_sync.py 或 subprocess 调脚本
    src = Path(sched.__file__).read_text(encoding="utf-8")
    assert "scripts/daily_sync.py" not in src
    assert "subprocess" not in src

    out = sched.run_tredict_sync_job()
    assert called.get("yes") is True
    assert out["ok"] is True


# ============================================================
# Seam 2：真上传 —— 注入 fake adapter，断言 upload_activity 调用
# ============================================================
def test_uploads_only_activities_missing_in_cloud(session, settings):
    """本地 3 条、云端已有 1 条 → 只上传另外 2 条，且 FIT 字节正确。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    a1, a2, a3 = "1001", "1002", "1003"
    g1, g2, g3 = "2026-10-30 01:00:00", "2026-10-30 02:00:00", "2026-10-30 03:00:00"
    _write_activities(raw_garmin, [_activity(a1, g1), _activity(a2, g2), _activity(a3, g3)])
    for a in (a1, a2, a3):
        _write_fit(raw_garmin, a, data=f"FIT-{a}".encode())

    cloud = FakeTredict(cloud=[_cloud_item("T1", g1)])  # 只有 a1 已在云端

    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=_fetcher({}), today=dt.date(2026, 10, 31),
    )

    uploaded_ids = sorted(c[0] for c in cloud.upload_calls)
    assert uploaded_ids == [a2, a3], f"应只上传缺失的 2 条，实际 {uploaded_ids}"
    assert all(c[1] in (b"FIT-1002", b"FIT-1003") for c in cloud.upload_calls)
    assert set(out["activities"]["uploaded"]) == {a2, a3}
    assert out["ok"] is True


def test_no_upload_when_cloud_complete(session, settings):
    """云端已全部覆盖 → 0 次上传（幂等，不制造重复活动）。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    g1 = "2026-10-30 01:00:00"
    _write_activities(raw_garmin, [_activity("1001", g1)])
    _write_fit(raw_garmin, "1001")

    cloud = FakeTredict(cloud=[_cloud_item("T1", g1)])
    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=_fetcher({}), today=dt.date(2026, 10, 31),
    )
    assert cloud.upload_calls == []
    assert out["ok"] is True


# ============================================================
# Seam 3：路径来自 settings（不是硬编码 ROOT/data/raw）
# ============================================================
def test_uses_settings_raw_data_dir(session, settings, tmp_path):
    """把数据放到自定义 raw_data_dir 下能读到 → 证明路径来自 settings。"""
    from app.services.tredict_sync_service import run_tredict_sync

    custom = tmp_path / "custom_volume" / "raw"
    settings.raw_data_dir = custom
    raw_garmin = custom / "garmin"
    g1 = "2026-10-30 01:00:00"
    _write_activities(raw_garmin, [_activity("1001", g1)])
    _write_fit(raw_garmin, "1001", b"CUSTOMFIT")

    cloud = FakeTredict(cloud=[])
    run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=_fetcher({}), today=dt.date(2026, 10, 31),
    )
    assert len(cloud.upload_calls) == 1
    assert cloud.upload_calls[0][1] == b"CUSTOMFIT"


def test_fit_path_is_under_settings_raw_dir(session, settings):
    """服务暴露的 FIT 目录解析函数必须基于 settings。"""
    from app.services.tredict_sync_service import fit_dir_for

    assert fit_dir_for(settings) == Path(settings.raw_data_dir) / "garmin" / "fit"


# ============================================================
# Q2：缺 FIT 自动拉取
# ============================================================
def test_downloads_missing_fit_before_upload(session, settings):
    """本地无 FIT 但 fit_fetcher 能取到 → 落盘并上传（补历史活动）。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    g1 = "2026-10-30 01:00:00"
    _write_activities(raw_garmin, [_activity("1001", g1)])  # 无 fit 文件

    cloud = FakeTredict(cloud=[])
    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=_fetcher({"1001": b"DOWNLOADED-FIT"}), today=dt.date(2026, 10, 31),
    )
    assert cloud.upload_calls[0][1] == b"DOWNLOADED-FIT"
    assert (raw_garmin / "fit" / "1001.fit").read_bytes() == b"DOWNLOADED-FIT"
    assert out["activities"]["no_fit"] == []


def test_missing_fit_is_reported_not_crash(session, settings):
    """FIT 拉不到 → 记入 no_fit，不崩整体，且不算 ok。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    _write_activities(raw_garmin, [_activity("1001", "2026-10-30 01:00:00")])
    cloud = FakeTredict(cloud=[])
    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=_fetcher({}), today=dt.date(2026, 10, 31),
    )
    assert out["activities"]["no_fit"] == ["1001"]
    assert cloud.upload_calls == []
    assert out["ok"] is False


# ============================================================
# Seam 4：失败可观测 —— audit + 告警
# ============================================================
def test_success_writes_audit(session, settings):
    """成功也写一条 audit（这次任务真的跑过，可追溯）。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    g1 = "2026-10-30 01:00:00"
    _write_activities(raw_garmin, [_activity("1001", g1)])
    _write_fit(raw_garmin, "1001")
    run_tredict_sync(
        session, settings, garmin=object(), tredict=FakeTredict(cloud=[]),
        fit_fetcher=_fetcher({}), today=dt.date(2026, 10, 31),
    )
    events = session.exec(select(AuditEventRow)).all()
    assert any(e.ref.startswith("tredictSync") for e in events)


def test_upload_failure_writes_audit_and_alert(session, settings):
    """上传失败 → audit 标记失败 + 消息中心告警（杜绝静默）。"""
    from app.services.tredict_sync_service import run_tredict_sync

    raw_garmin = Path(settings.raw_data_dir) / "garmin"
    g1 = "2026-10-30 01:00:00"
    _write_activities(raw_garmin, [_activity("1001", g1)])
    _write_fit(raw_garmin, "1001")
    cloud = FakeTredict(cloud=[], upload_error="HTTP 500 boom")

    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=cloud,
        fit_fetcher=_fetcher({}), today=dt.date(2026, 10, 31),
    )
    assert out["ok"] is False
    events = session.exec(select(AuditEventRow)).all()
    assert any(e.ref.startswith("tredictSync") and e.detail.get("ok") is False for e in events)
    msgs = session.exec(select(MessageRow)).all()
    assert len(msgs) >= 1, "上传失败必须落消息中心告警"


def test_default_fit_fetcher_extracts_fit_from_zip():
    """生产 FIT 下载：ORIGINAL 返回 ZIP，必须解出 .fit 且校验 FIT 头（Q2 命脉）。"""
    import io
    import zipfile

    from app.services.tredict_sync_service import default_fit_fetcher

    good = b"HDRXXXXX" + b".FIT" + b"\x00" * 20  # 第 8:12 字节 = b".FIT"

    def _zip(entries: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in entries.items():
                z.writestr(name, data)
        return buf.getvalue()

    class FakeClient:
        class ActivityDownloadFormat:
            ORIGINAL = "ORIGINAL"

        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def download_activity(self, aid, fmt):
            self.calls.append((aid, fmt))
            return self.payload

    class FakeGarmin:
        def __init__(self, client):
            self._c = client

        def client(self):
            return self._c

    client = FakeClient(_zip({"655296550.fit": good}))
    fetch = default_fit_fetcher(FakeGarmin(client))
    assert fetch("655296550") == good
    assert client.calls == [("655296550", "ORIGINAL")]


def test_default_fit_fetcher_rejects_bad_payload():
    """ZIP 内无 .fit / 多 .fit / FIT 头错 / 下载抛异常 → 一律返回 None，不崩。"""
    import io
    import zipfile

    from app.services.tredict_sync_service import default_fit_fetcher

    def _zip(names: list[str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                z.writestr(n, b"HDRXXXXX" + b".FIT" + b"\x00" * 20)
        return buf.getvalue()

    class FakeClient:
        class ActivityDownloadFormat:
            ORIGINAL = "ORIGINAL"

        def __init__(self, payload=None, boom=False):
            self.payload = payload
            self.boom = boom

        def download_activity(self, aid, fmt):
            if self.boom:
                raise RuntimeError("network down")
            return self.payload

    class FakeGarmin:
        def __init__(self, c):
            self._c = c

        def client(self):
            return self._c

    # 0 个 / 2 个 .fit
    assert default_fit_fetcher(FakeGarmin(FakeClient(_zip(["readme.txt"]))))("x") is None
    assert default_fit_fetcher(FakeGarmin(FakeClient(_zip(["a.fit", "b.fit"]))))("x") is None
    # 头不合法
    import zipfile as _zf

    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("a.fit", b"XXXXXXXXNOPE" + b"\x00" * 20)
    assert default_fit_fetcher(FakeGarmin(FakeClient(buf.getvalue())))("x") is None
    # 下载异常
    assert default_fit_fetcher(FakeGarmin(FakeClient(boom=True)))("x") is None


def test_exception_is_captured_not_silent(session, settings):
    """云端清单拉取抛异常 → 记录 error、写失败 audit、告警，不抛穿调度器。"""
    from app.services.tredict_sync_service import run_tredict_sync

    class Boom(FakeTredict):
        def fetch_activity_list(self, *, extended=False):
            raise RuntimeError("network down")

    out = run_tredict_sync(
        session, settings, garmin=object(), tredict=Boom(cloud=[]),
        fit_fetcher=_fetcher({}), today=dt.date(2026, 10, 31),
    )
    assert out["ok"] is False
    assert out["errors"], "异常必须被记录进 errors"
    events = session.exec(select(AuditEventRow)).all()
    assert any(e.ref.startswith("tredictSync") and e.detail.get("ok") is False for e in events)
    assert session.exec(select(MessageRow)).first() is not None
