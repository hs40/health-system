"""故障注入测试（TD.2）：外部超时/未知、数据缺失、文件损坏、LLM 失败。

对应需求：REQ-TASK-002（重试上限/退避）、REQ-TASK-004（结果未知不盲目重发）、
REQ-DATA-001（原始数据只读）、REQ-RISK-002（模型输出必须严格校验）。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.adapters.garmin import DATASETS, GarminAdapter
from app.adapters.llm import LlmAdapter, _parse_json
from app.config import Settings
from app.db.models import TrainingSessionRow
from app.errors import AppError
from app.schemas.domain import TrainingSession
from app.schemas.enums import ErrorCode, SessionStatus, Sport
from app.services import plan_service

D = dt.date(2026, 10, 14)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def settings(tmp_path):
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
    )


def _seed_session(session: Session) -> str:
    ts = TrainingSession(
        id="session-2026-10-14",
        date=D,
        sport=Sport.RUNNING,
        name="轻松跑",
        main=[{"name": "主项", "duration_sec": 1800, "hr_zone": 2}],
        status=SessionStatus.DRAFT,
    )
    session.add(
        TrainingSessionRow(
            id=ts.id,
            date=ts.date,
            sport=ts.sport.value,
            name=ts.name,
            payload=ts.model_dump(mode="json"),
            status=ts.status.value,
        )
    )
    session.commit()
    return ts.id


# ============================================================
# REQ-TASK-004：上传超时 → unknown → 不盲目重发
# ============================================================
def test_upload_timeout_marks_unknown_and_does_not_resend(session, settings, monkeypatch):
    sid = _seed_session(session)
    calls = {"n": 0}

    def _timeout(self, payload):  # noqa: ANN001
        calls["n"] += 1
        raise AppError(ErrorCode.EXTERNAL_TIMEOUT, "请求超时")

    monkeypatch.setattr(GarminAdapter, "upload_workout", _timeout)

    first = plan_service.upload_session(session, sid, settings)
    assert first.status == "unknown"
    assert calls["n"] == 1

    # 第二次调用：状态为 unknown → 必须回读，不得再次上传
    second = plan_service.upload_session(session, sid, settings)
    assert second.status == "unknown"
    assert calls["n"] == 1, "unknown 状态下不得盲目重发"
    assert "回读" in (second.message or "")


def test_non_guarded_error_is_retryable(session, settings, monkeypatch):
    """非超时类失败（如参数错误）可安全重试（REQ-TASK-002）。"""
    sid = _seed_session(session)
    calls = {"n": 0}

    def _fail(self, payload):  # noqa: ANN001
        calls["n"] += 1
        raise AppError(ErrorCode.VALIDATION, "参数非法")

    monkeypatch.setattr(GarminAdapter, "upload_workout", _fail)
    first = plan_service.upload_session(session, sid, settings)
    assert first.status == "failed"

    second = plan_service.upload_session(session, sid, settings)
    assert second.status == "failed"
    assert calls["n"] == 2, "failed 状态应允许重试"


# ============================================================
# REQ-TASK-004：回读核对
# ============================================================
def test_reconcile_found_updates_session(session, settings, monkeypatch):
    sid = _seed_session(session)
    # reconcile_unknown 已拆到 plan_upload（ARCHITECTURE_REVIEW A.1.2），
    # 它内部调用本模块的 scheduled_on，替换点必须在该模块命名空间。
    from app.services import plan_upload

    monkeypatch.setattr(
        plan_upload,
        "scheduled_on",
        lambda _s, _d: [{"itemType": "workout", "workoutId": 123, "id": 456}],
    )
    res = plan_service.reconcile_unknown(session, sid, settings)
    assert res.status == "scheduled"
    row = session.get(TrainingSessionRow, sid)
    assert row is not None and row.garmin_workout_id == 123


def test_reconcile_not_found_allows_resend(session, settings, monkeypatch):
    sid = _seed_session(session)
    from app.services import plan_upload

    monkeypatch.setattr(plan_upload, "scheduled_on", lambda _s, _d: [])
    res = plan_service.reconcile_unknown(session, sid, settings)
    assert res.status == "failed"
    assert "可安全重传" in (res.message or "")


# ============================================================
# 数据缺失 / 同步部分失败
# ============================================================
def _patch_fetch(monkeypatch, handler):
    monkeypatch.setattr(GarminAdapter, "client", lambda self: SimpleNamespace())
    monkeypatch.setattr(GarminAdapter, "_fetch", handler)


def test_sync_partial_when_one_dataset_missing(settings, monkeypatch):
    def _fetch(self, g, dataset, day):  # noqa: ANN001
        return {} if dataset == "spo2" else {"dataset": dataset}

    _patch_fetch(monkeypatch, _fetch)
    res = GarminAdapter(settings).sync_day(D)
    assert res.status == "partial"
    assert res.missing == ["spo2"]
    assert "spo2" not in res.written


def test_sync_failed_when_all_datasets_error(settings, monkeypatch):
    def _fetch(self, g, dataset, day):  # noqa: ANN001
        raise RuntimeError("网络中断")

    _patch_fetch(monkeypatch, _fetch)
    res = GarminAdapter(settings).sync_day(D)
    assert res.status == "failed"
    assert len(res.errors) == len(DATASETS)


# ============================================================
# 文件损坏 → 结构化错误（不静默、不自动修复）
# ============================================================
def test_corrupted_raw_file_raises_data_corrupt(settings):
    adapter = GarminAdapter(settings)
    p = adapter._raw_path("daily_summary", D)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(AppError) as ei:
        adapter.read_raw("daily_summary", D)
    assert ei.value.code == ErrorCode.DATA_CORRUPT
    assert ei.value.http_status == 500


def test_standardize_propagates_corruption(settings):
    adapter = GarminAdapter(settings)
    p = adapter._raw_path("sleep", D)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("]]broken[[", encoding="utf-8")

    with pytest.raises(AppError) as ei:
        adapter.standardize(D)
    assert ei.value.code == ErrorCode.DATA_CORRUPT


# ============================================================
# LLM 故障与输出校验
# ============================================================
class _FakeChat:
    def __init__(self, content: str | None = None, exc: Exception | None = None) -> None:
        self._content = content
        self._exc = exc

    def create(self, **_kw):  # noqa: ANN003
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._content))]
        )


def _fake_client(monkeypatch, content=None, exc=None):  # noqa: ANN001
    fake = SimpleNamespace(chat=SimpleNamespace(completions=_FakeChat(content, exc)))
    monkeypatch.setattr(LlmAdapter, "_get_client", lambda self: fake)


def test_llm_transport_error_is_external_unknown(monkeypatch):
    _fake_client(monkeypatch, exc=RuntimeError("connection reset"))
    with pytest.raises(AppError) as ei:
        LlmAdapter(Settings(_env_file=None)).chat_json([{"role": "user", "content": "hi"}])  # type: ignore[call-arg]
    assert ei.value.code == ErrorCode.EXTERNAL_UNKNOWN


def test_llm_non_json_output_rejected(monkeypatch):
    _fake_client(monkeypatch, content="抱歉，我无法完成该请求。")
    with pytest.raises(AppError) as ei:
        LlmAdapter(Settings(_env_file=None)).chat_json([{"role": "user", "content": "hi"}])  # type: ignore[call-arg]
    assert ei.value.code == ErrorCode.LLM_INVALID_OUTPUT


def test_llm_missing_required_key_rejected(monkeypatch):
    _fake_client(monkeypatch, content='{"summary": "ok"}')
    with pytest.raises(AppError) as ei:
        LlmAdapter(Settings(_env_file=None)).chat_json(  # type: ignore[call-arg]
            [{"role": "user", "content": "hi"}], required_keys=("summary", "over_under")
        )
    assert ei.value.code == ErrorCode.LLM_INVALID_OUTPUT


def test_llm_fenced_json_is_parsed(monkeypatch):
    _fake_client(monkeypatch, content='```json\n{"summary": "ok", "over_under": "无"}\n```')
    data = LlmAdapter(Settings(_env_file=None)).chat_json(  # type: ignore[call-arg]
        [{"role": "user", "content": "hi"}], required_keys=("summary", "over_under")
    )
    assert data["summary"] == "ok"


def test_parse_json_extracts_embedded_object():
    assert _parse_json('前言 {"a": 1} 后记') == {"a": 1}
    assert _parse_json("完全不是 JSON") is None
