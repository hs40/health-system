# 固定测试集回归（阶段③ T3.11 · REQ §14.2）

## 目的

模型 / 提示词 / 策略升级前，用**固定测试集**验证确定性逻辑结论**不漂移**。
这是「AI 与确定性逻辑分离」（§14.1）的落地保障：关键计算与安全规则由可验证程序
逻辑控制，不交给模型自由生成。

## 用例集（对齐 §14.2 八类）

`REGRESSION_CASES` + `ENV_CASES` 覆盖：

| §14.2 类别 | 用例 |
|---|---|
| 常规案例 | `normal_within_baseline` / `outdoor_mild` |
| 高风险案例 | `high_combo_r003`（组合 R-003） |
| 边界案例 | `boundary_hrv_equal_p10` / `boundary_rhr_equal_p90` / `cold_below_5` / `exactly_5_outdoor` |
| 数据缺失案例 | `missing_critical_hrv` / `weather_unavailable_no_fabricate` |
| 数据冲突案例 | `conflict_recorded_not_masked`（R-008 仅标注不抵消） |
| 背离案例 | `divergence_high`（R-006） |
| 计划生成拦截案例 | `test_plan_blocked_stable` / `test_needs_adjust_not_hard_blocked` |
| 策略版本变化案例 | `test_version_change_creates_new_and_supersedes` / `test_version_change_decision_isolation` |

`test_regression_set_covers_all_categories` 防止回归集退化（缺类即失败）。

## 与风险引擎阈值的对应（current: `candidate-0.1.0`）

结论依据 `domain/rule_registry.py` 的规则语义，**测试断言必须与登记表一致**：

- R-001（HRV < p10）、R-002（RHR > p90）、R-004（睡眠 < p10）→ `needsAdjustment`（非硬拦截）
- R-003（R-001 ∧ R-002 ∧ 主观疲劳）→ `high` + 硬拦截
- R-006（负荷-恢复-表现背离）→ `high` + 硬拦截
- R-007（关键数据缺失）→ `high` + 硬拦截
- R-008（多指标冲突）→ **仅标注**，不改等级；充分性降为 partial

阈值变更（T1.4 校准）后必须同步更新本测试集，并作为升级评审的 diff 依据。

## 分阶段启用与回滚（§14.2）

升级流程：

1. 运行本回归集（`pytest tests/regression -q`）→ 记录基线结论；
2. 引入变更（新模型 / 新提示词 / 新策略版本）→ **再次运行**；
3. 任一用例结论漂移 → **不得启用**；定位是阈值变更（预期）还是逻辑回归（缺陷）；
4. 通过 → 分阶段启用，**保留旧版本与回滚能力**（策略版本管理 + 自动回退，见 `docs/archive/策略版本与回退说明.md`）；
5. 记录升级依据与测试结果。

## 运行

```bash
# 仅回归集
.venv/Scripts/python.exe -m pytest tests/regression -q

# 全量
.venv/Scripts/python.exe -m pytest -q
```
