"""领域层：纯计算，无 IO，可单测（REQ-RISK-002 / REQ-TASK-003）。

- baseline.py     —— 个人基线引擎（分指标、版本化、未成熟→保守）
- load.py         —— 训练负荷 / ACWR 引擎（分运动 + 综合，去重同刺激）
- risk.py         —— 风险规则引擎（硬规则，Plan §2.4）
- quality.py      —— 数据质量分级（四档）
- prescription.py —— 训练处方生成器（完整处方）
"""
