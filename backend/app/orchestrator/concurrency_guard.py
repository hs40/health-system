"""策略切换并发守卫（阶段③ T3.4；REQ §7.8）。

**必须保证**：
1. 未验证策略不能参与依赖该策略的决策；
2. 正在使用的有效策略不会被候选版本意外覆盖；
3. 新旧版本不会混合使用；
4. 并发任务读取到的策略版本明确；
5. 版本切换具有明确的原子性边界；
6. 失败时能够识别当前实际生效的版本；
7. 验证任务失败不影响仍然有效的安全规则。

**实现策略（单用户/单进程部署）**：
- 进程内 ``RLock`` 串行化「校验 + 切换」临界区（SQLite 写串行，故足够）；
- 临界区内**先校验、后切换**：只有当候选为 verified 时才允许 activate；
- 「当前生效版本」始终从 DB 的 ``active`` 状态读取（**单一事实来源**），
  即使切换失败也不改变既有 active → 失败可识别；
- 提供 ``snapshot()`` 供并发任务在**一次读取**中拿到一致的当前版本
  （避免一个任务读到半个切换态）。
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from dataclasses import dataclass

from sqlmodel import Session

from app.db.models import StrategyVersionRow
from app.schemas.enums import StrategyStatus, StrategyType
from app.storage.strategy_store import StrategyVersionStore

# 进程内可重入锁：串行化校验/激活临界区（单进程部署足够）
_SWITCH_LOCK = threading.RLock()


class ConcurrencyConflictError(RuntimeError):
    """并发冲突：切换期间检测到版本已被改动（乐观校验失败）。"""


@dataclass(slots=True, frozen=True)
class VersionSnapshot:
    """一次读取得到的版本快照（供并发任务使用，保证读取明确）。"""

    version_id: str | None
    version_number: str | None
    status: str | None  # active 时为 active，否则 None
    payload: dict

    @property
    def is_active(self) -> bool:
        return self.status == StrategyStatus.ACTIVE.value


class ConcurrencyGuard:
    """并发守卫：提供原子切换与一致读。"""

    def __init__(self, session: Session, *, strategy_type: str = StrategyType.SEASON.value):
        self._session = session
        self._type = strategy_type
        self._store = StrategyVersionStore(session)

    # ------------------------------------------------------------
    # 一致读
    # ------------------------------------------------------------
    def snapshot(self) -> VersionSnapshot:
        """读取当前 active 版本的一致快照（无 active → 全部 None）。"""
        row = self._store.get_active(strategy_type=self._type)
        if row is None:
            return VersionSnapshot(None, None, None, {})
        return VersionSnapshot(
            version_id=row.strategy_version_id,
            version_number=row.version_number,
            status=row.status,
            payload=dict(row.payload or {}),
        )

    def active_version_id(self) -> str | None:
        """失败时用于识别「当前实际生效版本」。"""
        row = self._store.get_active(strategy_type=self._type)
        return row.strategy_version_id if row else None

    # ------------------------------------------------------------
    # 原子切换
    # ------------------------------------------------------------
    @contextlib.contextmanager
    def switch(self) -> Iterator[None]:
        """进入切换临界区（串行化）；异常不改变既有 active。"""
        with _SWITCH_LOCK:
            yield

    def activate_if_verified(
        self, version_id: str, *, expected_current_id: str | None = None
    ) -> StrategyVersionRow:
        """在临界区内原子激活候选版本。

        条件（不满足 → 抛错，且**不改变**当前 active）：
        - 候选存在且状态为 ``verified``（或已是 ``active``）；
        - 若给出 ``expected_current_id``，则当前 active 必须与之相符
          （乐观并发校验，防「读取后被他人切换」）。

        :returns: 切换后的 active 版本行。
        """
        with self.switch():
            target = self._store.get_version(version_id)
            if target is None:
                raise KeyError(f"策略版本不存在: {version_id}")

            # 1) 未验证策略不得参与决策（§7.8.1）
            if target.status not in (
                StrategyStatus.VERIFIED.value,
                StrategyStatus.ACTIVE.value,
            ):
                raise ConcurrencyConflictError(
                    f"版本 {target.version_number} 状态为 {target.status}，未通过校验，不得切换"
                )

            # 2) 乐观校验：当前 active 是否与读取时一致（§7.8.2）
            current = self._store.get_active(strategy_type=self._type)
            current_id = current.strategy_version_id if current else None
            if expected_current_id != current_id:
                raise ConcurrencyConflictError(
                    f"并发冲突：期望当前 active={expected_current_id}，实际 {current_id}"
                )

            # 3) 原子切换（store.activate 在同一事务内降级旧 active + 激活新）
            return self._store.activate(version_id)

    def assert_single_active(self) -> bool:
        """自检：同 strategy_type 至多一个 active。"""
        actives = self._store.list_versions(
            strategy_type=self._type, status=StrategyStatus.ACTIVE.value
        )
        return len(actives) <= 1
