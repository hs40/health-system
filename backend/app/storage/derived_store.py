"""派生层读写（REQ-DATA-002）：基线 / 负荷 / ACWR 的持久化。

派生数据可由标准化层重算，故允许覆盖（与原始层「只写一次」不同）。
"""

from __future__ import annotations

import datetime as dt

from sqlmodel import Session, select

from app.db.models import AcwrRow, BaselineRow, TrainingLoadRow
from app.domain.algo_version import ACWR_VERSION
from app.domain.baseline import with_thresholds
from app.schemas.domain import AcwrSnapshot, Baseline, TrainingLoad


# ---------------------------- 基线 ----------------------------
def upsert_baseline(session: Session, b: Baseline) -> None:
    row = session.exec(
        select(BaselineRow).where(
            BaselineRow.metric == b.metric, BaselineRow.version == b.version
        )
    ).first()
    data = {
        "window_days": b.window_days,
        "mean": b.mean,
        "std": b.std,
        "p10": b.p10,
        "p90": b.p90,
        "mature": b.mature,
        "computed_at": b.computed_at,
    }
    if row is None:
        session.add(BaselineRow(metric=b.metric, version=b.version, **data))
    else:
        for k, v in data.items():
            setattr(row, k, v)
        session.add(row)
    session.commit()


def upsert_baselines(session: Session, baselines: dict[str, Baseline]) -> None:
    for b in baselines.values():
        upsert_baseline(session, b)


def latest_baseline(session: Session, metric: str) -> Baseline | None:
    row = session.exec(
        select(BaselineRow)
        .where(BaselineRow.metric == metric)
        .order_by(BaselineRow.version.desc())  # type: ignore[attr-defined]
    ).first()
    if row is None:
        return None
    return with_thresholds(
        row.metric,
        Baseline(
            metric=row.metric,
            version=row.version,
            window_days=row.window_days,
            mean=row.mean,
            std=row.std,
            p10=row.p10,
            p90=row.p90,
            mature=row.mature,
            computed_at=row.computed_at,
        ),
    )


def all_latest_baselines(session: Session) -> dict[str, Baseline]:
    out: dict[str, Baseline] = {}
    for metric in ("hrv_last_night_avg", "resting_heart_rate", "sleep_duration_sec"):
        b = latest_baseline(session, metric)
        if b is not None:
            out[metric] = b
    return out


# ---------------------------- 负荷 / ACWR ----------------------------
def upsert_load(session: Session, load: TrainingLoad) -> None:
    row = session.exec(
        select(TrainingLoadRow).where(TrainingLoadRow.date == load.date)
    ).first()
    data = {
        "by_sport": {
            "items": load.by_sport,
            "not_computable": load.not_computable,
            "unverified_sports": load.unverified_sports,
        },
        "combined": load.combined,
        "method": load.method,
    }
    if row is None:
        session.add(TrainingLoadRow(date=load.date, **data))
    else:
        for k, v in data.items():
            setattr(row, k, v)
        session.add(row)
    session.commit()


def upsert_acwr(session: Session, snap: AcwrSnapshot) -> None:
    row = session.exec(select(AcwrRow).where(AcwrRow.date == snap.date)).first()
    # G-16：新写入盖当前口径版本；历史行不主动回填（NULL = legacy）
    data = {
        "acute": snap.acute,
        "chronic": snap.chronic,
        "ratio": snap.ratio,
        "acwr_version": ACWR_VERSION,
    }
    if row is None:
        session.add(AcwrRow(date=snap.date, **data))
    else:
        for k, v in data.items():
            setattr(row, k, v)
        session.add(row)
    session.commit()


def load_acwr(session: Session, day: dt.date) -> AcwrSnapshot | None:
    """取某日的 ACWR 快照；无记录返回 ``None``（不伪造 0）。"""
    row = session.exec(select(AcwrRow).where(AcwrRow.date == day)).first()
    if row is None:
        return None
    return AcwrSnapshot(date=row.date, acute=row.acute, chronic=row.chronic, ratio=row.ratio)


def load_range(session: Session, start: dt.date, end: dt.date) -> list[TrainingLoad]:
    rows = session.exec(
        select(TrainingLoadRow)
        .where(TrainingLoadRow.date >= start, TrainingLoadRow.date <= end)
        .order_by(TrainingLoadRow.date)  # type: ignore[attr-defined]
    ).all()
    return [
        TrainingLoad(
            date=r.date,
            by_sport=(r.by_sport or {}).get("items", {}),
            combined=r.combined,
            method=r.method,
            # 需求 4.5：不可计算活动必须随负荷一并读出，重算写回时不得丢失
            not_computable=(r.by_sport or {}).get("not_computable", []),
            # 需求 6.3：未验证类型随负荷读出（旧行缺该键 → 空 dict，不伪造）
            unverified_sports=(r.by_sport or {}).get("unverified_sports", {}),
        )
        for r in rows
    ]
