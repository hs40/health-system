"""同步失败韧性（P1）：client 初始化失败不得让整条日流水线静默崩溃。

背景（真实故障 2026-10-27）：
NAS 容器的出站流量被群晖防火墙拦截，所有域名解析失败。
``GarminAdapter.client()`` 抛 ``AppError``，而它位于 ``sync_day()`` 的**数据集循环之前**，
异常直接穿透到 ``run_daily()``，后果是：

  - ``audit()`` 未被调用（它写在 ``sync_day()`` 之后）→ 审计零记录
  - ``raw/`` 无任何落盘
  - 日报未生成
  - 只有 APScheduler 向 stderr 打一行 ERROR

用户侧完全无感知，次日才发现「昨天没有数据」。

修复契约（本文件锁定）：
1. ``client()`` 失败必须被 ``sync_day()`` 吸收，转换为**每个数据集**的 ``errors``；
2. ``result.status`` 必须为 ``"failed"``（``errors`` 非空且 ``written`` 为空）；
3. ``run_daily()`` 必须继续走完并生成日报（复用既有的 ``sync_errors`` 披露机制），
   且留下 ``DATA_SOURCE`` 审计记录。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.adapters.garmin import DATASETS, GarminAdapter
from app.config import Settings
from app.db.models import AuditEventRow
from app.errors import AppError, ErrorCode
from app.schemas.domain import StandardDaily
from app.schemas.enums import AuditCategory

DAY = dt.date(2026, 10, 27)
LOGIN_FAILURE = "Garmin 登录失败：Failed to retrieve social profile"


def _settings(tmp_path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        raw_data_dir=tmp_path / "raw",
        db_path=tmp_path / "t.db",
        audit_dir=tmp_path / "audit",
        garmin_tokenstore=tmp_path / "garmin_tokens",
    )


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _patch_client_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 GarminAdapter.client() 必然抛 AppError（模拟 DNS/网络层失败）。"""

    def _raise(self) -> object:  # noqa: ANN001
        raise AppError(ErrorCode.EXTERNAL_UNKNOWN, LOGIN_FAILURE)

    monkeypatch.setattr(GarminAdapter, "client", _raise)


# ---------------------------------------------------------------------------
# 契约 1 + 2：sync_day 必须吸收 client 异常
# ---------------------------------------------------------------------------


def test_sync_day_absorbs_client_failure(monkeypatch, tmp_path) -> None:
    """client() 抛异常时，sync_day 不得向外抛，而应返回 failed 结果。"""
    _patch_client_failure(monkeypatch)
    adapter = GarminAdapter(_settings(tmp_path))

    result = adapter.sync_day(DAY)  # 不得抛异常

    assert result.status == "failed"
    assert result.written == []
    assert result.fetched == []


def test_sync_day_attributes_failure_to_every_dataset(monkeypatch, tmp_path) -> None:
    """错误必须落到**每个**数据集上，而不是只记一个笼统的 key。

    原因：下游披露只看 ``errors['activities']``（见 test_activity_sync_disclosure.py），
    若只写一个 ``__all__`` 之类的聚合键，活动同步失败将不会被披露。
    """
    _patch_client_failure(monkeypatch)
    adapter = GarminAdapter(_settings(tmp_path))

    result = adapter.sync_day(DAY)

    assert set(result.errors) == set(DATASETS)
    assert all(LOGIN_FAILURE in msg for msg in result.errors.values())


# ---------------------------------------------------------------------------
# 契约 3：run_daily 必须继续产出日报与审计
# ---------------------------------------------------------------------------


def test_run_daily_generates_report_and_audit_when_client_fails(
    monkeypatch, session, tmp_path
) -> None:
    """同步全失败时，日报仍应生成，且审计必须留下 failed 记录。

    这是本次故障「静默一整天」的直接修复点。
    """
    from app.services.daily_pipeline import run_daily

    _patch_client_failure(monkeypatch)
    # standardize 依赖 raw 文件；同步已失败，显式给定标准化数据避免二次失败
    monkeypatch.setattr(
        GarminAdapter,
        "standardize",
        lambda self, day: StandardDaily(  # noqa: ARG005
            date=day,
            resting_heart_rate=50,
            max_heart_rate=180,
            hrv_last_night_avg=60.0,
            sleep_duration_sec=8 * 3600,
            steps=8000,
            body_battery_wake=70,
        ),
    )
    settings = _settings(tmp_path)

    sync, report = run_daily(session, DAY, settings)  # 不得抛异常

    assert sync.status == "failed"
    assert report is not None
    assert report.date == DAY

    rows = session.exec(
        select(AuditEventRow).where(
            AuditEventRow.category == AuditCategory.DATA_SOURCE.value,
            AuditEventRow.ref == DAY.isoformat(),
        )
    ).all()
    assert rows, "同步失败必须留下 DATA_SOURCE 审计记录"
    detail = rows[-1].detail or {}
    assert detail.get("status") == "failed"
    assert detail.get("errors"), "审计必须携带具体错误，便于事后定位"


def test_run_daily_discloses_activity_sync_failure(monkeypatch, session, tmp_path) -> None:
    """同步失败必须出现在日报 limitations 中（复用既有披露机制）。"""
    from app.services.daily_pipeline import run_daily

    _patch_client_failure(monkeypatch)
    monkeypatch.setattr(
        GarminAdapter,
        "standardize",
        lambda self, day: StandardDaily(  # noqa: ARG005
            date=day,
            resting_heart_rate=50,
            max_heart_rate=180,
            hrv_last_night_avg=60.0,
            sleep_duration_sec=8 * 3600,
            steps=8000,
            body_battery_wake=70,
        ),
    )
    settings = _settings(tmp_path)

    _, report = run_daily(session, DAY, settings)

    assert any("活动数据同步失败" in item for item in report.limitations)
