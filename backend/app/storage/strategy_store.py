"""策略版本存储（阶段③ T3.1；Plan §4.1.1 / §4.1.2）。

**不可变性是核心不变量**（§19.4.4）：正式版本禁止 UPDATE / DELETE。
- 新建版本 → ``create_version``（candidate 初始态）；
- **状态变更**（candidate→verified→active→superseded）通过受控方法完成，
  且**唯一可变字段是 status / effective_at / validation_***；
- 策略内容（payload）与版本元数据一经创建不可修改。

并发（§7.8）：激活采用「同事务内先降级旧 active、再激活新版本」，
保证任一时刻同 strategy_type 至多一个 active。
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlmodel import Session, select

from app.db.models import StrategyEventRow, StrategyVersionRow
from app.domain.strategy_versioning import next_version_number
from app.schemas.enums import StrategyChangeType, StrategyStatus, StrategyType

# 允许在状态流转中更新的字段（白名单，其余字段视为不可变）
_MUTABLE_FIELDS = {
    "status",
    "effective_at",
    "validation_status",
    "validation_result",
    "actual_effect",
}


class ImmutableVersionError(RuntimeError):
    """试图修改正式版本的不可变字段。"""


class StrategyVersionStore:
    """策略版本 + 事件存储（DB 实现）。"""

    def __init__(self, session: Session):
        self._session = session

    # ------------------------------------------------------------
    # 读
    # ------------------------------------------------------------
    def list_versions(
        self,
        *,
        strategy_type: str = StrategyType.SEASON.value,
        status: str | None = None,
    ) -> list[StrategyVersionRow]:
        stmt = select(StrategyVersionRow).where(
            StrategyVersionRow.strategy_type == strategy_type
        )
        if status:
            stmt = stmt.where(StrategyVersionRow.status == status)
        stmt = stmt.order_by(StrategyVersionRow.created_at.asc())  # type: ignore[attr-defined]
        return list(self._session.exec(stmt).all())

    def get_version(self, version_id: str) -> StrategyVersionRow | None:
        return self._session.get(StrategyVersionRow, version_id)

    def get_active(
        self, *, strategy_type: str = StrategyType.SEASON.value
    ) -> StrategyVersionRow | None:
        """当前有效版本（最多一个）。"""
        stmt = (
            select(StrategyVersionRow)
            .where(StrategyVersionRow.strategy_type == strategy_type)
            .where(StrategyVersionRow.status == StrategyStatus.ACTIVE.value)
        )
        return self._session.exec(stmt).first()

    def existing_numbers(self, *, strategy_type: str = StrategyType.SEASON.value) -> list[str]:
        return [v.version_number for v in self.list_versions(strategy_type=strategy_type)]

    # ------------------------------------------------------------
    # 写（受控）
    # ------------------------------------------------------------
    def create_version(
        self,
        *,
        payload: dict,
        change_type: StrategyChangeType,
        change_reason: str,
        created_by: str,
        parent_version_id: str | None = None,
        evidence_summary: str = "",
        expected_effect: str = "",
        applicability_conditions: dict | None = None,
        safety_constraints: dict | None = None,
        limitations: list[str] | None = None,
        status: StrategyStatus = StrategyStatus.CANDIDATE,
        strategy_type: str = StrategyType.SEASON.value,
        prefix: str = "season",
        created_at: dt.datetime | None = None,
    ) -> StrategyVersionRow:
        """新建一个版本（**不可变内容的唯一入口**）。"""
        existing = self.existing_numbers(strategy_type=strategy_type)
        version_number = next_version_number(prefix, existing)
        row = StrategyVersionRow(
            strategy_version_id=uuid.uuid4().hex,
            strategy_type=strategy_type,
            version_number=version_number,
            status=status.value,
            change_type=change_type.value,
            parent_version_id=parent_version_id,
            created_at=created_at or dt.datetime.now(),
            created_by=created_by,
            change_reason=change_reason,
            evidence_summary=evidence_summary,
            expected_effect=expected_effect,
            payload=payload,
            applicability_conditions=applicability_conditions or {},
            safety_constraints=safety_constraints or {},
            validation_status="pending",
            validation_result={},
            limitations=list(limitations or []),
            immutable=True,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def set_validation(
        self,
        version_id: str,
        *,
        status: str,
        result: dict,
    ) -> StrategyVersionRow:
        """写入校验结果（仅允许 candidate/verified 阶段；active 后不再改内容）。"""
        row = self._require(version_id)
        row.validation_status = status
        row.validation_result = result
        # 校验通过 → verified；未通过 → 停留 candidate（invalid 由上层显式标记）
        if status == "passed" and row.status == StrategyStatus.CANDIDATE.value:
            row.status = StrategyStatus.VERIFIED.value
        if status == "failed":
            row.status = StrategyStatus.INVALID.value
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def activate(self, version_id: str, *, at: dt.datetime | None = None) -> StrategyVersionRow:
        """原子激活：同事务内先把旧 active 降级为 superseded，再激活新版本（§7.8）。

        保证任一时刻同 strategy_type 至多一个 active。返回新 active 版本。
        """
        target = self._require(version_id)
        now = at or dt.datetime.now()
        # 1) 降级当前 active
        current = self.get_active(strategy_type=target.strategy_type)
        if current is not None and current.strategy_version_id != version_id:
            current.status = StrategyStatus.SUPERSEDED.value
            self._session.add(current)
        # 2) 激活目标
        target.status = StrategyStatus.ACTIVE.value
        target.effective_at = now
        self._session.add(target)
        self._session.commit()
        self._session.refresh(target)
        return target

    def mark_status(self, version_id: str, status: StrategyStatus) -> StrategyVersionRow:
        """仅改状态（如 invalid / superseded）。不改内容。"""
        row = self._require(version_id)
        row.status = status.value
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def _require(self, version_id: str) -> StrategyVersionRow:
        row = self._session.get(StrategyVersionRow, version_id)
        if row is None:
            raise KeyError(f"策略版本不存在: {version_id}")
        return row

    # ------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------
    def append_event(
        self,
        *,
        event_type: str,
        current_version_id: str | None = None,
        target_version_id: str | None = None,
        task_id: str | None = None,
        trigger_reason: str = "",
        steps: list[dict] | None = None,
        validation_result: dict | None = None,
        failure_reason: str | None = None,
        risk_state: str = "",
        notify_status: str = "pending",
        related_report_ref: str | None = None,
        follow_up_status: str = "open",
        at: dt.datetime | None = None,
    ) -> StrategyEventRow:
        row = StrategyEventRow(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            triggered_at=at or dt.datetime.now(),
            current_version_id=current_version_id,
            target_version_id=target_version_id,
            task_id=task_id,
            trigger_reason=trigger_reason,
            steps=list(steps or []),
            validation_result=validation_result or {},
            failure_reason=failure_reason,
            risk_state=risk_state,
            notify_status=notify_status,
            related_report_ref=related_report_ref,
            follow_up_status=follow_up_status,
        )
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return row

    def list_events(self, *, event_type: str | None = None) -> list[StrategyEventRow]:
        stmt = select(StrategyEventRow)
        if event_type:
            stmt = stmt.where(StrategyEventRow.event_type == event_type)
        stmt = stmt.order_by(StrategyEventRow.triggered_at.asc())  # type: ignore[attr-defined]
        return list(self._session.exec(stmt).all())

    def update_event_notify(self, event_id: str, notify_status: str) -> None:
        row = self._session.get(StrategyEventRow, event_id)
        if row is None:
            raise KeyError(f"策略事件不存在: {event_id}")
        row.notify_status = notify_status
        self._session.add(row)
        self._session.commit()
