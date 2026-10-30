"""算法口径版本（G-16，2026-10-24 用户决策：**只标记、不回填**）。

为什么需要
----------
风险规则早有 ``RULE_VERSION``（``domain/rule_registry.py``），每日落
``risk_assessment.rule_version``，报告关联块与前端都带。但 **ACWR**（EWMA
窗口 + 参考负荷 R）、**质量分级**（``REQUIRED_METRICS``）、**周/月聚合**三项
一直没有版本语义：口径一改，旧行与新行同表不可区分，趋势图会静默混用两种口径，
「这天的 P1 是旧口径判的还是新口径判的」也答不出来——阈值观察期（L-1/L-3/L-4）
尤其致命。

规则
----
1. **只标记，不回填**：历史行一律 ``NULL``（读作 ``legacy``）。重算会抹掉
   「当时用旧口径」的事实，与追溯目标相反；与 ``report_link`` 的
   「策略版本变化不自动重算历史」同源。
2. **改任何一项口径 → 手动 bump 对应常量**，并在
   ``docs/派生指标口径登记表.md`` 的变更记录里写清改了什么。
3. 三项独立 bump：只改质量分级不该抬高 ACWR 的版本号。
"""

from __future__ import annotations

# 口径版本：负荷 / ACWR / 质量分级 / 周月聚合
# load：与 ``load.METHOD_MIXED`` 同步（v2 = 有氧按 FIT 圈分段，v1 = 整次均心）
# acwr 1.1.0（2026-10-25 U-8）：ACWR 的输入（负荷）变了 → 升版
LOAD_VERSION = "v2"
ACWR_VERSION = "1.1.0"
QUALITY_VERSION = "1.0.0"
AGGREGATION_VERSION = "1.0.0"


def algo_versions() -> dict[str, str]:
    """当前生效的四项口径版本，供报告 payload / 接口 / 审计统一取用。"""
    return {
        "load": LOAD_VERSION,
        "acwr": ACWR_VERSION,
        "quality": QUALITY_VERSION,
        "aggregation": AGGREGATION_VERSION,
    }
