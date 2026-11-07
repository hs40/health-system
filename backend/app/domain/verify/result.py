"""同步校验引擎（P7）——逐条判定结果。

公共入口见 ``app.domain.verify``（包门面）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.verify.states import STATE_MISMATCH, STATE_OK


# ============================================================
@dataclass(slots=True)
class CheckResult:
    entity: str
    entity_id: str
    state: str
    local_hash: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == STATE_OK


def _result(
    entity: str,
    entity_id: str,
    problems: dict[str, Any],
    local_hash: str,
) -> CheckResult:
    return CheckResult(
        entity=entity,
        entity_id=entity_id,
        state=STATE_MISMATCH if problems else STATE_OK,
        local_hash=local_hash,
        detail={"problems": problems} if problems else {},
    )


