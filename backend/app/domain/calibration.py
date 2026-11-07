"""阈值校准流水线（ARCHITECTURE_REVIEW A.5.2）。

背景
----
``config.validate_min_replay_match`` 与 ``.env.example`` 反复标注「阈值均为候选值待验证」，
但现有 ``strategy_replay.replay_history`` 做的是**自一致性检验**（同一输入两次求值必须
相同），回答不了「把 ACWR_HIGH 从 1.5 调到 1.45，会有哪些天换优先级」。

而且阈值目前是 import 时烧死在 ``rule_registry`` 模块常量里的：``thresholds`` 字典
是**建表时拷贝的字面量**，``RULES`` 是全局单例——换一套阈值重跑，无处可换。

本模块提供最小可用的对照回放
----------------------------
``calibrate(session, overrides)``：
1. 以 ``rule_registry.RULES`` 为基准，按 ``overrides`` 覆盖各规则的 ``thresholds``；
2. 同一批历史数据，分别用「当前阈值」与「候选阈值」求值；
3. 输出逐日差异 + 优先级迁移矩阵（``P2 -> P1``: 3 天）。

**只读**：不写库、不改规则注册表、不产生策略版本。它的产出是给人看的报告，
用来判断「候选值切在哪里」。

边界
----
- 只校准 ``thresholds``；``params``（处方合成参数）不参与判定条件，本轮不覆盖。
- 覆盖是按 ``rule_id`` 精确匹配的：``{"P1-LOAD-HRV": {"acwr_high": 1.45}}``。
  写错 rule_id 直接报错，不静默忽略——静默忽略会让人以为「调了但没效果」。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from sqlmodel import Session, select

from app.db.models import AcwrRow, DailyReportRow, StandardDailyRow
from app.domain import rule_registry as rr
from app.domain.priority import judge_data_state, judge_priority
from app.domain.risk import RiskInput
from app.domain.strategy_replay import _latest_baselines, _to_risk_input

DEFAULT_SAMPLE_LIMIT = rr.__dict__.get("DEFAULT_REPLAY_LIMIT", 470)


@dataclass(slots=True)
class OverrideError(ValueError):
    """候选阈值覆盖写错了（未知 rule_id 或未知 threshold 键）。"""

    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class DayShift:
    """一天在候选阈值下的判定变化。"""

    date: Any
    baseline_priority: str
    candidate_priority: str
    changed_rules: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.baseline_priority != self.candidate_priority


@dataclass(slots=True)
class CalibrationReport:
    """对照回放结果。"""

    overrides: dict[str, dict[str, float]]
    total: int
    changed: int
    shifts: list[DayShift] = field(default_factory=list)

    @property
    def change_rate(self) -> float:
        return self.changed / self.total if self.total else 0.0

    def migration_matrix(self) -> dict[str, int]:
        """优先级迁移统计，如 ``{"P2 -> P1": 3, "P1 -> P2": 1}``。"""
        out: dict[str, int] = {}
        for shift in self.shifts:
            if not shift.changed:
                continue
            key = f"{shift.baseline_priority} -> {shift.candidate_priority}"
            out[key] = out.get(key, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "overrides": self.overrides,
            "total": self.total,
            "changed": self.changed,
            "changeRate": round(self.change_rate, 4),
            "migration": self.migration_matrix(),
            "shifts": [
                {
                    "date": str(s.date),
                    "from": s.baseline_priority,
                    "to": s.candidate_priority,
                    "rules": list(s.changed_rules),
                }
                for s in self.shifts
                if s.changed
            ],
        }


def build_candidate_rules(
    overrides: dict[str, dict[str, float]],
    *,
    base: dict[str, rr.RuleSpec] | None = None,
) -> dict[str, rr.RuleSpec]:
    """按 ``overrides`` 覆盖阈值，返回**新的**规则集（不改动全局 ``RULES``）。

    未知 rule_id 或未知 threshold 键 → ``OverrideError``。
    ``base`` 缺省取 ``rr.effective_rules()``（含数据文件覆盖）。
    """
    source = base if base is not None else rr.effective_rules()
    unknown_rules = sorted(set(overrides) - set(source))
    if unknown_rules:
        raise OverrideError(f"未知规则：{unknown_rules}；可用：{sorted(source)}")

    out: dict[str, rr.RuleSpec] = {}
    for rule_id, spec in source.items():
        patch = overrides.get(rule_id)
        if not patch:
            out[rule_id] = spec
            continue
        unknown_keys = sorted(set(patch) - set(spec.thresholds))
        if unknown_keys:
            raise OverrideError(
                f"规则 {rule_id} 没有这些阈值键：{unknown_keys}；"
                f"可用：{sorted(spec.thresholds)}"
            )
        merged = {**spec.thresholds, **patch}
        out[rule_id] = replace(spec, thresholds=merged)
    return out


def _evaluate_with(rules: dict[str, rr.RuleSpec], inp: RiskInput) -> str:
    """用指定规则集求值，返回优先级字符串。

    复用 ``judge_priority`` 的判定语义，但遍历的是传入的规则集而不是全局 ``RULES``——
    这正是「换一套阈值重跑」所必需的能力。
    """
    state_inp = inp.to_state_input()
    state, _ = judge_data_state(state_inp)
    result = judge_priority(state_inp, state, specs=tuple(rules.values()))
    return result.priority.value


def calibrate(
    session: Session,
    overrides: dict[str, dict[str, float]],
    *,
    limit: int = DEFAULT_SAMPLE_LIMIT,
    baseline: dict[str, rr.RuleSpec] | None = None,
) -> CalibrationReport:
    """同一批历史数据，用「当前阈值」与「候选阈值」各求值一次，输出差异。

    :param overrides: ``{rule_id: {threshold_key: 新值}}``，如
        ``{"P1-LOAD-HRV": {"acwr_high": 1.45}}``。传空 dict 且想比较
        「当前代码默认值 vs 已生效的数据文件」时，用 ``baseline=RULES``。
    :param limit: 取最近多少天的日报作为样本（默认 470，与 ``strategy_replay`` 一致）。
    :param baseline: 基线规则集；缺省是**当前实际生效**的规则
        （代码默认 + ``data/rule_thresholds.json`` 覆盖）。这样「候选 vs 当前」
        才是运维眼里的真正对照——否则已上线覆盖值会被当成候选。
    """
    candidate = build_candidate_rules(overrides, base=baseline)
    base_rules = baseline if baseline is not None else rr.effective_rules()

    baselines = _latest_baselines(session)
    records = session.exec(
        select(DailyReportRow).order_by(DailyReportRow.date.asc())  # type: ignore[attr-defined]
    ).all()
    standards = {r.date: r for r in session.exec(select(StandardDailyRow)).all()}
    acwr = {r.date: r.ratio for r in session.exec(select(AcwrRow)).all()}

    shifts: list[DayShift] = []
    for rep in records[-limit:]:
        sd = standards.get(rep.date)
        if sd is None:
            continue
        inp = _to_risk_input(sd, baselines, acwr.get(rep.date))
        before = _evaluate_with(base_rules, inp)
        after = _evaluate_with(candidate, inp)
        shifts.append(
            DayShift(
                date=rep.date,
                baseline_priority=before,
                candidate_priority=after,
                changed_rules=tuple(sorted(overrides)),
            )
        )

    return CalibrationReport(
        overrides=overrides,
        total=len(shifts),
        changed=sum(1 for s in shifts if s.changed),
        shifts=shifts,
    )
