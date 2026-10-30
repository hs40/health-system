"""策略 / 消息 / 故障 接口端到端测试（阶段③ T3.12）。

通过 FastAPI TestClient + 依赖覆盖（内存库）验证 HTTP 层：
- 策略接口：current / versions / optimize / validate / rollback / events
- 消息接口：list / read / resolve / delete
- 故障接口：list / report / precheck / confirm
- 错误码 → HTTP 状态映射（AppError 处理器）

同时充当 §19.4~§19.7 的**接口层**验收补充。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.config import Settings, get_settings
from app.db import models  # noqa: F401  — 注册全部模型，确保 create_all 建齐表
from app.db.models import (  # noqa: F401
    BaselineRow,
    DailyReportRow,
    StandardDailyRow,
)
from app.db.session import get_session
from app.main import app


def _settings() -> Settings:
    return Settings(_env_file=None, validate_min_replay_match=0.80)


@pytest.fixture
def client() -> Iterator[TestClient]:
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # 让所有连接共享同一内存库
    )
    SQLModel.metadata.create_all(eng)

    def _override_session() -> Iterator[Session]:
        with Session(eng) as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_settings] = _settings
    with TestClient(app) as c:
        c._engine = eng  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()


def _seed(engine) -> None:  # noqa: ANN001
    with Session(engine) as s:
        s.add(
            BaselineRow(
                metric="hrv_last_night_avg", version=1, window_days=28,
                mean=60, std=5, p10=52, p90=68, mature=True,
            )
        )
        s.add(
            BaselineRow(
                metric="resting_heart_rate", version=1, window_days=28,
                mean=52, std=3, p10=48, p90=56, mature=True,
            )
        )
        s.add(
            BaselineRow(
                metric="sleep_duration_sec", version=1, window_days=28,
                mean=25200, std=3600, p10=18000, p90=32400, mature=True,
            )
        )
        base = dt.date(2026, 1, 1)
        for i in range(30):
            day = base + dt.timedelta(days=i)
            s.add(
                StandardDailyRow(
                    date=day, resting_heart_rate=52, sleep_duration_sec=25200,
                    hrv_last_night_avg=60,
                )
            )
            s.add(
                DailyReportRow(
                    date=day, payload={"risk": {"level": "normal", "triggered_rules": []}}
                )
            )
        s.commit()


# ---------------------------- 策略接口 ----------------------------
def test_current_initializes(client):
    r = client.get("/api/strategy/current")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "active"
    assert body["version_number"] == "season-1.0.0"


def test_versions_and_get(client):
    client.get("/api/strategy/current")
    r = client.get("/api/strategy/versions")
    assert r.status_code == 200
    versions = r.json()["versions"]
    assert len(versions) == 1
    vid = versions[0]["strategy_version_id"]
    r2 = client.get(f"/api/strategy/versions/{vid}")
    assert r2.status_code == 200
    assert r2.json()["strategy_version_id"] == vid


def test_get_version_not_found(client):
    r = client.get("/api/strategy/versions/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "E_NOT_FOUND"


def test_optimize_creates_candidate(client):
    _seed(client._engine)  # type: ignore[attr-defined]
    client.get("/api/strategy/current")
    r = client.post(
        "/api/strategy/optimize",
        json={"payload": {"season": "summer", "focus": "高温适应"}, "change_reason": "接口优化"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["version_number"] == "season-1.0.1"
    assert body["status"] == "verified"


def test_validate_endpoint(client):
    client.get("/api/strategy/current")
    r = client.get("/api/strategy/versions")
    vid = r.json()["versions"][0]["strategy_version_id"]
    r2 = client.post(f"/api/strategy/{vid}/validate")
    assert r2.status_code == 200
    # 无历史 → 校验失败 → invalid
    assert r2.json()["status"] == "invalid"


def test_events_endpoint(client):
    client.get("/api/strategy/current")  # 触发初始化 → 产生事件
    r = client.get("/api/strategy/events")
    assert r.status_code == 200
    assert len(r.json()["events"]) >= 1


# ---------------------------- 消息接口 ----------------------------
def test_messages_empty(client):
    r = client.get("/api/messages")
    assert r.status_code == 200
    assert r.json() == {"items": [], "unread_count": 0}


def test_restore_generates_message_then_interact(client):
    """手动恢复 → 产生消息 → 读取/解决/删除全流程。"""
    _seed(client._engine)  # type: ignore[attr-defined]
    client.get("/api/strategy/current")
    versions = client.get("/api/strategy/versions").json()["versions"]
    vid = versions[0]["strategy_version_id"]

    r = client.post(f"/api/strategy/{vid}/restore", json={"reason": "手动恢复"})
    assert r.status_code == 200

    msgs = client.get("/api/messages").json()
    assert msgs["unread_count"] == 1
    mid = msgs["items"][0]["message_id"]
    assert msgs["items"][0]["is_read"] is False

    # 标记已读
    r2 = client.post(f"/api/messages/{mid}/read")
    assert r2.status_code == 200 and r2.json()["is_read"] is True

    # 已读 ≠ 已解决
    r3 = client.post(f"/api/messages/{mid}/resolve")
    assert r3.status_code == 200 and r3.json()["is_resolved"] is True

    # 清理
    r4 = client.request("DELETE", "/api/messages", json={"ids": [mid]})
    assert r4.status_code == 200 and r4.json()["deleted"] == 1
    assert client.get("/api/messages").json()["items"] == []


def test_mark_read_not_found(client):
    r = client.post("/api/messages/nope/read")
    assert r.status_code == 404


# ---------------------------- 恢复前检查 / 差异比较 / 筛选 ----------------------------
def test_restore_refused_without_history(client):
    """恢复前检查不通过（无历史回放）→ 拒绝恢复并给出冲突说明（§9.2）。"""
    client.get("/api/strategy/current")
    versions = client.get("/api/strategy/versions").json()["versions"]
    vid = versions[0]["strategy_version_id"]
    r = client.post(f"/api/strategy/{vid}/restore", json={"reason": "尝试恢复"})
    assert r.status_code == 409
    assert r.json()["error"]["detail"]["conflicts"]


def test_diff_three_layers(client):
    """三层差异比较（§9.6）：摘要 / 关键差异 / 完整差异（含未变化项）。"""
    _seed(client._engine)  # type: ignore[attr-defined]
    client.get("/api/strategy/current")
    r = client.post("/api/strategy/optimize", json={"change_reason": "差异比较用例"})
    assert r.status_code == 200
    versions = client.get("/api/strategy/versions").json()["versions"]
    v1, v2 = versions[0], versions[-1]
    d = client.get(
        f"/api/strategy/diff?from_id={v1['strategy_version_id']}&to_id={v2['strategy_version_id']}"
    )
    assert d.status_code == 200
    body = d.json()
    assert "summary" in body and "key_changes" in body and "full_diff" in body
    # 第三层包含未变化项
    assert "包含未发生变化的项目" in body["full_diff"]["note"]
    assert body["full_diff"]["payload"]


def test_versions_filter_by_change_type(client):
    """按变化类型筛选（§9.7）。"""
    client.get("/api/strategy/current")
    all_v = client.get("/api/strategy/versions").json()["versions"]
    assert len(all_v) == 1
    r = client.get("/api/strategy/versions?change_type=autoRollback")
    assert r.status_code == 200
    assert r.json()["versions"] == []


# ---------------------------- 故障接口 ----------------------------
def test_fault_report_and_precheck_and_confirm(client):
    _seed(client._engine)  # type: ignore[attr-defined]
    client.get("/api/strategy/current")

    r = client.post(
        "/api/ops/faults",
        json={"kind": "safety", "impact_scope": "安全策略", "fix_measures": "已修复"},
    )
    assert r.status_code == 200
    fault = r.json()
    assert fault["severity"] == "major"
    fid = fault["fault_id"]

    r2 = client.get("/api/ops/faults")
    assert r2.status_code == 200 and len(r2.json()["faults"]) == 1

    r3 = client.get(f"/api/ops/faults/{fid}/precheck")
    assert r3.status_code == 200
    assert r3.json()["fault"]["precheck_status"] == "passed"

    r4 = client.post(f"/api/ops/faults/{fid}/confirm")
    assert r4.status_code == 200
    assert r4.json()["user_confirmed"] is True


def test_confirm_without_precheck_returns_error(client):
    r = client.post("/api/ops/faults", json={"kind": "safety"})
    fid = r.json()["fault_id"]
    r2 = client.post(f"/api/ops/faults/{fid}/confirm")
    assert r2.status_code == 400  # E_VALIDATION


def test_fault_precheck_not_found(client):
    r = client.get("/api/ops/faults/nope/precheck")
    assert r.status_code == 404
