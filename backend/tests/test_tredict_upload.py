"""P8 上传脚本测试。

上传是**唯一会改云端**的动作，所以这里重点测「不该传的时候确实不传」：

1. **闸门**：``sync_ledger`` 有校验失败就拒绝；且 ``local2tredict`` 自身失败不能反过来锁死自己。
2. **歧义不传**：被多条 Tredict 认领的 Garmin 活动必须跳过，否则云端会多出一条重复活动。
3. **只补空**：title / notes 已存在时不覆盖，除非 ``--force-meta``。
4. **端点语义**：``FILE_EXISTS`` 不是失败；4xx 不重试。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest
from sqlmodel import Session, SQLModel

from app.adapters.tredict import TredictAdapter
from app.config import Settings
from app.db import models as _models  # noqa: F401  — 注册全部模型
from app.db.models import SyncLedgerRow
from app.db.session import engine
from app.domain.matching import CONFIDENCE_AMBIGUOUS, MatchOutcome

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upload_tredict_activity.py"
_spec = importlib.util.spec_from_file_location("upload_tredict_activity", SCRIPT)
upload = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upload)


# ============================================================
# 纯函数
# ============================================================
def test_is_strength_matches_node_rule() -> None:
    assert upload.is_strength("strength_training")
    assert upload.is_strength("indoor_strength")
    assert not upload.is_strength("running")


def test_build_title_prefers_payload_name() -> None:
    act = {"payload": {"activityName": "力量训练A"}, "name": "旧名"}
    assert upload.build_title(act) == "力量训练A"


def test_build_title_falls_back_and_truncates() -> None:
    act = {"payload": {}, "name": "x" * 300}
    assert upload.build_title(act) == "x" * upload.TITLE_LIMIT


def test_ambiguous_garmin_ids_collects_every_claimant() -> None:
    outcomes = [
        MatchOutcome("t1", None, CONFIDENCE_AMBIGUOUS, {"candidates": ["100", "101"]}),
        MatchOutcome(
            "t2", None, CONFIDENCE_AMBIGUOUS, {"garmin_id": "102", "claimants": ["t2", "t3"]}
        ),
        MatchOutcome("t4", "200", "high", {}),
    ]
    assert upload.ambiguous_garmin_ids(outcomes) == {"100", "101", "102"}


def _act(aid: str, atype: str = "running") -> dict:
    return {"activity_id": aid, "activity_type": atype, "name": aid, "payload": {}}


def test_select_targets_filters_by_ids_and_strength() -> None:
    garmin = {
        "1": _act("1", "running"),
        "2": _act("2", "strength_training"),
        "3": _act("3", "running"),
    }
    every = upload.select_targets(garmin, ids=set(), only_strength=False)
    assert [a["activity_id"] for a in every] == ["1", "2", "3"]
    assert [
        a["activity_id"] for a in upload.select_targets(garmin, ids={"3"}, only_strength=False)
    ] == ["3"]
    assert [
        a["activity_id"] for a in upload.select_targets(garmin, ids=set(), only_strength=True)
    ] == ["2"]


# ============================================================
# 闸门
# ============================================================
@pytest.fixture()
def ledger_db():
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for row in s.query(SyncLedgerRow).all():
            s.delete(row)
        s.commit()
    yield
    with Session(engine) as s:
        for row in s.query(SyncLedgerRow).all():
            s.delete(row)
        s.commit()


def _ledger(entity: str, entity_id: str, direction: str, state: str) -> SyncLedgerRow:
    return SyncLedgerRow(
        entity=entity, entity_id=entity_id, direction=direction, state=state
    )


def test_gate_passes_on_empty_ledger(ledger_db) -> None:
    gate = upload.check_gate()
    assert gate["ok"] is True
    assert gate["blocked"] == 0


def test_gate_blocks_on_verify_failure(ledger_db) -> None:
    with Session(engine) as s:
        s.add(_ledger("garmin_activity", "1", "garmin2local", "mismatch"))
        s.add(_ledger("tredict_activity", "x", "tredict2local", "missing_remote"))
        s.add(_ledger("garmin_activity", "2", "garmin2local", "ok"))
        s.commit()

    gate = upload.check_gate()
    assert gate["ok"] is False
    assert gate["blocked"] == 2
    assert gate["by_state"] == {"mismatch": 1, "missing_remote": 1}


def test_gate_ignores_push_direction_failures(ledger_db) -> None:
    """上传自己失败（local2tredict）不能把自己锁死，否则一次网络抖动就要 --force。"""
    with Session(engine) as s:
        s.add(_ledger("tredict_activity", "1", "local2tredict", "missing_remote"))
        s.commit()

    assert upload.check_gate()["ok"] is True


# ============================================================
# 适配器端点
# ============================================================
def _adapter() -> TredictAdapter:
    return TredictAdapter(Settings(_env_file=None, tredict_token="test-token"))


def test_upload_activity_sends_multipart(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_request(method, url, **kw):
        calls.append({"method": method, "url": url, "kw": kw})
        return httpx.Response(200, json={"id": "abc"})

    monkeypatch.setattr(httpx, "request", fake_request)

    result = _adapter().upload_activity(
        "651169725", b"FITBYTES", name="力量训练A", notes="力量明细：深蹲"
    )
    assert result["ok"] is True
    assert result["exists"] is False

    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/activity/upload")
    assert call["kw"]["files"]["file"][0] == "651169725.fit"
    assert call["kw"]["files"]["file"][1] == b"FITBYTES"
    assert call["kw"]["data"] == {"name": "力量训练A", "notes": "力量明细：深蹲"}


def test_upload_activity_treats_file_exists_as_not_error(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx, "request", lambda *a, **k: httpx.Response(409, json={"error": "FILE_EXISTS"})
    )
    result = _adapter().upload_activity("1", b"x")
    assert result["ok"] is False
    assert result["exists"] is True


def test_upload_activity_does_not_retry_client_errors(monkeypatch) -> None:
    calls: list[int] = []

    def fake_request(*a, **k):
        calls.append(1)
        return httpx.Response(400, text="bad")

    monkeypatch.setattr(httpx, "request", fake_request)
    result = _adapter().upload_activity("1", b"x")
    assert result["ok"] is False
    assert result["status"] == 400
    assert len(calls) == 1  # 4xx 不重试


def test_update_activity_wraps_patch_in_training(monkeypatch) -> None:
    import json

    calls: list[dict] = []

    def fake_request(method, url, **kw):
        calls.append({"url": url, "kw": kw})
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "request", fake_request)
    result = _adapter().update_activity("abc", {"title": "T", "notes": "N"})
    assert result["ok"] is True
    assert calls[0]["url"].endswith("/activity/update/abc")
    assert json.loads(calls[0]["kw"]["content"]) == {"training": {"title": "T", "notes": "N"}}
