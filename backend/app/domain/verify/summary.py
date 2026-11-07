"""同步校验引擎（P7）——汇总与退出码。

公共入口见 ``app.domain.verify``（包门面）。
"""

from __future__ import annotations

from app.domain.verify.result import CheckResult
from app.domain.verify.states import (
    FAIL_STATES,
    STATE_AMBIGUOUS,
    STATE_MISMATCH,
    STATE_MISSING_LOCAL,
    STATE_MISSING_REMOTE,
    STATE_OK,
)


# ============================================================
# 汇总
# ============================================================
def summarize(results: list[CheckResult]) -> dict[str, int]:
    out = {
        STATE_OK: 0,
        STATE_MISMATCH: 0,
        STATE_MISSING_LOCAL: 0,
        STATE_MISSING_REMOTE: 0,
        STATE_AMBIGUOUS: 0,
    }
    for r in results:
        out[r.state] = out.get(r.state, 0) + 1
    return out


def exit_code(
    results: list[CheckResult],
    *,
    online_failed: bool = False,
    online_diff: int = 0,
    needs_repair: bool = False,
) -> int:
    """Q5=A + D=A：``mismatch`` / ``missing_*`` 才失败；``ambiguous`` 只警告。

    在线校验没跑成也算失败——「没验」不等于「验过了」，不能给假绿灯。
    在线校验**跑成了但有差异**同样失败：只记录不报错的巡检，等于把盲区
    从「没人看」改成「看了不说」（2026-10-15~17 缺 RMSSD 事故的教训）。

    ``needs_repair=True`` 返回 **3**（可修复）：调用方据此触发「重跑一轮上传」，
    修完再验一次；与 1（需人工处理）区分开，避免自动修复把真故障也吞掉。
    """
    if online_failed:
        return 1
    if online_diff:
        return 3 if needs_repair else 1
    return 1 if any(r.state in FAIL_STATES for r in results) else 0
