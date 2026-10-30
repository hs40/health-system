"""隐私与授权验证（TD.3 / REQ-PRIV-001 / REQ-PRIV-002）。

验证要点：
- 外发内容**仅**聚合/派生指标 + 原始摘要，绝不包含原始逐秒/逐点明细；
- 每次外发都有审计记录（目的 / 数据范围 / 模型 / 结果）；
- 数据范围声明（``DEFAULT_SCOPE``）随审计落库，便于事后核查。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.adapters.llm import DEFAULT_SCOPE, LlmAdapter
from app.config import Settings
from app.db.models import AuditEventRow
from app.errors import AppError
from app.schemas.enums import ErrorCode
from app.services.coach_service import _report_payload, analyze_daily
from app.services.report_service import generate_daily

D = dt.date(2026, 10, 14)

# 允许外发的聚合字段白名单（REQ-PRIV-002）
ALLOWED_KEYS = {
    "date",
    "priority",
    "data_state",
    "risk_rules",
    "missing_inputs",
    "data_quality",
    "quality_missing",
    "resting_heart_rate",
    "hrv_last_night_avg",
    # 2026-10-22：外发改用**醒时电量**，残值 body_battery_wake 不再发给模型
    # （否则模型会把傍晚残值当成「晨起电量」写进早间建议）。
    "body_battery_morning",
    "sleep_score",
    "sleep_duration_sec",
    "acwr",
    "load_combined",
    "limitations",
}

# 原始明细类字段：一旦出现即视为隐私违规
FORBIDDEN_TOKENS = (
    "heartRateValues",
    "bodyBatteryValues",
    "spO2Values",
    "stressValues",
    "respirationValues",
    "sleepLevels",
    "sleepMovement",
    "sleepStartTimestampGMT",
    "activityId",
    "rawData",
)


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


def test_report_payload_is_aggregate_only(session, settings):
    report = generate_daily(session, D, settings)
    payload = _report_payload(report)
    assert set(payload) <= ALLOWED_KEYS, f"外发字段越界：{set(payload) - ALLOWED_KEYS}"


def test_llm_outbound_content_has_no_raw_detail(session, settings, monkeypatch):
    """捕获实际外发的消息体，断言不含原始明细字段。"""
    generate_daily(session, D, settings)
    captured: dict = {}

    def fake_chat_json(self, messages, **_kw):  # noqa: ANN001
        captured["messages"] = messages
        return {"summary": "s", "over_under": "o", "suggestions": [], "cautions": []}

    monkeypatch.setattr(LlmAdapter, "chat_json", fake_chat_json)
    analyze_daily(session, D, settings)

    user_content = captured["messages"][1]["content"]
    start, end = user_content.index("{"), user_content.rindex("}")
    sent = json.loads(user_content[start : end + 1])

    assert set(sent) <= ALLOWED_KEYS
    for token in FORBIDDEN_TOKENS:
        assert token not in user_content, f"外发内容出现原始明细字段：{token}"


def test_each_outbound_call_is_audited(session, settings, monkeypatch):
    """REQ-PRIV-001：每次外发记录 目的 / 范围 / 模型 / 结果。"""
    generate_daily(session, D, settings)

    monkeypatch.setattr(
        LlmAdapter,
        "chat_json",
        lambda self, messages, **_kw: {  # noqa: ANN001
            "summary": "s",
            "over_under": "o",
            "suggestions": [],
            "cautions": [],
        },
    )
    analyze_daily(session, D, settings)

    rows = session.exec(
        select(AuditEventRow).where(AuditEventRow.category == "modelChange")
    ).all()
    events = [r for r in rows if r.detail.get("purpose") == "daily_analysis"]
    assert events, "缺少外发审计记录"

    detail = events[-1].detail
    assert detail["scope"] == DEFAULT_SCOPE
    assert detail["model"]
    assert detail["result"] == "ok"


def test_audit_jsonl_archive_written(session, settings, monkeypatch):
    generate_daily(session, D, settings)
    monkeypatch.setattr(
        LlmAdapter,
        "chat_json",
        lambda self, messages, **_kw: {  # noqa: ANN001
            "summary": "s",
            "over_under": "o",
            "suggestions": [],
            "cautions": [],
        },
    )
    analyze_daily(session, D, settings)

    # JSONL 按**记录时刻**的日期命名（audit_store.record 用 now.date()），
    # 与报告日期 D 未必同日 → 不硬编码文件名，取目录内全部归档再校验内容。
    archives = list(settings.audit_dir.glob("*.jsonl"))
    assert archives, "缺少审计 JSONL 归档"
    text = "".join(p.read_text(encoding="utf-8") for p in archives)
    assert "daily_analysis" in text
    assert DEFAULT_SCOPE in text


def test_default_scope_declares_aggregate_only():
    assert DEFAULT_SCOPE == "aggregated_daily_metrics"


def test_analyze_requires_existing_report(session, settings):
    with pytest.raises(AppError) as ei:
        analyze_daily(session, D, settings)
    assert ei.value.code == ErrorCode.NOT_FOUND
