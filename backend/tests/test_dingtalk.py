"""钉钉日报推送测试（2026-10-26）。

覆盖：
- 加签算法（HMAC-SHA256 → base64 → urlencode）；
- 客户端请求体形状与错误处理（``errcode≠0`` / 未配置）；
- 消息组装（``build_daily_markdown``）；
- 推送服务（取计划、审计、失败不阻断）；
- API 端点（push / push-enabled）。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import urllib.parse

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.adapters.dingtalk import DingTalkClient, sign_dingtalk, signed_url
from app.config import Settings
from app.db.models import AuditEventRow, DailyReportRow, TrainingSessionRow
from app.schemas.domain import AcwrSnapshot, DailyReport, DataQualityReport, RiskAssessment
from app.schemas.enums import DataState, PriorityLevel, QualityGrade, QualityLevel
from app.services import dingtalk_service
from app.services.dingtalk_service import build_daily_markdown, push_daily_report


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


# ------------------------------------------------------------
# 加签与 URL
# ------------------------------------------------------------
def test_sign_matches_dingtalk_spec():
    """加签必须符合钉钉规范：base64(HMAC-SHA256(secret, f"{ts}\\n{secret}")) → urlencode。"""
    secret = "SECtest"
    ts = 1_600_000_000_000
    expected = urllib.parse.quote_plus(
        base64.b64encode(
            hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
        )
    )
    assert sign_dingtalk(secret, ts) == expected


def test_signed_url_appends_timestamp_and_sign():
    url = signed_url(
        "https://oapi.dingtalk.com/robot/send?access_token=abc", "SECx", 1_600_000_000_000
    )
    assert "access_token=abc" in url
    assert "timestamp=1600000000000" in url
    assert "sign=" in url


def test_signed_url_without_secret_is_unchanged():
    url = "https://oapi.dingtalk.com/robot/send?access_token=abc"
    assert signed_url(url, "") == url


# ------------------------------------------------------------
# 客户端
# ------------------------------------------------------------
def test_send_markdown_posts_expected_payload():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    s = _settings(dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=abc")
    data = DingTalkClient(s, client=client).send_markdown("标题", "正文")

    assert data["errcode"] == 0
    assert captured["body"]["msgtype"] == "markdown"
    assert captured["body"]["markdown"] == {"title": "标题", "text": "正文"}
    assert "access_token=abc" in captured["url"]


def test_send_markdown_raises_on_nonzero_errcode():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 310000, "errmsg": "sign not match"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    s = _settings(dingtalk_webhook="https://oapi.dingtalk.com/robot/send")
    with pytest.raises(RuntimeError, match="钉钉返回错误"):
        DingTalkClient(s, client=client).send_markdown("t", "x")


def test_send_markdown_raises_when_not_configured():
    with pytest.raises(RuntimeError, match="未配置"):
        DingTalkClient(_settings()).send_markdown("t", "x")


# ------------------------------------------------------------
# 服务层：消息组装 + 推送 + 审计
# ------------------------------------------------------------
D = dt.date(2026, 10, 26)


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
        dingtalk_webhook="https://oapi.dingtalk.com/robot/send?access_token=abc",
        app_base_url="http://base",
    )


def _report(day: dt.date = D) -> DailyReport:
    return DailyReport(
        date=day,
        quality=DataQualityReport(
            date=day, level=QualityLevel.ADJUSTMENT, grade=QualityGrade.OK
        ),
        risk=RiskAssessment(
            date=day,
            priority=PriorityLevel.REDUCE,
            data_state=DataState.FULL,
            rule_version="0.3.0",
            decided_at=dt.datetime(2026, 10, 26, 7, 30),
        ),
        acwr=AcwrSnapshot(date=day, acute=20.0, chronic=18.0, ratio=1.09, sufficient=True),
        recovery={
            "resting_heart_rate": 51,
            "hrv_last_night_avg": 60,
            "body_battery_morning": 66,
        },
        sleep={"duration_sec": int(7.8 * 3600)},
        limitations=["ACWR 仅供参考"],
    )


def _seed(session, day: dt.date = D, *, plan: str | None = "Z2 有氧 40min") -> None:
    session.add(DailyReportRow(date=day, payload=_report(day).model_dump(mode="json"), version=1))
    if plan is not None:
        session.add(
            TrainingSessionRow(
                id=f"session-{day.isoformat()}",
                date=day,
                sport="running",
                name=plan,
                payload={},
                status="draft",
            )
        )
    session.commit()


def test_build_daily_markdown_contains_key_lines():
    title, text = build_daily_markdown(
        _report(), plan_name="Z2 有氧 40min", base_url="http://base"
    )
    assert "2026-10-26" in title
    assert "P2" in text and "ok" in text
    assert "ACWR：1.09" in text
    assert "51 bpm" in text and "60 ms" in text and "7.8h" in text and "66" in text
    assert "今日安排：Z2 有氧 40min" in text
    assert "http://base/reports/daily?date=2026-10-26" in text


def test_build_daily_markdown_handles_missing_plan_and_acwr():
    rep = _report().model_copy(update={"acwr": None})
    _, text = build_daily_markdown(rep, plan_name=None, base_url="")
    assert "ACWR：不可计算" in text
    assert "今日安排：无计划 / 休息" in text
    assert "http://" not in text


def test_push_daily_report_sends_and_audits(session, settings, monkeypatch):
    _seed(session)
    monkeypatch.setattr(DingTalkClient, "send_markdown", lambda self, title, text: {"errcode": 0})
    out = push_daily_report(session, D, settings)
    assert out["ok"] is True
    rows = session.exec(
        select(AuditEventRow).where(AuditEventRow.ref == f"dingtalk:{D.isoformat()}")
    ).all()
    assert rows, "推送必须落审计"


def test_push_daily_report_failure_returns_not_ok(session, settings, monkeypatch):
    _seed(session)

    def boom(self, title, text):
        raise RuntimeError("network down")

    monkeypatch.setattr(DingTalkClient, "send_markdown", boom)
    out = push_daily_report(session, D, settings)
    assert out["ok"] is False and "network down" in out["error"]
    rows = session.exec(
        select(AuditEventRow).where(AuditEventRow.ref == f"dingtalk:{D.isoformat()}")
    ).all()
    assert rows and rows[-1].detail["ok"] is False


def test_push_daily_report_skips_when_not_configured(session):
    _seed(session)
    out = push_daily_report(session, D, _settings())
    assert out == {"ok": False, "skipped": True, "error": "未配置 DINGTALK_WEBHOOK"}


def test_push_daily_report_missing_report_raises_404(session, settings):
    from app.errors import AppError
    from app.schemas.enums import ErrorCode

    with pytest.raises(AppError) as ei:
        push_daily_report(session, D, settings)
    assert ei.value.code == ErrorCode.NOT_FOUND


# ------------------------------------------------------------
# API 端点
# ------------------------------------------------------------
def test_push_enabled_reflects_config(monkeypatch):
    from app.api import reports as api_reports

    monkeypatch.setattr(
        api_reports, "get_settings", lambda: _settings(dingtalk_webhook="https://x")
    )
    assert api_reports.daily_push_enabled() == {"enabled": True}
    monkeypatch.setattr(api_reports, "get_settings", lambda: _settings())
    assert api_reports.daily_push_enabled() == {"enabled": False}


def test_push_endpoint_delegates(session, monkeypatch):
    from app.api import reports as api_reports

    called: dict = {}

    def fake_push(s, day, settings):
        called["day"] = day
        return {"ok": True}

    monkeypatch.setattr(api_reports, "get_settings", lambda: _settings(dingtalk_webhook="https://x"))
    monkeypatch.setattr(dingtalk_service, "push_daily_report", fake_push)
    out = api_reports.daily_push(day=D, session=session)
    assert out == {"ok": True} and called["day"] == D


# ------------------------------------------------------------
# 调度器：日报生成后自动推送
# ------------------------------------------------------------
def test_run_daily_job_pushes_to_dingtalk(monkeypatch):
    import contextlib

    from app.orchestrator import scheduler
    from app.schemas.domain import StandardDaily

    class _Sync:
        status = "success"
        errors: dict = {}

    class _Rep:
        risk = None

    monkeypatch.setattr(scheduler, "get_settings", lambda: _settings(dingtalk_webhook="https://x"))
    monkeypatch.setattr(scheduler, "session_scope", lambda: contextlib.nullcontext(object()))
    # 睡眠完整（有入睡、有起床）→ 不触发「睡眠未结束」延迟门控
    monkeypatch.setattr(scheduler, "_sync_daily", lambda s, d, st, refresh=False: _Sync())
    monkeypatch.setattr(
        scheduler,
        "_get_standard",
        lambda s, d: StandardDaily(
            date=d,
            sleep_start_ts=dt.datetime.combine(d, dt.time(0, 30)),
            sleep_end_ts=dt.datetime.combine(d, dt.time(7, 0)),
        ),
    )
    # scheduler 顶层导入下游服务（ARCHITECTURE_REVIEW A.2.2），替换点为 scheduler 命名空间
    monkeypatch.setattr(scheduler, "generate_report", lambda s, d, st, sync_errors=None: _Rep())
    monkeypatch.setattr(scheduler, "analyze_daily", lambda s, d, st: {"summary": "ok"})
    monkeypatch.setattr(scheduler, "build_today_plan", lambda s, d, st: None)
    pushed: dict = {}
    monkeypatch.setattr(
        scheduler,
        "push_daily_report",
        lambda s, d, st: pushed.setdefault("day", d) or {"ok": True},
    )

    out = scheduler.run_daily_job(D)
    assert out["steps"]["plan"] == "not_generated"
    assert pushed["day"] == D, "日报生成后必须自动推送（即使当天无计划）"
