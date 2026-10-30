"""策略版本号与状态流转的纯逻辑（阶段③ T3.1；Plan §4.1.1 / §17.1）。

**职责**：把「版本号如何递增」「状态如何流转」做成**纯函数**，与 IO 分离，
便于单测与历史回放。存储层（``storage/strategy_store.py``）只负责读写。

版本号规则：``season-<major>.<minor>.<patch>``。
- AI 优化 / 自动回退 / 手动恢复 → **每次产生新版本**（§19.4.1~19.4.3）；
- 稳定递增：patch 位 +1（同一 minor 内演进），不跳号、不复用已用版本号。
"""

from __future__ import annotations

import re

from app.schemas.enums import StrategyStatus

_VERSION_RE = re.compile(r"^(?P<prefix>[a-z]+)-(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


def parse_version(version_number: str) -> tuple[str, int, int, int] | None:
    """解析版本号；格式非法返回 ``None``（不猜测）。"""
    m = _VERSION_RE.match(version_number.strip())
    if not m:
        return None
    return (
        m.group("prefix"),
        int(m.group("major")),
        int(m.group("minor")),
        int(m.group("patch")),
    )


def next_version_number(prefix: str, existing: list[str]) -> str:
    """在既有版本号集合上递增 patch 位（纯函数，稳定递增）。

    :param prefix: 策略前缀（季节策略为 ``season``）。
    :param existing: 已存在的版本号（含 candidate/active/superseded 全部）。
    :returns: 下一个可用版本号；集合为空时返回 ``<prefix>-1.0.0``。
    """
    highest: tuple[int, int, int] | None = None
    for v in existing:
        parsed = parse_version(v)
        if parsed is None:
            continue
        _, major, minor, patch = parsed
        cur = (major, minor, patch)
        if highest is None or cur > highest:
            highest = cur
    if highest is None:
        return f"{prefix}-1.0.0"
    major, minor, patch = highest
    return f"{prefix}-{major}.{minor}.{patch + 1}"


def is_active(status: StrategyStatus) -> bool:
    """当前有效版本判定（同一 strategy_type 同时最多一个 active）。"""
    return status == StrategyStatus.ACTIVE


def can_participate_in_decision(status: StrategyStatus) -> bool:
    """该状态的版本**是否允许参与依赖它的训练决策**（§7.8 / §19.4.7）。

    只有 ``active`` 版本可参与决策；``verified`` 已通过校验但尚未生效，
    仍需经原子切换方可生效；``candidate``/``invalid``/``superseded`` 一律禁止。
    """
    return status == StrategyStatus.ACTIVE


def is_immutable_status(status: StrategyStatus) -> bool:
    """正式版本（非 candidate）不可改不可删（§19.4.4）。"""
    return status != StrategyStatus.CANDIDATE
