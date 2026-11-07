"""编排层：任务调度、幂等、重试、补算（REQ-TASK-001..005）。

- scheduler.py  —— APScheduler 日/周/月定时
- backfill.py   —— 恢复后按依赖顺序补算并校验

任务状态机 ``task_state`` 已下移至 ``app/storage/task_state.py``：
它只依赖 db/schemas，属纯基础设施，services 与 orchestrator 都平等使用它
（ARCHITECTURE_REVIEW A.1.1 / A.2.2，消除 services → orchestrator 的层级倒置）。
"""
